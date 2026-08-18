"""Static analysis of a shipped app archive (.apk / .aab / .xapk / .apks / .ipa).

Everything reported here comes from bytes that are actually present in the
archive. Nothing is inferred from the store listing.
"""

from __future__ import annotations

import hashlib
import json
import plistlib
import re
import struct
import zipfile
from dataclasses import dataclass, field
from typing import Any

from . import arsc, axml, signals

MAX_DEX_BYTES = 400 * 1024 * 1024
HERMES_MAGIC = bytes.fromhex("c61fbc03c103191f")

RE_EXPO_MODULE_DIR = re.compile(rb"expo/modules/([a-z0-9_]+)")
RE_HERMES_RN_VERSION = re.compile(rb"for RN (\d+\.\d+\.\d+)")
RE_EXPO_DOCS_VERSION = re.compile(rb"docs\.expo\.dev/versions/v(\d+\.\d+\.\d+)")
RE_REACT_RENDERER = re.compile(rb"react-native-renderer:\s+(\d+\.\d+\.\d+)")
RE_RN_MENTION = re.compile(rb"React Native (\d+\.\d+\.\d+)")

# Substrings in the JS bundle that name a JavaScript package.
JS_BUNDLE_PACKAGE_HINTS = {
    b"reactnavigation.org": "@react-navigation/*",
    b"expo-router": "expo-router",
    b"react-native-reanimated": "react-native-reanimated",
    b"react-native-worklets": "react-native-worklets",
    b"react-native-gesture-handler": "react-native-gesture-handler",
    b"react-native-mmkv": "react-native-mmkv",
    b"@sentry/react-native": "@sentry/react-native",
    b"@tanstack/react-query": "@tanstack/react-query",
    b"react-native-svg": "react-native-svg",
    b"nativewind": "nativewind",
    b"react-native-web": "react-native-web",
    b"@shopify/flash-list": "@shopify/flash-list",
    b"react-native-safe-area-context": "react-native-safe-area-context",
}


@dataclass
class Evidence:
    signal: str
    where: str
    weight: int
    framework: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"signal": self.signal, "where": self.where, "weight": self.weight, "framework": self.framework}


@dataclass
class Package:
    name: str
    source: str
    kind: str = "native"

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "source": self.source, "kind": self.kind}


@dataclass
class Result:
    file: dict[str, Any] = field(default_factory=dict)
    app: dict[str, Any] = field(default_factory=dict)
    verdict: dict[str, Any] = field(default_factory=dict)
    stack: dict[str, Any] = field(default_factory=dict)
    expo: dict[str, Any] = field(default_factory=dict)
    packages: list[Package] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    scores: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        seen: dict[str, Package] = {}
        for p in self.packages:
            if p.name not in seen:
                seen[p.name] = p
        return {
            "file": self.file,
            "app": self.app,
            "verdict": self.verdict,
            "stack": self.stack,
            "expo": self.expo,
            "packages": [p.as_dict() for p in sorted(seen.values(), key=lambda x: x.name.lower())],
            "evidence": [e.as_dict() for e in sorted(self.evidence, key=lambda x: -x.weight)],
            "notes": self.notes,
            "scores": self.scores,
        }


class AnalysisError(Exception):
    pass


# --------------------------------------------------------------------------
# entry point


def analyze_path(path: str, display_name: str | None = None) -> dict[str, Any]:
    name = display_name or path.rsplit("/", 1)[-1]
    with open(path, "rb") as fh:
        head = fh.read(4)

    size = _file_size(path)
    digest = _sha256(path)

    if head[:2] == b"\x1f\x8b":
        result = _analyze_tarball(path, name)
        result.file = {"name": name, "size": size, "sha256": digest}
        return result.as_dict()

    if head[:2] != b"PK":
        raise AnalysisError(
            f"{name} is not an app archive. Expected .apk, .aab, .xapk, .apks, .ipa, or a .tar.gz "
            "holding a simulator .app bundle."
        )

    zf = zipfile.ZipFile(path)
    names = set(zf.namelist())

    if "AndroidManifest.xml" in names or any(n.endswith(".dex") for n in names):
        result = _analyze_apk(zf, names)
    elif "base/manifest/AndroidManifest.xml" in names:
        result = _analyze_aab(zf, names)
    elif any(n.endswith(".apk") for n in names):
        result = _analyze_split_container(path, zf, names)
    elif any(".app/" in n for n in names):
        result = _analyze_ipa(zf, names)
    else:
        raise AnalysisError(
            f"{name} is a zip file but does not look like an app archive "
            "(no AndroidManifest.xml, no classes.dex, no *.app bundle)."
        )

    result.file = {"name": name, "size": size, "sha256": digest}
    return result.as_dict()


