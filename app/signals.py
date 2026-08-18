"""Marker tables used to classify an app bundle.

Every marker is a thing that is physically present in a shipped app archive:
a file path, a native library name, or a string inside a .dex / Mach-O binary.
Weights are used only to rank confidence, not to make up facts.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- marker kinds -----------------------------------------------------------
# path      : exact archive path (android) or path relative to the .app (ios)
# path_part : substring of any archive path
# lib       : native library file name (android lib/<abi>/<name>, ios framework dir)
# dex       : byte string present in any classes*.dex
# macho     : byte string present in the main executable or a framework binary


@dataclass(frozen=True)
class Marker:
    kind: str
    value: str
    weight: int
    label: str


@dataclass(frozen=True)
class FrameworkRule:
    key: str
    name: str
    android: tuple[Marker, ...] = ()
    ios: tuple[Marker, ...] = ()


REACT_NATIVE = FrameworkRule(
    key="react_native",
    name="React Native",
    android=(
        Marker("path", "assets/index.android.bundle", 40, "JavaScript bundle assets/index.android.bundle"),
        Marker("lib", "libreactnative.so", 40, "libreactnative.so (React Native 0.76+ merged core library)"),
        Marker("lib", "libreactnativejni.so", 40, "libreactnativejni.so (React Native bridge, pre-0.76)"),
        Marker("dex", "com/facebook/react/", 35, "com.facebook.react classes in dex"),
        Marker("lib", "libjsi.so", 10, "libjsi.so (JavaScript Interface)"),
        Marker("lib", "libfbjni.so", 5, "libfbjni.so"),
        Marker("lib", "libyoga.so", 10, "libyoga.so (Yoga layout engine)"),
    ),
    ios=(
        Marker("path", "main.jsbundle", 40, "JavaScript bundle main.jsbundle"),
        Marker("lib", "React.framework", 35, "React.framework"),
        Marker("lib", "React_Core.framework", 35, "React_Core.framework"),
        Marker("lib", "ReactNativeDependencies.framework", 35, "ReactNativeDependencies.framework"),
        Marker("path_part", "RCTI18nUtil", 10, "React Native resource bundle"),
        Marker("macho", "RCTBridge", 30, "RCTBridge symbols in the app binary"),
        Marker("macho", "RCTRootView", 25, "RCTRootView symbols in the app binary"),
        Marker("macho", "facebook::react", 25, "facebook::react symbols in the app binary"),
    ),
)

EXPO = FrameworkRule(
    key="expo",
    name="Expo",
    android=(
        Marker("lib", "libexpo-modules-core.so", 40, "libexpo-modules-core.so"),
        Marker("dex", "expo/modules/kotlin", 40, "expo.modules.kotlin (Expo Modules API) in dex"),
        Marker("dex", "expo/modules/", 30, "expo.modules.* classes in dex"),
        Marker("path", "assets/app.config", 35, "assets/app.config (Expo app config embedded by expo-constants)"),
        Marker("path", "assets/app.manifest", 25, "assets/app.manifest (expo-updates embedded manifest)"),
        Marker("path", "assets/expo-root.pem", 15, "assets/expo-root.pem (expo-updates code signing root)"),
        Marker("dex", "expo.modules.updates", 20, "expo.modules.updates configuration keys"),
        Marker("dex", "ExpoModulesPackage", 25, "ExpoModulesPackage in dex"),
    ),
    ios=(
        Marker("path_part", "EXConstants.bundle/app.config", 35, "EXConstants.bundle/app.config (Expo app config)"),
        Marker("lib", "ExpoModulesCore.framework", 40, "ExpoModulesCore.framework"),
        Marker("path_part", "app.manifest", 25, "app.manifest (expo-updates embedded manifest)"),
        Marker("path", "Expo.plist", 25, "Expo.plist (expo-updates configuration)"),
        Marker("path_part", "EXUpdates.bundle", 20, "EXUpdates.bundle"),
        Marker("macho", "ExpoModulesCore", 30, "ExpoModulesCore symbols in the app binary"),
        Marker("macho", "EXAppDefinesLoader", 20, "EXAppDefinesLoader symbols in the app binary"),
    ),
)

EXPO_GO = FrameworkRule(
    key="expo_go",
    name="Expo Go",
    android=(
        Marker("dex", "host/exp/exponent", 40, "host.exp.exponent classes (Expo Go runtime)"),
        Marker("path", "assets/kernel.android.bundle", 30, "assets/kernel.android.bundle (Expo Go kernel)"),
    ),
    ios=(
        Marker("macho", "EXKernel", 30, "EXKernel symbols (Expo Go runtime)"),
        Marker("path", "kernel.ios.bundle", 30, "kernel.ios.bundle (Expo Go kernel)"),
    ),
)

OTHER_FRAMEWORKS = (
    FrameworkRule(
        key="flutter",
        name="Flutter",
        android=(
            Marker("lib", "libflutter.so", 40, "libflutter.so"),
            Marker("path_part", "assets/flutter_assets/", 40, "assets/flutter_assets/"),
            Marker("dex", "io/flutter/", 30, "io.flutter classes in dex"),
        ),
        ios=(
            Marker("lib", "Flutter.framework", 40, "Flutter.framework"),
            Marker("path_part", "flutter_assets", 35, "flutter_assets"),
        ),
    ),
    FrameworkRule(
        key="capacitor",
        name="Capacitor",
        android=(
            Marker("dex", "com/getcapacitor", 40, "com.getcapacitor classes in dex"),
            Marker("path", "assets/capacitor.config.json", 40, "assets/capacitor.config.json"),
            Marker("path_part", "assets/capacitor.plugins.json", 30, "assets/capacitor.plugins.json"),
        ),
        ios=(
            Marker("path", "capacitor.config.json", 40, "capacitor.config.json"),
            Marker("lib", "Capacitor.framework", 40, "Capacitor.framework"),
        ),
    ),
    FrameworkRule(
        key="cordova",
        name="Cordova / Ionic (legacy)",
        android=(
            Marker("path", "assets/www/cordova.js", 40, "assets/www/cordova.js"),
            Marker("dex", "org/apache/cordova", 40, "org.apache.cordova classes in dex"),
        ),
        ios=(
            Marker("path_part", "www/cordova.js", 40, "www/cordova.js"),
            Marker("macho", "CDVViewController", 30, "CDVViewController symbols"),
        ),
    ),
    FrameworkRule(
        key="unity",
        name="Unity",
        android=(
            Marker("lib", "libunity.so", 40, "libunity.so"),
            Marker("path_part", "assets/bin/Data/", 35, "assets/bin/Data/"),
        ),
        ios=(Marker("lib", "UnityFramework.framework", 40, "UnityFramework.framework"),),
    ),
    FrameworkRule(
        key="dotnet",
        name=".NET MAUI / Xamarin",
        android=(
            Marker("lib", "libmonodroid.so", 40, "libmonodroid.so"),
            Marker("lib", "libmonosgen-2.0.so", 30, "libmonosgen-2.0.so"),
            Marker("path_part", "assemblies/Mono.Android.dll", 35, "assemblies/Mono.Android.dll"),
        ),
        ios=(Marker("path_part", ".monotouch", 30, "Mono runtime files"),),
    ),
)

# --- JavaScript engine ------------------------------------------------------

JS_ENGINES_ANDROID = {
    "libhermes.so": "Hermes",
    "libhermestooling.so": "Hermes",
    "libhermes-executor-release.so": "Hermes",
    "libjsc.so": "JavaScriptCore",
    "libjscexecutor.so": "JavaScriptCore",
}

JS_ENGINES_IOS = {
    "hermes.framework": "Hermes",
    "JavaScriptCore.framework": "JavaScriptCore",
}

# --- package inventory ------------------------------------------------------
# Native library file name -> npm package that ships it.
LIB_TO_PACKAGE = {
    "libexpo-modules-core.so": "expo",
    "libreanimated.so": "react-native-reanimated",
    "libworklets.so": "react-native-worklets",
    "libgesturehandler.so": "react-native-gesture-handler",
    "librnscreens.so": "react-native-screens",
    "libreact-native-mmkv.so": "react-native-mmkv",
    "libsentry-android.so": "@sentry/react-native",
    "libsentry.so": "@sentry/react-native",
    "libVisionCamera.so": "react-native-vision-camera",
    "librnsvg.so": "react-native-svg",
    "libreactnativeblob.so": "react-native-blob-util / rn-fetch-blob",
    "libsqliteX.so": "react-native-sqlite-storage",
    "libmmkv.so": "react-native-mmkv",
    "librealm.so": "realm",
    "libop-sqlite.so": "@op-engineering/op-sqlite",
    "libRNLlama.so": "llama.rn",
}

# Java/Kotlin package prefix in dex -> npm package.
DEX_PREFIX_TO_PACKAGE = {
    "com/swmansion/reanimated": "react-native-reanimated",
    "com/swmansion/gesturehandler": "react-native-gesture-handler",
    "com/swmansion/rnscreens": "react-native-screens",
    "com/swmansion/worklets": "react-native-worklets",
    "com/th3rdwave/safeareacontext": "react-native-safe-area-context",
    "com/horcrux/svg": "react-native-svg",
    "com/oblador/vectoricons": "react-native-vector-icons",
    "com/mrousavy/camera": "react-native-vision-camera",
    "com/reactnativecommunity/asyncstorage": "@react-native-async-storage/async-storage",
    "com/reactnativecommunity/netinfo": "@react-native-community/netinfo",
    "com/reactnativecommunity/webview": "react-native-webview",
    "com/reactnativecommunity/clipboard": "@react-native-clipboard/clipboard",
    "com/reactnativecommunity/slider": "@react-native-community/slider",
    "com/reactnativecommunity/picker": "@react-native-picker/picker",
    "com/reactnativecommunity/datetimepicker": "@react-native-community/datetimepicker",
    "com/airbnb/android/react/maps": "react-native-maps",
    "com/airbnb/android/react/lottie": "lottie-react-native",
    "com/rnmapbox/rnmbx": "@rnmapbox/maps",
    "com/reactnativestripesdk": "@stripe/stripe-react-native",
    "io/sentry/react": "@sentry/react-native",
    "io/invertase/firebase": "@react-native-firebase/*",
    "com/dylanvann/fastimage": "react-native-fast-image",
    "com/th3rdwave/safeareaview": "react-native-safe-area-context",
    "com/shopify/reactnative/skia": "@shopify/react-native-skia",
    "com/shopify/reactnative/flash_list": "@shopify/flash-list",
    "com/margelo/nitro": "react-native-nitro-modules",
    "com/bitdrift/capture": "@bitdrift/react-native",
    "io/bitdrift/capture": "@bitdrift/react-native",
    "com/microsoft/codepush": "react-native-code-push",
    "com/facebook/react": "react-native",
}

# expo/modules/<dir> -> the npm package that owns it.
# Only entries we are confident about are listed. A directory that is missing
# from this table is reported by its raw namespace instead of a guessed name,
# because app authors and third-party libraries also publish Expo modules
# under expo.modules.* (for example @bsky.app/expo-dynamic-app-icon).
EXPO_MODULE_DIRS = {
    "adapters": "expo-modules-core",
    "application": "expo-application",
    "asset": "expo-asset",
    "audio": "expo-audio",
    "av": "expo-av",
    "backgroundfetch": "expo-background-fetch",
    "backgroundnotificationhandler": "expo-notifications",
    "backgroundtask": "expo-background-task",
    "battery": "expo-battery",
    "blur": "expo-blur",
    "brightness": "expo-brightness",
    "calendar": "expo-calendar",
    "camera": "expo-camera",
    "cellular": "expo-cellular",
    "checkbox": "expo-checkbox",
    "clipboard": "expo-clipboard",
    "constants": "expo-constants",
    "contacts": "expo-contacts",
    "core": "expo-modules-core",
    "crypto": "expo-crypto",
    "devclient": "expo-dev-client",
    "devlauncher": "expo-dev-launcher",
    "devmenu": "expo-dev-menu",
    "device": "expo-device",
    "documentpicker": "expo-document-picker",
    "easclient": "expo-eas-client",
    "fetch": "expo-fetch",
    "filesystem": "expo-file-system",
    "font": "expo-font",
    "gl": "expo-gl",
    "glview": "expo-gl",
    "haptics": "expo-haptics",
    "image": "expo-image",
    "imageloader": "expo-image-loader",
    "imagemanipulator": "expo-image-manipulator",
    "imagepicker": "expo-image-picker",
    "insights": "expo-insights",
    "intentlauncher": "expo-intent-launcher",
    "interfaces": "expo-modules-core",
    "jsonutils": "expo-modules-core",
    "keepawake": "expo-keep-awake",
    "kotlin": "expo-modules-core",
    "lineargradient": "expo-linear-gradient",
    "linking": "expo-linking",
    "localauthentication": "expo-local-authentication",
    "localization": "expo-localization",
    "location": "expo-location",
    "mailcomposer": "expo-mail-composer",
    "manifests": "expo-manifests",
    "medialibrary": "expo-media-library",
    "navigationbar": "expo-navigation-bar",
    "network": "expo-network",
    "notifications": "expo-notifications",
    "permissions": "expo-permissions",
    "print": "expo-print",
    "random": "expo-random",
    "rncompatibility": "expo-modules-core",
    "screencapture": "expo-screen-capture",
    "screenorientation": "expo-screen-orientation",
    "securestore": "expo-secure-store",
    "sensors": "expo-sensors",
    "sharing": "expo-sharing",
    "sms": "expo-sms",
    "speech": "expo-speech",
    "splashscreen": "expo-splash-screen",
    "sqlite": "expo-sqlite",
    "storereview": "expo-store-review",
    "structuredheaders": "expo-modules-core",
    "symbols": "expo-symbols",
    "systemui": "expo-system-ui",
    "task": "expo-task-manager",
    "taskmanager": "expo-task-manager",
    "trackingtransparency": "expo-tracking-transparency",
    "updates": "expo-updates",
    "apploader": "expo-updates",
    "updatesinterface": "expo-updates-interface",
    "video": "expo-video",
    "videothumbnails": "expo-video-thumbnails",
    "webbrowser": "expo-web-browser",
}

# iOS framework directory name -> npm package.
IOS_FRAMEWORK_TO_PACKAGE = {
    "ExpoModulesCore.framework": "expo",
    "hermes.framework": "react-native (Hermes engine)",
    "React.framework": "react-native",
    "RNScreens.framework": "react-native-screens",
    "RNReanimated.framework": "react-native-reanimated",
    "RNGestureHandler.framework": "react-native-gesture-handler",
    "RNSVG.framework": "react-native-svg",
    "RNCAsyncStorage.framework": "@react-native-async-storage/async-storage",
    "Sentry.framework": "@sentry/react-native",
    "SentryPrivate.framework": "@sentry/react-native",
}
