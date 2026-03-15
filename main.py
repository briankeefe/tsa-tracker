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
import re

import aiosqlite
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
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

# Airports supported by Port Authority API
AIRPORTS = ["EWR", "JFK", "LGA", "BOS", "PHL", "PIT", "CLT", "MIA", "MCO", "DEN", "ORD", "MDW", "LAX", "SFO", "SEA", "DFW", "IAH", "IAD", "DCA", "BWI"]

AIRPORT_NAMES = {
    "EWR": "Newark Liberty",
    "JFK": "JFK International",
    "LGA": "LaGuardia",
    "BOS": "Boston Logan",
    "PHL": "Philadelphia",
    "PIT": "Pittsburgh",
    "CLT": "Charlotte Douglas",
    "MIA": "Miami International",
    "MCO": "Orlando International",
    "DEN": "Denver International",
    "ORD": "O'Hare International",
    "MDW": "Chicago Midway",
    "LAX": "Los Angeles International",
    "SFO": "San Francisco International",
    "SEA": "Seattle-Tacoma",
    "DFW": "Dallas/Fort Worth",
    "IAH": "Houston Bush",
    "IAD": "Washington Dulles",
    "DCA": "Reagan National",
    "BWI": "Baltimore/Washington",
}

AIRPORT_COORDS = {
    "EWR": (40.6895, -74.1745),
    "JFK": (40.6413, -73.7781),
    "LGA": (40.7769, -73.8740),
    "BOS": (42.3656, -71.0096),
    "PHL": (39.8744, -75.2424),
    "PIT": (40.4915, -80.2329),
    "CLT": (35.2140, -80.9431),
    "MIA": (25.7959, -80.2870),
    "MCO": (28.4312, -81.3081),
    "DEN": (39.8561, -104.6737),
    "ORD": (41.9742, -87.9073),
    "MDW": (41.7868, -87.7522),
    "LAX": (33.9416, -118.4085),
    "SFO": (37.6213, -122.3790),
    "SEA": (47.4502, -122.3088),
    "DFW": (32.8998, -97.0403),
    "IAH": (29.9902, -95.3368),
    "IAD": (38.9531, -77.4565),
    "DCA": (38.8521, -77.0377),
    "BWI": (39.1754, -76.6682),
}

scheduler = AsyncIOScheduler()


class LeaveTimeRequest(BaseModel):
    origin: str
    airport_code: str
    flight_time: str


class LeaveTimeResponse(BaseModel):
    leave_by: str
    leave_by_display: str
    drive_minutes: int
    security_minutes: int
    buffer_minutes: int
    arrive_by_display: str
    airport: str


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
    port_authority_tasks = [scrape_port_authority(airport) for airport in AIRPORTS]
    await asyncio.gather(*port_authority_tasks, scrape_atl())
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
            "airport_name": AIRPORT_NAMES.get(row[0], row[0]),
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


