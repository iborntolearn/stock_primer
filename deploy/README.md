# Running stock_primer on this PC

Everything listens on `127.0.0.1`. **Caddy** is the only way in: HTTPS, security headers and a login wall
(password + authenticator code). Telegram needs no inbound access, because the bot only makes outbound calls.

```
browser ──https──> Caddy ──forward_auth──> API /auth/check (session cookie valid?)
                     ├── /auth/*  -> API (login page, no check)
                     ├── /api/*   -> API 127.0.0.1:8000
                     └── /*       -> web/dist/web/browser (Angular)
scheduler, bot: local processes, same database
```

## One-time setup

1. **Install Caddy:** `winget install CaddyServer.Caddy` (then open a new terminal).
2. **Set up the login** (password + authenticator app; it asks for a code to confirm):
   `.venv\Scripts\python -m api.auth setup`
3. **Create the backup key** (already done if `.env` has `STOCK_PRIMER_BACKUP_KEY`):
   `.venv\Scripts\python -m ops.backup keygen`
   **Copy the key into your password manager.** Without it, no backup can be restored.
4. Optional, in `.env`:
   - `STOCK_PRIMER_BACKUP_COPY_DIR=C:\Users\<you>\OneDrive\stock_primer_backups` keeps a second copy.
     Only encrypted files are copied.
   - `STOCK_PRIMER_ANTHROPIC_API_KEY`, `STOCK_PRIMER_TELEGRAM_BOT_TOKEN`, `STOCK_PRIMER_TELEGRAM_OWNER_CHAT_ID`.

## Start / stop

```powershell
powershell -ExecutionPolicy Bypass -File deploy\start.ps1                 # https://localhost
powershell -ExecutionPolicy Bypass -File deploy\start.ps1 -Site stock-primer.<tailnet>.ts.net
powershell -ExecutionPolicy Bypass -File deploy\stop.ps1
```

`start.ps1` builds the dashboard, then starts the API, scheduler, bot (if a token is set) and Caddy hidden.
Logs go to `data\logs`. To start at logon, use Task Scheduler: *Create Task → Triggers: At log on →
Actions: Start a program* `powershell.exe` with arguments `-ExecutionPolicy Bypass -File <path>\deploy\start.ps1 -NoBuild`.

### Which address?

| Site | Reach it from | Certificate |
|---|---|---|
| `localhost` | this PC only | Caddy's local CA. The first start asks to install it in Windows' trust store |
| `<name>.<tailnet>.ts.net` | your phone/laptop on Tailscale | Real certificate via Tailscale. Run `tailscale cert` once and enable HTTPS in the Tailscale admin |
| a LAN name (`primer.lan`) | home Wi-Fi | Uncomment `tls internal` in the Caddyfile and trust Caddy's root on each device |

Do not port-forward this to the internet; use Tailscale for access from outside.

## Login details

- Password: scrypt-hashed in `.env`. Code: standard 6-digit TOTP; any authenticator app works.
- Five wrong attempts in 15 minutes lock logins for 15 minutes. A code can't be reused.
  Every attempt is stored as a `security_event`.
- Session cookie: HttpOnly, Secure, SameSite=Strict, 12 hours (`STOCK_PRIMER_AUTH_SESSION_HOURS`).
- Sign out: Settings → Sign out. Sign out everywhere: `python -m api.auth rotate-sessions`, then restart the API.
- Lost phone: run `python -m api.auth setup` again on this PC.
- Scripts can still use `STOCK_PRIMER_API_TOKEN` as a Bearer token against `127.0.0.1:8000`.

## Backups

- Nightly at 02:30 (scheduler):
  - Online SQLite copy, safe while running.
  - `PRAGMA integrity_check`, then gzip, then AES-256-GCM.
  - Saved to `data\backups` (plus the copy folder), with a checksum in `backups.json`.
- Rotation: the newest backup of each of the last 7 days, 4 weeks and 12 months.
- Every Sunday at 03:30 the latest backup is restored into a scratch file and integrity-checked.
- A failed backup or restore test sends a Telegram alert. `/api/v1/health` shows the last backup.

```powershell
.venv\Scripts\python -m ops.backup run          # now
.venv\Scripts\python -m ops.backup list
.venv\Scripts\python -m ops.backup verify       # latest, or give a file
.venv\Scripts\python -m ops.backup restore data\backups\stock_primer-YYYYMMDD-HHMMSS.db.gz.enc
#   -> writes data\restored-<time>.db and checks it. To switch over: deploy\stop.ps1, replace
#      data\stock_primer.db with it (or point STOCK_PRIMER_DATABASE_URL at it), deploy\start.ps1.
```

Postgres (later): use `pg_dump` instead; `ops.backup` refuses non-SQLite URLs.
