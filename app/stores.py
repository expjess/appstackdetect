"""Resolve store links to app identifiers, read public store metadata, and
fetch an Android APK for analysis.

The App Store gives no public way to download an IPA, so the iOS path returns
metadata only and asks for an uploaded .ipa.
"""

from __future__ import annotations

import asyncio
import html
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any

import httpx

APKEEP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "apkeep")
APKEEP_TIMEOUT = int(os.environ.get("APKEEP_TIMEOUT", "900"))
DOWNLOAD_SOURCES = [s.strip() for s in os.environ.get("APK_SOURCES", "apk-pure,f-droid,huawei-app-gallery").split(",") if s.strip()]
DOWNLOAD_SOURCE = DOWNLOAD_SOURCES[0]
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"

RE_PLAY = re.compile(r"[?&]id=([A-Za-z0-9_.]+)")
RE_APPSTORE_ID = re.compile(r"/id(\d+)")
RE_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")


class StoreError(Exception):
    pass


@dataclass
class Target:
    platform: str  # "android" | "ios"
    identifier: str  # android package name, or numeric App Store id / bundle id
    source_url: str = ""


def parse_input(raw: str) -> Target:
    text = raw.strip()
    if not text:
        raise StoreError("Enter a store link, an Android package name, or an App Store id.")

    low = text.lower()
    if "play.google.com" in low:
        m = RE_PLAY.search(text)
        if not m:
            raise StoreError("That Play Store link has no ?id= package name in it.")
        return Target("android", m.group(1), text)

    if "apps.apple.com" in low or "itunes.apple.com" in low:
        m = RE_APPSTORE_ID.search(text)
        if not m:
            raise StoreError("That App Store link has no /idNNNNNNNN in it.")
        return Target("ios", m.group(1), text)

    if low.startswith("id") and low[2:].isdigit():
        return Target("ios", text[2:])
    if text.isdigit():
        return Target("ios", text)
    if RE_PACKAGE.match(text):
        return Target("android", text)

    raise StoreError(
        "Could not read that input. Use a Play Store link, an App Store link, "
        "an Android package name such as com.example.app, or an App Store id such as id284882215."
    )


_ITUNES_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
ITUNES_CACHE_TTL = int(os.environ.get("ITUNES_CACHE_TTL", "21600"))  # 6 hours


async def itunes_lookup(identifier: str) -> dict[str, Any]:
    """Look up App Store metadata by numeric id or by bundle id.

    Results are cached, because Apple's public lookup endpoint is a shared
    courtesy service and repeat queries for the same app are common.
    """
    import time as _time

    cached = _ITUNES_CACHE.get(identifier)
    if cached and _time.time() - cached[0] < ITUNES_CACHE_TTL:
        return cached[1]

    field = "id" if identifier.isdigit() else "bundleId"
    url = f"https://itunes.apple.com/lookup?{field}={identifier}&country=us&entity=software"
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": USER_AGENT})
    if resp.status_code in (403, 429):
        raise StoreError(
            f"Apple's lookup service answered HTTP {resp.status_code}, which means this machine is "
            "being rate limited. Wait a few minutes and try again."
        )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("results"):
        raise StoreError(f"The App Store has no app matching {identifier}.")
    r = data["results"][0]
    meta = {
        "store": "App Store",
        "name": r.get("trackName", ""),
        "developer": r.get("artistName", ""),
        "bundle_id": r.get("bundleId", ""),
        "store_id": str(r.get("trackId", "")),
        "version": r.get("version", ""),
        "released": r.get("currentVersionReleaseDate", ""),
        "minimum_os_version": r.get("minimumOsVersion", ""),
        "size_bytes": int(r.get("fileSizeBytes") or 0),
        "icon": r.get("artworkUrl512") or r.get("artworkUrl100", ""),
        "url": r.get("trackViewUrl", ""),
        "genres": r.get("genres", []),
        "seller_url": r.get("sellerUrl", ""),
        "developer_url": r.get("artistViewUrl", ""),
    }
    for key in {identifier, meta["bundle_id"], meta["store_id"]}:
        if key:
            _ITUNES_CACHE[key] = (_time.time(), meta)
    return meta


