import base64
import io
import json

import pyotp
import pytest

from otp.accounts import Account, parse_any
from otp.storage import AccountStore

SECRET = "JBSWY3DPEHPK3PXP"


def _varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _field(num, value):
    if isinstance(value, int):
        return _varint(num << 3) + _varint(value)
    return _varint(num << 3 | 2) + _varint(len(value)) + value


def migration_uri(entries):
    payload = b""
    for e in entries:
        otp = (_field(1, e["secret"]) + _field(2, e["name"].encode())
               + _field(3, e["issuer"].encode()) + _field(4, e.get("algorithm", 1))
               + _field(5, e.get("digits", 1)) + _field(6, e.get("type", 2)))
        payload += _field(1, otp)
    payload += _field(2, 1)
    return "otpauth-migration://offline?data=" + base64.b64encode(payload).decode()


def test_plain_totp_uri():
    [acc], skipped = parse_any(f"otpauth://totp/PyPI:me%40x.com?secret={SECRET}&issuer=PyPI")
    assert (acc.name, acc.issuer, acc.secret, skipped) == ("PyPI:me@x.com", "PyPI", SECRET, 0)
    assert acc.totp().now() == pyotp.TOTP(SECRET).now()


def test_uri_with_params_any_order():
    [acc], _ = parse_any(
        f"otpauth://totp/Acme?digits=8&secret={SECRET.lower()}&algorithm=SHA256&period=60"
    )
    assert (acc.digits, acc.period, acc.algorithm, acc.secret) == (8, 60, "SHA256", SECRET)
    assert len(acc.totp().now()) == 8


def test_bare_secret():
    [acc], _ = parse_any("jbsw y3dp ehpk 3pxp")
    assert acc.secret == SECRET and acc.name.startswith("Default- ")


@pytest.mark.parametrize("bad", ["hello!", "otpauth://totp/x?digits=6", "otpauth://hotp/x?secret=" + SECRET])
def test_invalid(bad):
    with pytest.raises(ValueError):
        parse_any(bad)


def test_google_migration():
    raw = b"\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a"
    uri = migration_uri([
        {"secret": raw, "name": "me@gmail.com", "issuer": "Google"},
        {"secret": raw, "name": "gh", "issuer": "GitHub", "algorithm": 2, "digits": 2},
        {"secret": raw, "name": "counter", "issuer": "Old", "type": 1},
    ])
    accounts, skipped = parse_any(uri)
    assert skipped == 1
    assert [a.name for a in accounts] == ["Google:me@gmail.com", "GitHub:gh"]
    assert accounts[0].secret == base64.b32encode(raw).decode().rstrip("=")
    assert (accounts[1].algorithm, accounts[1].digits) == ("SHA256", 8)


def test_store_roundtrip_and_legacy(tmp_path):
    path = tmp_path / "otp_secrets.json"
    path.write_text(json.dumps({"PyPI": SECRET}))  # old {name: secret} format
    store = AccountStore(path)
    accounts = store.load()
    assert accounts["PyPI"] == Account(name="PyPI", secret=SECRET)
    accounts["X"] = Account(name="X", secret=SECRET, digits=8)
    store.save(accounts)
    assert AccountStore(path).load() == accounts


def test_decode_qr_image():
    qrcode = pytest.importorskip("qrcode")
    from otp.qr import decode_qr

    uri = f"otpauth://totp/Test?secret={SECRET}"
    buf = io.BytesIO()
    qrcode.make(uri).save(buf, format="PNG")
    assert decode_qr(buf.getvalue()) == [uri]
