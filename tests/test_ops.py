"""Login (password + TOTP, session cookie, forward_auth check) and encrypted backups."""
import asyncio
import io
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api import auth as A
from ops import backup as B

IST = timezone(timedelta(hours=5, minutes=30))
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"      # base32("12345678901234567890"), RFC 6238 appendix B


# -- primitives --------------------------------------------------------------------------------------
def test_password_hash_roundtrip():
    h = A.hash_password("correct horse battery", n=2 ** 12)
    assert A.verify_password("correct horse battery", h) and not A.verify_password("wrong", h)
    assert not A.verify_password("x", "garbage") and h != A.hash_password("correct horse battery", n=2 ** 12)


def test_totp_rfc6238_vectors_and_window():
    assert A.totp_at(RFC_SECRET, 59 // 30) == "287082"
    assert A.totp_at(RFC_SECRET, 1111111109 // 30) == "081804"
    assert A.totp_match(RFC_SECRET, "287 082", now=59) == 1
    assert A.totp_match(RFC_SECRET, "287082", now=59 + 30) == 1          # one step of clock drift allowed
    assert A.totp_match(RFC_SECRET, "287082", now=59 + 90) is None
    assert A.totp_match(RFC_SECRET, "12345", now=59) is None


def test_sessions_and_redirect_safety():
    t = A.issue_session("k", 1, now=1000)
    assert A.valid_session(t, "k", now=1001) and not A.valid_session(t, "k", now=1000 + 3601)
    assert not A.valid_session(t, "other", now=1001) and not A.valid_session(t[:-2] + "xx", "k", now=1001)
    assert not A.valid_session(None, "k") and not A.valid_session(t, "", now=1001)
    assert A.safe_next("/portfolios/1?x=2") == "/portfolios/1?x=2"
    for bad in ("https://evil.example", "//evil.example", "/\\evil", "/auth/login", None, ""):
        assert A.safe_next(bad) == "/"


def test_guard_locks_and_blocks_replay():
    g = A.Guard()
    for i in range(4):
        assert g.fail(100 + i) is False
    assert g.fail(105) is True and g.locked(106) and not g.locked(105 + A.LOCK_SECONDS + 1)
    assert g.succeed(7) and not g.succeed(7) and not g.succeed(6) and g.succeed(8)


# -- end to end through the API ---------------------------------------------------------------------------------
@pytest.fixture()
def login_api(monkeypatch):
    from core.settings import get_settings

    from .test_phase4 import deps_for
    from .synth import synthetic_db

    monkeypatch.setenv("STOCK_PRIMER_AUTH_PASSWORD_HASH", A.hash_password("a long test password", n=2 ** 12))
    monkeypatch.setenv("STOCK_PRIMER_AUTH_TOTP_SECRET", RFC_SECRET)
    monkeypatch.setenv("STOCK_PRIMER_AUTH_SESSION_KEY", "test-session-key")
    monkeypatch.setenv("STOCK_PRIMER_AUTH_COOKIE_SECURE", "false")
    get_settings.cache_clear()
    _, f = synthetic_db(n_up=1, n_flat=0, n_down=1, days=30)

    @asynccontextmanager
    async def deps_cm():
        yield deps_for(f)

    from api.main import create_app

    with TestClient(create_app(deps_cm, f), follow_redirects=False) as c:
        c.f = f
        yield c
    get_settings.cache_clear()


def code_now(offset=0):
    import time
    return A.totp_at(RFC_SECRET, int(time.time() // 30) + offset)


def test_login_flow(login_api):
    c = login_api
    assert c.get("/auth/login").status_code == 200
    r = c.get("/auth/check", headers={"X-Forwarded-Uri": "/portfolios/2"})
    assert r.status_code == 302 and r.headers["location"] == "/auth/login?next=/portfolios/2"
    r = c.get("/auth/check", headers={"X-Forwarded-Uri": "/api/v1/digest"})
    assert r.status_code == 401 and r.json()["code"] == "LOGIN_REQUIRED"
    assert c.get("/api/v1/recommendations").json()["code"] == "LOGIN_REQUIRED"     # API itself also refuses

    bad = c.post("/auth/login", content="password=nope&code=000000&next=/x",
                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert bad.status_code == 401 and "Wrong password or code" in bad.text and "sp_session" not in bad.cookies

    code = code_now()
    ok = c.post("/auth/login", content=f"password=a+long+test+password&code={code}&next=%2Finbox",
                headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert ok.status_code == 303 and ok.headers["location"] == "/inbox"
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert c.get("/auth/check").status_code == 200
    assert c.get("/api/v1/recommendations").status_code == 200

    c.cookies.clear()
    replay = c.post("/auth/login", content=f"password=a+long+test+password&code={code}",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert replay.status_code == 401                                   # the same code can't be used twice

    from sqlalchemy import select
    from core import models as m
    with c.f() as s:
        flags = [e.flag for e in s.scalars(select(m.SecurityEvent).where(m.SecurityEvent.agent == "login"))]
    assert flags == ["login_failed", "login_ok", "login_failed"]

    out = c.post("/auth/logout")
    assert out.status_code == 303 and 'sp_session=""' in out.headers["set-cookie"]


def test_login_lockout(login_api):
    for _ in range(A.MAX_FAILURES):
        login_api.post("/auth/login", content="password=x&code=1", headers={"Content-Type": "application/x-www-form-urlencoded"})
    r = login_api.post("/auth/login", content=f"password=a+long+test+password&code={code_now()}",
                       headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 429


# -- backups ------------------------------------------------------------------------------------------------------
def make_db(path: Path, rows=500):
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE price_daily (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO price_daily (v) VALUES (?)", [(f"row{i}" * 20,) for i in range(rows)])
    con.commit()
    return con


def settings(tmp: Path, **kw):
    base = dict(database_url=f"sqlite:///{(tmp / 'live.db').as_posix()}", backup_dir=tmp / "bk", backup_copy_dir="",
                backup_key=B.new_key(), backup_keep_daily=7, backup_keep_weekly=4, backup_keep_monthly=12, data_dir=tmp)
    base.update(kw)
    return SimpleNamespace(**base)


def test_stream_encryption_detects_tampering_and_truncation():
    key = B.new_key()
    data = bytes(range(256)) * 9000                                      # > 2 chunks
    enc = io.BytesIO()
    B.encrypt_stream(io.BytesIO(data), enc, key)
    blob = enc.getvalue()
    assert b"".join(B.decrypt_stream(io.BytesIO(blob), key)) == data
    with pytest.raises(B.BackupError, match="wrong key"):
        list(B.decrypt_stream(io.BytesIO(blob), B.new_key()))
    flipped = bytearray(blob); flipped[100] ^= 1
    with pytest.raises(B.BackupError, match="modified"):
        list(B.decrypt_stream(io.BytesIO(bytes(flipped)), key))
    first_chunk_end = len(B.MAGIC) + 8 + 5 + B.CHUNK + 16
    with pytest.raises(B.BackupError, match="truncated"):
        list(B.decrypt_stream(io.BytesIO(blob[:first_chunk_end]), key))     # drops later chunks cleanly at a boundary


def test_backup_verify_restore_and_copy(tmp_path):
    live = make_db(tmp_path / "live.db")                                 # open connection, WAL: backup while "in use"
    st = settings(tmp_path, backup_copy_dir=str(tmp_path / "offsite"))
    res = B.run_backup(st, now=datetime(2026, 10, 14, 2, 30, tzinfo=IST))
    live.close()
    assert res.file.name == "stock_primer-20261014-023000.db.gz.enc" and res.entry["encrypted"] and not res.warnings
    assert res.entry["rows"] == {"price_daily": 500} and (tmp_path / "offsite" / res.file.name).exists()
    assert B.verify(st)["rows"] == {"price_daily": 500}
    out = B.restore_to(st, res.file, tmp_path / "restored.db")
    assert sqlite3.connect(tmp_path / "restored.db").execute("SELECT COUNT(*) FROM price_daily").fetchone()[0] == 500
    with pytest.raises(B.BackupError, match="exists"):
        B.restore_to(st, res.file, tmp_path / "restored.db")
    assert out["rows"]["price_daily"] == 500
    with open(res.file, "r+b") as fh:                                   # corrupt it: the manifest checksum catches it
        fh.seek(50); b = fh.read(1); fh.seek(50); fh.write(bytes([b[0] ^ 1]))
    with pytest.raises(B.BackupError, match="checksum"):
        B.verify(st)


def test_unencrypted_backup_warns_and_never_copies(tmp_path):
    make_db(tmp_path / "live.db").close()
    st = settings(tmp_path, backup_key="", backup_copy_dir=str(tmp_path / "offsite"))
    res = B.run_backup(st)
    assert res.file.suffix == ".gz" and "NOT encrypted" in res.warnings[0] and res.copied_to is None
    assert not (tmp_path / "offsite").exists()
    assert B.verify(st)["ok"]


def test_rotation_keeps_daily_weekly_monthly(tmp_path):
    start = datetime(2026, 1, 1, 2, 30, tzinfo=IST)
    entries = [{"file": f"f{i}", "created_at": (start + timedelta(days=i)).astimezone(timezone.utc).isoformat()} for i in range(200)]
    keep = B.keep_set(entries, 7, 4, 12)
    assert {f"f{i}" for i in range(193, 200)} <= keep                    # last 7 days
    assert len(keep) <= 7 + 4 + 12
    assert "f30" in keep and "f0" not in keep                            # 31 Jan is January's newest; 1 Jan is not kept
    months = {(start + timedelta(days=int(f[1:]))).strftime("%Y-%m") for f in keep}
    assert len(months) == 7                                              # Jan..Jul 2026, newest of each


def test_non_sqlite_is_refused(tmp_path):
    with pytest.raises(B.BackupError, match="pg_dump"):
        B.run_backup(settings(tmp_path, database_url="postgresql+psycopg://x/y"))


def test_backup_job_alerts_on_failure(tmp_path):
    from scheduler import jobs
    from orchestrator.fakes import RecordingNotifier

    rt = SimpleNamespace(settings=settings(tmp_path), now=lambda: datetime.now(IST),
                         deps=SimpleNamespace(notifier=RecordingNotifier()))
    out = asyncio.run(jobs.backup_job(rt))                               # no live.db -> error, alert sent
    assert out["ok"] is False and "Nightly backup failed" in rt.deps.notifier.sent[-1]["text"]
    make_db(tmp_path / "live.db").close()
    assert asyncio.run(jobs.backup_job(rt))["ok"] is True
    assert asyncio.run(jobs.backup_verify_job(rt))["ok"] is True
