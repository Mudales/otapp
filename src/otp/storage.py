"""Persists accounts.

On Android/iOS the accounts JSON lives in SecureStorage (encrypted with a key held in
the Android Keystore / iOS Keychain). Elsewhere (web, desktop development) it's a plain
JSON file in the app's data directory. Accounts saved to that file by older versions
are moved into SecureStorage on first run.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from otp.accounts import Account

SECURE_KEY = "otapp.accounts"


def _encode(accounts: dict[str, Account]) -> str:
    return json.dumps({
        name: {k: v for k, v in acc.to_dict().items() if k != "name"}
        for name, acc in accounts.items()
    }, indent=4)


def _decode(text: str) -> dict[str, Account]:
    return {name: Account.from_stored(name, value) for name, value in json.loads(text).items()}


class AccountStore:
    def __init__(self, file_path: Path | None = None, secure=None):
        """`secure` is a flet_secure_storage.SecureStorage, or None to use the file."""
        if file_path is None:
            # Set by `flet run` / `flet build`; survives app upgrades on Android
            data_dir = Path(os.getenv("FLET_APP_STORAGE_DATA", "storage/data"))
            file_path = data_dir / "otp_secrets.json"
        self.file_path = Path(file_path)
        self.secure = secure

    def _read_file(self) -> str | None:
        try:
            return self.file_path.read_text()
        except FileNotFoundError:
            return None

    def _write_file(self, text: str):
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file_path.with_suffix(".tmp")
        tmp.write_text(text)
        tmp.replace(self.file_path)

    async def load(self) -> dict[str, Account]:
        """Raises if stored data exists but can't be read, so callers never overwrite it."""
        if self.secure is None:
            text = self._read_file()
            return _decode(text) if text else {}

        text = await self.secure.get(SECURE_KEY)
        if text is not None:
            return _decode(text)

        legacy = self._read_file()
        if not legacy:
            return {}
        accounts = _decode(legacy)
        await self.secure.set(SECURE_KEY, _encode(accounts))
        # Only drop the plain-text copy once the secure copy reads back identical
        if await self.secure.get(SECURE_KEY) == _encode(accounts):
            self.file_path.unlink()
            logging.info("Moved %d accounts into secure storage", len(accounts))
        return accounts

    async def save(self, accounts: dict[str, Account]):
        text = _encode(accounts)
        if self.secure is None:
            self._write_file(text)
        else:
            await self.secure.set(SECURE_KEY, text)
