> PLEASE NOTE: This is a copy of the project on August 18 and may not be kept updated. This README was written by
> an AI agent and has not been reviewed by a human, so treat the descriptions here as unverified.

# App Stack Detector

Answers one question about a published mobile app: **is it React Native, and does it use Expo?**

It answers by reading the shipped app archive — native libraries, `classes.dex`, the JavaScript
bundle, the Android manifest, `Info.plist` — not by guessing from the store listing.

It runs as a small web service on port 8787, plus a command-line entry point. There is no shared
deployment: you run your own instance. Throughout this README, `$SERVICE` means the base URL of your
instance — `http://127.0.0.1:8787` locally.

## What you give it

| Input | What happens |
| --- | --- |
| Play Store link or Android package name | The APK is downloaded and scanned. Split (XAPK) bundles are merged first. |
| App Store link or `id123456789` | App Store metadata is read. If Google Play ships the same identifier, that Android build is scanned and the result is reported as an inference about the iOS app. |
| Uploaded `.apk` `.aab` `.xapk` `.apks` `.ipa` | Scanned directly. This is the only way to inspect an iOS binary. |
| Uploaded `.tar.gz` holding a simulator `.app` | Scanned directly. This is the shape of an EAS simulator build. |

### Why iOS cannot be downloaded

Apple publishes no app binaries, and an IPA obtained from the App Store is FairPlay-encrypted.
For an App Store link the tool works down this ladder, cheapest first, and reports which rung
answered. Downloading from the App Store spends a request against a real Apple ID, so it comes
last, not first.

1. **A report we already have.** Indexed by identifier and version; costs one metadata call.
2. **Android build under the same bundle identifier.** When that Android build embeds an Expo app
   config declaring the same `ios.bundleIdentifier`, both platforms are proven to come from one
   Expo project.
3. **Android build found by app name plus developer name.** Most apps ship Android under a
   different package id — Airbnb is `com.airbnb.app` on iOS and `com.airbnb.android` on Play. A
   candidate is accepted only when its Play listing names the same developer, so a search for
   "Grok Bot" by Anysphere does not wrongly match X Corp's "Grok".
4. **GitHub code search for the bundle identifier.** An Expo project declares
   `ios.bundleIdentifier` in `app.json` or `app.config.*`, so a public repository answers the
   question outright, read from its `package.json`.
5. **The App Store binary**, downloaded with the signed-in Apple ID. Last resort.
6. **An uploaded `.ipa`**, by drag and drop, by `curl`, or with the helper script.

Rungs 2 to 4 answer a question about the *codebase*: React Native and Expo are properties of a
project, not of one platform's binary, so the Android build of the same project answers the same
question without touching Apple. The report always states which binary was actually read.

**Why not read the iOS binary every time?** It is the most direct evidence, and if directness
matters more than restraint, send `{"ios_binary": true}` to jump straight to rung 5. The default
ordering trades some bandwidth — Android APKs are often larger than IPAs — for not spending Apple
account requests on questions that are already answered.

The developer-website fingerprint (`react-native-web`, `expo-router`, `_expo/static` in the site's
script bundles) runs alongside these but never decides the verdict on its own. It is evidence about
their web app, and is labeled that way.

When every rung fails, the result page says so and shows what each probe did, rather than going
blank.

An uploaded App Store IPA still works. FairPlay encrypts only the main executable, so the
JavaScript bundle, `Info.plist`, `EXConstants.bundle/app.config` and the frameworks stay readable.
The tool reports when the executable is encrypted and which signals it had to skip.

## Getting an iOS build

An IPA is the only way to measure an iOS app directly. Three routes, easiest first.

**One command from your Mac.** Downloads the app with your own Apple ID, uploads it, prints the
verdict and a link:

```bash
curl -O "$SERVICE/static/get-ipa.sh"
bash get-ipa.sh https://apps.apple.com/us/app/bluesky-social/id6444370199
```

Downloaded this way, the script already points at the instance you fetched it from. Copied straight
out of this repository instead, it needs `SERVICE` set: `SERVICE=http://127.0.0.1:8787 bash
get-ipa.sh ...`.

