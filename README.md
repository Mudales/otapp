# otapp
### Android app for OTP codes
Shows OTP codes like Google Authenticator. Built with [Flet](https://flet.dev) 1.0.

## Features
- Tap a code to copy it to the clipboard
- **Swipe right** on a card for options (copy, rename, delete), **swipe left** to delete (long-press also opens options)
- Add accounts by:
  - **Camera scan** of a QR code (top-bar scanner icon)
  - **QR from an image** / screenshot in the gallery (top-bar image icon)
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
```bash
flet build apk
```
The camera permission is declared in `pyproject.toml` (`[tool.flet.android.permission]`).
`pyzbar` and `pillow` Android wheels come from Flet's mobile package index.
