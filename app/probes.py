"""Indirect probes used when no app binary can be obtained.

These never replace binary analysis. They exist so that an iOS-only app returns
something useful instead of an empty page, and every finding says where it came
from and how strong it is.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import subprocess
from typing import Any

import httpx

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"

# GitHub code search runs through the Tuft gh shim when this is a Tuft machine,
# and through a plain authenticated `gh` anywhere else. Neither is required: the
# GitHub probe just returns nothing when no gh is available.
_TUFT = "/home/tuft/.local/share/tuft/bin/tuft"
if os.path.exists(_TUFT):
    GH_SHIM = [
        _TUFT,
        "gh-shim",
        "--project-dir",
        "/home/tuft/.config/tuft",
        "--real-gh",
        "/usr/bin/gh",
        "--",
    ]
else:
    GH_SHIM = [os.environ.get("GH_BIN", "gh")]

RE_PLAY_PKG = re.compile(r"/store/apps/details\?id=([A-Za-z0-9_.]+)")
CORPORATE_SUFFIXES = re.compile(r"\b(inc|inc\.|llc|ltd|limited|corp|corporation|gmbh|bv|ab|oy|pbc|pbllc|co|company|technologies|labs|software|studio|studios|apps|mobile)\b")

# Strings that only appear when a site was built from React Native or Expo code.
WEB_NEEDLES = {
    "_expo/static": "Expo web export (Metro / Expo Router output)",
    "expo-router": "expo-router",
    "react-native-web": "react-native-web",
    "expo-modules-core": "expo-modules-core",
    "__expo": "Expo web runtime globals",
}


def _normalize(name: str) -> str:
    text = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    text = CORPORATE_SUFFIXES.sub(" ", text)
    return " ".join(text.split())


# --------------------------------------------------------------- Google Play


async def play_search(query: str, limit: int = 8) -> list[str]:
    """Package names from a Google Play search page, in result order."""
    url = "https://play.google.com/store/search"
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        resp = await client.get(
            url, params={"q": query, "c": "apps", "hl": "en", "gl": "us"}, headers={"User-Agent": USER_AGENT}
        )
    if resp.status_code != 200:
        return []
    return list(dict.fromkeys(RE_PLAY_PKG.findall(resp.text)))[:limit]


async def verify_play_candidate(package: str, ios_name: str, ios_developer: str) -> dict[str, Any] | None:
    """Accept a Play package only when its listing names the same developer.

    Searching by app name alone is not enough: a search for "Grok Bot" returns
    X Corp's "Grok", which is a different company's app.
    """
    url = f"https://play.google.com/store/apps/details?id={package}&hl=en&gl=us"
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": USER_AGENT})
    if resp.status_code != 200:
        return None
    body = resp.text
    title_match = re.search(r"<title[^>]*>([^<]+)</title>", body)
    title = html.unescape(title_match.group(1)).replace(" - Apps on Google Play", "").strip() if title_match else ""

    dev_norm = _normalize(ios_developer)
    name_norm = _normalize(ios_name)
    # Play renders the developer name deep inside the document, so scan all of it.
    body_norm = _normalize(body)

    developer_match = bool(dev_norm) and (
        dev_norm in body_norm or ios_developer.lower() in body.lower()
    )
    title_match_exact = bool(name_norm) and name_norm == _normalize(title)

    if not developer_match:
        return None
    return {
        "package": package,
        "title": title,
        "url": url,
        "matched_on": "developer name and app name" if title_match_exact else "developer name",
    }


async def find_android_counterpart(bundle_id: str, ios_name: str, ios_developer: str) -> dict[str, Any]:
    """Look for the Android build of an iOS app, by identifier and then by name."""
    steps: list[str] = []

    if bundle_id:
        direct = await verify_play_candidate(bundle_id, ios_name, ios_developer)
        if direct:
            direct["matched_on"] = "identical bundle identifier"
            return {"found": direct, "steps": [f"Google Play ships {bundle_id} by the same developer."]}
        steps.append(f"No Google Play listing under the same identifier ({bundle_id}).")

    queries = [q for q in (f"{ios_name} {ios_developer}".strip(), ios_name.strip()) if q]
    seen: set[str] = set()
    for query in queries:
        candidates = [p for p in await play_search(query) if p not in seen and p != bundle_id]
        seen.update(candidates)
        steps.append(f'Searched Google Play for "{query}": {len(candidates)} candidates.')
        for package in candidates[:6]:
            verified = await verify_play_candidate(package, ios_name, ios_developer)
            if verified:
                steps.append(f"{package} is published by the same developer.")
                return {"found": verified, "steps": steps}
    steps.append(f"No Play result is published by {ios_developer or 'the same developer'}.")
    return {"found": None, "steps": steps}


# ------------------------------------------------------------------- GitHub


async def _gh_api(args: list[str], timeout: int = 45) -> Any:
    try:
        proc = await asyncio.create_subprocess_exec(
            *GH_SHIM, *args, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except (FileNotFoundError, PermissionError):
        return None  # no gh on this machine; the GitHub probe is optional
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return None
    if proc.returncode != 0 or not out.strip():
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


async def _raw_json(repo: str, path: str) -> Any:
    """Read a file from a public repository without authentication."""
    url = f"https://raw.githubusercontent.com/{repo}/HEAD/{path}"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT})
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        return json.loads(resp.text)
    except ValueError:
        return None


async def github_probe(bundle_id: str) -> dict[str, Any]:
    """Search public code for the bundle identifier.

    An Expo project declares ios.bundleIdentifier in app.json or app.config.*,
    so a public repository answers the question directly.
    """
    result: dict[str, Any] = {"ran": True, "bundle_id": bundle_id, "hits": [], "verdict": None}
    if not bundle_id:
        result["ran"] = False
        return result

    data = await _gh_api(["api", "-X", "GET", "search/code", "-f", f'q="{bundle_id}"'])
    if not data or not isinstance(data, dict):
        result["error"] = "GitHub code search is unavailable on this machine."
        return result

    items = data.get("items") or []
    result["total"] = data.get("total_count", 0)
    interesting = [
        it
        for it in items
        if re.search(r"(^|/)(app\.config\.(js|ts|json|mjs|cjs)|app\.json|package\.json)$", it.get("path", ""))
    ]
    for item in (interesting or items)[:6]:
        result["hits"].append({"repo": item["repository"]["full_name"], "path": item["path"], "url": item["html_url"]})

    for hit in result["hits"]:
        # The Tuft gh credentials only cover our own org, so read public files
        # straight from raw.githubusercontent.com instead.
        manifest = await _raw_json(hit["repo"], "package.json")
        if not isinstance(manifest, dict):
            continue
        deps = {**(manifest.get("dependencies") or {}), **(manifest.get("devDependencies") or {})}
        if "expo" in deps or any(d.startswith("expo-") for d in deps):
            result["verdict"] = {
                "repo": hit["repo"],
                "react_native": "react-native" in deps,
                "expo": True,
                "expo_version": deps.get("expo", ""),
                "react_native_version": deps.get("react-native", ""),
                "expo_router": "expo-router" in deps,
            }
            return result
        if "react-native" in deps:
            result["verdict"] = {
                "repo": hit["repo"],
                "react_native": True,
                "expo": False,
                "react_native_version": deps.get("react-native", ""),
            }
            return result
    return result


# ---------------------------------------------------------------- developer site


async def web_probe(url: str, max_scripts: int = 5, max_bytes: int = 8_000_000) -> dict[str, Any]:
    """Fetch a site and its script bundles, looking for React Native / Expo web output."""
    out: dict[str, Any] = {"ran": bool(url), "url": url, "hits": [], "scanned": []}
    if not url:
        return out
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT})
            if resp.status_code != 200:
                out["error"] = f"The site returned HTTP {resp.status_code}."
                return out
            base = resp.url
            body = resp.text
            found = {label for needle, label in WEB_NEEDLES.items() if needle in body}
            out["scanned"].append(str(base))

            srcs = re.findall(r'<script[^>]+src="([^"]+)"', body)[:max_scripts]
            for src in srcs:
                full = base.join(src)
                try:
                    script = await client.get(full, headers={"User-Agent": USER_AGENT})
                except httpx.HTTPError:
                    continue
                if script.status_code != 200:
                    continue
                text = script.text[:max_bytes]
                out["scanned"].append(str(full))
                found |= {label for needle, label in WEB_NEEDLES.items() if needle in text}
            out["hits"] = sorted(found)
    except httpx.HTTPError as exc:
        out["error"] = f"Could not read the site: {type(exc).__name__}."
    return out
