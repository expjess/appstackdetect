"""Detection tests against real app archives in fixtures/.

Each fixture is a shipped release build downloaded from F-Droid or a GitHub
release. A fixture that is not present is skipped rather than failing, so the
suite still runs on a machine with a partial fixture set.
"""

from __future__ import annotations

import os

import pytest

from app.analyze import analyze_path

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")

# fixture file, expected framework, expected react_native, expected uses_expo, expected expo level
CASES = [
    ("bluesky-1.130.0.apk", "React Native", True, True, "expo-app"),
    ("com.etesync.notes_10700.apk", "React Native", True, True, "expo-modules"),
    ("com.awakeonlanmobile_10000204.apk", "React Native", True, False, "none"),
    ("dev.mateusznowak.demodulate_100224.apk", "React Native", True, False, "none"),
    ("com.elementlo.spark_list_7.apk", "Flutter", False, False, "none"),
    ("futuristicgoo.emotic_113.apk", "Flutter", False, False, "none"),
    ("io.cardijey.schulrechner_11500.apk", "Cordova / Ionic (legacy)", False, False, "none"),
    ("de.moroway.oc_100313.apk", "Cordova / Ionic (legacy)", False, False, "none"),
    ("com.ktprograms.ohmsnow_12.apk", "Native Android (Kotlin/Java)", False, False, "none"),
    ("com.github.ashutoshgngwr.tenbitclockwidget_221.apk", "Native Android (Kotlin/Java)", False, False, "none"),
]


def _load(name: str):
    path = os.path.join(FIXTURES, name)
    if not os.path.exists(path):
        pytest.skip(f"fixture {name} is not downloaded")
    return analyze_path(path)


@pytest.mark.parametrize("name,framework,is_rn,uses_expo,level", CASES)
def test_verdict(name, framework, is_rn, uses_expo, level):
    r = _load(name)
    assert r["verdict"]["framework"] == framework
    assert r["verdict"]["react_native"] is is_rn
    assert r["verdict"]["uses_expo"] is uses_expo
    assert r["verdict"]["expo_level"] == level


def test_bluesky_details():
    """The richest fixture: a current Expo app with an embedded app config."""
    r = _load("bluesky-1.130.0.apk")
    assert r["app"]["package"] == "xyz.blueskyweb.app"
    assert r["app"]["version_name"] == "1.130.0"
    assert r["expo"]["sdk_version"] == "54.0.0"
    assert r["expo"]["slug"] == "bluesky"
    assert r["expo"]["eas_project_id"] == "55bd077a-d905-4184-9c7f-94789ba0f302"
    assert r["expo"]["ios_bundle_identifier"] == "xyz.blueskyweb.app"
    assert r["expo"]["update_service"].startswith("self-hosted")
    assert r["stack"]["react_native_version"] == "0.81.5"
    assert r["stack"]["js_engine"] == "Hermes"
    assert r["stack"]["js_bundle"]["format"] == "Hermes bytecode"
    names = {p["name"] for p in r["packages"]}
    assert "expo-updates" in names
    assert "react-native-reanimated" in names


def test_old_expo_bare_project():
    """SDK 39 app: Expo packages and a self-hosted update URL, no embedded app config."""
    r = _load("com.etesync.notes_10700.apk")
    assert r["expo"]["sdk_version"] == "39.0.0"
    assert r["expo"]["level"] == "expo-modules"
    assert "expo.etesync.com" in r["expo"]["update_url"]
    assert r["expo"].get("app_config_found") is None


def test_plain_react_native_reports_version_and_no_expo():
    r = _load("dev.mateusznowak.demodulate_100224.apk")
    assert r["stack"]["react_native_version"] == "0.79.6"
    assert r["expo"]["uses_expo"] is False
    assert not [p for p in r["packages"] if p["name"].startswith("expo")]


