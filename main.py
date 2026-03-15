#!/usr/bin/env python3
"""
TSA Wait Times Tracker

A FastAPI web application that scrapes TSA wait times from airport websites,
stores them in SQLite, and provides REST API + Prometheus metrics endpoints.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
import logging

import aiosqlite
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import Gauge, generate_latest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.getenv("DATA_DIR", "."), "tsa_wait_times.db")

# Prometheus metrics — use default REGISTRY to avoid duplicate timeseries bug
tsa_wait_minutes = Gauge(
    'tsa_wait_minutes',
    'Current TSA wait time in minutes',
    ['airport', 'terminal', 'lane'],
)

PA_API_BASE = "https://avi-prod-mpp-webapp-api.azurewebsites.net/api/v1/SecurityWaitTimesPoints"
PA_HEADERS = {
    "Referer": "https://www.jfkairport.com/",
    "User-Agent": "Mozilla/5.0 (compatible; TSATracker/1.0)",
}

BROWSERLESS_URL = os.getenv("BROWSERLESS_URL", "http://localhost:3100")
BROWSERLESS_TOKEN = os.getenv("BROWSERLESS_TOKEN", "poseidon-scraper-token")

scheduler = AsyncIOScheduler()


async def init_database():
    """Initialize SQLite database with wait_times table."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS wait_times (
                id INTEGER PRIMARY KEY,
                airport TEXT NOT NULL,
                terminal TEXT NOT NULL,
                lane TEXT NOT NULL,
                wait_minutes INTEGER NOT NULL,
                scraped_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_wait_times_airport_terminal_lane 
            ON wait_times(airport, terminal, lane)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_wait_times_scraped_at 
            ON wait_times(scraped_at)
        """)
        await db.commit()


async def store_wait_time(airport: str, terminal: str, lane: str, wait_minutes: int):
    """Store a wait time record in the database."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO wait_times (airport, terminal, lane, wait_minutes) VALUES (?, ?, ?, ?)",
            (airport, terminal, lane, wait_minutes)
        )
        await db.commit()
        
    tsa_wait_minutes.labels(airport=airport, terminal=terminal, lane=lane).set(wait_minutes)
    logger.debug(f"Stored: {airport} {terminal} {lane} = {wait_minutes}min")


