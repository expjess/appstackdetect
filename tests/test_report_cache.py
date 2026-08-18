"""Report reuse: the same build must not be downloaded twice."""

from app import server


def _job(job_id, platform, identifier, binary_version, store_version):
    return {
        "id": job_id,
        "created": 1.0,
        "result": {
            "app": {"platform": platform, "bundle_id": identifier, "version_name": binary_version},
            "store": {"version": store_version},
            "verdict": {"summary": "cached summary"},
        },
    }


def test_index_and_reuse(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RESULTS_DIR", str(tmp_path))
    job = _job("abc123", "ios", "co.example.app", "1.1.0", "1.1")
    server._persist(job)

    # The store and the binary disagree on the version string, so both work.
    assert server._cached_report("ios", "co.example.app", "1.1.0")["verdict"]["summary"] == "cached summary"
    assert server._cached_report("ios", "co.example.app", "1.1")["verdict"]["summary"] == "cached summary"


def test_other_versions_and_apps_are_not_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RESULTS_DIR", str(tmp_path))
    server._persist(_job("abc123", "ios", "co.example.app", "1.1.0", "1.1"))

    assert server._cached_report("ios", "co.example.app", "1.2.0") is None
    assert server._cached_report("ios", "co.other.app", "1.1.0") is None
    assert server._cached_report("android", "co.example.app", "1.1.0") is None


def test_incomplete_reports_are_not_indexed(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RESULTS_DIR", str(tmp_path))
    job = _job("abc123", "ios", "co.example.app", "", "")
    server._persist(job)
    assert server._cached_report("ios", "co.example.app", "") is None