def _file_size(path: str) -> int:
    import os

    return os.path.getsize(path)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# Android


class MultiZip:
    """Several split APKs presented as one archive. The base APK wins on ties."""

    def __init__(self, zips: list[zipfile.ZipFile]) -> None:
        self.zips = zips
        self._owner: dict[str, zipfile.ZipFile] = {}
        for z in zips:
            for name in z.namelist():
                self._owner.setdefault(name, z)

    def namelist(self) -> list[str]:
        return list(self._owner)

    def read(self, name: str) -> bytes:
        return self._owner[name].read(name)

    def getinfo(self, name: str) -> zipfile.ZipInfo:
        return self._owner[name].getinfo(name)


def _analyze_split_container(path: str, zf: zipfile.ZipFile, names: set[str]) -> Result:
    """An .xapk / .apks bundle: a zip of split APKs. Analyze them as one app."""
    import os
    import tempfile

    inner = sorted(n for n in names if n.endswith(".apk"))
    with tempfile.TemporaryDirectory() as tmp:
        opened: list[tuple[bool, zipfile.ZipFile]] = []
        for n in inner:
            dest = os.path.join(tmp, n.replace("/", "_"))
            with zf.open(n) as src, open(dest, "wb") as out:
                while chunk := src.read(1 << 20):
                    out.write(chunk)
            sub = zipfile.ZipFile(dest)
            is_base = any(x.startswith("classes") and x.endswith(".dex") for x in sub.namelist())
            opened.append((is_base, sub))
        if not any(is_base for is_base, _ in opened):
            raise AnalysisError("No base APK with classes.dex was found inside this split bundle.")

        ordered = [z for is_base, z in opened if is_base] + [z for is_base, z in opened if not is_base]
        multi = MultiZip(ordered)
        merged = set(multi.namelist())
        result = _analyze_android_common(
            multi,
            merged,
            dex_names=[n for n in merged if n.startswith("classes") and n.endswith(".dex")],
            lib_prefixes=("lib/",),
            asset_prefixes=("assets/",),
            manifest_name="AndroidManifest.xml",
            read_zf=multi,
        )
    result.notes.append(
        f"Split app bundle: {len(inner)} APKs were merged before scanning, which is how Google Play "
        "delivers large apps."
    )
    return result


def _analyze_aab(zf: zipfile.ZipFile, names: set[str]) -> Result:
    """Google Play app bundle. Paths are prefixed (base/dex, base/lib, base/assets)."""
    result = _analyze_android_common(
        zf,
        names,
        dex_names=[n for n in names if n.endswith(".dex")],
        lib_prefixes=("base/lib/", "lib/"),
        asset_prefixes=("base/assets/", "assets/"),
        manifest_name="base/manifest/AndroidManifest.xml",
    )
    result.notes.append("Analyzed an Android App Bundle (.aab); paths inside an .aab are prefixed by module.")
    return result


def _analyze_apk(zf: zipfile.ZipFile, names: set[str], extra_names: set[str] | None = None) -> Result:
    return _analyze_android_common(
        zf,
        names | (extra_names or set()),
        dex_names=[n for n in names if n.startswith("classes") and n.endswith(".dex")],
        lib_prefixes=("lib/",),
        asset_prefixes=("assets/",),
        manifest_name="AndroidManifest.xml",
        read_zf=zf,
    )


