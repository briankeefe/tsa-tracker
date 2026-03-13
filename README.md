# TSA Wait Times Tracker

A real-time TSA security checkpoint wait times monitoring system with REST API, Prometheus metrics, and web UI.

## Features

- **Real-time scraping** of TSA wait times every 5 minutes
- **SQLite database** for historical data storage
- **REST API** with current times and historical data endpoints
- **Prometheus metrics** for monitoring integration
- **Web UI** showing current wait times in a clean table
- **Trend analysis** by hour of day and day of week
- **Docker deployment** with docker-compose

## Currently Supported Airports

### ✅ Working (Live Data)
- **EWR (Newark)** - Text parsing from newarkairport.com
- **ATL (Atlanta)** - HTML scraping from atl.com/times/

### 🚧 Available but Needs Implementation  
- **JFK** - Requires headless browser (same Port Authority platform as EWR)
- **LGA (LaGuardia)** - Requires headless browser (Vue.js SPA)

### ❌ No Official Live Data
- **BOS (Boston Logan)** - Only third-party crowdsourced estimates available

## Quick Start

```bash
# Clone and enter directory
cd /home/brian/code/tsa-tracker/

# Deploy with Docker
docker compose up -d

# Verify endpoints
curl http://localhost:5100/health
curl http://localhost:5100/api/current
curl http://localhost:5100/metrics
```

## API Endpoints

- `GET /` - Web UI showing current wait times
- `GET /api/current` - JSON of all current wait times
- `GET /api/history?airport=EWR&terminal=C&days=7` - Historical data
- `GET /api/trends?airport=EWR&terminal=C&lane=general` - Trend analysis
- `GET /metrics` - Prometheus metrics
- `GET /health` - Health check

## Data Model

```sql
CREATE TABLE wait_times (
  id INTEGER PRIMARY KEY,
  airport TEXT,      -- 'EWR', 'ATL', etc.
  terminal TEXT,     -- 'A', 'B', 'C', etc.
  lane TEXT,         -- 'general' or 'precheck'
  wait_minutes INTEGER,
  scraped_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

## Prometheus Metrics

```
tsa_wait_minutes{airport="EWR", terminal="C", lane="general"} 15
tsa_wait_minutes{airport="EWR", terminal="C", lane="precheck"} 7
```

## Airport Research Summary

### EWR (Newark) ✅
- **Source**: newarkairport.com
- **Method**: HTTP + text parsing
- **Format**: "Terminal A General Line 15 min TSA Pre✓ Line 7 min"
- **Terminals**: A, B, C
- **Lanes**: General + PreCheck per terminal

### ATL (Atlanta) ✅
- **Source**: atl.com/times/
- **Method**: HTTP + HTML parsing
- **Format**: Server-rendered HTML with integer wait times
- **Checkpoints**: Main, North, Lower North, South (PreCheck only), International
- **Refresh**: Every 15 seconds
- **Technology**: Xovis physical sensors

### JFK (John F. Kennedy) 🚧
- **Source**: jfkairport.com (same Port Authority platform as EWR)
- **Method**: Requires headless browser (Next.js SPA)
- **API**: avi-prod-mpp-webapp-api.azurewebsites.net (Azure backend)
- **Terminals**: 1, 4, 5, 7, 8
- **Implementation needed**: Playwright/Puppeteer to execute JS and capture API calls

### LGA (LaGuardia) 🚧
- **Source**: laguardiaairport.com/security-wait-times
- **Method**: Requires headless browser (Vue.js SPA)  
- **API**: Same Azure backend as EWR/JFK
- **Terminals**: A, B, C
- **Implementation needed**: Headless browser to intercept API response

### BOS (Boston Logan) ❌
- **Source**: massport.com/logan-airport (no live data)
- **Available alternatives**: 
  - tsawaittimes.com (paid API, crowdsourced)
  - TSA MyTSA API (currently broken due to gov shutdown)
- **Note**: No official airport-published live data exists

## Configuration

Environment variables:
- `DATA_DIR` - Directory for SQLite database (default: current directory)

## Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally
python main.py

# App runs on http://localhost:5100
```

## Future Enhancements

1. **Add JFK/LGA support** - Implement Playwright-based scraping
2. **Add CLEAR lane tracking** - Where available at airports
3. **Mobile app API** - Extended endpoints for mobile clients
4. **Historical analysis** - Deeper statistical analysis and predictions
5. **Alert system** - Notifications for unusually long waits

## Technical Notes

- Uses APScheduler for 5-minute scraping intervals
- SQLite with proper indexing for performance
- Prometheus metrics with custom registry to avoid conflicts
- FastAPI with async/await throughout
- Error handling with graceful fallbacks

## License

MIT License - Feel free to use and modify.