async def geocode_address(address: str) -> tuple[float, float]:
    """Geocode address to lat/lon using Nominatim. Returns (lat, lon)."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": address,
                "format": "json",
                "limit": 1
            },
            headers={
                "User-Agent": "TSA-Tracker/1.0 (tsa.briankeefe.dev)"
            }
        )
        response.raise_for_status()
    
    results = response.json()
    if not results:
        raise ValueError(f"Could not find that address: {address}")
    
    # Nominatim returns lat/lon as strings
    return float(results[0]["lat"]), float(results[0]["lon"])


async def get_drive_time_minutes(origin_lat: float, origin_lon: float, 
                                dest_lat: float, dest_lon: float) -> float:
    """Calculate drive time in minutes using OSRM. Returns minutes as float."""
    # OSRM coordinate format: lon,lat (longitude first!)
    coords = f"{origin_lon},{origin_lat};{dest_lon},{dest_lat}"
    
    osrm_servers = [
        "https://router.project-osrm.org/route/v1/driving",
        "https://routing.openstreetmap.de/routed-car/route/v1/driving"
    ]
    
    last_error = None
    for server_url in osrm_servers:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    f"{server_url}/{coords}",
                    params={
                        "overview": "false",
                        "steps": "false"
                    }
                )
                response.raise_for_status()
            
            data = response.json()
            
            # Check OSRM response code
            if data.get("code") != "Ok":
                raise ValueError(f"OSRM routing error: {data.get('message', data['code'])}")
            
            routes = data.get("routes", [])
            if not routes:
                raise ValueError("No route found between locations")
            
            # Duration is in seconds, convert to minutes
            return routes[0]["duration"] / 60.0
            
        except (httpx.HTTPError, ValueError, KeyError) as e:
            last_error = e
            logger.warning(f"OSRM server {server_url} failed: {e}")
            continue
    
    raise ValueError(f"All routing services failed. Last error: {last_error}")


async def get_airport_security_wait(airport_code: str) -> int:
    """Get current average security wait time for airport in minutes."""
    wait_times = await get_current_wait_times()
    airport_waits = [w for w in wait_times if w['airport'] == airport_code]
    
    if not airport_waits:
        # Default fallbacks if no current data
        return 20  # 20 min default for general security
    
    # Calculate average across all terminals and lanes for the airport
    total_wait = sum(w['wait_minutes'] for w in airport_waits if w['wait_minutes'] > 0)
    count = len([w for w in airport_waits if w['wait_minutes'] > 0])
    
    if count == 0:
        return 20  # Default if no valid data
    
    return int(total_wait / count)


def parse_time_to_minutes(time_str: str) -> int:
    """Parse time string like '15:45' to minutes since midnight."""
    if ":" not in time_str:
        raise ValueError("Time must be in HH:MM format")
    
    parts = time_str.split(":")
    if len(parts) != 2:
        raise ValueError("Time must be in HH:MM format")
    
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
    except ValueError:
        raise ValueError("Time must be in HH:MM format")
    
    if hours < 0 or hours > 23 or minutes < 0 or minutes > 59:
        raise ValueError("Invalid time values")
    
    return hours * 60 + minutes


def minutes_to_time_string(minutes: int) -> str:
    """Convert minutes since midnight to HH:MM format."""
    # Handle negative minutes (previous day)
    while minutes < 0:
        minutes += 24 * 60
    
    # Handle overflow (next day)
    while minutes >= 24 * 60:
        minutes -= 24 * 60
    
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours:02d}:{mins:02d}"


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


@app.post("/api/leave-time")
async def api_leave_time(request: LeaveTimeRequest):
    """Calculate when to leave for the airport based on drive time + security wait."""
    try:
        # Validate airport code
        if request.airport_code not in AIRPORT_COORDS:
            raise HTTPException(status_code=400, detail=f"Unsupported airport: {request.airport_code}")
        
        # Parse flight time
        try:
            flight_minutes = parse_time_to_minutes(request.flight_time)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid flight time: {e}")
        
        # Get airport coordinates
        airport_lat, airport_lon = AIRPORT_COORDS[request.airport_code]
        
        # Geocode origin address
        try:
            origin_lat, origin_lon = await geocode_address(request.origin)
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Address lookup timed out")
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=502, detail=f"Address lookup failed: HTTP {e.response.status_code}")
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        
        # Get drive time
        try:
            drive_minutes = await get_drive_time_minutes(origin_lat, origin_lon, airport_lat, airport_lon)
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Drive time calculation timed out")
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=502, detail=f"Drive time service failed: HTTP {e.response.status_code}")
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        
        # Get current security wait time
        try:
            security_minutes = await get_airport_security_wait(request.airport_code)
        except Exception as e:
            logger.warning(f"Could not get current wait times for {request.airport_code}: {e}")
            security_minutes = 20  # Default fallback
        
        buffer_minutes = 30
        recommended_arrive_minutes = 90
        
        arrive_at_airport_minutes = flight_minutes - recommended_arrive_minutes
        leave_minutes = arrive_at_airport_minutes - int(drive_minutes) - security_minutes
        
        leave_time_24h = minutes_to_time_string(leave_minutes)
        arrive_time_24h = minutes_to_time_string(arrive_at_airport_minutes)
        
        def to_12_hour_format(time_24h: str) -> str:
            hours, mins = map(int, time_24h.split(':'))
            if hours == 0:
                return f"12:{mins:02d} AM"
            elif hours < 12:
                return f"{hours}:{mins:02d} AM"
            elif hours == 12:
                return f"12:{mins:02d} PM"
            else:
                return f"{hours-12}:{mins:02d} PM"
        
        leave_by_display = to_12_hour_format(leave_time_24h)
        arrive_by_display = to_12_hour_format(arrive_time_24h)
        
        return LeaveTimeResponse(
            leave_by=leave_time_24h,
            leave_by_display=leave_by_display,
            drive_minutes=int(drive_minutes),
            security_minutes=security_minutes,
            buffer_minutes=buffer_minutes,
            arrive_by_display=arrive_by_display,
            airport=f"{AIRPORT_NAMES.get(request.airport_code, request.airport_code)} ({request.airport_code})"
        )
        
    except HTTPException:
        raise  # Re-raise HTTP exceptions as-is
    except Exception as e:
        logger.error(f"Unexpected error in leave time calculation: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


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