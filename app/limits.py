"""Usage limits.

Two separate concerns:

* A per-address hourly limit, so one script cannot turn the tool into a bulk
  scanner. A person checking apps by hand never reaches it.
* A global daily ceiling on App Store downloads, because those spend requests
  against one real Apple ID. This one is deliberately low.
"""

from __future__ import annotations

import datetime
import json
import os
import threading
import time
from collections import defaultdict, deque

STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state")
APPSTORE_STATE = os.path.join(STATE_DIR, "appstore-usage.json")

PER_IP_LIMIT = int(os.environ.get("PER_IP_LIMIT", "20"))
PER_IP_WINDOW = int(os.environ.get("PER_IP_WINDOW", "3600"))
APPSTORE_DAILY_LIMIT = int(os.environ.get("APPSTORE_DAILY_LIMIT", "25"))

# Requests from the machine itself are not rate limited: reaching them already
# requires shell access here.
LOCAL_ADDRESSES = {"127.0.0.1", "::1", "localhost", ""}

PER_IP_MESSAGE = (
    "You have run {count} analyses from this address in the past hour, which is the limit. "
    "Please talk to Jess if you need to check links at this frequency."
)

APPSTORE_MESSAGE = (
    "This tool has already made {count} App Store downloads today, which is its daily limit. "
    "The limit is deliberately conservative, to preserve Jess's Apple account. Please try again "
    "tomorrow. An uploaded .ipa is never limited, and a Google Play link for the same app still works."
)

_lock = threading.Lock()
_hits: dict[str, deque[float]] = defaultdict(deque)


def _prune(key: str, now: float) -> None:
    window = _hits[key]
    while window and now - window[0] > PER_IP_WINDOW:
        window.popleft()


def check_per_ip(address: str) -> tuple[bool, str]:
    """Record one request from this address. Returns (allowed, message)."""
    if address in LOCAL_ADDRESSES:
        return True, ""
    now = time.time()
    with _lock:
        _prune(address, now)
        if len(_hits[address]) >= PER_IP_LIMIT:
            return False, PER_IP_MESSAGE.format(count=PER_IP_LIMIT)
        _hits[address].append(now)
    return True, ""


def _today() -> str:
    return datetime.date.today().isoformat()


def _read_appstore_state() -> dict[str, int]:
    try:
        with open(APPSTORE_STATE) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def appstore_downloads_today() -> int:
    return int(_read_appstore_state().get(_today(), 0))


def appstore_allowance() -> tuple[bool, str]:
    """Whether another App Store download is allowed today."""
    used = appstore_downloads_today()
    if used >= APPSTORE_DAILY_LIMIT:
        return False, APPSTORE_MESSAGE.format(count=used)
    return True, ""


def record_appstore_download() -> int:
    """Count one App Store download. Survives restarts, so the day's total is real."""
    with _lock:
        state = _read_appstore_state()
        today = _today()
        state[today] = int(state.get(today, 0)) + 1
        # Keep the file small; a week of history is plenty for a sanity check.
        for day in sorted(state)[:-7]:
            state.pop(day, None)
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(APPSTORE_STATE, "w") as fh:
                json.dump(state, fh)
        except OSError:
            pass
        return state[today]


def status() -> dict[str, object]:
    return {
        "per_ip_limit": PER_IP_LIMIT,
        "per_ip_window_seconds": PER_IP_WINDOW,
        "app_store_daily_limit": APPSTORE_DAILY_LIMIT,
        "app_store_downloads_today": appstore_downloads_today(),
    }