def _analyze_android_common(
    zf: "zipfile.ZipFile | MultiZip",
    names: set[str],
    dex_names: list[str],
    lib_prefixes: tuple[str, ...],
    asset_prefixes: tuple[str, ...],
    manifest_name: str,
    read_zf: "zipfile.ZipFile | MultiZip | None" = None,
) -> Result:
    read_zf = read_zf or zf
    result = Result()
    result.app["platform"] = "android"

    lib_names: set[str] = set()
    abis: set[str] = set()
    for n in names:
        for prefix in lib_prefixes:
            if n.startswith(prefix) and n.count("/") >= 2:
                parts = n[len(prefix) :].split("/")
                if len(parts) >= 2:
                    abis.add(parts[0])
                    lib_names.add(parts[-1])

    def asset(rel: str) -> str | None:
        for prefix in asset_prefixes:
            if prefix + rel in names:
                return prefix + rel
        return None

    # --- manifest
    manifest_meta: dict[str, str] = {}
    if manifest_name in names:
        try:
            root = axml.parse(read_zf.read(manifest_name))
            result.app["package"] = root.attrs.get("package", "")
            result.app["version_name"] = root.attrs.get("versionName", "")
            result.app["version_code"] = root.attrs.get("versionCode", "")
            for el in axml.iter_elements(root):
                if el.name == "uses-sdk":
                    result.app["min_sdk"] = el.attrs.get("minSdkVersion", "")
                    result.app["target_sdk"] = el.attrs.get("targetSdkVersion", "")
            manifest_meta = axml.meta_data(root)
        except axml.AxmlError as exc:
            result.notes.append(f"Could not parse AndroidManifest.xml: {exc}")

    # --- dex scan
    dex_hits: set[str] = set()
    expo_module_dirs: set[str] = set()
    dex_packages: set[str] = set()
    all_dex_markers = _android_dex_markers()
    scanned = 0
    for n in sorted(dex_names):
        info = zf.getinfo(n)
        if scanned + info.file_size > MAX_DEX_BYTES:
            result.notes.append(f"Stopped dex scanning after {scanned // (1024 * 1024)} MB.")
            break
        data = zf.read(n)
        scanned += len(data)
        for marker in all_dex_markers:
            if marker.encode() in data:
                dex_hits.add(marker)
        for m in RE_EXPO_MODULE_DIR.finditer(data):
            expo_module_dirs.add(m.group(1).decode())
        for prefix, pkg in signals.DEX_PREFIX_TO_PACKAGE.items():
            if prefix.encode() in data:
                dex_packages.add(pkg)
        del data
    result.app["dex_files"] = len(dex_names)
    result.app["abis"] = sorted(abis)

    ctx = _MatchContext(names=names, libs=lib_names, dex_hits=dex_hits, macho_hits=set())
    _score_frameworks(ctx, result, platform="android")

    # --- JS engine
    engines = {name for lib, name in signals.JS_ENGINES_ANDROID.items() if lib in lib_names}
    if engines:
        result.stack["js_engine"] = " + ".join(sorted(engines))

    # --- React Native version, from the Hermes build stamp
    hermes_lib = next(
        (n for n in sorted(names) if n.split("/")[-1].startswith("libhermes") and n.split("/")[-1].endswith(".so")),
        None,
    )
    if hermes_lib:
        m = RE_HERMES_RN_VERSION.search(read_zf.read(hermes_lib))
        if m:
            result.stack["react_native_version"] = m.group(1).decode()
            result.evidence.append(
                Evidence(f'Hermes build stamp "for RN {m.group(1).decode()}"', hermes_lib, 30, "react_native")
            )

    # --- New Architecture hints
    codegen_libs = sorted(x for x in lib_names if x.startswith("libreact_codegen_"))
    if codegen_libs or "libappmodules.so" in lib_names:
        result.stack["new_architecture"] = "likely"
        where = ", ".join(codegen_libs[:5]) or "libappmodules.so"
        result.evidence.append(Evidence("React Native codegen libraries present", where, 15, "react_native"))

    # --- Expo config assets
    app_config_path = asset("app.config")
    if app_config_path:
        try:
            cfg = json.loads(read_zf.read(app_config_path))
            _read_expo_app_config(cfg, result)
        except (ValueError, KeyError) as exc:
            result.notes.append(f"assets/app.config present but could not be parsed: {exc}")

    manifest_path = asset("app.manifest")
    if manifest_path:
        try:
            emb = json.loads(read_zf.read(manifest_path))
            result.expo["embedded_update_id"] = emb.get("id", "")
            if "runtimeVersion" in emb:
                result.expo.setdefault("runtime_version", emb["runtimeVersion"])
        except ValueError:
            pass

    # --- expo-updates configuration from the manifest meta-data.
    # Values are often stored as references into resources.arsc, so resolve them.
    expo_meta = {k: v for k, v in manifest_meta.items() if k.startswith("expo.")}
    if expo_meta and any(v.startswith("@0x") for v in expo_meta.values()) and "resources.arsc" in names:
        try:
            table = arsc.ResourceTable(read_zf.read("resources.arsc"))
            for key, value in list(expo_meta.items()):
                if value.startswith("@0x"):
                    resolved = table.resolve(int(value[1:], 16))
                    if resolved:
                        expo_meta[key] = resolved
        except Exception as exc:  # noqa: BLE001 - resolution is best effort
            result.notes.append(f"Could not read resources.arsc to resolve manifest references: {exc}")
    if expo_meta:
        result.expo["android_manifest_meta_data"] = expo_meta
        url = expo_meta.get("expo.modules.updates.EXPO_UPDATE_URL")
        if url:
            _classify_update_url(url, result)
        rv = expo_meta.get("expo.modules.updates.EXPO_RUNTIME_VERSION")
        if rv:
            result.expo.setdefault("runtime_version", rv)
        sdk = expo_meta.get("expo.modules.updates.EXPO_SDK_VERSION")
        if sdk:
            result.expo.setdefault("sdk_version", sdk)
        if expo_meta.get("expo.modules.updates.CODE_SIGNING_CERTIFICATE"):
            result.expo["update_code_signing"] = "enabled"
        enabled = expo_meta.get("expo.modules.updates.ENABLED")
        if enabled:
            result.expo["updates_enabled"] = enabled
        check = expo_meta.get("expo.modules.updates.EXPO_UPDATES_CHECK_ON_LAUNCH")
        if check:
            result.expo["updates_check_on_launch"] = check

    # --- packages
    for lib in sorted(lib_names):
        pkg = signals.LIB_TO_PACKAGE.get(lib)
        if pkg:
            result.packages.append(Package(pkg, f"native library {lib}"))
    for pkg in sorted(dex_packages):
        result.packages.append(Package(pkg, "classes in dex"))
    _record_expo_modules(expo_module_dirs, result)

    # --- JS bundle
    bundle_path = asset("index.android.bundle")
    if bundle_path:
        _scan_js_bundle(read_zf.read(bundle_path), bundle_path, result)

    _finalize(result)
    return result