def test_ios_simulator_bundle():
    """A real Apple .app bundle: the iOS build of Expo Go, from Expo's own CDN."""
    r = _load("expo-go-ios-2.25.1.tar.gz")
    assert r["app"]["platform"] == "ios"
    assert r["app"]["bundle_id"] == "host.exp.Exponent"
    assert r["app"]["version_name"] == "2.25.1"
    assert r["app"]["binary_encrypted"] is False
    assert r["verdict"]["react_native"] is True
    assert r["verdict"]["expo_level"] == "expo-go"
    matched = {e["where"] for e in r["evidence"]}
    # Mach-O symbol markers and iOS resource paths, confirmed against a real bundle.
    assert {"RCTBridge", "RCTRootView", "facebook::react"} <= matched
    assert {"ExpoModulesCore", "EXConstants.bundle/app.config"} <= matched
    assert {"EXKernel", "kernel.ios.bundle"} <= matched
    assert r["stack"]["js_bundle"]["path"] == "kernel.ios.bundle"


def test_app_store_ipa():
    """A real App Store IPA: FairPlay-encrypted executable, readable resources.

    Account metadata (iTunesMetadata.plist) was stripped from this fixture,
    because a downloaded IPA carries the purchaser's Apple ID inside it.
    """
    r = _load("bluesky-ios-1.130.0.ipa")
    assert r["app"]["platform"] == "ios"
    assert r["app"]["bundle_id"] == "xyz.blueskyweb.app"
    assert r["app"]["binary_encrypted"] is True
    assert r["verdict"]["react_native"] is True
    assert r["verdict"]["expo_level"] == "expo-app"

    # Detection survives the encrypted executable, using resources instead.
    assert r["expo"]["sdk_version"] == "54.0.0"
    assert r["expo"]["slug"] == "bluesky"
    assert r["expo"]["eas_project_id"] == "55bd077a-d905-4184-9c7f-94789ba0f302"
    assert r["stack"]["js_bundle"]["path"] == "main.jsbundle"
    assert r["stack"]["js_bundle"]["format"] == "Hermes bytecode"
    assert r["stack"]["js_engine"] == "Hermes"

    # expo-updates config comes from Expo.plist on current Expo versions.
    assert r["expo"]["runtime_version"] == "1.130.0"
    assert r["expo"]["update_url"] == "https://updates.bsky.app/manifest"
    assert r["expo"]["update_code_signing"] == "enabled"
    assert r["expo"]["updates_check_on_launch"] == "NEVER"


def test_ios_and_android_builds_agree():
    """The same app, read from two different binaries, must tell one story."""
    ios = _load("bluesky-ios-1.130.0.ipa")
    android = _load("bluesky-1.130.0.apk")
    assert ios["expo"]["sdk_version"] == android["expo"]["sdk_version"]
    assert ios["expo"]["eas_project_id"] == android["expo"]["eas_project_id"]
    assert ios["expo"]["slug"] == android["expo"]["slug"]
    assert ios["expo"]["update_url"] == android["expo"]["update_url"]
    assert ios["verdict"]["expo_level"] == android["verdict"]["expo_level"] == "expo-app"


def test_ipa_report_contains_no_apple_account_data():
    """Reports must never carry the downloader's Apple ID."""
    import json

    blob = json.dumps(_load("bluesky-ios-1.130.0.ipa"))
    assert "apple-id" not in blob
    assert "@expo.io" not in blob
    assert "iTunesMetadata" not in blob


def test_non_app_zip_is_rejected(tmp_path):
    import zipfile

    from app.analyze import AnalysisError

    path = tmp_path / "plain.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("hello.txt", "hi")
    with pytest.raises(AnalysisError):
        analyze_path(str(path))


def test_non_zip_is_rejected(tmp_path):
    from app.analyze import AnalysisError

    path = tmp_path / "notes.txt"
    path.write_text("not an app")
    with pytest.raises(AnalysisError):
        analyze_path(str(path))
