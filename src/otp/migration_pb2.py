"""Manual protobuf wire-format decoder for Google Authenticator migration payload.

Avoids requiring protoc or version-specific protobuf internals.
Schema: https://github.com/nickoala/extract_otp_secret_keys
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = data[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def _parse_fields(data: bytes) -> dict[int, list]:
    """Parse protobuf wire format into {field_number: [values]}."""
    fields: dict[int, list] = {}
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        field_number = tag >> 3
        wire_type = tag & 0x07

        if wire_type == 0:  # varint
            value, pos = _read_varint(data, pos)
        elif wire_type == 2:  # length-delimited
            length, pos = _read_varint(data, pos)
            value = data[pos : pos + length]
            pos += length
        elif wire_type == 1:  # 64-bit
            value = struct.unpack_from("<Q", data, pos)[0]
            pos += 8
        elif wire_type == 5:  # 32-bit
            value = struct.unpack_from("<I", data, pos)[0]
            pos += 4
        else:
            raise ValueError(f"Unsupported wire type {wire_type}")

        fields.setdefault(field_number, []).append(value)
    return fields


@dataclass
class OtpParameters:
    secret: bytes = b""
    name: str = ""
    issuer: str = ""
    algorithm: int = 0  # 0=unspecified, 1=SHA1, 2=SHA256, 3=SHA512, 4=MD5
    digits: int = 0  # 0=unspecified, 1=SIX, 2=EIGHT
    type: int = 0  # 0=unspecified, 1=HOTP, 2=TOTP
    counter: int = 0

    @classmethod
    def from_bytes(cls, data: bytes) -> OtpParameters:
        fields = _parse_fields(data)
        return cls(
            secret=fields.get(1, [b""])[0],
            name=fields.get(2, [b""])[0].decode("utf-8") if fields.get(2) else "",
            issuer=fields.get(3, [b""])[0].decode("utf-8") if fields.get(3) else "",
            algorithm=fields.get(4, [0])[0],
            digits=fields.get(5, [0])[0],
            type=fields.get(6, [0])[0],
            counter=fields.get(7, [0])[0],
        )


@dataclass
class MigrationPayload:
    otp_parameters: list[OtpParameters] = field(default_factory=list)
    version: int = 0
    batch_size: int = 0
    batch_index: int = 0
    batch_id: int = 0

    @classmethod
    def from_bytes(cls, data: bytes) -> MigrationPayload:
        fields = _parse_fields(data)
        otp_params = [
            OtpParameters.from_bytes(raw) for raw in fields.get(1, [])
        ]
        return cls(
            otp_parameters=otp_params,
            version=fields.get(2, [0])[0],
            batch_size=fields.get(3, [0])[0],
            batch_index=fields.get(4, [0])[0],
            batch_id=fields.get(5, [0])[0],
        )
