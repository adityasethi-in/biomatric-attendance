# BIOMATRIC server deployment

This app should be served through HTTPS. Mobile browser camera access works on
`localhost` for testing, but on a real phone it needs a secure origin such as
`https://attendance.yourdomain.com`.

## Recommended domain layout

```text
attendance.yourdomain.com  -> BIOMATRIC / FacePass
dms.yourdomain.com         -> DMS Cloud
site1.yourdomain.com       -> optional static website
site2.yourdomain.com       -> optional static website
```

Only expose these public ports on the server:

```text
22   SSH
80   HTTP, used by Caddy/Let's Encrypt
443  HTTPS
```

Do not publicly expose PostgreSQL, backend port `7000`, or admin port `7200`.

## 1. Point DNS to the server

At your domain provider, create `A` records:

```text
attendance  -> SERVER_PUBLIC_IP
dms         -> SERVER_PUBLIC_IP
site1       -> SERVER_PUBLIC_IP   optional
site2       -> SERVER_PUBLIC_IP   optional
```

Wait until DNS resolves:

```bash
nslookup attendance.yourdomain.com
nslookup dms.yourdomain.com
```

## 2. Prepare Ubuntu server

SSH into the server:

```bash
ssh root@SERVER_PUBLIC_IP
```

Update packages:

```bash
apt update && apt upgrade -y
apt install -y ca-certificates curl git ufw nano
```

Install Docker:

```bash
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
docker version
docker compose version
```

Create the shared network used by DMS and BIOMATRIC:

```bash
docker network create school-net || true
```

Enable firewall:

```bash
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
ufw status
```

## 3. Install Caddy for HTTPS

```bash
apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | tee /etc/apt/sources.list.d/caddy-stable.list
apt update
apt install -y caddy
systemctl enable --now caddy
```

Copy the template from this repo:

```bash
cp /opt/BIOMATRIC/deploy/Caddyfile.example /etc/caddy/Caddyfile
nano /etc/caddy/Caddyfile
```

Replace:

```text
attendance.yourdomain.com
dms.yourdomain.com
site1.yourdomain.com
site2.yourdomain.com
```

Then reload Caddy:

```bash
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
systemctl status caddy --no-pager
```

## 4. Upload or clone projects

Use one folder for all apps:

```bash
mkdir -p /opt/apps
cd /opt/apps
```

Clone or upload your DMS project:

```bash
git clone YOUR_DMS_REPO_URL dms-cloud
```

Clone or upload this BIOMATRIC project:

```bash
git clone YOUR_BIOMATRIC_REPO_URL biomatric
```

If you are copying from laptop instead of GitHub:

```bash
scp -r "C:\Users\adity\BIOMATRIC" root@SERVER_PUBLIC_IP:/opt/apps/biomatric
```

## 5. Start DMS first

BIOMATRIC currently expects DMS database/backend on the shared Docker network.
DMS should expose containers similar to:

```text
dms-db
dms-backend
```

Start DMS:

```bash
cd /opt/apps/dms-cloud
docker compose up -d --build
```

Check container names and networks:

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Networks}}'
```

If DMS containers are not on `school-net`, add `school-net` to the DMS compose
file or connect them manually:

```bash
docker network connect school-net dms-db || true
docker network connect school-net dms-backend || true
```

## 6. Configure BIOMATRIC

```bash
cd /opt/apps/biomatric
cp .env.example .env
nano .env
```

Set these values:

```env
POSTGRES_USER=your_dms_db_user
POSTGRES_PASSWORD=your_dms_db_password
POSTGRES_DB=delight_school
DB_HOST=dms-db
BIOMATRIC_DB_SCHEMA=biomatric

ADMIN_TOKEN_SECRET=paste-output-of-openssl-rand-hex-32
DEFAULT_ADMIN_USERNAME=admin
DEFAULT_ADMIN_PASSWORD=choose-a-strong-password-or-leave-empty

ALLOWED_ORIGINS=https://attendance.yourdomain.com
LIVENESS_MODE=basic

DMS_BASE_URL=http://dms-backend:8000/api/v1
DMS_WEBHOOK_SECRET=use-the-same-secret-as-dms-biomatric-webhook-secret
```

Generate secret:

```bash
openssl rand -hex 32
```

For the low-RAM hybrid deployment, first keep:

```env
FACE_ENGINE_MODE=server
```

After the updated browser UI has re-registered profiles with client-side face
vectors, switch to:

```env
FACE_ENGINE_MODE=client
CLIENT_FACE_MATCH_THRESHOLD=0.58
CLIENT_FACE_DUPLICATE_THRESHOLD=0.58
```

In `client` mode the backend does not load the heavy Python face-recognition
model. The mobile/browser creates the 128-d face vector and the server only
does vector matching plus attendance storage. Keep `server` mode while old
profiles only have the legacy 512-d server embeddings.

## 7. Start BIOMATRIC

```bash
cd /opt/apps/biomatric
docker compose up -d --build
docker compose ps
docker compose logs --tail=120 backend
```

Health check from server:

```bash
curl http://127.0.0.1:7000/health
curl https://attendance.yourdomain.com/api/health
```

Open in browser:

```text
https://attendance.yourdomain.com
```

## 8. Camera test

On a mobile phone:

1. Open `https://attendance.yourdomain.com`.
2. Login with scanner/admin credentials.
3. Allow camera permission.
4. The front camera preview should open.
5. Attendance should auto-scan after login/start camera.

If camera does not open, check:

```text
The URL must be HTTPS.
Browser camera permission must be allowed.
Phone and browser should not be in private/restricted mode.
```

## 9. Useful maintenance commands

Restart BIOMATRIC:

```bash
cd /opt/apps/biomatric
docker compose restart
```

Update BIOMATRIC:

```bash
cd /opt/apps/biomatric
git pull
docker compose up -d --build
```

View logs:

```bash
docker compose logs -f backend
docker compose logs -f admin
```

Check RAM/CPU:

```bash
docker stats
free -h
df -h
```

Manual DB backup:

```bash
cd /opt/apps/biomatric
bash ops/backup-postgres.sh
```

## 10. Common deployment blockers

If BIOMATRIC says `network school-net declared as external, but could not be found`:

```bash
docker network create school-net
docker compose up -d
```

If BIOMATRIC cannot connect to database:

```bash
docker ps --format 'table {{.Names}}\t{{.Networks}}'
docker network inspect school-net
```

Make sure `dms-db` is reachable from BIOMATRIC, or set `DB_HOST` in `.env` to
the actual DMS database container name.

If mobile camera does not work:

```text
Use https://attendance.yourdomain.com, not http://SERVER_IP:7200.
```
