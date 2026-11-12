"""Database backups (§11): nightly, encrypted, verified, rotated.

    python -m ops.backup keygen          # create STOCK_PRIMER_BACKUP_KEY in .env (copy it somewhere safe!)
    python -m ops.backup run             # back up now
    python -m ops.backup list
    python -m ops.backup verify [FILE]   # decrypt + integrity-check (latest if no FILE)
    python -m ops.backup restore FILE [--to PATH] [--force]

Steps: SQLite online backup API (safe while the scheduler writes) -> PRAGMA integrity_check on the copy ->
gzip -> AES-256-GCM in 1 MiB chunks (each chunk authenticated with its index and a last-chunk flag, so
reordering or truncation is detected) -> sha256 recorded in backups.json -> optional copy to a second
folder (encrypted files only) -> rotation: newest per day for 7 days, per week for 4 weeks, per month for 12 months.
Without a key, backups are still made locally but unencrypted, and are never copied elsewhere.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import struct
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Optional

IST = timezone(timedelta(hours=5, minutes=30))
MAGIC = b"SPBK1"
CHUNK = 1 << 20
MANIFEST = "backups.json"


class BackupError(Exception):
    pass


# -- key & streaming encryption -------------------------------------------------------------------------
def new_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def _key(key: str) -> bytes:
    try:
        raw = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
    except ValueError as e:
        raise BackupError("backup key is not valid base64") from e
    if len(raw) != 32:
        raise BackupError("backup key must be 32 bytes (use: python -m ops.backup keygen)")
    return raw


def encrypt_stream(src: BinaryIO, dst: BinaryIO, key: str) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    aead, prefix = AESGCM(_key(key)), secrets.token_bytes(8)
    dst.write(MAGIC + prefix)
    i, chunk = 0, src.read(CHUNK)
    while True:
        nxt = src.read(CHUNK)
        last = not nxt
        head = struct.pack(">IB", i, last)
        ct = aead.encrypt(prefix + struct.pack(">I", i), chunk, MAGIC + head)
        dst.write(struct.pack(">IB", len(ct), last) + ct)
        if last:
            return
        i, chunk = i + 1, nxt


def decrypt_stream(src: BinaryIO, key: str) -> Iterator[bytes]:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if src.read(len(MAGIC)) != MAGIC:
        raise BackupError("not an encrypted stock_primer backup")
    aead, prefix, i = AESGCM(_key(key)), src.read(8), 0
    while True:
        hdr = src.read(5)
        if len(hdr) < 5:
            raise BackupError("backup is truncated")
        n, last = struct.unpack(">IB", hdr)
        ct = src.read(n)
        if len(ct) < n:
            raise BackupError("backup is truncated")
        try:
            yield aead.decrypt(prefix + struct.pack(">I", i), ct, MAGIC + struct.pack(">IB", i, last))
        except InvalidTag as e:
            raise BackupError("wrong key or the backup was modified") from e
        if last:
            if src.read(1):
                raise BackupError("unexpected data after the last chunk")
            return
        i += 1


# -- helpers ------------------------------------------------------------------------------------------------
def sqlite_path(database_url: str) -> Path:
    if not database_url.startswith("sqlite:///"):
        raise BackupError("only SQLite is backed up here; for Postgres use pg_dump (see deploy/README.md)")
    return Path(database_url.removeprefix("sqlite:///"))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


def integrity(db: Path) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        res = con.execute("PRAGMA integrity_check").fetchall()
        if res != [("ok",)]:
            raise BackupError(f"integrity check failed: {res[:3]}")
        tables = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                  for t in ("price_daily", "recommendation", "paper_trade", "fundamental_snapshot")
                  if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()}
        return {"rows": tables}
    finally:
        con.close()


def load_manifest(d: Path) -> list[dict[str, Any]]:
    try:
        return json.loads((d / MANIFEST).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return []


def _save_manifest(d: Path, entries: list[dict[str, Any]]) -> None:
    tmp = d / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(entries, indent=1), encoding="utf-8")
    os.replace(tmp, d / MANIFEST)


def latest_entry(d: Path) -> Optional[dict[str, Any]]:
    e = [x for x in load_manifest(Path(d)) if (Path(d) / x["file"]).exists()]
    return e[-1] if e else None


def keep_set(entries: list[dict[str, Any]], daily: int, weekly: int, monthly: int) -> set[str]:
    """Grandfather-father-son: the newest backup of each of the last N days, ISO weeks and months."""
    by_time = sorted(entries, key=lambda x: x["created_at"], reverse=True)
    keep: set[str] = set()
    for n, keyf in ((daily, lambda t: t.date()), (weekly, lambda t: t.isocalendar()[:2]), (monthly, lambda t: (t.year, t.month))):
        seen = []
        for e in by_time:
            k = keyf(datetime.fromisoformat(e["created_at"]).astimezone(IST))
            if k not in seen:
                seen.append(k)
                if len(seen) > n:
                    break
                keep.add(e["file"])
    return keep


@dataclass
class Result:
    file: Path
    entry: dict[str, Any]
    copied_to: Optional[Path]
    pruned: list[str]
    warnings: list[str]


# -- operations ---------------------------------------------------------------------------------------------
def run_backup(settings, now: Optional[datetime] = None) -> Result:
    now = now or datetime.now(IST)
    db = sqlite_path(settings.database_url)
    if not db.exists():
        raise BackupError(f"database not found: {db}")
    out_dir = Path(settings.backup_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    warnings = []
    key = settings.backup_key or ""
    if key:
        _key(key)                                       # fail early on a bad key
    else:
        warnings.append("no STOCK_PRIMER_BACKUP_KEY: backup is NOT encrypted and was not copied off this folder")
    stamp = now.astimezone(IST).strftime("%Y%m%d-%H%M%S")
    name = f"stock_primer-{stamp}.db.gz" + (".enc" if key else "")
    final = out_dir / name
    with tempfile.TemporaryDirectory(dir=out_dir) as tmpd:
        snap = Path(tmpd) / "snapshot.db"
        src = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=60)
        dst = sqlite3.connect(snap)
        try:
            src.backup(dst, pages=4096, sleep=0.05)    # in steps, so writers are not blocked for long
        finally:
            dst.close()
            src.close()
        info = integrity(snap)
        gz = Path(tmpd) / "snapshot.db.gz"
        with open(snap, "rb") as fi, gzip.open(gz, "wb", compresslevel=6) as fo:
            shutil.copyfileobj(fi, fo, CHUNK)
        part = out_dir / (name + ".part")
        if key:
            with open(gz, "rb") as fi, open(part, "wb") as fo:
                encrypt_stream(fi, fo, key)
        else:
            shutil.copyfile(gz, part)
        os.replace(part, final)
        db_bytes = snap.stat().st_size
    entry = {"file": name, "created_at": now.astimezone(timezone.utc).isoformat(), "size": final.stat().st_size,
             "db_bytes": db_bytes, "sha256": _sha256(final), "encrypted": bool(key), **info}
    entries = [e for e in load_manifest(out_dir) if (out_dir / e["file"]).exists()] + [entry]
    copied = None
    if key and settings.backup_copy_dir:
        cdir = Path(settings.backup_copy_dir)
        try:
            cdir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(final, cdir / (name + ".part"))
            os.replace(cdir / (name + ".part"), cdir / name)
            if _sha256(cdir / name) != entry["sha256"]:
                raise BackupError("copy checksum mismatch")
            copied = cdir / name
        except (OSError, BackupError) as e:
            warnings.append(f"copy to {cdir} failed: {e}")
    keep = keep_set(entries, settings.backup_keep_daily, settings.backup_keep_weekly, settings.backup_keep_monthly)
    pruned = []
    for e in entries:
        if e["file"] not in keep:
            for d in [out_dir] + ([Path(settings.backup_copy_dir)] if settings.backup_copy_dir else []):
                try:
                    (d / e["file"]).unlink(missing_ok=True)
                except OSError:
                    pass
            pruned.append(e["file"])
    _save_manifest(out_dir, [e for e in entries if e["file"] in keep])
    if copied is not None:
        _save_manifest(Path(settings.backup_copy_dir), [e for e in entries if e["file"] in keep and e["encrypted"]])
    return Result(final, entry, copied, pruned, warnings)


def restore_to(settings, backup: Path, target: Path, force: bool = False) -> dict[str, Any]:
    """Decrypt/unzip into `target` and integrity-check it. Never touches the live database unless it is the target and force=True."""
    backup = Path(backup)
    entry = next((e for e in load_manifest(backup.parent) if e["file"] == backup.name), None)
    if entry and _sha256(backup) != entry["sha256"]:
        raise BackupError("checksum differs from the manifest: file damaged or replaced")
    if target.exists() and not force:
        raise BackupError(f"{target} exists; pass --force to overwrite (stop the API, scheduler and bot first)")
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    with open(backup, "rb") as fi, open(part, "wb") as fo:
        if backup.name.endswith(".enc"):
            if not settings.backup_key:
                raise BackupError("this backup is encrypted; set STOCK_PRIMER_BACKUP_KEY")
            import zlib

            z = zlib.decompressobj(16 + zlib.MAX_WBITS)
            for chunk in decrypt_stream(fi, settings.backup_key):
                fo.write(z.decompress(chunk))
            fo.write(z.flush())
        else:
            with gzip.open(fi) as g:
                shutil.copyfileobj(g, fo, CHUNK)
    try:
        info = integrity(part)
    except BackupError:
        part.unlink(missing_ok=True)
        raise
    for side in ("-wal", "-shm"):
        Path(str(target) + side).unlink(missing_ok=True)
    os.replace(part, target)
    return {"restored_to": str(target), **info}


def verify(settings, backup: Optional[Path] = None) -> dict[str, Any]:
    d = Path(settings.backup_dir)
    if backup is None:
        e = latest_entry(d)
        if e is None:
            raise BackupError("no backups yet")
        backup = d / e["file"]
    with tempfile.TemporaryDirectory(dir=d) as tmpd:
        out = restore_to(settings, Path(backup), Path(tmpd) / "verify.db")
    return {"file": Path(backup).name, "ok": True, "rows": out["rows"]}


# -- CLI ----------------------------------------------------------------------------------------------------
def main() -> None:
    import argparse

    from core.settings import ROOT, Settings

    p = argparse.ArgumentParser(prog="python -m ops.backup")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("keygen")
    sub.add_parser("run")
    sub.add_parser("list")
    v = sub.add_parser("verify")
    v.add_argument("file", nargs="?")
    r = sub.add_parser("restore")
    r.add_argument("file")
    r.add_argument("--to")
    r.add_argument("--force", action="store_true")
    a = p.parse_args()
    st = Settings()
    try:
        if a.cmd == "keygen":
            if st.backup_key:
                raise SystemExit("A backup key already exists in .env; replacing it would make existing backups unreadable.")
            from api.auth import _write_env

            k = new_key()
            _write_env(ROOT / ".env", {"backup_key": k})
            print("Backup key written to .env as STOCK_PRIMER_BACKUP_KEY.\n"
                  "Store a copy OUTSIDE this machine (password manager): without it no backup can be restored.")
        elif a.cmd == "run":
            res = run_backup(st)
            print(f"{res.file} ({res.entry['size'] / 1e6:.1f} MB, {'encrypted' if res.entry['encrypted'] else 'NOT encrypted'})")
            for w in res.warnings:
                print("warning:", w)
            if res.copied_to:
                print("copied to", res.copied_to)
            if res.pruned:
                print("pruned:", ", ".join(res.pruned))
        elif a.cmd == "list":
            for e in load_manifest(Path(st.backup_dir)):
                print(f"{e['created_at'][:19]}  {e['size'] / 1e6:8.1f} MB  {'enc' if e['encrypted'] else 'PLAIN'}  {e['file']}")
        elif a.cmd == "verify":
            print(json.dumps(verify(st, Path(a.file) if a.file else None), indent=1))
        elif a.cmd == "restore":
            target = Path(a.to) if a.to else Path(st.data_dir) / f"restored-{datetime.now(IST):%Y%m%d-%H%M%S}.db"
            print(json.dumps(restore_to(st, Path(a.file), target, a.force), indent=1))
            if not a.to:
                print("To use it: stop the API, scheduler and bot, then point STOCK_PRIMER_DATABASE_URL at it "
                      "or copy it over data/stock_primer.db.")
    except BackupError as e:
        raise SystemExit(f"backup error: {e}")


if __name__ == "__main__":
    main()
