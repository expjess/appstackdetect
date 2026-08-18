"""Asking about the same app twice in one day must not fetch it twice."""

import time

from app import server


def _job(job_id, platform, identifier, version, created, summary="cached summary"):
    return {
        "id": job_id,
        "created": created,
        "result": {
            "app": {"platform": platform, "bundle_id": identifier, "package": identifier,
                    "version_name": version},
            "store": {"version": version},
            "verdict": {"summary": summary},
        },
    }


def test_same_app_today_is_reused_even_when_the_version_differs(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RESULTS_DIR", str(tmp_path))
    server._persist(_job("aaa111", "ios", "co.example.app", "1.0.0", time.time()))

    # A new store listing reports 1.1, but we already read this app today.
    assert server._cached_report("ios", "co.example.app", "1.1") is None
    recent = server._recent_report("ios", "co.example.app")
    assert recent is not None
    assert recent[0]["verdict"]["summary"] == "cached summary"


def test_yesterdays_report_is_not_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RESULTS_DIR", str(tmp_path))
    yesterday = time.time() - 26 * 3600
    server._persist(_job("aaa111", "ios", "co.example.app", "1.0.0", yesterday))
    assert server._recent_report("ios", "co.example.app") is None

    # The exact build is still valid, so that route still reuses it.
    assert server._cached_report("ios", "co.example.app", "1.0.0") is not None


def test_the_newest_report_of_the_day_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RESULTS_DIR", str(tmp_path))
    now = time.time()
    server._persist(_job("old111", "ios", "co.example.app", "1.0.0", now - 3600, "older"))
    server._persist(_job("new222", "ios", "co.example.app", "1.1.0", now, "newer"))
    assert server._recent_report("ios", "co.example.app")[0]["verdict"]["summary"] == "newer"


def test_platforms_and_apps_do_not_cross_over(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RESULTS_DIR", str(tmp_path))
    server._persist(_job("aaa111", "ios", "co.example.app", "1.0.0", time.time()))
    assert server._recent_report("android", "co.example.app") is None
    assert server._recent_report("ios", "co.other.app") is None
