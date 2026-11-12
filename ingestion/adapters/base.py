"""Adapter contract (§4.3): every source returns normalised records plus provenance.

Free sources are rate-limited, occasionally wrong and change without notice, so
every adapter sits behind a token bucket, a disk cache and retry-with-jitter.
"""
from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, TypeVar

T = TypeVar("T")


class RetryableError(Exception):
    """Transient failure (timeout, 429, 5xx, blocked): retry with backoff, then fall back."""


class PermanentError(Exception):
    """The data does not exist (holiday, delisted, 404): do not retry."""


@dataclass
class Health:
    ok: bool
    latency_ms: Optional[int] = None
    note: str = ""


@dataclass
class FetchResult:
    records: list[dict[str, Any]]
    source: str
    as_of: datetime
    cache_hit: bool = False
    raw_ref: Optional[str] = None
    warnings: list[str] = field(default_factory=list)


class SourceAdapter(Protocol):
    name: str

    def healthcheck(self) -> Health: ...


class TokenBucket:
    """Per-source rate limit: `rate` requests per second with a burst of `capacity`."""

    def __init__(self, rate: float, capacity: int = 1):
        self.rate, self.capacity = rate, capacity
        self.tokens = float(capacity)
        self.updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                time.sleep((1 - self.tokens) / self.rate)


class DiskCache:
    """Raw responses keyed by (source, endpoint, args, as_of_date). Raw files make re-parsing free."""

    def __init__(self, root: Path):
        self.root = root

    def _path(self, source: str, endpoint: str, args: dict, as_of: date, ext: str) -> Path:
        key = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:16]
        return self.root / source / endpoint / f"{as_of.isoformat()}_{key}.{ext}"

    def get(self, source: str, endpoint: str, args: dict, as_of: date, ext: str = "txt") -> Optional[str]:
        p = self._path(source, endpoint, args, as_of, ext)
        return p.read_text(encoding="utf-8") if p.exists() else None

    def put(self, source: str, endpoint: str, args: dict, as_of: date, text: str, ext: str = "txt") -> Path:
        p = self._path(source, endpoint, args, as_of, ext)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def get_bytes(self, source: str, endpoint: str, args: dict, as_of: date, ext: str = "bin") -> Optional[bytes]:
        p = self._path(source, endpoint, args, as_of, ext)
        return p.read_bytes() if p.exists() else None

    def put_bytes(self, source: str, endpoint: str, args: dict, as_of: date, data: bytes, ext: str = "bin") -> Path:
        p = self._path(source, endpoint, args, as_of, ext)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p


def with_retries(fn: Callable[[], T], *, attempts: int = 3, base_delay: float = 1.0,
                 sleep: Callable[[float], None] = time.sleep) -> T:
    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            return fn()
        except PermanentError:
            raise
        except Exception as e:  # network libraries raise many types; treat unknowns as transient
            last = e
            if i < attempts - 1:
                sleep(base_delay * (2 ** i) + random.uniform(0, base_delay))
    raise RetryableError(str(last)) from last


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
