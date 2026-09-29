"""OTP account model and parsing of everything a user can paste or scan:
otpauth:// URIs, Google Authenticator otpauth-migration:// exports and bare base32 secrets."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import date
from urllib.parse import parse_qs, unquote, urlparse

import pyotp

from otp.migration_pb2 import MigrationPayload

# Google Authenticator migration enums -> standard values
MIGRATION_ALGORITHMS = {0: "SHA1", 1: "SHA1", 2: "SHA256", 3: "SHA512", 4: "MD5"}
MIGRATION_DIGITS = {0: 6, 1: 6, 2: 8}
MIGRATION_HOTP = 1

SUPPORTED_ALGORITHMS = {"SHA1", "SHA256", "SHA512", "MD5"}


@dataclass
class Account:
    name: str
    secret: str
    issuer: str = ""
    digits: int = 6
    period: int = 30
    algorithm: str = "SHA1"

    def totp(self) -> pyotp.TOTP:
        return pyotp.TOTP(
            self.secret,
            digits=self.digits,
            interval=self.period,
            digest=getattr(hashlib, self.algorithm.lower()),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_stored(cls, name: str, value) -> Account:
        # Older versions of the app stored just {name: secret}
        if isinstance(value, str):
            return cls(name=name, secret=value)
        value = {k: v for k, v in value.items() if k in cls.__dataclass_fields__}
        value["name"] = name
        return cls(**value)


def normalize_secret(secret: str) -> str:
    secret = re.sub(r"[\s-]", "", secret).upper().rstrip("=")
    if not secret or not re.fullmatch(r"[A-Z2-7]+", secret):
        raise ValueError("Secret is not valid base32")
    try:
        base64.b32decode(secret + "=" * (-len(secret) % 8))
    except binascii.Error as e:
        raise ValueError("Secret is not valid base32") from e
    return secret


def parse_otpauth_uri(uri: str) -> Account:
    parsed = urlparse(uri)
    if parsed.scheme != "otpauth":
        raise ValueError("Not an otpauth:// URI")
    if parsed.netloc.lower() != "totp":
        raise ValueError(f"Only TOTP codes are supported (got {parsed.netloc.upper()})")

    params = {k.lower(): v[0] for k, v in parse_qs(parsed.query).items()}
    if "secret" not in params:
        raise ValueError("URI has no secret")

    label = unquote(parsed.path.lstrip("/"))
    issuer = params.get("issuer", "")
    if not issuer and ":" in label:
        issuer = label.split(":", 1)[0]

    algorithm = params.get("algorithm", "SHA1").upper()
    if algorithm not in SUPPORTED_ALGORITHMS:
        raise ValueError(f"Unsupported algorithm {algorithm}")

    return Account(
        name=label or issuer or f"Default- {date.today():%d/%m/%Y}",
        secret=normalize_secret(params["secret"]),
        issuer=issuer,
        digits=int(params.get("digits", 6)),
        period=int(params.get("period", 30)),
        algorithm=algorithm,
    )


def parse_migration_uri(uri: str) -> tuple[list[Account], int]:
    """Decode a Google Authenticator export. Returns (accounts, skipped_hotp_count)."""
    data = parse_qs(urlparse(uri).query).get("data", [None])[0]
    if not data:
        raise ValueError("Migration URI has no data")
    # parse_qs turns '+' into ' ' when the URI wasn't percent-encoded
    data = data.replace(" ", "+")
    payload = MigrationPayload.from_bytes(base64.b64decode(data + "=" * (-len(data) % 4)))

    accounts, skipped = [], 0
    for otp in payload.otp_parameters:
        if otp.type == MIGRATION_HOTP:
            skipped += 1
            continue
        if otp.issuer and otp.name and not otp.name.startswith(f"{otp.issuer}:"):
            name = f"{otp.issuer}:{otp.name}"
        else:
            name = otp.name or otp.issuer
        accounts.append(Account(
            name=name,
            secret=base64.b32encode(otp.secret).decode("ascii").rstrip("="),
            issuer=otp.issuer,
            digits=MIGRATION_DIGITS.get(otp.digits, 6),
            algorithm=MIGRATION_ALGORITHMS.get(otp.algorithm, "SHA1"),
        ))
    return accounts, skipped


def parse_any(text: str) -> tuple[list[Account], int]:
    """Parse pasted/scanned text. Returns (accounts, skipped_count)."""
    text = text.strip()
    if text.lower().startswith("otpauth-migration://"):
        return parse_migration_uri(text)
    if text.lower().startswith("otpauth://"):
        return [parse_otpauth_uri(text)], 0
    return [Account(name=f"Default- {date.today():%d/%m/%Y}", secret=normalize_secret(text))], 0
