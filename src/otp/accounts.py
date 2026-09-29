"""OTP account model and parsing of everything a user can paste or scan:
otpauth:// URIs, Google Authenticator otpauth-migration:// exports and bare base32 secrets."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import date
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

import pyotp

from otp.migration_pb2 import MigrationPayload, OtpParameters

# Google Authenticator migration enums -> standard values
MIGRATION_ALGORITHMS = {0: "SHA1", 1: "SHA1", 2: "SHA256", 3: "SHA512", 4: "MD5"}
MIGRATION_DIGITS = {0: 6, 1: 6, 2: 8}
MIGRATION_HOTP = 1
MIGRATION_TOTP = 2
MIGRATION_BATCH_SIZE = 10  # accounts per QR code, same as Google Authenticator

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

    def to_uri(self) -> str:
        params = {"secret": self.secret}
        if self.issuer:
            params["issuer"] = self.issuer
        if self.algorithm != "SHA1":
            params["algorithm"] = self.algorithm
        if self.digits != 6:
            params["digits"] = self.digits
        if self.period != 30:
            params["period"] = self.period
        return f"otpauth://totp/{quote(self.name, safe=':@')}?{urlencode(params, quote_via=quote)}"

    def secret_bytes(self) -> bytes:
        return base64.b32decode(self.secret + "=" * (-len(self.secret) % 8))

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


def migration_compatible(account: Account) -> bool:
    """Google Authenticator's export format has no period field and only 6 or 8 digits."""
    return account.period == 30 and account.digits in (6, 8)


def build_migration_uris(accounts: list[Account]) -> list[str]:
    """Encodes accounts as otpauth-migration:// URIs (one per QR code) for Google Authenticator."""
    algorithms = {"SHA1": 1, "SHA256": 2, "SHA512": 3, "MD5": 4}
    accounts = [a for a in accounts if migration_compatible(a)]
    batches = [
        accounts[i:i + MIGRATION_BATCH_SIZE]
        for i in range(0, len(accounts), MIGRATION_BATCH_SIZE)
    ]
    batch_id = secrets.randbits(31)
    uris = []
    for index, batch in enumerate(batches):
        params = []
        for acc in batch:
            # We store "Issuer:account" as the name; Google keeps the issuer separately
            name = acc.name
            if acc.issuer and name.startswith(f"{acc.issuer}:"):
                name = name[len(acc.issuer) + 1:]
            params.append(OtpParameters(
                secret=acc.secret_bytes(),
                name=name,
                issuer=acc.issuer,
                algorithm=algorithms[acc.algorithm],
                digits=1 if acc.digits == 6 else 2,
                type=MIGRATION_TOTP,
            ))
        payload = MigrationPayload(
            otp_parameters=params,
            version=1,
            batch_size=len(batches),
            batch_index=index,
            batch_id=batch_id,
        )
        data = base64.b64encode(payload.to_bytes()).decode("ascii")
        uris.append(f"otpauth-migration://offline?data={quote(data, safe='')}")
    return uris
