# TAKE coffee&more — Telegram Loyalty System (MVP)

An async FastAPI + Telegram WebApp loyalty platform: customers hold a
QR-code loyalty card inside your bot, baristas scan it to earn (3%
cashback) or redeem bonuses, and every transaction fires a Telegram push
notification and lands in a multi-branch ledger.

```
take-loyalty/
├── .env.example          # copy to .env and fill in
├── requirements.txt
├── alembic.ini            # DB migrations config
├── migrations/             # Alembic migration environment + versions
└── app/
    ├── config.py           # env-driven settings (pydantic-settings)
    ├── database.py         # async engine, session, init_db()
    ├── models.py            # User, Branch, Transaction (SQLAlchemy 2.0)
    ├── schemas.py            # Pydantic request/response models
    ├── auth.py                # Telegram initData HMAC + QR signing + barista PIN
    ├── main.py                 # FastAPI app, all routes, business logic
    ├── services/
    │   └── bot.py               # Telegram Bot API sendMessage notifications
    ├── templates/
    │   ├── client.html           # Customer WebApp (/app)
    │   └── barista.html           # Barista scanner WebApp (/barista)
    └── static/                    # local static assets (favicon, etc.)
```

---

## 1. Prerequisites

- Python 3.11+
- A PostgreSQL database (local, or a free instance on
  [Supabase](https://supabase.com) / [Neon](https://neon.tech))
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- (For real device testing) an HTTPS URL — Telegram WebApps refuse to open
  plain `http://` URLs. Use `ngrok`, `cloudflared`, or deploy to a host
  with TLS (Render, Fly.io, Railway, etc.)

## 2. Local setup

```bash
# 1. Clone / unzip the project, then create a virtual environment
cd take-loyalty
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env:
#   BOT_TOKEN       -> token from @BotFather
#   DATABASE_URL    -> postgresql+asyncpg://user:pass@host:5432/dbname
#   QR_SECRET_KEY   -> generate with:
#                      python -c "import secrets; print(secrets.token_hex(32))"
#   BARISTA_PIN     -> change from the default "1234" before going live
#   BASE_URL        -> your public https URL (ngrok URL while developing)
#   DEV_MODE        -> true ONLY for local testing without a real Telegram
#                      client (skips initData signature verification)
```

## 3. Database: tables & migrations

For the pilot, the fastest path is letting the app create tables itself on
startup (`init_db()` in `app/database.py` calls `create_all` and seeds
`Branch(id=1, name="TAKE #1")` automatically) — nothing extra to run.

For the 6-branch rollout, switch to Alembic-managed migrations so schema
changes are tracked and reversible:

```bash
# Generate the initial migration from the current models
alembic revision --autogenerate -m "initial schema"

# Apply it
alembic upgrade head

# Later, after changing models.py:
alembic revision --autogenerate -m "describe your change"
alembic upgrade head
```

`migrations/env.py` is already wired to read `DATABASE_URL` from your `.env`
via `app.config.settings` and to target `app.models.Base.metadata` — no
manual configuration needed.

To add branches 2–6 once the pilot is validated:

```sql
INSERT INTO branches (name, address, is_active) VALUES
  ('TAKE #2', 'Branch 2 address', true),
  ('TAKE #3', 'Branch 3 address', true),
  ('TAKE #4', 'Branch 4 address', true),
  ('TAKE #5', 'Branch 5 address', true),
  ('TAKE #6', 'Branch 6 address', true);
```
(Baristas at each location select their branch via the `branch_id` sent
with each transaction — wire this to a branch picker in `barista.html` if
staff move between locations, or hardcode per-device via a query param.)

## 4. Running the server

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

- `GET  /health` → `{"status": "ok"}` — confirms the app and DB are up.
- `GET  /app` → customer WebApp.
- `GET  /barista` → barista scanner WebApp.
- Interactive API docs: `http://localhost:8000/docs`

### Testing without a real Telegram client
Set `DEV_MODE=true` in `.env`. The `/api/auth` endpoint will accept any
`init_data` string and log you in as a fixed debug user (`telegram_id=1`).
**Never** set `DEV_MODE=true` in production — it disables signature
verification entirely.

## 5. Exposing the app over HTTPS (required by Telegram)

Telegram WebApps only launch from `https://` URLs. While developing:

```bash
ngrok http 8000
```

Copy the `https://xxxx.ngrok-free.app` URL into:
- `.env` → `BASE_URL`
- Your bot's WebApp button (see step 6)

## 6. Wiring up the Telegram bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` (or reuse an
   existing bot) → copy the token into `.env` as `BOT_TOKEN`.
2. Set the bot's menu button to open the customer WebApp directly. You can
   do this once via `setChatMenuButton` — the project already includes a
   ready-to-call helper:

   ```bash
   python -c "
   import asyncio
   from app.services.bot import set_webapp_menu_button
   asyncio.run(set_webapp_menu_button())
   "
   ```

   This points the button at `{BASE_URL}/app`.
3. For the barista device(s), just open `{BASE_URL}/barista` directly in
   the device's browser or as a second WebApp button / bot command — it
   doesn't need to run inside a customer chat.
4. Bonus notifications are sent automatically after every EARN/REDEEM via
   `services/bot.py` → `sendMessage`. No extra setup required beyond a
   valid `BOT_TOKEN`; customers must have started a chat with the bot at
   least once (standard Telegram requirement for bots to message a user).

## 7. Pilot → 6-branch rollout checklist

- [ ] Change `BARISTA_PIN` from the default per branch, or extend
      `auth.py`/`schemas.py` to support a PIN-per-branch map if each
      location needs its own code.
- [ ] Insert the remaining 5 branches (see SQL above) and confirm each
      barista device passes the correct `branch_id` on `/api/barista/earn`
      and `/api/barista/redeem`.
- [ ] Switch schema management to Alembic (`alembic upgrade head`) instead
      of relying on `create_all`.
- [ ] Point `DATABASE_URL` at your production Postgres (Supabase/Neon
      connection strings work as-is with the `postgresql+asyncpg://`
      prefix — Supabase's "Session pooler" string needs the prefix swapped
      from `postgresql://` to `postgresql+asyncpg://`).
- [ ] Deploy behind HTTPS (Render/Fly.io/Railway/your own TLS-terminated
      reverse proxy) and update `BASE_URL` + the bot's menu button.
- [ ] Set `DEV_MODE=false`.
- [ ] Rotate `QR_SECRET_KEY` to a freshly generated value distinct from
      any value used during development.

## 8. Quick manual smoke test (no phone needed)

With `DEV_MODE=true` and the server running:

```bash
# 1. Simulate the customer handshake
curl -s -X POST http://localhost:8000/api/auth \
  -H "Content-Type: application/json" \
  -d '{"init_data":"anything"}'
# -> returns { user, branch, qr_payload }

# 2. Barista logs in with the PIN
curl -s -X POST http://localhost:8000/api/barista/login \
  -H "Content-Type: application/json" -d '{"pin":"1234"}'
# -> returns { ok: true, session_token }

# 3. Barista "scans" the qr_payload from step 1
curl -s -X POST http://localhost:8000/api/barista/verify-qr \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <session_token>" \
  -d '{"qr_payload":"<qr_payload from step 1>"}'

# 4. Record an earn transaction
curl -s -X POST http://localhost:8000/api/barista/earn \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <session_token>" \
  -d '{"telegram_id": 1, "bill_amount": 5000}'
# -> bonus_change: 150 (3% of 5000, floored)
```

This exact flow (auth → PIN login → QR verify → earn → redeem →
over-redeem rejection → history) was run against a live Postgres instance
while building this project and produced correct results at every step.