def _android_dex_markers() -> list[str]:
    out: list[str] = []
    for rule in (signals.REACT_NATIVE, signals.EXPO, signals.EXPO_GO, *signals.OTHER_FRAMEWORKS):
        out.extend(m.value for m in rule.android if m.kind == "dex")
    return sorted(set(out))


# --------------------------------------------------------------------------
# iOS


class DirReader:
    """A directory on disk presented with the same read()/namelist() API as a zip."""

    def __init__(self, root: str) -> None:
        import os

        self.root = root
        self._names: list[str] = []
        for base, _dirs, files in os.walk(root):
            for f in files:
                full = os.path.join(base, f)
                self._names.append(os.path.relpath(full, root).replace(os.sep, "/"))

    def namelist(self) -> list[str]:
        return self._names

    def read(self, name: str) -> bytes:
        import os

        with open(os.path.join(self.root, name), "rb") as fh:
            return fh.read()


def _analyze_tarball(path: str, display_name: str) -> Result:
    """A .tar.gz holding a simulator .app bundle, as produced by an EAS simulator build."""
    import os
    import tarfile
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(path, "r:gz") as tar:
            for member in tar.getmembers():
                target = os.path.realpath(os.path.join(tmp, member.name))
                if not target.startswith(os.path.realpath(tmp)):
                    raise AnalysisError("The archive contains paths outside itself, so it was not unpacked.")
            tar.extractall(tmp, filter="tar")  # members were checked above

        root = tmp
        if not os.path.exists(os.path.join(root, "Info.plist")):
            candidates = [
                os.path.join(base, d)
                for base, dirs, _ in os.walk(tmp)
                for d in dirs
                if d.endswith(".app")
            ]
            if not candidates:
                raise AnalysisError(
                    f"{display_name} does not contain an .app bundle or a top-level Info.plist."
                )
            root = min(candidates, key=len)

        reader = DirReader(root)
        names = {"App.app/" + n for n in reader.namelist()}

        class _Prefixed:
            def read(self, name: str) -> bytes:
                return reader.read(name[len("App.app/") :])

        result = _analyze_ios(_Prefixed(), names, "App.app/")
    result.notes.append(
        "Analyzed a simulator .app bundle from a .tar.gz. Simulator builds are not FairPlay-encrypted, "
        "so symbols inside the main binary are readable."
    )
    return result


