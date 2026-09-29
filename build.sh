#!/usr/bin/env bash
# Builds two APKs into dist/:
#   otapp-<version>-universal.apk  - runs on every Android phone (bigger)
#   otapp-<version>-arm64.apk      - modern 64-bit phones only (about a third of the size)
# Extra arguments are passed to both `flet build apk` runs (e.g. --build-version 0.3.0).
set -euo pipefail
cd "$(dirname "$0")"

FLET="${FLET:-.venv/bin/flet}"
[ -x "$FLET" ] || FLET=flet

VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
for ((i = 1; i <= $#; i++)); do
  if [ "${!i}" = "--build-version" ]; then j=$((i + 1)); VERSION="${!j}"; fi
done

mkdir -p dist

echo "==> Building universal APK"
"$FLET" build apk --yes --no-rich-output "$@"
cp build/apk/*.apk "dist/otapp-$VERSION-universal.apk"

echo "==> Building arm64 APK"
"$FLET" build apk --yes --no-rich-output --arch arm64-v8a "$@"
cp build/apk/*.apk "dist/otapp-$VERSION-arm64.apk"

ls -lh dist/otapp-"$VERSION"-*.apk
