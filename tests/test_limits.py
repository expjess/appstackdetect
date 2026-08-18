"""Usage limits: per-address hourly cap and the daily App Store ceiling."""

import time

import pytest

from app import limits


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    monkeypatch.setattr(limits, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(limits, "APPSTORE_STATE", str(tmp_path / "appstore-usage.json"))
    limits._hits.clear()
    yield
    limits._hits.clear()


def test_per_ip_allows_up_to_the_limit():
    for i in range(limits.PER_IP_LIMIT):
        allowed, message = limits.check_per_ip("203.0.113.5")
        assert allowed, f"request {i + 1} should be allowed"
        assert message == ""


def test_per_ip_blocks_beyond_the_limit_and_names_jess():
    for _ in range(limits.PER_IP_LIMIT):
        limits.check_per_ip("203.0.113.5")
    allowed, message = limits.check_per_ip("203.0.113.5")
    assert allowed is False
    assert "talk to Jess" in message


def test_per_ip_is_tracked_separately_for_each_address():
    for _ in range(limits.PER_IP_LIMIT):
        limits.check_per_ip("203.0.113.5")
    assert limits.check_per_ip("203.0.113.5")[0] is False
    assert limits.check_per_ip("198.51.100.9")[0] is True


def test_per_ip_window_expires(monkeypatch):
    for _ in range(limits.PER_IP_LIMIT):
        limits.check_per_ip("203.0.113.5")
    assert limits.check_per_ip("203.0.113.5")[0] is False

    real_time = time.time
    monkeypatch.setattr(limits.time, "time", lambda: real_time() + limits.PER_IP_WINDOW + 1)
    assert limits.check_per_ip("203.0.113.5")[0] is True


def test_local_requests_are_not_limited():
    for _ in range(limits.PER_IP_LIMIT * 3):
        assert limits.check_per_ip("127.0.0.1")[0] is True


def test_app_store_daily_ceiling():
    assert limits.appstore_downloads_today() == 0
    for _ in range(limits.APPSTORE_DAILY_LIMIT):
        assert limits.appstore_allowance()[0] is True
        limits.record_appstore_download()

    allowed, message = limits.appstore_allowance()
    assert allowed is False
    assert "conservative" in message
    assert "try again tomorrow" in message.lower()
    assert "Jess's Apple account" in message


def test_app_store_count_survives_a_restart():
    limits.record_appstore_download()
    limits.record_appstore_download()
    # A fresh read of the state file is what a restarted process would do.
    assert limits.appstore_downloads_today() == 2
