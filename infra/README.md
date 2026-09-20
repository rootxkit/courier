# infra

Local development stack, database migrations, and deployment configuration.

- `docker-compose.dev.yml` — PostGIS, TimescaleDB, Redis and NATS for local work
- `.env.example` — every variable the services read, with safe local defaults
- `initdb/` — one-shot SQL run on first start of each database volume

Kubernetes manifests arrive later; Compose is the development target.