It accepts an App Store link, a bundle id, or a local file. It installs `ipatool` through Homebrew
if needed, signs in interactively, and asks before acquiring a free license (which adds the app to
that Apple ID's purchase history).

**Already have a file:**

```bash
curl -F file=@YourApp.ipa "$SERVICE/api/jobs/upload"
```

The response carries a `/j/<id>` link to the finished report. Reports are written to `results/` and
survive a restart, so links stay shareable.

**Let the server download it.** `bin/ipatool` is installed here and is signed in, so an App Store
link is downloaded and scanned with no upload step. To sign in again, or as a different account:

```bash
bin/ipatool auth login -e you@example.com --keychain-passphrase "$(cat .ipatool-passphrase)"
```

The keyring passphrase is a random string in `.ipatool-passphrase` (mode 0600), which lets the
service unlock the keyring unattended. Pass that flag, or ipatool will prompt for a passphrase that
the service cannot reproduce.

**A downloaded IPA carries the downloader's Apple ID** in `iTunesMetadata.plist`. Reports never read
that file, and it was stripped from the IPA fixture in this repository. Keep it in mind before
sharing an IPA you downloaded.

After that, pasting an App Store link downloads and scans the real iOS binary, with no upload step.
The credentials live in ipatool's keyring on this machine; `bin/ipatool auth revoke` removes them.
Nothing signs in automatically.

### What this does not do to your devices

The download writes an `.ipa` to disk over HTTPS. It installs nothing on any iPhone or iPad, and it
does not use a device at all.

The one step that changes account state is acquiring a **licence** for an app the Apple ID has never
obtained. That adds the app to the account's purchase history, and an iPhone signed in to the same
Apple ID will install it on its own when Settings > App Store > Automatic Downloads > Apps is on.

So licence acquisition is off by default and cannot happen by accident:

- The server never passes `--purchase` unless it is started with `ALLOW_PURCHASE=1`.
- `get-ipa.sh` refuses and explains, unless it is run as `PURCHASE=1 bash get-ipa.sh ...`.
- Without a licence the download fails with a clear message rather than acquiring one.

To avoid the question entirely, sign in with an Apple ID that is signed in to no device, or turn off
Automatic Downloads for apps on the phone.

## What it reports

- **Framework**: React Native, Flutter, Cordova/Ionic, Capacitor, Unity, .NET MAUI/Xamarin, or native.
- **Expo**, at one of three levels:
  - `expo-app` — Expo packages plus an embedded Expo app config.
  - `expo-modules` — Expo packages in a React Native app, with no embedded app config.
  - `none`.
  - `expo-go` — the archive is the Expo Go client itself.
- **Expo SDK version**, from the embedded app config, from the expo-updates configuration, or from
  the `docs.expo.dev/versions/vXX` URL inside the JavaScript bundle.
- **React Native version**, from the `for RN x.y.z` build stamp inside the Hermes library.
- **React version**, JS engine (Hermes or JavaScriptCore), bundle format and size, New Architecture hints.
- **EAS**: project id, update URL, and whether updates are hosted by Expo (`u.expo.dev`) or self-hosted.
- **Package inventory**: Expo SDK modules, config plugins, native React Native libraries, and packages
  named in the JavaScript bundle. Expo modules published by the app itself or by third parties are
  listed by namespace rather than given a guessed npm name.
- **Evidence**: every signal that matched, what it matched on, and its weight. Nothing in the verdict
  is unexplained.

## How detection works

`app/signals.py` holds the marker tables. A marker is a file path, a native library name, or a byte
string inside `classes.dex` or a Mach-O binary. Markers carry weights; a framework needs 40 points to
be called, 70 to be called with certainty. Below 40 the tool says "possible" instead of guessing —
Instagram, for example, ships `com.facebook.react` classes without being a React Native app, and is
reported as a native app that embeds some React Native code.

`app/axml.py` is a small binary AndroidManifest.xml reader, used for the package name, version, and
the `expo.modules.updates.*` meta-data.

## Setup

Three things are not in this repository: the virtualenv, the two downloader binaries, and the test
fixtures. They are per-machine, and the binaries and fixtures are other people's builds.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p bin fixtures results
```

`bin/apkeep` (1.0.0) downloads Android builds, and `bin/ipatool` (2.3.2) downloads App Store builds.
Put a release binary for your platform in `bin/` and make it executable:

- apkeep — https://github.com/EFForg/apkeep/releases
- ipatool — https://github.com/majd/ipatool/releases

Android scanning works with `bin/apkeep` alone. App Store downloads need `bin/ipatool` plus a
signed-in Apple ID (see [Getting an iOS build](#getting-an-ios-build)). Uploads need neither.

To let the service unlock ipatool's keyring unattended, write a random passphrase to
`.ipatool-passphrase` (never committed, and listed in `.gitignore`):

```bash
head -c 30 /dev/urandom | base64 > .ipatool-passphrase
chmod 600 .ipatool-passphrase
```

## Running it

```bash
./serve.sh                      # start or restart on port 8787
.venv/bin/python -m pytest -q   # 55 tests; fixture tests skip if fixtures/ is empty
.venv/bin/python run_cli.py fixtures/*.apk   # command-line output, used during development
```

`serve.sh` starts a plain background process bound to `127.0.0.1`, with no supervisor behind it, so
it stops if the machine is recycled. Re-run `./serve.sh` to bring it back. Reaching it from anywhere
else needs your own proxy or tunnel in front of that port.

### `/api/health` reports the signed-in account

`GET /api/health` returns whether downloads are available, the APK sources in use, the current usage
limits, and — under `ios_download` — the **display name of the Apple ID signed in to `bin/ipatool`
on that machine**, for example `{"signed_in": true, "account": "Ada Lovelace"}`. It is how the web UI
knows whether App Store downloads can work at all.

There is no authentication on that endpoint. If you put this instance behind a public URL, anyone who
finds it can read the account name, see the daily App Store download counter, and spend requests
against your Apple ID up to the configured limits. Keep the instance local, put your own
authentication in front of it, or edit the `health()` handler in `app/server.py` to drop the
`ios_download` field.

Environment variables: `PORT` (default 8787), `APK_SOURCES` (default
`apk-pure,f-droid,huawei-app-gallery`, tried in order), `APKEEP_TIMEOUT` (default 900 seconds),
`MAX_UPLOAD_BYTES` (default 2 GB).

GitHub code search runs through the Tuft `gh` shim when this is a Tuft machine, and through a plain
authenticated `gh` (or `$GH_BIN`) anywhere else. Neither is required — without one, the GitHub probe
simply returns nothing. File contents are read from raw.githubusercontent.com without
authentication, because the Tuft shim only issues credentials for our own organization.

`serve.sh` writes to `logs/server.log`; set `LOG` to send it somewhere else.

## Test fixtures

The fixtures are not committed: they are 467 MB of other people's release builds, which is past what
GitHub takes and not ours to redistribute. `tests/test_fixtures.py` skips any fixture that is missing,
so the rest of the suite still runs on a fresh clone. To restore them, download each build into
`fixtures/` under the exact file name listed in `tests/test_fixtures.py` — the APKs from F-Droid or
the project's own GitHub releases, Expo Go from Expo's CDN, and the Bluesky `.ipa` from the App Store.

`tests/test_fixtures.py` checks the verdict for eleven real release builds:

| Fixture | Expected |
| --- | --- |
| Bluesky 1.130.0 | React Native 0.81.5 + Expo SDK 54, `expo-app` |
| EteSync Notes 1.7.0 | React Native + Expo SDK 39, `expo-modules` (no app config) |
| Awake on LAN, Demodulate | React Native, no Expo |
| Spark List, Emotic | Flutter |
| Schulrechner, Open Colonies | Cordova |
| Ohm's Now, 10-bit Clock Widget | native Android |
| Expo Go 2.25.1 for iOS (`.tar.gz` from Expo's CDN) | React Native + Expo, detected as the Expo Go client |
| Bluesky 1.130.0 for iOS (App Store `.ipa`) | React Native + Expo SDK 54, `expo-app`, encrypted executable |

Two tests exist only to keep the tool honest: one asserts that the iOS and Android builds of Bluesky
report the same Expo SDK, EAS project id, slug and update URL, and one asserts that no report
contains Apple account data.

Verified by hand against live apps beyond the fixtures: Expo Go (detected as the Expo Go client),
Discord and Shopify (React Native, no Expo), Coinbase (React Native + Expo SDK 56), Pinterest and
DuckDuckGo (native), Instagram (native with some React Native code).

## Usage limits

Three limits, aimed at keeping casual internal use easy while ruling out bulk scanning.

| Limit | Value | Why |
| --- | --- | --- |
| Job starts per address | 20 per hour (`PER_IP_LIMIT`) | A person checking apps by hand never reaches it; a script does at once. Requests from the machine itself are exempt. |
| App Store downloads | 25 per day, globally (`APPSTORE_DAILY_LIMIT`) | These spend requests against one real Apple ID, so the ceiling is deliberately low and shared by everyone. |
| Same app, same day | reused, not re-fetched | Asking twice in one day serves the stored report. |

The per-address counter uses the socket peer address and deliberately ignores `X-Forwarded-For`.
The tuft proxy preserves the real client address — the access log shows 20 distinct external
addresses rather than one proxy address — so honouring the header would only let a caller forge a
fresh identity per request and walk past the limit.

Uploading a file is never limited: it costs no download and no App Store request.

`{"refresh": true}` bypasses reuse and forces a fresh analysis. Current usage is visible at
`/api/health`.

## Repeat work

Two Apple endpoints are involved, and they carry different risk.

**The public lookup endpoint** (`itunes.apple.com/lookup`) is unauthenticated metadata. A burst of
25 requests from this machine returned HTTP 200 every time, so no limit was reached in practice.
Apple documents its search endpoints as being limited to roughly 20 calls per minute, and that
allowance is not a promise. Lookups are therefore cached in memory for 6 hours
(`ITUNES_CACHE_TTL`), and HTTP 403 or 429 is reported as rate limiting rather than as a broken app.

**The authenticated download path** (`ipatool`, signed in as a real Apple ID) is where restraint
matters. It is a normal account performing normal downloads; the exact thresholds are not published,
and finding them by experiment would risk the account. So the tool avoids repeat work instead:

- Every finished report is indexed by platform, identifier and version in `results/index.json`.
- A request for a build that was already analyzed returns the stored report and downloads nothing.
  The store listing and the binary can state versions differently — Grok Bot is `1.1` on the App
  Store and `1.1.0` in `Info.plist` — so both spellings are indexed.
- A request for an app analyzed **earlier the same day** reuses that report even when the version
  differs or is unknown, which is the common case for Android, where the version is not known until
  after the download.
- `{"refresh": true}` in the request body forces a fresh download.

The practical effect: asking the same question twice costs one metadata call instead of a 40 MB
download. Repeat lookups drop from about 30 seconds to about 1 second.

## Known limits

- iOS detection is verified against two real Apple bundles: a downloaded App Store IPA
  (Bluesky 1.130.0) and a simulator `.app` (Expo Go). Working against real data corrected two
  mistakes: current Expo writes its update configuration to **`Expo.plist`**, not to `Info.plist`,
  and the embedded manifest lives at **`EXUpdates.bundle/app.manifest`**, not at the bundle root.
- The Hermes `for RN x.y.z` stamp is missing on iOS when a project sets
  `buildReactNativeFromSource`, as Bluesky does, so the React Native version can be absent from an
  iOS-only report even though the React version and Expo SDK are present. The Android build of the
  same app fills that gap.
- Package inventory is a lower bound. A library with no native code and no distinctive string in the
  bundle is invisible to static analysis.
- APKs come from APKPure, then F-Droid, then Huawei AppGallery. A mirror may lag the current Play
  Store release, ship a regional variant, or lack an app entirely; the version analyzed and the
  mirror it came from are always shown, and Huawei results carry an explicit warning. APKPure does
  not serve every app — Airbnb, for example, is only reachable through AppGallery. F-Droid is read
  through `index-v1.jar` directly, because apkeep's own F-Droid backend fails to parse the current
  index.
- The indirect probes are evidence, not proof. A public repository's `package.json` is strong; a
  developer's website is weak and says nothing about the binary. The UI labels each one.
- The New Architecture flag is an inference from codegen libraries, not a recorded build flag.
