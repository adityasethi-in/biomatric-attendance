# BIOMATRIC — Server deployment & DMS integration

## 1. First-time setup

```bash
cd /opt/biomatric
cp .env.example .env

# Generate two strong secrets:
openssl rand -hex 32          # paste as ADMIN_TOKEN_SECRET
openssl rand -hex 24          # paste as POSTGRES_PASSWORD (or your own)

# Edit .env: set POSTGRES_USER, POSTGRES_PASSWORD, ADMIN_TOKEN_SECRET,
# ALLOWED_ORIGINS (the public URL of your admin UI), and optionally
# DEFAULT_ADMIN_PASSWORD if you want a pre-seeded admin row.

docker compose up -d --build
```

The backend stays on `127.0.0.1:7000`; the admin UI is on `127.0.0.1:7200`.
Front them with nginx/Caddy/Cloudflare Tunnel for TLS.

## 2. Linking with Delight Model School (DMS)

The integration is HMAC-signed and idempotent — every successful face-scan in
BIOMATRIC mirrors an attendance row to the DMS database.

### On the DMS host (`DELIGHT MODEL SCHOOL - CLAUDE`)

Add one variable to the DMS `.env`:

```bash
BIOMATRIC_WEBHOOK_SECRET=<paste a 32+ char random string>
```

Re-deploy DMS so the new variable is picked up:

```bash
docker compose up -d --build backend
```

The DMS backend now exposes three signed endpoints under `/api/v1`:

- `GET  /integrations/biomatric/health`
- `GET  /integrations/biomatric/roster`
- `POST /integrations/biomatric/attendance`

A new `teacher_attendance` table is created automatically; the existing
`attendance` table gains `source` and `source_ref` columns (idempotent).

### On the BIOMATRIC host

Two ways to register the link:

**Option A — pre-seed in `.env`** (auto-applies to the default Delight Model
School organization on first boot):

```
DMS_BASE_URL=https://dms.example.com/api/v1
DMS_WEBHOOK_SECRET=<same value as BIOMATRIC_WEBHOOK_SECRET in DMS>
```

**Option B — link from the admin UI** (per organization):

1. Log in as the org admin in the BIOMATRIC admin panel.
2. Open the **Delight Model School link** card.
3. Paste the DMS API base URL (e.g. `https://dms.example.com/api/v1`) and
   the shared secret. BIOMATRIC will round-trip a signed request to verify
   the link before saving.

## 3. Mapping people

Once the link is verified the admin enrollment form gains a **Link to DMS
person** dropdown sourced from DMS's roster (students + teachers). Pick the
matching DMS person while enrolling face samples — already-linked entries
are greyed out so each DMS person is mapped to exactly one face profile.

Profiles without a DMS link still capture attendance locally; only
DMS-linked profiles trigger the webhook.

## 4. Webhook delivery & monitoring

- Every successful `/attendance/mark` for a linked profile is queued in
  the `dms_outbox` table.
- A background worker drains the queue every 5 seconds, signing each call
  with HMAC-SHA256 over `METHOD\nPATH\nTIMESTAMP\nBODY`.
- Failures retry with exponential backoff (5s → 1h cap).
- The admin summary card shows live counters: **DMS Linked / DMS Pending /
  DMS Failing**.
- `GET /api/dms/outbox` (admin-auth) returns the last 100 events with
  status, last error, and next attempt time.

## 5. Security checklist for going live

- [x] `ADMIN_TOKEN_SECRET` set to a unique 64-hex-char string (boot fails
      otherwise).
- [x] `POSTGRES_PASSWORD` set to a unique strong password.
- [x] DB port is **not** published on the host — only inside the docker
      network.
- [x] CORS allowlist is set to your real admin UI origin(s) only.
- [x] Login endpoint rate-limited to 10/min per IP, attendance to 60/min.
- [x] Passwords stored with bcrypt (legacy SHA-256 hashes auto-upgrade on
      first successful login).
- [x] HTTPS is provided by your reverse proxy / load balancer.
- [x] `LIVENESS_MODE=basic` (or `strict`) blocks printed-photo spoofs.

## 6. Endpoints added in v1.1

| Method | Path                  | Auth                | Purpose                             |
| ------ | --------------------- | ------------------- | ----------------------------------- |
| POST   | `/dms/configure`      | admin token         | Save & verify DMS link              |
| POST   | `/dms/disconnect`     | admin token         | Remove DMS link                     |
| GET    | `/dms/status`         | admin token         | Live link health                    |
| GET    | `/dms/roster`         | admin token         | List DMS students/teachers          |
| GET    | `/dms/outbox`         | admin token         | Webhook delivery log                |

DMS-side (added under `/api/v1` prefix):

| Method | Path                                  | Auth                | Purpose                       |
| ------ | ------------------------------------- | ------------------- | ----------------------------- |
| GET    | `/integrations/biomatric/health`      | HMAC sig            | Round-trip verification       |
| GET    | `/integrations/biomatric/roster`      | HMAC sig            | Students + teachers list      |
| POST   | `/integrations/biomatric/attendance`  | HMAC sig            | Idempotent attendance ingest  |
