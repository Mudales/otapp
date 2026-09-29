"""Persists accounts as JSON in the app's private data directory."""

import json
import os
from pathlib import Path

from otp.accounts import Account


class AccountStore:
    def __init__(self, file_path: Path | None = None):
        if file_path is None:
            # Set by `flet run` / `flet build`; survives app upgrades on Android
            data_dir = Path(os.getenv("FLET_APP_STORAGE_DATA", "storage/data"))
            file_path = data_dir / "otp_secrets.json"
        self.file_path = Path(file_path)

    def load(self) -> dict[str, Account]:
        try:
            raw = json.loads(self.file_path.read_text())
        except FileNotFoundError:
            return {}
        return {name: Account.from_stored(name, value) for name, value in raw.items()}

    def save(self, accounts: dict[str, Account]):
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            name: {k: v for k, v in acc.to_dict().items() if k != "name"}
            for name, acc in accounts.items()
        }
        tmp = self.file_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=4))
        tmp.replace(self.file_path)
