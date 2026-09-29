# otapp
### Android app for OTP codes
Shows OTP codes like Google Authenticator. Built with [Flet](https://flet.dev) 1.0.

## Features
- Tap a code to copy it to the clipboard
- Light / dark theme switch in the top bar (remembered between launches)
- **App lock** (padlock in the top bar): unlock with fingerprint/face or the phone's PIN/pattern. Off by default; re-locks after 30s in the background
- **Swipe right** on a card for options (copy, rename, delete), **swipe left** to delete (long-press also opens options)
- Add accounts with the **+** button (bottom right):
  - **Camera scan** of a QR code
  - **QR from an image** / screenshot in the gallery
  - Pasting an `otpauth://totp/...` URI or a bare base32 secret
- **Google Authenticator export**: scan or paste the `otpauth-migration://` QR (Google Authenticator → Transfer accounts → Export). All accounts in it are imported; HOTP (counter-based) accounts are skipped.
- Supports custom digits / period / algorithm (SHA1, SHA256, SHA512)

Secrets are stored in the app's private data directory (`FLET_APP_STORAGE_DATA`), not in `assets/`.

## Development
```bash
pip install "flet[all]==1.0.2" -r requirements.txt pytest
brew install zbar            # or: sudo apt install libzbar0 (needed by pyzbar on desktop)
flet run --web src/main.py   # camera works in web/Android/iOS, not desktop
pytest
```

## Build APK
Locally (Flet downloads Flutter, the JDK and the Android SDK on first run):
```bash
source .venv/bin/activate   # Python 3.10+ environment with flet installed
./build.sh
# -> dist/otapp-<version>-universal.apk  (every phone)
# -> dist/otapp-<version>-arm64.apk      (modern 64-bit phones, ~1/3 the size)
```
Install one on a USB-connected phone with `adb install -r dist/otapp-<version>-arm64.apk`.
A single `flet build apk` also works and writes `build/apk/otapp.apk`.
The camera permission is declared in `pyproject.toml` (`[tool.flet.android.permission]`).
`pyzbar` and `pillow` Android wheels come from Flet's mobile package index.

### CI builds and releases
`.github/workflows/build-apk.yml`:
- **Actions → Build APK → Run workflow** builds a test APK (download it from the run's artifacts).
- Pushing a tag `vX.Y.Z` builds both APKs and, once the signing secrets below are set, publishes a GitHub Release with them attached.

**Signing:** add these repository secrets so every build is signed with the same key. Without them,
each build gets a throwaway debug key, and Android refuses to install it over the previous build
(you'd have to uninstall, which deletes your saved accounts).

| Secret | Value |
|---|---|
| `ANDROID_KEYSTORE_BASE64` | `base64 -i upload-keystore.jks` |
| `ANDROID_KEYSTORE_PASSWORD` | keystore password |
| `ANDROID_KEY_PASSWORD` | key password |
| `ANDROID_KEY_ALIAS` | key alias (e.g. `upload`) |

Create the keystore once with `keytool -genkey -v -keystore upload-keystore.jks -keyalg RSA -keysize 2048 -validity 10000 -alias upload` and keep it safe — losing it means users can't update in place.
