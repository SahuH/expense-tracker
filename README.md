# Household Finance Tracker

Self-hosted, Dockerized finance tracker for a two-person household. Built on:
- **Firefly III** — accounting ledger and source of truth
- **Python FastAPI** — PDF parser using pdfplumber (low-confidence extractions flagged for manual review)
- **Streamlit** — dashboard, upload UI, and AI chat
- **Claude Sonnet 4.6** — natural-language finance assistant
- **Caddy** — automatic HTTPS reverse proxy

## Quick start

See [.github/SETUP.md](.github/SETUP.md) for the complete step-by-step setup guide.

```bash
cp .env.example .env
# Edit .env with your values
docker compose up -d
```

## Services

| Service | Port (internal) | URL |
|---|---|---|
| Firefly III | 8080 | `https://yourdomain.com` |
| Streamlit | 8501 | `https://yourdomain.com/dashboard` |
| Firefly Importer | 8080 | `https://yourdomain.com/import` |
| Parser API | 8000 | Internal only |

## PDF parsing behaviour

The parser uses pdfplumber to extract transactions deterministically. If extraction confidence is low or the balance check fails, the statement is flagged as `needs_review` and the user can correct transactions in an editable table before importing.

## Stopping and restarting

**Shut down** (stops all containers, all your data is preserved):
```bash
docker compose down
```

**Start back up:**
```bash
docker compose up -d
```

Data is stored in named Docker volumes and is never affected by `docker compose down`. Only `docker compose down -v` would delete volumes — avoid that flag unless you intend to wipe everything.

## Backup & Restore

```bash
# Manual backup
./scripts/backup.sh

# Restore
./scripts/restore.sh ~/backups/firefly_20240601.sql.gz
```

## Repository structure

```
expense-tracker-v2/
├── docker-compose.yml
├── .env.example
├── Caddyfile
├── parser/             FastAPI PDF parser (pdfplumber)
├── streamlit/          Streamlit dashboard
│   └── pages/          4 app pages
├── scripts/            Setup, backup, restore
└── .github/SETUP.md    Full manual setup guide
```