def _analyze_ipa(zf: zipfile.ZipFile, names: set[str]) -> Result:
    app_dirs = sorted({n[: n.index(".app/") + 5] for n in names if ".app/" in n})
    if not app_dirs:
        raise AnalysisError("No *.app directory found in this archive.")
    return _analyze_ios(zf, names, min(app_dirs, key=len))


def _analyze_ios(zf: Any, names: set[str], app_dir: str) -> Result:
    result = Result()
    result.app["platform"] = "ios"
    result.app["bundle_dir"] = app_dir

    rel = {n[len(app_dir) :] for n in names if n.startswith(app_dir)}
    frameworks = sorted({r.split("/")[1] for r in rel if r.startswith("Frameworks/") and r.count("/") >= 2})
    result.app["frameworks"] = frameworks

    # --- Info.plist
    info: dict[str, Any] = {}
    if app_dir + "Info.plist" in names:
        try:
            info = plistlib.loads(zf.read(app_dir + "Info.plist"))
        except Exception as exc:  # plistlib raises a variety of parse errors
            result.notes.append(f"Could not parse Info.plist: {exc}")
    result.app["bundle_id"] = info.get("CFBundleIdentifier", "")
    result.app["name"] = info.get("CFBundleDisplayName") or info.get("CFBundleName", "")
    result.app["version_name"] = info.get("CFBundleShortVersionString", "")
    result.app["version_code"] = info.get("CFBundleVersion", "")
    result.app["min_os"] = info.get("MinimumOSVersion", "")

    # --- main executable: scan strings unless FairPlay-encrypted
    macho_hits: set[str] = set()
    exe_name = info.get("CFBundleExecutable")
    encrypted = False
    if exe_name and app_dir + exe_name in names:
        exe = zf.read(app_dir + exe_name)
        encrypted = _macho_is_encrypted(exe)
        result.app["binary_encrypted"] = encrypted
        markers = _ios_macho_markers()
        for marker in markers:
            if marker.encode() in exe:
                macho_hits.add(marker)
        if encrypted:
            result.notes.append(
                "The main executable is FairPlay-encrypted, which is normal for an IPA downloaded from the "
                "App Store. Symbol names inside it are unreadable, so detection relies on unencrypted "
                "resources (JavaScript bundle, plists, frameworks) instead."
            )
        del exe

    ctx = _MatchContext(names=rel, libs=set(frameworks), dex_hits=set(), macho_hits=macho_hits)
    _score_frameworks(ctx, result, platform="ios")

    # --- JS engine
    engines = {name for fw, name in signals.JS_ENGINES_IOS.items() if fw in frameworks}
    if engines:
        result.stack["js_engine"] = " + ".join(sorted(engines))

    # --- React Native version from the Hermes framework binary
    hermes_bin = app_dir + "Frameworks/hermes.framework/hermes"
    if hermes_bin in names:
        m = RE_HERMES_RN_VERSION.search(zf.read(hermes_bin))
        if m:
            result.stack["react_native_version"] = m.group(1).decode()
            result.evidence.append(
                Evidence(f'Hermes build stamp "for RN {m.group(1).decode()}"', hermes_bin, 30, "react_native")
            )

    # --- Expo app config, shipped by expo-constants
    cfg_rel = next((r for r in sorted(rel) if r.endswith("EXConstants.bundle/app.config")), None)
    if cfg_rel:
        try:
            _read_expo_app_config(json.loads(zf.read(app_dir + cfg_rel)), result)
        except ValueError as exc:
            result.notes.append(f"{cfg_rel} present but could not be parsed: {exc}")

    # --- expo-updates configuration. Current Expo versions write Expo.plist;
    # older ones put the same keys straight into Info.plist.
    expo_keys = {k: v for k, v in info.items() if k.startswith("EXUpdates")}
    if "Expo.plist" in rel:
        try:
            expo_plist = plistlib.loads(zf.read(app_dir + "Expo.plist"))
            for key, value in expo_plist.items():
                expo_keys.setdefault(key, value)
        except Exception as exc:  # plistlib raises a variety of parse errors
            result.notes.append(f"Could not parse Expo.plist: {exc}")
    if expo_keys:
        result.expo["ios_info_plist_keys"] = {k: str(v) for k, v in expo_keys.items()}
        if expo_keys.get("EXUpdatesURL"):
            _classify_update_url(str(expo_keys["EXUpdatesURL"]), result)
        if expo_keys.get("EXUpdatesRuntimeVersion"):
            result.expo.setdefault("runtime_version", str(expo_keys["EXUpdatesRuntimeVersion"]))
        if expo_keys.get("EXUpdatesSDKVersion"):
            result.expo.setdefault("sdk_version", str(expo_keys["EXUpdatesSDKVersion"]))
        if expo_keys.get("EXUpdatesCodeSigningCertificate"):
            result.expo["update_code_signing"] = "enabled"
        if expo_keys.get("EXUpdatesEnabled") is not None:
            result.expo["updates_enabled"] = str(expo_keys["EXUpdatesEnabled"]).lower()
        if expo_keys.get("EXUpdatesCheckOnLaunch"):
            result.expo["updates_check_on_launch"] = str(expo_keys["EXUpdatesCheckOnLaunch"])

    # expo-updates ships the embedded manifest in EXUpdates.bundle on iOS.
    manifest_rel = next((r for r in sorted(rel) if r.endswith("app.manifest")), None)
    if manifest_rel:
        try:
            emb = json.loads(zf.read(app_dir + manifest_rel))
            result.expo["embedded_update_id"] = emb.get("id", "")
        except ValueError:
            pass

    # --- packages from frameworks
    for fw in frameworks:
        pkg = signals.IOS_FRAMEWORK_TO_PACKAGE.get(fw)
        if pkg:
            result.packages.append(Package(pkg, f"framework {fw}"))
        elif fw.startswith("EX") or fw.startswith("Expo"):
            result.packages.append(Package(fw.replace(".framework", ""), f"framework {fw}"))

    # --- JS bundle
    candidates = [r for r in ("main.jsbundle", "main.bundle") if r in rel]
    candidates += sorted(
        r for r in rel if r.endswith((".jsbundle", ".bundle")) and "/" not in r and r not in candidates
    )
    for bundle_rel in candidates:
        data = zf.read(app_dir + bundle_rel)
        if data[:8] == HERMES_MAGIC or b"__d(" in data[:200000] or b"react" in data[:200000].lower():
            _scan_js_bundle(data, bundle_rel, result)
            break

    _finalize(result)
    return result


