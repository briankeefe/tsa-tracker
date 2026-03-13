# TSA Wait Times Tracker

Real-time TSA security checkpoint wait times with REST API, Prometheus metrics, and web UI.

## Supported Airports

| Airport | Method | Status |
|---------|--------|--------|
| **EWR** (Newark) | Port Authority JSON API | ✅ Live |
| **JFK** (John F. Kennedy) | Port Authority JSON API | ✅ Live |
| **LGA** (LaGuardia) | Port Authority JSON API | ✅ Live |
| **ATL** (Atlanta) | Browserless + HTML scraping | ⚠️ Cloudflare-blocked |
| **BOS** (Boston Logan) | — | ❌ No official data source |

## Quick Start

```bash
docker compose up -d

curl http://localhost:5100/health
curl http://localhost:5100/api/current
curl http://localhost:5100/metrics
```

## API Endpoints

- `GET /` — Web UI with current wait times
- `GET /api/current` — JSON of all current wait times
- `GET /api/history?airport=EWR&terminal=C&days=7` — Historical data
- `GET /api/trends?airport=EWR&terminal=C&lane=general` — Trend analysis
- `GET /metrics` — Prometheus metrics
- `GET /health` — Health check

## Architecture

EWR, JFK, and LGA use the Port Authority of NY/NJ's internal JSON API directly — no browser rendering needed. The API endpoint is `avi-prod-mpp-webapp-api.azurewebsites.net/api/v1/SecurityWaitTimesPoints/{airport}` and requires only a `Referer` header.

ATL (`atl.com/times/`) is a server-rendered WordPress page behind Cloudflare. The scraper attempts to use browserless (headless Chrome) at `localhost:3100` to bypass the challenge, but Cloudflare currently blocks it. The scraper handles this gracefully and logs a warning.

All scrapers run in parallel every 5 minutes via APScheduler.

## Data Model

```sql
CREATE TABLE wait_times (
  id INTEGER PRIMARY KEY,
  airport TEXT,       -- 'EWR', 'JFK', 'LGA', 'ATL'
  terminal TEXT,      -- 'A', 'B/40-49', '1', 'Domestic/NORTH'
  lane TEXT,          -- 'general' or 'precheck'
  wait_minutes INTEGER,
  scraped_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

Terminal values include gate ranges for airports with multiple checkpoints per terminal (e.g., EWR Terminal B has `B/40-49`, `B/51-57`, `B/60-68`).

## Prometheus Metrics

```
tsa_wait_minutes{airport="EWR", terminal="C", lane="general"} 15
tsa_wait_minutes{airport="JFK", terminal="1", lane="precheck"} 7
```

Uses the default Prometheus registry — includes standard process/Python metrics alongside TSA data.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `DATA_DIR` | `.` | SQLite database directory |
| `BROWSERLESS_URL` | `http://localhost:3100` | Browserless instance for ATL |
| `BROWSERLESS_TOKEN` | `poseidon-scraper-token` | Browserless auth token |

## Development

```bash
pip install -r requirements.txt
python main.py
# Runs on http://localhost:5100
```
