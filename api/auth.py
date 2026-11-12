"""Single-user login (§11): password + authenticator code (TOTP), then a signed session cookie.

Caddy protects everything through `forward_auth` to GET /auth/check; the login page and its POST are the
only unauthenticated routes. The API also accepts the cookie itself, so it is safe even if reached directly.

    python -m api.auth setup            # choose a password, enrol an authenticator app, write .env
    python -m api.auth rotate-sessions  # sign out every browser (new session key)

Stdlib only: scrypt for the password, RFC 6238 TOTP, HMAC-SHA256 session tokens.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import threading
import time
from html import escape
from typing import Optional
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

COOKIE = "sp_session"
MAX_FAILURES, LOCK_SECONDS = 5, 15 * 60


# -- password (scrypt) ------------------------------------------------------------------------------
def hash_password(password: str, *, n: int = 2 ** 15, r: int = 8, p: int = 1) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=128 * 1024 * 1024, dklen=32)
    return f"scrypt${n}${r}${p}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=128 * 1024 * 1024, dklen=len(base64.b64decode(dk)))
        return hmac.compare_digest(got, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


# -- TOTP (RFC 6238, SHA-1, 6 digits, 30 s) ----------------------------------------------------------
def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_at(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[off:off + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def totp_match(secret: str, code: str, now: Optional[float] = None, window: int = 1) -> Optional[int]:
    """The matching time-step counter (to block replays), or None."""
    code = "".join(ch for ch in code if ch.isdigit())
    if len(code) != 6 or not secret:
        return None
    step = int((now if now is not None else time.time()) // 30)
    for c in range(step - window, step + window + 1):
        if hmac.compare_digest(totp_at(secret, c), code):
            return c
    return None


def otpauth_uri(secret: str, account: str = "owner", issuer: str = "stock_primer") -> str:
    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}&digits=6&period=30"


# -- session tokens -------------------------------------------------------------------------------------
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def issue_session(key: str, hours: int, now: Optional[float] = None) -> str:
    now = now if now is not None else time.time()
    body = _b64(json.dumps({"iat": int(now), "exp": int(now + hours * 3600), "n": secrets.token_hex(8)}).encode())
    return body + "." + _b64(hmac.new(key.encode(), body.encode(), hashlib.sha256).digest())


def valid_session(token: Optional[str], key: str, now: Optional[float] = None) -> bool:
    if not token or not key or "." not in token:
        return False
    body, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _b64(hmac.new(key.encode(), body.encode(), hashlib.sha256).digest())):
        return False
    try:
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError:
        return False
    return (now if now is not None else time.time()) < claims.get("exp", 0)


# -- brute-force guard ----------------------------------------------------------------------------------
class Guard:
    """Single user, so one global counter: 5 failures in 15 minutes lock logins for 15 minutes."""

    def __init__(self):
        self.failures: list[float] = []
        self.locked_until = 0.0
        self.last_counter = -1
        self.lock = threading.Lock()

    def locked(self, now: float) -> bool:
        return now < self.locked_until

    def fail(self, now: float) -> bool:
        with self.lock:
            self.failures = [t for t in self.failures if now - t < LOCK_SECONDS] + [now]
            if len(self.failures) >= MAX_FAILURES:
                self.locked_until, self.failures = now + LOCK_SECONDS, []
                return True
        return False

    def succeed(self, counter: int) -> bool:
        """False if this code was already used (replay)."""
        with self.lock:
            if counter <= self.last_counter:
                return False
            self.last_counter, self.failures = counter, []
            return True


def configured(settings) -> bool:
    return bool(settings.auth_password_hash and settings.auth_totp_secret and settings.auth_session_key)


def safe_next(target: Optional[str]) -> str:
    """Only same-site relative paths, so the login can't be used as an open redirect."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target or target.startswith("/auth/"):
        return "/"
    return target


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in · stock_primer</title><meta name="robots" content="noindex"><style>
:root{{--page:#f9f9f7;--card:#fff;--ink:#0b0b0b;--muted:#6f6d68;--line:rgba(11,11,11,.12);--accent:#2a78d6;--bad:#b42e2e}}
@media (prefers-color-scheme:dark){{:root{{--page:#0d0d0d;--card:#1a1a19;--ink:#fff;--muted:#9a988f;--line:rgba(255,255,255,.12);--accent:#3987e5;--bad:#e66767}}}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--page);color:var(--ink);font:15px system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:16px}}
form{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px;width:100%;max-width:340px;display:grid;gap:14px}}
h1{{font-size:18px;margin:0}} label{{display:grid;gap:4px;color:var(--muted);font-size:13px}}
input{{font:inherit;padding:9px 10px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--ink)}}
button{{font:inherit;padding:10px;border:0;border-radius:8px;background:var(--accent);color:#fff;cursor:pointer}}
.err{{color:var(--bad);margin:0;font-size:14px}} .foot{{color:var(--muted);font-size:12px;margin:0}}
</style></head><body><form method="post" action="/auth/login">
<h1>stock_primer</h1>{error}
<input type="hidden" name="next" value="{next}">
<label>Password<input type="password" name="password" autocomplete="current-password" required autofocus></label>
<label>Authenticator code<input name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9 ]{{6,7}}" maxlength="7" required></label>
<button type="submit">Sign in</button>
<p class="foot">Decision-support only. Not investment advice.</p></form></body></html>"""


def router(get_settings, record=None) -> APIRouter:
    """record(flag, detail) stores a security_event; optional so tests can omit it."""
    r = APIRouter()
    guard = Guard()

    def page(next_: str, error: str = "", status: int = 200) -> HTMLResponse:
        err = f'<p class="err" role="alert">{escape(error)}</p>' if error else ""
        return HTMLResponse(LOGIN_PAGE.format(error=err, next=escape(next_)), status_code=status,
                            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    @r.get("/auth/login", include_in_schema=False)
    def login_form(request: Request, next: str = "/"):
        return page(safe_next(next))

    @r.post("/auth/login", include_in_schema=False)
    async def login(request: Request):
        s = get_settings()
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode(errors="replace")).items()}
        nxt = safe_next(form.get("next"))
        now = time.time()
        ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?")
        if not configured(s):
            return page(nxt, "Login is not set up. Run: python -m api.auth setup", 503)
        if guard.locked(now):
            return page(nxt, "Too many attempts. Try again in a few minutes.", 429)
        counter = totp_match(s.auth_totp_secret, form.get("code", ""), now) \
            if verify_password(form.get("password", ""), s.auth_password_hash) else None
        if counter is None or not guard.succeed(counter):
            locked = guard.fail(now)
            if record:
                record("login_locked" if locked else "login_failed", ip)
            return page(nxt, "Wrong password or code.", 401)
        if record:
            record("login_ok", ip)
        resp = RedirectResponse(nxt, status_code=303)
        resp.set_cookie(COOKIE, issue_session(s.auth_session_key, s.auth_session_hours, now),
                        max_age=s.auth_session_hours * 3600, httponly=True, secure=s.auth_cookie_secure,
                        samesite="strict", path="/")
        return resp

    @r.post("/auth/logout", include_in_schema=False)
    def logout():
        resp = RedirectResponse("/auth/login", status_code=303)
        resp.delete_cookie(COOKIE, path="/", httponly=True, secure=get_settings().auth_cookie_secure, samesite="strict")
        return resp

    @r.get("/auth/check", include_in_schema=False)
    def check(request: Request):
        """Caddy forward_auth target: 200 lets the request through; anything else is returned to the browser."""
        s = get_settings()
        if not configured(s):
            return JSONResponse({"code": "AUTH_NOT_CONFIGURED", "message": "run python -m api.auth setup"}, status_code=503)
        if valid_session(request.cookies.get(COOKIE), s.auth_session_key):
            return Response(status_code=200)
        uri = request.headers.get("x-forwarded-uri", "/")
        if uri.startswith("/api/"):
            return JSONResponse({"code": "LOGIN_REQUIRED", "message": "sign in again", "details": None, "retryable": False},
                                status_code=401)
        return RedirectResponse("/auth/login?next=" + quote(safe_next(uri), safe="/"), status_code=302)

    return r


# -- CLI ----------------------------------------------------------------------------------------------------
def _write_env(path, values: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    keys = {f"STOCK_PRIMER_{k.upper()}": v for k, v in values.items()}
    out, seen = [], set()
    for line in lines:
        k = line.split("=", 1)[0].strip()
        if k in keys:
            out.append(f"{k}={keys[k]}")
            seen.add(k)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in keys.items() if k not in seen]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> None:
    import argparse
    import getpass

    from core.settings import ROOT

    p = argparse.ArgumentParser(prog="python -m api.auth")
    p.add_argument("command", choices=["setup", "rotate-sessions"])
    args = p.parse_args()
    env = ROOT / ".env"
    if args.command == "rotate-sessions":
        _write_env(env, {"auth_session_key": secrets.token_urlsafe(32)})
        print("New session key written; every browser must sign in again after the API restarts.")
        return
    while True:
        pw = getpass.getpass("New password (12+ characters): ")
        if len(pw) < 12:
            print("Too short.")
        elif pw != getpass.getpass("Repeat: "):
            print("Didn't match.")
        else:
            break
    secret = new_totp_secret()
    print("\nAdd this to your authenticator app (Google Authenticator, Microsoft Authenticator, 1Password, ...):")
    print(f"  Key:  {' '.join(secret[i:i + 4] for i in range(0, len(secret), 4))}")
    print(f"  Or open this link on the phone: {otpauth_uri(secret)}\n")
    for _ in range(3):
        if totp_match(secret, input("Enter the 6-digit code it shows: ")) is not None:
            break
        print("That code doesn't match; check the phone's clock and try again.")
    else:
        raise SystemExit("Not saved.")
    _write_env(env, {"auth_password_hash": hash_password(pw), "auth_totp_secret": secret,
                     "auth_session_key": secrets.token_urlsafe(32)})
    print(f"Saved to {env}. Restart the API. Keep the key somewhere safe: it's needed to re-enrol a new phone.")


if __name__ == "__main__":
    main()
