#!/usr/bin/env bash
# Get an iOS app package and analyze it with App Stack Detector.
#
#   ./get-ipa.sh https://apps.apple.com/us/app/grok-bot/id6794501026
#   ./get-ipa.sh co.anysphere.sand
#   ./get-ipa.sh ~/Downloads/SomeApp.ipa
#
# Run this on a Mac (or any machine with an Apple ID you can sign in with).
# It downloads the .ipa with ipatool, uploads it, and prints the result link.
# The .ipa never leaves your machine except to this service.

set -euo pipefail

# Downloading this script from a running instance rewrites the placeholder below to that instance's
# own base URL. Run it straight out of the repository and you have to say where the service is.
SERVICE="${SERVICE:-__SERVICE_URL__}"
INPUT="${1:-}"

if [ -z "$INPUT" ]; then
  echo "usage: $0 <app-store-url | bundle.id | path/to/App.ipa>" >&2
  exit 64
fi

case "$SERVICE" in
  __SERVICE_URL__*)
    echo "error: set SERVICE to the base URL of your App Stack Detector instance," >&2
    echo "       e.g. SERVICE=http://127.0.0.1:8787 $0 $INPUT" >&2
    exit 78
    ;;
esac

say() { printf '\033[36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

upload_and_report() {
  local ipa="$1"
  [ -f "$ipa" ] || die "no such file: $ipa"
  say "Uploading $(basename "$ipa") ($(du -h "$ipa" | cut -f1))"

  local response job
  response=$(curl -sf -F "file=@${ipa}" "${SERVICE}/api/jobs/upload") || die "upload failed"
  job=$(printf '%s' "$response" | grep -o '"job":"[^"]*"' | cut -d'"' -f4)
  [ -n "$job" ] || die "the service did not return a job id: $response"

  say "Analyzing. Result link: ${SERVICE}/j/${job}"
  local status="running" body
  while [ "$status" = "running" ]; do
    sleep 3
    body=$(curl -sf "${SERVICE}/api/jobs/${job}") || die "could not read job status"
    status=$(printf '%s' "$body" | grep -o '"status":"[^"]*"' | head -1 | cut -d'"' -f4)
  done

  if [ "$status" = "error" ]; then
    printf '%s' "$body" | grep -o '"error":"[^"]*"' | head -1 | cut -d'"' -f4
    exit 1
  fi
  printf '\n\033[32m%s\033[0m\n' "$(printf '%s' "$body" | grep -o '"summary":"[^"]*"' | head -1 | cut -d'"' -f4)"
  printf 'Full report: %s/j/%s\n' "$SERVICE" "$job"
}

# A local file needs no App Store round trip.
case "$INPUT" in
  *.ipa|*.apk|*.aab|*.xapk|*.apks|*.tar.gz)
    upload_and_report "$INPUT"
    exit 0
    ;;
esac

# Otherwise resolve the input to a bundle identifier.
BUNDLE_ID="$INPUT"
case "$INPUT" in
  *apps.apple.com*)
    APP_ID=$(printf '%s' "$INPUT" | grep -o '/id[0-9]\{4,\}' | head -1 | tr -d '/id')
    [ -n "$APP_ID" ] || die "could not read an app id out of that App Store link"
    say "Looking up app $APP_ID"
    BUNDLE_ID=$(curl -sf "https://itunes.apple.com/lookup?id=${APP_ID}&country=us" \
      | grep -o '"bundleId":"[^"]*"' | head -1 | cut -d'"' -f4)
    [ -n "$BUNDLE_ID" ] || die "the App Store returned no bundle id for $APP_ID"
    ;;
esac
say "Bundle identifier: $BUNDLE_ID"

# Make sure ipatool is present.
if ! command -v ipatool >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    say "Installing ipatool with Homebrew"
    brew install ipatool
  else
    die "ipatool is not installed. Install Homebrew and run: brew install ipatool"
  fi
fi

if ! ipatool auth info >/dev/null 2>&1; then
  say "Sign in to the App Store (Apple ID password and 2FA code)"
  ipatool auth login
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
OUT="${WORK}/${BUNDLE_ID}.ipa"

say "Downloading $BUNDLE_ID from the App Store"
# This writes a file to this computer. It installs nothing on any device.
if ! ipatool download -b "$BUNDLE_ID" -o "$OUT" 2>"${WORK}/err"; then
  if grep -qi license "${WORK}/err"; then
    if [ "${PURCHASE:-0}" = "1" ]; then
      say "Acquiring the free licence for $BUNDLE_ID"
      ipatool download -b "$BUNDLE_ID" -o "$OUT" --purchase
    else
      cat >&2 <<MSG

Your Apple ID has no licence for this app, so it cannot be downloaded yet.

Acquiring the free licence is the one step that changes your account: the app joins your
purchase history, and an iPhone signed in to the same Apple ID will install it by itself if
Settings > App Store > Automatic Downloads > Apps is turned on.

To go ahead anyway:      PURCHASE=1 $0 $INPUT
To avoid it altogether:  use an Apple ID that already has the app, or one signed in to no device.

MSG
      exit 1
    fi
  else
    cat "${WORK}/err" >&2
    die "ipatool could not download $BUNDLE_ID"
  fi
fi

upload_and_report "$OUT"