async def play_lookup(package: str) -> dict[str, Any]:
    """Read what the public Play Store listing page exposes. Best effort."""
    url = f"https://play.google.com/store/apps/details?id={package}&hl=en&gl=us"
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": USER_AGENT})
    out: dict[str, Any] = {"store": "Google Play", "package": package, "url": url}
    if resp.status_code == 404:
        raise StoreError(f"Google Play has no listing for {package}.")
    resp.raise_for_status()
    body = resp.text
    m = re.search(r'<meta name="appstore:developer_url" content="([^"]+)"', body)
    if m:
        out["developer_url"] = html.unescape(m.group(1))
    m = re.search(r"<title[^>]*>([^<]+)</title>", body)
    if m:
        out["name"] = html.unescape(m.group(1)).replace(" - Apps on Google Play", "").strip()
    m = re.search(r'<meta property="og:image" content="([^"]+)"', body)
    if m:
        out["icon"] = html.unescape(m.group(1))
    m = re.search(r'\[\[\["([\d.]+)"\]\],\[\[\["', body)
    if m:
        out["version"] = m.group(1)
    return out


def apkeep_available() -> bool:
    return os.path.isfile(APKEEP) and os.access(APKEEP, os.X_OK)


async def fetch_apk(package: str, dest_dir: str, on_step=None) -> tuple[str, str]:
    """Download the current APK for a package.

    Sources are tried in order, because no single mirror carries every app.
    Returns (file path, the source it came from).
    """
    if not apkeep_available():
        raise StoreError("The APK downloader is not installed on this server, so upload the .apk instead.")

    for source in DOWNLOAD_SOURCES:
        if on_step:
            on_step(f"Asking {source} for {package}.")
        if source == "f-droid":
            path = await _fdroid_download(package, dest_dir)
            if path:
                return path, source
            continue

        proc = await asyncio.create_subprocess_exec(
            APKEEP, "-a", package, "-d", source, dest_dir,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=APKEEP_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            raise StoreError(f"Downloading {package} from {source} timed out after {APKEEP_TIMEOUT}s.")
        files = [
            os.path.join(dest_dir, f)
            for f in os.listdir(dest_dir)
            if f.endswith((".apk", ".xapk", ".apks", ".zip"))
        ]
        if files:
            return max(files, key=os.path.getsize), source

    raise StoreError(
        f"None of the configured mirrors ({', '.join(DOWNLOAD_SOURCES)}) carry {package}. "
        "The app may be new, region-restricted, or Play-only. Upload the APK to analyze it anyway."
    )


IPATOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "ipatool")
# ipatool keeps the App Store token in a file keyring locked by a passphrase.
# The passphrase lives in a 0600 file next to the app so the service can run
# unattended; it protects the keyring on this machine only.
IPATOOL_PASSPHRASE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".ipatool-passphrase"
)


def ipatool_available() -> bool:
    return os.path.isfile(IPATOOL) and os.access(IPATOOL, os.X_OK)


def _ipatool_args() -> list[str]:
    """Common flags, including the keyring passphrase when one is stored."""
    args = ["--format", "json", "--non-interactive"]
    try:
        with open(IPATOOL_PASSPHRASE_FILE) as fh:
            passphrase = fh.read().strip()
        if passphrase:
            args += ["--keychain-passphrase", passphrase]
    except OSError:
        pass
    return args


