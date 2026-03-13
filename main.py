#!/usr/bin/env python3
"""
TSA Wait Times Tracker

A FastAPI web application that scrapes TSA wait times from airport websites,
stores them in SQLite, and provides REST API + Prometheus metrics endpoints.
"""

import asyncio
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
import logging

import aiosqlite
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from beautifulsoup4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from prometheus_client import Gauge, generate_latest, CONTENT_TYPE_LATEST

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.getenv("DATA_DIR", "."), "tsa_wait_times.db")

tsa_wait_minutes = Gauge(
    'tsa_wait_minutes', 
    'Current TSA wait time in minutes',
    ['airport', 'terminal', 'lane']
)

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
    logger.info(f"Stored: {airport} {terminal} {lane} = {wait_minutes}min")


async def scrape_ewr():
    """Scrape EWR (Newark) wait times from newarkairport.com."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get("https://www.newarkairport.com/")
            response.raise_for_status()
            
        text = re.sub(r"\s+", " ", response.text)
        
        # Parse terminal wait times using regex patterns
        # Example: "Terminal A Terminal A Gates All Gates General Line 15 min TSA Pre✓ Line 7 min"
        terminals = ["A", "B", "C"]
        
        for terminal in terminals:
            terminal_pattern = f"Terminal {terminal}.*?(?=Terminal [ABC]|$)"
            terminal_match = re.search(terminal_pattern, text, re.IGNORECASE | re.DOTALL)
            
            if terminal_match:
                terminal_text = terminal_match.group(0)
                
                general_match = re.search(r"General Line (\d+) min", terminal_text, re.IGNORECASE)
                if general_match:
                    general_wait = int(general_match.group(1))
                    await store_wait_time("EWR", terminal, "general", general_wait)
                
                precheck_match = re.search(r"TSA Pre[✓✔] Line (\d+) min", terminal_text, re.IGNORECASE)
                if precheck_match:
                    precheck_wait = int(precheck_match.group(1))
                    await store_wait_time("EWR", terminal, "precheck", precheck_wait)
                    
    except Exception as e:
        logger.error(f"Error scraping EWR: {e}")


async def scrape_other_airports():
    """Check and scrape other airports if they have live wait time pages."""
    # TODO: Research and implement scrapers for JFK, LGA, BOS, ATL
    # For now, this is a placeholder
    airports_to_check = [
        ("JFK", "https://www.jfkairport.com/"),
        ("LGA", "https://www.laguardiaairport.com/"), 
        ("BOS", "https://www.massport.com/logan-airport/"),
        ("ATL", "https://www.atl.com/")
    ]
    
    for airport_code, url in airports_to_check:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                
                text = response.text.lower()
                if any(keyword in text for keyword in ["wait time", "security wait", "tsa", "checkpoint"]):
                    logger.info(f"{airport_code}: Found potential wait time data at {url}")
                    # TODO: Implement specific parsing logic for each airport
                else:
                    logger.info(f"{airport_code}: No wait time data found at {url}")
                    
        except Exception as e:
            logger.warning(f"Error checking {airport_code}: {e}")


async def run_scraper():
    """Run all scrapers."""
    logger.info("Starting scraper run")
    await scrape_ewr()
    await scrape_other_airports()
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

templates = Jinja2Templates(directory=".")


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Home page showing current wait times in a table."""
    current_times = await get_current_wait_times()
    
    grouped_data = {}
    for item in current_times:
        airport = item["airport"]
        terminal = item["terminal"]
        key = f"{airport}-{terminal}"
        
        if key not in grouped_data:
            grouped_data[key] = {
                "airport": airport,
                "terminal": terminal,
                "general": None,
                "precheck": None,
                "last_updated": None
            }
            
        if item["lane"] == "general":
            grouped_data[key]["general"] = item["wait_minutes"]
        elif item["lane"] == "precheck":
            grouped_data[key]["precheck"] = item["wait_minutes"]
            
        grouped_data[key]["last_updated"] = item["scraped_at"]
    
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>TSA Wait Times Tracker</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { 
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; 
                max-width: 1200px; 
                margin: 0 auto; 
                padding: 20px;
                background: #f5f5f5;
            }
            .container {
                background: white;
                padding: 30px;
                border-radius: 8px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            }
            h1 { 
                color: #2c3e50; 
                text-align: center;
                margin-bottom: 30px;
            }
            table { 
                width: 100%; 
                border-collapse: collapse; 
                margin: 20px 0;
            }
            th, td { 
                border: 1px solid #ddd; 
                padding: 12px; 
                text-align: left; 
            }
            th { 
                background: #3498db; 
                color: white;
                font-weight: 600;
            }
            tr:nth-child(even) { 
                background: #f8f9fa; 
            }
            .wait-time {
                font-weight: bold;
                padding: 6px 12px;
                border-radius: 4px;
                display: inline-block;
                min-width: 60px;
                text-align: center;
            }
            .wait-low { background: #d4edda; color: #155724; }
            .wait-medium { background: #fff3cd; color: #856404; }
            .wait-high { background: #f8d7da; color: #721c24; }
            .last-updated { 
                font-size: 0.9em; 
                color: #6c757d; 
            }
            .no-data { 
                color: #6c757d; 
                font-style: italic; 
            }
            .refresh-note {
                text-align: center;
                margin-top: 20px;
                font-size: 0.9em;
                color: #6c757d;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🛂 TSA Wait Times Tracker</h1>
            
            <table>
                <thead>
                    <tr>
                        <th>Airport</th>
                        <th>Terminal</th>
                        <th>General Security</th>
                        <th>TSA PreCheck</th>
                        <th>Last Updated</th>
                    </tr>
                </thead>
                <tbody>
    """
    
    if not grouped_data:
        html_content += """
                    <tr>
                        <td colspan="5" class="no-data">No wait time data available yet. Scraping in progress...</td>
                    </tr>
        """
    else:
        for data in sorted(grouped_data.values(), key=lambda x: (x["airport"], x["terminal"])):
            def format_wait_time(minutes):
                if minutes is None:
                    return '<span class="no-data">--</span>'
                
                css_class = "wait-low"
                if minutes >= 15:
                    css_class = "wait-medium" 
                if minutes >= 30:
                    css_class = "wait-high"
                    
                return f'<span class="wait-time {css_class}">{minutes} min</span>'
            
            last_updated = data["last_updated"]
            if last_updated:
                dt = datetime.fromisoformat(last_updated.replace('Z', '+00:00'))
                formatted_time = dt.strftime("%H:%M")
            else:
                formatted_time = "--"
                
            html_content += f"""
                    <tr>
                        <td><strong>{data["airport"]}</strong></td>
                        <td>{data["terminal"]}</td>
                        <td>{format_wait_time(data["general"])}</td>
                        <td>{format_wait_time(data["precheck"])}</td>
                        <td class="last-updated">{formatted_time}</td>
                    </tr>
            """
    
    html_content += """
                </tbody>
            </table>
            
            <div class="refresh-note">
                Data refreshes automatically every 5 minutes. 
                <a href="/api/current">View JSON data</a> | 
                <a href="/metrics">Prometheus metrics</a>
            </div>
        </div>
    </body>
    </html>
    """
    
    return HTMLResponse(content=html_content)


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
    """Prometheus metrics endpoint."""
    return generate_latest()


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=5100, reload=True)