async def scrape_port_authority(airport_code: str):
    """Scrape wait times from Port Authority JSON API (EWR/JFK/LGA)."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{PA_API_BASE}/{airport_code}",
                headers=PA_HEADERS,
            )
            response.raise_for_status()

        data = response.json()
        count = 0

        for point in data:
            if not point.get("queueOpen"):
                continue

            terminal = point.get("terminal", "")
            gate = point.get("gate", "All Gates")
            queue_type = point.get("queueType", "")

            if queue_type == "Reg":
                lane = "general"
            elif queue_type == "TSAPre":
                lane = "precheck"
            else:
                continue

            if gate and gate != "All Gates":
                terminal_id = f"{terminal}/{gate}"
            else:
                terminal_id = terminal

            if point.get("isWaitTimeAvailable"):
                wait_minutes = point.get("timeInMinutes", 0)
            else:
                wait_minutes = 0

            await store_wait_time(airport_code, terminal_id, lane, wait_minutes)
            count += 1

        logger.info(f"{airport_code}: stored {count} wait time records")

    except Exception as e:
        logger.error(f"Error scraping {airport_code}: {e}")


async def scrape_atl():
    """Scrape ATL wait times via browserless (needed to bypass Cloudflare)."""
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(
                f"{BROWSERLESS_URL}/content",
                params={"token": BROWSERLESS_TOKEN},
                headers={"Content-Type": "application/json"},
                json={
                    "url": "https://www.atl.com/times/",
                    "gotoOptions": {
                        "waitUntil": "networkidle2",
                        "timeout": 25000,
                    },
                    "bestAttempt": True,
                },
            )
            response.raise_for_status()

        html = response.text

        if "Just a moment" in html[:1000] or "challenge-platform" in html[:2000]:
            logger.warning("ATL: Cloudflare challenge not bypassed, skipping")
            return

        soup = BeautifulSoup(html, "html.parser")
        count = 0

        for section_selector, section_name in [
            ("div.col-lg-4.nesclasser2", "Domestic"),
            ("div.col-lg-5.nesclasser1", "International"),
        ]:
            section = soup.select_one(section_selector)
            if not section:
                continue

            for row in section.select("div.row"):
                h2 = row.select_one("h2")
                h3 = row.select_one("h3")
                span = row.select_one("div.declasser3 button span")

                if not h2 or not span:
                    continue

                if h3 and "CLOSED" in h3.get_text():
                    continue

                checkpoint = h2.get_text(strip=True)
                wait_val = span.get_text(strip=True)

                if wait_val == "X" or not wait_val.isdigit():
                    continue

                terminal = f"{section_name}/{checkpoint}"
                await store_wait_time("ATL", terminal, "general", int(wait_val))
                count += 1

        logger.info(f"ATL: stored {count} wait time records")

    except httpx.ConnectError:
        logger.warning("ATL: browserless not reachable, skipping")
    except Exception as e:
        logger.error(f"Error scraping ATL: {e}")


async def run_scraper():
    logger.info("Starting scraper run")
    await asyncio.gather(
        scrape_port_authority("EWR"),
        scrape_port_authority("JFK"),
        scrape_port_authority("LGA"),
        scrape_atl(),
    )
    logger.info("Scraper run completed")


async def get_current_wait_times() -> List[Dict[str, Any]]:
    """Get the most recent wait times for all airport/terminal/lane combinations."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT airport, terminal, lane, wait_minutes, scraped_at
            FROM wait_times w1
            WHERE scraped_at = (
                SELECT MAX(scraped_at) 
                FROM wait_times w2 
                WHERE w1.airport = w2.airport 
                AND w1.terminal = w2.terminal 
                AND w1.lane = w2.lane
            )
            ORDER BY airport, terminal, lane
        """)
        rows = await cursor.fetchall()
        
    return [
        {
            "airport": row[0],
            "terminal": row[1], 
            "lane": row[2],
            "wait_minutes": row[3],
            "scraped_at": row[4]
        }
        for row in rows
    ]


async def get_historical_data(airport: str, terminal: Optional[str] = None, days: int = 7) -> List[Dict[str, Any]]:
    """Get historical wait time data."""
    since_date = datetime.now() - timedelta(days=days)
    
    query = """
        SELECT airport, terminal, lane, wait_minutes, scraped_at
        FROM wait_times
        WHERE scraped_at >= ? AND airport = ?
    """
    params = [since_date.isoformat(), airport]
    
    if terminal:
        query += " AND terminal = ?"
        params.append(terminal)
        
    query += " ORDER BY scraped_at DESC"
    
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        
    return [
        {
            "airport": row[0],
            "terminal": row[1],
            "lane": row[2], 
            "wait_minutes": row[3],
            "scraped_at": row[4]
        }
        for row in rows
    ]


async def get_trend_data(airport: str, terminal: str, lane: str) -> Dict[str, Any]:
    """Get trend data (average wait by hour of day and day of week)."""
    async with aiosqlite.connect(DB_PATH) as db:
        hour_cursor = await db.execute("""
            SELECT 
                CAST(strftime('%H', scraped_at) AS INTEGER) as hour,
                AVG(wait_minutes) as avg_wait
            FROM wait_times 
            WHERE airport = ? AND terminal = ? AND lane = ?
            AND scraped_at >= datetime('now', '-30 days')
            GROUP BY hour
            ORDER BY hour
        """, (airport, terminal, lane))
        hour_data = await hour_cursor.fetchall()
        
        dow_cursor = await db.execute("""
            SELECT 
                CAST(strftime('%w', scraped_at) AS INTEGER) as day_of_week,
                AVG(wait_minutes) as avg_wait
            FROM wait_times 
            WHERE airport = ? AND terminal = ? AND lane = ?
            AND scraped_at >= datetime('now', '-30 days')
            GROUP BY day_of_week
            ORDER BY day_of_week
        """, (airport, terminal, lane))
        dow_data = await dow_cursor.fetchall()
        
    return {
        "by_hour": {hour: round(avg_wait, 1) for hour, avg_wait in hour_data},
        "by_day_of_week": {dow: round(avg_wait, 1) for dow, avg_wait in dow_data}
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    await init_database()
    
    scheduler.add_job(run_scraper, "interval", minutes=5, id="scraper")
    scheduler.start()
    logger.info("Scheduler started - scraping every 5 minutes")
    
    await run_scraper()
    
    yield
    
    scheduler.shutdown()
    logger.info("Application shutdown")


app = FastAPI(
    title="TSA Wait Times Tracker",
    description="Real-time TSA security wait times with historical data and trends",
    version="1.0.0",
    lifespan=lifespan
)

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def home():
    with open("static/index.html", "r") as f:
        return HTMLResponse(content=f.read())


@app.get("/api/current")
async def api_current():
    """REST API endpoint returning current wait times as JSON."""
    return await get_current_wait_times()


@app.get("/api/history")
async def api_history(
    airport: str,
    terminal: Optional[str] = Query(None),
    days: int = Query(7, ge=1, le=30)
):
    """REST API endpoint returning historical wait time data."""
    return await get_historical_data(airport, terminal, days)


@app.get("/api/trends")
async def api_trends(airport: str, terminal: str, lane: str):
    """REST API endpoint returning trend analysis data."""
    return await get_trend_data(airport, terminal, lane)


@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics():
    return generate_latest()


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5100)