def _ios_macho_markers() -> list[str]:
    out: list[str] = []
    for rule in (signals.REACT_NATIVE, signals.EXPO, signals.EXPO_GO, *signals.OTHER_FRAMEWORKS):
        out.extend(m.value for m in rule.ios if m.kind == "macho")
    return sorted(set(out))


def _macho_is_encrypted(data: bytes) -> bool:
    """True when the Mach-O carries an LC_ENCRYPTION_INFO command with cryptid != 0."""
    if len(data) < 32:
        return False
    magic = struct.unpack_from("<I", data, 0)[0]
    slices: list[int] = []
    if magic in (0xCAFEBABE, 0xBEBAFECA):  # fat binary
        n = struct.unpack_from(">I", data, 4)[0]
        for i in range(min(n, 8)):
            offset = struct.unpack_from(">I", data, 8 + i * 20 + 8)[0]
            slices.append(offset)
    else:
        slices.append(0)

    for base in slices:
        if base + 32 > len(data):
            continue
        magic = struct.unpack_from("<I", data, base)[0]
        if magic == 0xFEEDFACF:
            header = 32
        elif magic == 0xFEEDFACE:
            header = 28
        else:
            continue
        ncmds = struct.unpack_from("<I", data, base + 16)[0]
        off = base + header
        for _ in range(min(ncmds, 4096)):
            if off + 8 > len(data):
                break
            cmd, cmdsize = struct.unpack_from("<II", data, off)
            if cmdsize == 0:
                break
            if cmd in (0x21, 0x2C) and off + 20 <= len(data):
                cryptid = struct.unpack_from("<I", data, off + 16)[0]
                if cryptid != 0:
                    return True
            off += cmdsize
    return False


# --------------------------------------------------------------------------
# shared helpers


@dataclass
class _MatchContext:
    names: set[str]
    libs: set[str]
    dex_hits: set[str]
    macho_hits: set[str]

    def matches(self, marker: signals.Marker) -> bool:
        if marker.kind == "path":
            return marker.value in self.names
        if marker.kind == "path_part":
            return any(marker.value in n for n in self.names)
        if marker.kind == "lib":
            return marker.value in self.libs
        if marker.kind == "dex":
            return marker.value in self.dex_hits
        if marker.kind == "macho":
            return marker.value in self.macho_hits
        return False


