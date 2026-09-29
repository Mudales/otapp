import asyncio
import json

import pytest

from otp.accounts import Account, build_migration_uris, parse_any
from otp.backup import BackupError, decrypt_backup, encrypt_backup
from otp.storage import SECURE_KEY, AccountStore

ACCOUNTS = [
    Account(name="GitHub:me@x.com", secret="JBSWY3DPEHPK3PXP", issuer="GitHub"),
    Account(name="Bank", secret="KRSXG5CTMVRXEZLUKN2XAZLSKNSWG4TF", digits=8, algorithm="SHA256"),
    Account(name="Weird period", secret="JBSWY3DPEHPK3PXP", period=60),
]


class FakeSecureStorage:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value):
        self.data[key] = value


@pytest.mark.parametrize("account", ACCOUNTS)
def test_uri_roundtrip(account):
    [parsed], _ = parse_any(account.to_uri())
    assert parsed == account


def test_uri_escapes_spaces():
    uri = Account(name="My Bank:joe smith", secret="JBSWY3DPEHPK3PXP", issuer="My Bank").to_uri()
    assert " " not in uri
    assert parse_any(uri)[0][0].name == "My Bank:joe smith"


def test_migration_roundtrip_skips_incompatible():
    [uri] = build_migration_uris(ACCOUNTS)
    parsed, skipped = parse_any(uri)
    assert skipped == 0
    assert parsed == ACCOUNTS[:2]  # 60s period can't be expressed in Google's format


def test_migration_batches():
    many = [Account(name=f"a{i}", secret="JBSWY3DPEHPK3PXP") for i in range(23)]
    uris = build_migration_uris(many)
    assert len(uris) == 3
    names = [a.name for uri in uris for a in parse_any(uri)[0]]
    assert names == [a.name for a in many]


def test_backup_roundtrip():
    data = encrypt_backup(ACCOUNTS, "correct horse")
    assert b"JBSWY3DPEHPK3PXP" not in data
    assert decrypt_backup(data, "correct horse") == ACCOUNTS


def test_backup_wrong_password():
    data = encrypt_backup(ACCOUNTS, "correct horse")
    with pytest.raises(BackupError, match="Wrong password"):
        decrypt_backup(data, "wrong horse!")


def test_backup_tampered_header():
    doc = json.loads(encrypt_backup(ACCOUNTS, "correct horse"))
    doc["kdf"]["n"] = 2**14
    with pytest.raises(BackupError):
        decrypt_backup(json.dumps(doc).encode(), "correct horse")


def test_backup_rejects_short_password_and_junk():
    with pytest.raises(BackupError):
        encrypt_backup(ACCOUNTS, "short")
    with pytest.raises(BackupError):
        decrypt_backup(b"not json", "correct horse")
    with pytest.raises(BackupError, match="isn't an OTP App backup"):
        decrypt_backup(b'{"format": "other"}', "correct horse")


def test_secure_store_migrates_plain_file(tmp_path):
    path = tmp_path / "otp_secrets.json"
    path.write_text(json.dumps({"PyPI": "JBSWY3DPEHPK3PXP"}))
    secure = FakeSecureStorage()
    store = AccountStore(path, secure=secure)

    accounts = asyncio.run(store.load())
    assert list(accounts) == ["PyPI"]
    assert not path.exists()
    assert "JBSWY3DPEHPK3PXP" in secure.data[SECURE_KEY]

    accounts["New"] = Account(name="New", secret="KRSXG5CTMVRXEZLU")
    asyncio.run(store.save(accounts))
    assert list(asyncio.run(AccountStore(path, secure=secure).load())) == ["PyPI", "New"]
    assert not path.exists()


def test_secure_store_keeps_file_if_readback_differs(tmp_path):
    path = tmp_path / "otp_secrets.json"
    path.write_text(json.dumps({"PyPI": "JBSWY3DPEHPK3PXP"}))

    class LossyStorage(FakeSecureStorage):
        async def set(self, key, value):
            self.data[key] = value[:-1]

    asyncio.run(AccountStore(path, secure=LossyStorage()).load())
    assert path.exists()


def test_qr_png_roundtrip():
    from otp.qr import decode_qr, make_qr_png

    [uri] = build_migration_uris(ACCOUNTS)
    assert decode_qr(make_qr_png(uri)) == [uri]


def test_merge_restored():
    from otp.backup import merge_restored

    here = [
        Account(name="Renamed locally", secret="JBSWY3DPEHPK3PXP"),  # was "GitHub:me@x.com"
        Account(name="Bank", secret="MFRGGZDFMZTWQ2LK"),  # same name, different account
    ]
    backup = ACCOUNTS + [Account(name="Bank", secret="MFRGGZDFMZTWQ2LK")]
    to_add, already = merge_restored(here, backup)
    assert already == 2  # renamed GitHub + identical Bank
    # The backup's other "Bank" has a different secret, so it's kept under a new name;
    # same secret with a different period is a different account
    assert [a.name for a in to_add] == ["Bank (2)", "Weird period"]
    assert to_add[0].secret == ACCOUNTS[1].secret
