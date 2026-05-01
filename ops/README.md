# Production Operations

## Secrets

Do not commit `.env`. Copy `.env.example` to `.env` on the server and replace
every placeholder with strong values.

Generate secrets:

```sh
openssl rand -hex 32
```

## TLS

The app container serves plain HTTP on `127.0.0.1:7200`. Put Caddy, Nginx, or a
cloud load balancer in front of it and use a real TLS certificate from
Let's Encrypt or your cloud provider.

## Migrations

Alembic is installed in the backend image.

```sh
docker compose exec backend alembic -c alembic.ini upgrade head
```

The SaaS tenant databases are created by the application when an organization
is registered. Keep migration files updated whenever schema SQL changes.

## Backups

PowerShell:

```powershell
.\ops\backup-postgres.ps1 -DbUser $env:POSTGRES_USER
```

Linux shell:

```sh
POSTGRES_USER=fras_user ./ops/backup-postgres.sh
```

Store backups outside the droplet as well, for example S3/Spaces.