def _score_frameworks(ctx: _MatchContext, result: Result, platform: str) -> None:
    for rule in (signals.REACT_NATIVE, signals.EXPO, signals.EXPO_GO, *signals.OTHER_FRAMEWORKS):
        markers = rule.android if platform == "android" else rule.ios
        score = 0
        for marker in markers:
            if ctx.matches(marker):
                score += marker.weight
                result.evidence.append(Evidence(marker.label, marker.value, marker.weight, rule.key))
        if score:
            result.scores[rule.key] = score


def _read_expo_app_config(cfg: dict[str, Any], result: Result) -> None:
    result.expo["app_config_found"] = True
    for key in ("sdkVersion", "slug", "owner", "name", "version", "scheme", "platforms", "jsEngine"):
        if key in cfg:
            result.expo[_snake(key)] = cfg[key]
    ios_id = (cfg.get("ios") or {}).get("bundleIdentifier")
    if ios_id:
        result.expo["ios_bundle_identifier"] = ios_id
    android_id = (cfg.get("android") or {}).get("package")
    if android_id:
        result.expo["android_package"] = android_id

    if isinstance(cfg.get("runtimeVersion"), dict):
        result.expo["runtime_version_policy"] = cfg["runtimeVersion"].get("policy", "")
    elif cfg.get("runtimeVersion"):
        result.expo["runtime_version"] = cfg["runtimeVersion"]

    updates = cfg.get("updates") or {}
    if updates:
        result.expo["updates_config"] = updates
        if updates.get("url"):
            _classify_update_url(str(updates["url"]), result)

    project_id = ((cfg.get("extra") or {}).get("eas") or {}).get("projectId")
    if project_id:
        result.expo["eas_project_id"] = project_id

    plugins = cfg.get("plugins") or []
    plugin_names = []
    for item in plugins:
        name = item[0] if isinstance(item, list) and item else item
        if isinstance(name, str):
            plugin_names.append(name)
    if plugin_names:
        result.expo["config_plugins"] = plugin_names
        for name in plugin_names:
            if name.startswith("."):
                result.packages.append(Package(f"{name} (local config plugin)", "app.config plugins", "config-plugin"))
            else:
                result.packages.append(Package(name, "app.config plugins", "config-plugin"))


def _classify_update_url(url: str, result: Result) -> None:
    result.expo["update_url"] = url
    host = url.split("//", 1)[-1].split("/", 1)[0].lower()
    if host.endswith("u.expo.dev") or host.endswith("exp.host"):
        result.expo["update_service"] = "EAS Update (hosted by Expo)"
    else:
        result.expo["update_service"] = f"self-hosted or third-party updates server ({host})"


def _record_expo_modules(dirs: set[str], result: Result) -> None:
    if not dirs:
        return
    known, unresolved = [], []
    for d in sorted(dirs):
        pkg = signals.EXPO_MODULE_DIRS.get(d)
        if pkg:
            known.append(pkg)
            result.packages.append(Package(pkg, f"expo.modules.{d} in dex", "expo-module"))
        else:
            unresolved.append(d)
    if known:
        result.expo["expo_modules"] = sorted(set(known))
    if unresolved:
        result.expo["unresolved_expo_modules"] = unresolved
        for d in unresolved:
            result.packages.append(
                Package(
                    f"expo.modules.{d}",
                    "Expo module whose npm package we cannot name: app-owned, third-party, or newer than our table",
                    "unresolved-expo-module",
                )
            )