async def ipatool_account() -> dict[str, Any]:
    """Report whether an Apple ID is signed in on this machine.

    Signing in is a deliberate, human action: it needs an Apple ID password and
    a 2FA code. Nothing here logs in on anyone's behalf.
    """
    import json as _json

    if not ipatool_available():
        return {"available": False, "signed_in": False}
    proc = await asyncio.create_subprocess_exec(
        IPATOOL, "auth", "info", *_ipatool_args(),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        return {"available": True, "signed_in": False, "error": "ipatool did not respond."}
    try:
        payload = _json.loads(out.decode("utf-8", "replace").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"available": True, "signed_in": False}
    if payload.get("success") is False:
        return {"available": True, "signed_in": False}
    return {
        "available": True,
        "signed_in": True,
        "account": payload.get("name") or payload.get("email") or "",
    }


ALLOW_PURCHASE = os.environ.get("ALLOW_PURCHASE", "0") == "1"


async def fetch_ipa(bundle_id: str, dest_dir: str, purchase: bool = False) -> str:
    """Download the App Store build of an app with the signed-in Apple ID.

    The download writes a file to this server's disk. It installs nothing on any
    device. Acquiring a licence is a separate action, is off unless
    ALLOW_PURCHASE=1 is set on the server, and is the only step that touches the
    Apple ID's account state.
    """
    import json as _json

    if not ipatool_available():
        raise StoreError("ipatool is not installed on this server.")
    dest = os.path.join(dest_dir, f"{bundle_id}.ipa")
    args = [IPATOOL, "download", "-b", bundle_id, "-o", dest, *_ipatool_args()]
    if purchase and ALLOW_PURCHASE:
        args.append("--purchase")
    proc = await asyncio.create_subprocess_exec(*args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=APKEEP_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise StoreError(f"Downloading {bundle_id} from the App Store timed out.")

    log = out.decode("utf-8", "replace").strip()
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    # Surface ipatool's own message; a missing license is the common case.
    message = log.splitlines()[-1] if log else "no output"
    try:
        message = _json.loads(message).get("error", message)
    except ValueError:
        pass
    if "license" in message.lower():
        raise StoreError(
            f"The signed-in Apple ID has no licence for {bundle_id}, and this server will not acquire "
            "one. Acquiring a licence is the step that changes account state: it adds the app to that "
            "Apple ID's purchase history, and any iPhone signed in to the same account will install it "
            "on its own if Settings > App Store > Automatic Downloads > Apps is on. Use an Apple ID that "
            "already has the app, use a dedicated Apple ID signed in to no device, or download the .ipa "
            "on your Mac and upload it here."
        )
    raise StoreError(f"ipatool could not download {bundle_id}: {message}")


FDROID_REPO = "https://f-droid.org/repo"
FDROID_CACHE = os.path.join(tempfile.gettempdir(), "appstack-fdroid-index.json")
FDROID_CACHE_TTL = 24 * 3600


async def _fdroid_index() -> dict[str, str]:
    """Map package name -> newest APK file name in the F-Droid repository.

    apkeep's own f-droid backend fails to read the current index, so this reads
    index-v1.jar directly and caches the small mapping it needs.
    """
    import json
    import time
    import zipfile

    if os.path.exists(FDROID_CACHE) and time.time() - os.path.getmtime(FDROID_CACHE) < FDROID_CACHE_TTL:
        try:
            with open(FDROID_CACHE) as fh:
                return json.load(fh)
        except ValueError:
            pass

    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        resp = await client.get(f"{FDROID_REPO}/index-v1.jar")
    resp.raise_for_status()

    tmp = tempfile.mkdtemp(prefix="fdroid-")
    try:
        jar = os.path.join(tmp, "index-v1.jar")
        with open(jar, "wb") as fh:
            fh.write(resp.content)
        with zipfile.ZipFile(jar) as z:
            raw = json.loads(z.read("index-v1.json"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    mapping: dict[str, str] = {}
    for package, builds in (raw.get("packages") or {}).items():
        newest = max(builds, key=lambda b: b.get("versionCode", 0), default=None)
        if newest and newest.get("apkName"):
            mapping[package] = newest["apkName"]
    with open(FDROID_CACHE, "w") as fh:
        json.dump(mapping, fh)
    return mapping


async def _fdroid_download(package: str, dest_dir: str) -> str | None:
    try:
        index = await _fdroid_index()
    except Exception:  # noqa: BLE001 - a mirror being down is not fatal
        return None
    apk_name = index.get(package)
    if not apk_name:
        return None
    dest = os.path.join(dest_dir, apk_name)
    async with httpx.AsyncClient(timeout=APKEEP_TIMEOUT, follow_redirects=True) as client:
        async with client.stream("GET", f"{FDROID_REPO}/{apk_name}") as resp:
            if resp.status_code != 200:
                return None
            with open(dest, "wb") as fh:
                async for chunk in resp.aiter_bytes(1 << 20):
                    fh.write(chunk)
    return dest


async def list_versions(package: str) -> list[str]:
    if not apkeep_available():
        return []
    proc = await asyncio.create_subprocess_exec(
        APKEEP, "-a", package, "-l", "-d", DOWNLOAD_SOURCE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=90)
    except asyncio.TimeoutError:
        proc.kill()
        return []
    text = out.decode("utf-8", "replace")
    m = re.search(r"\|\s*(.+)", text)
    return [v.strip() for v in m.group(1).split(",")] if m else []


def temp_dir() -> str:
    return tempfile.mkdtemp(prefix="appstack-")


def cleanup(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
