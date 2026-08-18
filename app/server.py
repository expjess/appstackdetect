"""HTTP service behind the App Stack Detector web UI."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .analyze import AnalysisError, analyze_path
from . import limits, probes, stores

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024)))
JOB_TTL_SECONDS = 3600
RESULTS_DIR = os.path.join(ROOT, "results")

app = FastAPI(title="App Stack Detector", docs_url=None, redoc_url=None)

JOBS: dict[str, dict[str, Any]] = {}


def _new_job(label: str) -> str:
    _reap()
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "id": job_id,
        "label": label,
        "status": "running",
        "steps": [],
        "result": None,
        "error": None,
        "created": time.time(),
    }
    return job_id


def _reap() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    for job_id in [k for k, v in JOBS.items() if v["created"] < cutoff]:
        JOBS.pop(job_id, None)


def _step(job_id: str, text: str) -> None:
    job = JOBS.get(job_id)
    if job:
        job["steps"].append({"at": round(time.time() - job["created"], 1), "text": text})


def _finish(job_id: str, result: dict[str, Any]) -> None:
    job = JOBS.get(job_id)
    if job:
        job["result"] = result
        job["status"] = "done"
        _persist(job)


def _persist(job: dict[str, Any]) -> None:
    """Keep finished reports on disk so a shared /j/<id> link keeps working."""
    import json

    try:
        os.makedirs(RESULTS_DIR, exist_ok=True)
        with open(os.path.join(RESULTS_DIR, f"{job['id']}.json"), "w") as fh:
            json.dump(job, fh)
        _index_report(job)
    except OSError:
        pass


def _report_key(platform: str, identifier: str, version: str) -> str:
    return f"{platform}:{identifier}:{version}"


def _index_report(job: dict[str, Any]) -> None:
    """Index a finished report by app identity, so the same build is not fetched twice."""
    import json

    result = job.get("result") or {}
    app_info = result.get("app") or {}
    platform = app_info.get("platform", "")
    identifier = app_info.get("bundle_id") or app_info.get("package") or ""
    # A store listing and the binary itself can state the version differently:
    # Grok Bot is "1.1" on the App Store and "1.1.0" in Info.plist. Index both.
    store = result.get("store_ios") or result.get("store") or {}
    versions = {app_info.get("version_name") or "", store.get("version") or ""} - {""}
    if not (platform and identifier and versions):
        return
    path = os.path.join(RESULTS_DIR, "index.json")
    try:
        with open(path) as fh:
            index = json.load(fh)
    except (OSError, ValueError):
        index = {}
    for version in versions:
        index[_report_key(platform, identifier, version)] = {"job": job["id"], "at": job.get("created", 0)}
    try:
        with open(path, "w") as fh:
            json.dump(index, fh)
    except OSError:
        pass


def _cached_report(platform: str, identifier: str, version: str) -> dict[str, Any] | None:
    """Return a stored analysis of this exact build, if one exists.

    Re-downloading a binary we have already read wastes a large download and
    puts avoidable load on Apple and the APK mirrors.
    """
    import json

    try:
        with open(os.path.join(RESULTS_DIR, "index.json")) as fh:
            index = json.load(fh)
    except (OSError, ValueError):
        return None
    entry = index.get(_report_key(platform, identifier, version))
    if not entry:
        return None
    stored = _load_persisted(entry["job"])
    return stored.get("result") if stored else None


def _recent_report(platform: str, identifier: str) -> tuple[dict[str, Any], float] | None:
    """The newest report for this app analyzed today, whatever version it was.

    Asking the same question twice in one day should not cost a second download.
    """
    import datetime
    import json

    try:
        with open(os.path.join(RESULTS_DIR, "index.json")) as fh:
            index = json.load(fh)
    except (OSError, ValueError):
        return None
    prefix = f"{platform}:{identifier}:"
    today = datetime.date.today()
    best = None
    for key, entry in index.items():
        if not key.startswith(prefix):
            continue
        at = entry.get("at", 0)
        if datetime.date.fromtimestamp(at) != today:
            continue
        if best is None or at > best[1]:
            best = (entry["job"], at)
    if not best:
        return None
    stored = _load_persisted(best[0])
    result = stored.get("result") if stored else None
    return (result, best[1]) if result else None


def _reuse(job_id: str, result: dict[str, Any], identifier: str, at: float, reason: str) -> None:
    import datetime

    when = datetime.datetime.fromtimestamp(at).strftime("%H:%M")
    _step(job_id, f"{reason} Serving that report instead of downloading again.")
    out = dict(result)
    out["reused"] = {"identifier": identifier, "analyzed_at": at}
    out.setdefault("notes", []).append(
        f"This report was produced at {when} today for {identifier}, and is being reused. Nothing was "
        "downloaded. Send refresh=true to force a fresh analysis."
    )
    _finish(job_id, out)


def _load_persisted(job_id: str) -> dict[str, Any] | None:
    import json

    path = os.path.join(RESULTS_DIR, f"{job_id}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _client_address(request: Request) -> str:
    """The caller's address.

    The tuft proxy preserves the real client address at the socket level: the
    access log shows 20 distinct external addresses, not one proxy address. So
    the peer is trustworthy, and X-Forwarded-For is deliberately ignored —
    honouring it would let any caller forge a fresh identity per request and
    walk straight past the rate limit.
    """
    return request.client.host if request.client else ""


def _fail(job_id: str, message: str) -> None:
    job = JOBS.get(job_id)
    if job:
        job["error"] = message
        job["status"] = "error"


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "apk_download": stores.apkeep_available(),
        "source": ", ".join(stores.DOWNLOAD_SOURCES),
        "ios_download": await stores.ipatool_account(),
        "limits": limits.status(),
    }


@app.post("/api/jobs/url")
async def start_url_job(payload: dict[str, Any], request: Request) -> JSONResponse:
    allowed, message = limits.check_per_ip(_client_address(request))
    if not allowed:
        return JSONResponse({"error": message}, status_code=429)
    raw = str((payload or {}).get("input", ""))
    purchase = bool((payload or {}).get("purchase", True))
    refresh = bool((payload or {}).get("refresh", False))
    ios_binary = bool((payload or {}).get("ios_binary", False))
    try:
        target = stores.parse_input(raw)
    except stores.StoreError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    job_id = _new_job(raw.strip())
    asyncio.create_task(_run_store_job(job_id, target, purchase, refresh, ios_binary))
    return JSONResponse({"job": job_id, "url": f"/j/{job_id}"})


@app.post("/api/jobs/upload")
async def start_upload_job(request: Request, file: UploadFile = File(...), name: str = Form("")) -> JSONResponse:
    allowed, message = limits.check_per_ip(_client_address(request))
    if not allowed:
        return JSONResponse({"error": message}, status_code=429)
    filename = name or file.filename or "upload.bin"
    job_id = _new_job(filename)
    tmp = stores.temp_dir()
    dest = os.path.join(tmp, os.path.basename(filename))
    written = 0
    with open(dest, "wb") as out:
        while chunk := await file.read(1 << 20):
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                stores.cleanup(tmp)
                _fail(job_id, f"Upload is larger than the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.")
                return JSONResponse({"job": job_id})
            out.write(chunk)
    _step(job_id, f"Received {filename} ({written / 1e6:.1f} MB).")
    asyncio.create_task(_run_file_job(job_id, dest, filename, tmp))
    return JSONResponse({"job": job_id, "url": f"/j/{job_id}"})


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str) -> JSONResponse:
    job = JOBS.get(job_id) or _load_persisted(job_id)
    if not job:
        return JSONResponse({"error": "Unknown job. Reports are kept after they finish, so this id was never used."}, status_code=404)
    return JSONResponse(job)


async def _run_file_job(job_id: str, path: str, filename: str, tmp: str) -> None:
    try:
        _step(job_id, "Unpacking and scanning the archive.")
        result = await asyncio.to_thread(analyze_path, path, filename)
        result["source"] = {"kind": "upload", "label": filename}
        _finish(job_id, result)
    except AnalysisError as exc:
        _fail(job_id, str(exc))
    except Exception as exc:  # noqa: BLE001 - surface the failure to the UI
        _fail(job_id, f"{type(exc).__name__}: {exc}")
    finally:
        stores.cleanup(tmp)


async def _run_store_job(job_id: str, target: stores.Target, purchase: bool = True, refresh: bool = False, ios_binary: bool = False) -> None:
    tmp = stores.temp_dir()
    try:
        if target.platform == "android":
            await _android_flow(job_id, target.identifier, tmp, store_meta=None, refresh=refresh)
        else:
            await _ios_flow(job_id, target, tmp, purchase, refresh, ios_binary)
    except stores.StoreError as exc:
        _fail(job_id, str(exc))
    except AnalysisError as exc:
        _fail(job_id, str(exc))
    except Exception as exc:  # noqa: BLE001
        _fail(job_id, f"{type(exc).__name__}: {exc}")
    finally:
        stores.cleanup(tmp)


async def _android_flow(
    job_id: str, package: str, tmp: str, store_meta: dict[str, Any] | None, refresh: bool = False
) -> dict[str, Any]:
    if not refresh:
        recent = _recent_report("android", package)
        if recent:
            _reuse(job_id, recent[0], package, recent[1], f"{package} was already analyzed today.")
            return recent[0]

    _step(job_id, f"Reading the Google Play listing for {package}.")
    meta = store_meta
    if meta is None:
        try:
            meta = await stores.play_lookup(package)
        except Exception as exc:  # noqa: BLE001 - the listing is a nice-to-have
            meta = {"store": "Google Play", "package": package, "note": f"listing unavailable: {exc}"}

    _step(job_id, f"Downloading the APK for {package}. Large apps take a few minutes.")
    apk, source = await stores.fetch_apk(package, tmp, on_step=lambda text: _step(job_id, text))
    size_mb = os.path.getsize(apk) / 1e6
    _step(job_id, f"Downloaded {os.path.basename(apk)} ({size_mb:.1f} MB) from {source}. Scanning it now.")

    result = await asyncio.to_thread(analyze_path, apk, os.path.basename(apk))
    result["source"] = {"kind": "play-store", "label": package, "download_source": source}
    result["store"] = meta
    if source == "huawei-app-gallery":
        result.setdefault("notes", []).append(
            "This APK came from Huawei AppGallery, which sometimes ships a regional variant of the app. "
            "Check the version shown above against the Play Store listing before quoting it."
        )
    _finish(job_id, result)
    return result


IOS_NOTE = (
    "Apple does not publish app binaries, and an App Store IPA is FairPlay-encrypted, so this tool "
    "cannot download and scan the iOS build directly."
)


async def _ios_flow(
    job_id: str,
    target: stores.Target,
    tmp: str,
    purchase: bool = True,
    refresh: bool = False,
    ios_binary: bool = False,
) -> None:
    """Answer an App Store link using the cheapest sufficient evidence.

    Downloading from the App Store means spending a request against a real
    Apple ID, so it comes last. An Android build of the same project answers
    the same question — React Native and Expo are properties of the codebase,
    not of one platform's binary — and a public repository answers it for free.
    Pass ios_binary=true to go straight to the iOS binary anyway.
    """
    _step(job_id, "Reading App Store metadata.")
    meta = await stores.itunes_lookup(target.identifier)
    bundle_id = meta.get("bundle_id", "")
    name = meta.get("name", "")
    developer = meta.get("developer", "")
    _step(job_id, f"{name or target.identifier} by {developer or 'unknown developer'} — bundle id {bundle_id or 'unknown'}.")

    # 1. A build we have already read costs nothing at all.
    if bundle_id and not refresh:
        if meta.get("version"):
            cached = _cached_report("ios", bundle_id, meta["version"])
            if cached:
                _reuse(job_id, cached, bundle_id, time.time(), f"Version {meta['version']} was already analyzed.")
                return
        recent = _recent_report("ios", bundle_id)
        if recent:
            _reuse(job_id, recent[0], bundle_id, recent[1], f"{bundle_id} was already analyzed today.")
            return

    if ios_binary:
        if await _try_app_store_binary(job_id, meta, bundle_id, tmp, purchase, probe_payload=None):
            return
        _step(job_id, "Falling back to the indirect routes.")

    # 2. Cheap probes: a Play listing, a public repository, the developer's site.
    _step(job_id, "Looking for an Android build of the same project, and running indirect probes.")
    counterpart, github, web = await asyncio.gather(
        probes.find_android_counterpart(bundle_id, name, developer),
        probes.github_probe(bundle_id),
        probes.web_probe(meta.get("seller_url", "")),
        return_exceptions=True,
    )
    counterpart = counterpart if isinstance(counterpart, dict) else {"found": None, "steps": ["Play search failed."]}
    github = github if isinstance(github, dict) else {"ran": False}
    web = web if isinstance(web, dict) else {"ran": False}

    for line in counterpart.get("steps", []):
        _step(job_id, line)
    if github.get("verdict"):
        _step(job_id, f"Found the project on GitHub: {github['verdict']['repo']}.")
    if web.get("hits"):
        _step(job_id, f"The developer's website shows: {', '.join(web['hits'])}.")

    probe_payload = {"github": github, "website": web, "play_search": counterpart.get("steps", [])}

    # 3. The Android build of the same project, when there is one.
    found = counterpart.get("found")
    if found:
        package = found["package"]
        _step(job_id, f"Analyzing the Android build {package} ({found['matched_on']}). No App Store request needed.")
        try:
            result = await _android_flow(job_id, package, tmp, store_meta=None, refresh=refresh)
        except stores.StoreError as exc:
            _step(job_id, f"{exc}")
            found = None
        else:
            declared_ios = (result.get("expo") or {}).get("ios_bundle_identifier", "")
            confirmed = bool(declared_ios) and declared_ios == bundle_id
            result["ios_inference"] = {
                "requested": {"platform": "ios", "identifier": target.identifier, "bundle_id": bundle_id},
                "analyzed": {"platform": "android", "package": package},
                "matched_on": found["matched_on"],
                "confirmed_same_project": confirmed,
                "explanation": (
                    "The Android build declares this exact iOS bundle identifier in its embedded Expo app "
                    "config, so both platforms are built from one Expo project."
                    if confirmed
                    else f"The Android app was matched by {found['matched_on']}, so the two builds very likely "
                    "share a codebase. This is an inference about the iOS build, not a direct measurement of it."
                ),
            }
            result["probes"] = probe_payload
            result.setdefault("notes", []).append(
                "The Android build answered this, so no App Store download was needed. Send "
                "ios_binary=true to read the iOS binary instead."
            )
            result["store_ios"] = meta
            _finish(job_id, result)
            return

    # 4. A public repository states the answer in its own package.json.
    if github.get("verdict"):
        _step(job_id, "Answering from the project's public source. No download needed.")
        _finish(
            job_id,
            {
                "store": meta,
                "store_ios": meta,
                "source": {"kind": "app-store", "label": target.identifier},
                "ios_only": True,
                "probes": probe_payload,
                "next_steps": _ipa_instructions(),
                "notes": [
                    IOS_NOTE,
                    f"Answered from {github['verdict']['repo']}, which declares this bundle identifier. "
                    "Send ios_binary=true to read the shipped iOS binary instead.",
                ],
            },
        )
        return

    # 5. Last resort: spend an App Store request.
    if await _try_app_store_binary(job_id, meta, bundle_id, tmp, purchase, probe_payload):
        return

    _finish(
        job_id,
        {
            "store": meta,
            "store_ios": meta,
            "source": {"kind": "app-store", "label": target.identifier},
            "ios_only": True,
            "probes": probe_payload,
            "next_steps": _ipa_instructions(),
            "notes": [
                IOS_NOTE,
                f"No Android build by {developer or 'this developer'} was found on Google Play, no public "
                "repository declares this bundle identifier, and the App Store download did not succeed.",
            ],
        },
    )


async def _try_app_store_binary(
    job_id: str,
    meta: dict[str, Any],
    bundle_id: str,
    tmp: str,
    purchase: bool,
    probe_payload: dict[str, Any] | None,
) -> bool:
    """Download and scan the real iOS binary. Returns True when it succeeded."""
    account = await stores.ipatool_account()
    if not (account.get("signed_in") and bundle_id):
        return False
    allowed, message = limits.appstore_allowance()
    if not allowed:
        _step(job_id, message)
        return False
    _step(job_id, f"Signed in to the App Store as {account.get('account') or 'an Apple ID'}. Downloading the IPA.")
    try:
        ipa = await stores.fetch_ipa(bundle_id, tmp, purchase=purchase)
    except stores.StoreError as exc:
        _step(job_id, f"App Store download failed: {exc}")
        return False
    used = limits.record_appstore_download()
    size_mb = os.path.getsize(ipa) / 1e6
    _step(
        job_id,
        f"Downloaded {os.path.basename(ipa)} ({size_mb:.1f} MB). Scanning the iOS binary. "
        f"({used} of {limits.APPSTORE_DAILY_LIMIT} App Store downloads used today.)",
    )
    result = await asyncio.to_thread(analyze_path, ipa, os.path.basename(ipa))
    result["source"] = {"kind": "app-store", "label": bundle_id, "download_source": "App Store via ipatool"}
    result["store"] = meta
    result["store_ios"] = meta
    if probe_payload:
        result["probes"] = probe_payload
    _finish(job_id, result)
    return True


def _ipa_instructions() -> list[str]:
    return [
        "On a Mac, install Apple Configurator, sign in with an Apple ID, then use Add > Apps to download "
        "the app. Configurator leaves the .ipa in ~/Library/Group Containers/K36BKF7T3D.group.com."
        "apple.configurator/Library/Caches/Assets/TemporaryItems/MobileApps/.",
        "Drop that .ipa on this page. FairPlay encrypts only the main executable, so the JavaScript bundle, "
        "Info.plist and frameworks still identify the stack.",
        "If you have the Android package name for this app, paste it here directly — that path is fully automatic.",
    ]


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/j/{job_id}")
async def shared_result(job_id: str) -> FileResponse:
    """A link to one finished analysis. The page loads the job by id."""
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/static/get-ipa.sh")
async def get_ipa_script(request: Request) -> Response:
    """The Mac helper script, stamped with this instance's own base URL.

    The file on disk carries a placeholder instead of a hard-coded host, so the
    repository names no particular deployment. Served, it points back here.
    """
    with open(os.path.join(STATIC, "get-ipa.sh")) as fh:
        body = fh.read()
    base = str(request.base_url).rstrip("/")
    return Response(
        body.replace("__SERVICE_URL__", base),
        media_type="text/x-shellscript",
        headers={"Content-Disposition": 'attachment; filename="get-ipa.sh"'},
    )


# Registered after the explicit route above, so that one wins for get-ipa.sh.
app.mount("/static", StaticFiles(directory=STATIC), name="static")
