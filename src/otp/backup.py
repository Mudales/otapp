"""Password-protected backup files.

Format (JSON): scrypt derives a 256-bit key from the password, AES-256-GCM encrypts
{"accounts": [...]}. The header fields are authenticated as associated data, so
tampering with them makes decryption fail.
"""

from __future__ import annotations

import base64
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from otp.accounts import Account

FORMAT = "otapp-backup"
VERSION = 1
MIN_PASSWORD_LENGTH = 8
# ~32 MB of memory and well under a second on a phone
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1


class BackupError(ValueError):
    pass


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _derive_key(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(password.encode("utf-8"))


def _associated_data(header: dict) -> bytes:
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")


def encrypt_backup(accounts: list[Account], password: str) -> bytes:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise BackupError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    salt, nonce = os.urandom(16), os.urandom(12)
    header = {
        "format": FORMAT,
        "version": VERSION,
        "kdf": {"name": "scrypt", "salt": _b64(salt), "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P},
        "cipher": {"name": "AES-256-GCM", "nonce": _b64(nonce)},
    }
    key = _derive_key(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    plaintext = json.dumps({"accounts": [a.to_dict() for a in accounts]}).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, _associated_data(header))
    return json.dumps({**header, "data": _b64(ciphertext)}, indent=2).encode("utf-8")


def decrypt_backup(data: bytes, password: str) -> list[Account]:
    try:
        doc = json.loads(data)
        if doc.get("format") != FORMAT:
            raise BackupError("This isn't an OTP App backup file")
        if doc.get("version") != VERSION:
            raise BackupError(f"Unsupported backup version {doc.get('version')}")
        header = {k: doc[k] for k in ("format", "version", "kdf", "cipher")}
        kdf, cipher = doc["kdf"], doc["cipher"]
        key = _derive_key(password, base64.b64decode(kdf["salt"]), kdf["n"], kdf["r"], kdf["p"])
        plaintext = AESGCM(key).decrypt(
            base64.b64decode(cipher["nonce"]), base64.b64decode(doc["data"]), _associated_data(header)
        )
    except InvalidTag:
        raise BackupError("Wrong password, or the file is damaged") from None
    except BackupError:
        raise
    except (ValueError, KeyError, TypeError) as ex:
        raise BackupError("This isn't a valid OTP App backup file") from ex

    return [
        Account.from_stored(item["name"], {k: v for k, v in item.items() if k != "name"})
        for item in json.loads(plaintext)["accounts"]
    ]


def merge_restored(existing: list[Account], restored: list[Account]) -> tuple[list[Account], int]:
    """Picks the restored accounts that aren't already here. Returns (to_add, already_here).

    An account is "already here" if one with the same secret and settings exists under any
    name (it may have been renamed since the backup). A different account whose name is
    taken gets a " (2)" style suffix instead of replacing anything.
    """
    def key(a: Account):
        return (a.secret, a.digits, a.period, a.algorithm)

    known = {key(a) for a in existing}
    names = {a.name for a in existing}
    to_add, already = [], 0
    for acc in restored:
        if key(acc) in known:
            already += 1
            continue
        name, n = acc.name, 2
        while name in names:
            name, n = f"{acc.name} ({n})", n + 1
        acc = Account(**{**acc.to_dict(), "name": name})
        known.add(key(acc))
        names.add(name)
        to_add.append(acc)
    return to_add, already