def _scan_js_bundle(data: bytes, where: str, result: Result) -> None:
    info: dict[str, Any] = {"path": where, "size": len(data)}
    if data[:8] == HERMES_MAGIC:
        info["format"] = "Hermes bytecode"
        if len(data) >= 12:
            info["hermes_bytecode_version"] = struct.unpack_from("<I", data, 8)[0]
        # A Hermes bytecode bundle proves the engine even when the framework is
        # linked statically, as newer React Native versions do on iOS.
        result.stack.setdefault("js_engine", "Hermes")
    else:
        info["format"] = "plain JavaScript"
    result.stack["js_bundle"] = info

    m = RE_EXPO_DOCS_VERSION.search(data)
    if m and not result.expo.get("sdk_version"):
        result.expo["sdk_version"] = m.group(1).decode()
        result.evidence.append(
            Evidence(f"Expo SDK docs URL for v{m.group(1).decode()} in the JS bundle", where, 20, "expo")
        )
    m = RE_REACT_RENDERER.search(data)
    if m:
        result.stack["react_version"] = m.group(1).decode()
    if not result.stack.get("react_native_version"):
        m = RE_RN_MENTION.search(data)
        if m:
            result.stack["react_native_version_hint"] = m.group(1).decode()

    for needle, pkg in JS_BUNDLE_PACKAGE_HINTS.items():
        if needle in data:
            result.packages.append(Package(pkg, "referenced in the JavaScript bundle", "javascript"))


def _snake(key: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()


def _finalize(result: Result) -> None:
    scores = result.scores
    rn = scores.get("react_native", 0)
    expo = scores.get("expo", 0)
    expo_go = scores.get("expo_go", 0)

    others = [
        (rule.name, scores.get(rule.key, 0))
        for rule in signals.OTHER_FRAMEWORKS
        if scores.get(rule.key, 0) >= 35
    ]

    is_rn = rn >= 40
    uses_expo = expo >= 40

    if is_rn:
        framework = "React Native"
        confidence = "certain" if rn >= 70 else "likely"
    elif others:
        framework, best = max(others, key=lambda x: x[1])
        confidence = "certain" if best >= 70 else "likely"
    elif rn > 0:
        framework, confidence = "Native app with some React Native code", "possible"
    elif result.app.get("platform") == "android":
        framework, confidence = "Native Android (Kotlin/Java)", "likely"
    elif result.app.get("platform") == "ios":
        framework, confidence = "Native iOS (Swift/Objective-C)", "likely"
    else:
        framework, confidence = "Not detected", "unknown"

    if uses_expo:
        if result.expo.get("app_config_found"):
            expo_level = "expo-app"
            expo_label = "Expo app (Expo app config embedded)"
        else:
            expo_level = "expo-modules"
            expo_label = "React Native app using Expo modules (no embedded Expo app config)"
    elif expo > 0:
        expo_level = "expo-traces"
        expo_label = "Weak Expo traces only"
    else:
        expo_level = "none"
        expo_label = "No Expo"

    if expo_go >= 40:
        expo_level = "expo-go"
        expo_label = "This is the Expo Go client itself"

    result.verdict = {
        "framework": framework,
        "framework_confidence": confidence,
        "react_native": is_rn,
        "uses_expo": uses_expo,
        "expo_level": expo_level,
        "expo_label": expo_label,
        "other_frameworks": [name for name, _ in others],
        "summary": _summary(framework, confidence, is_rn, uses_expo, expo_level, result),
    }

    result.expo["uses_expo"] = uses_expo
    result.expo["level"] = expo_level
    if result.expo.get("sdk_version"):
        result.stack["expo_sdk_version"] = result.expo["sdk_version"]


def _summary(framework: str, confidence: str, is_rn: bool, uses_expo: bool, level: str, result: Result) -> str:
    if level == "expo-go":
        return "This archive is the Expo Go client. Apps opened inside it are Expo projects loaded at runtime."
    if is_rn and uses_expo:
        sdk = result.expo.get("sdk_version")
        core = "Built with React Native and Expo"
        if sdk:
            core += f" (Expo SDK {sdk})"
        rn_version = result.stack.get("react_native_version")
        if rn_version:
            core += f", React Native {rn_version}"
        return core + "."
    if is_rn:
        rn_version = result.stack.get("react_native_version")
        base = f"Built with React Native{' ' + rn_version if rn_version else ''}, with no Expo packages detected."
        return base
    if uses_expo:
        return "Expo packages are present, but React Native core markers were not found. Worth a manual look."
    if confidence == "possible":
        return (
            "Some React Native code is present, but the core runtime files that a React Native app "
            "always ships are missing. This is usually a native app that embeds React Native in part "
            "of the product. Check the evidence below."
        )
    if framework.startswith("Native Android"):
        return "No cross-platform framework detected. This looks like a native Android app (Kotlin/Java)."
    if framework.startswith("Native iOS"):
        return "No cross-platform framework detected. This looks like a native iOS app (Swift/Objective-C)."
    return f"Built with {framework} ({confidence}). Not a React Native app."
