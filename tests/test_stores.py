"""Store input parsing. No network access."""

import pytest

from app.stores import StoreError, parse_input


@pytest.mark.parametrize(
    "raw,platform,identifier",
    [
        ("https://play.google.com/store/apps/details?id=xyz.blueskyweb.app", "android", "xyz.blueskyweb.app"),
        ("https://play.google.com/store/apps/details?id=com.foo.bar&hl=en_US", "android", "com.foo.bar"),
        ("https://apps.apple.com/us/app/bluesky-social/id6444370199", "ios", "6444370199"),
        ("https://apps.apple.com/gb/app/x/id284882215?platform=iphone", "ios", "284882215"),
        ("com.duckduckgo.mobile.android", "android", "com.duckduckgo.mobile.android"),
        ("id284882215", "ios", "284882215"),
        ("284882215", "ios", "284882215"),
        ("  com.foo.bar  ", "android", "com.foo.bar"),
    ],
)
def test_parse_input(raw, platform, identifier):
    target = parse_input(raw)
    assert target.platform == platform
    assert target.identifier == identifier


@pytest.mark.parametrize("raw", ["", "   ", "hello world", "https://example.com/app", "https://play.google.com/store"])
def test_parse_input_rejects_junk(raw):
    with pytest.raises(StoreError):
        parse_input(raw)
