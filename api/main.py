import hashlib
import os
import re
from datetime import datetime, timezone

import aiosqlite
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "laz.db")

app = FastAPI()


async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


@app.on_event("startup")
async def startup():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = await get_db()
    await db.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            slug TEXT NOT NULL,
            ip_hash TEXT NOT NULL,
            direction TEXT NOT NULL CHECK(direction IN ('up', 'down')),
            created_at TEXT NOT NULL,
            PRIMARY KEY (slug, ip_hash)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            email TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        )
    """)
    await db.commit()
    await db.close()


def hash_ip(ip: str) -> str:
    return hashlib.sha256(ip.encode()).hexdigest()


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host


class VoteRequest(BaseModel):
    slug: str
    direction: str  # "up" or "down"


class SubscribeRequest(BaseModel):
    email: str


@app.post("/api/vote")
async def vote(req: VoteRequest, request: Request):
    if req.direction not in ("up", "down"):
        return JSONResponse({"error": "direction must be 'up' or 'down'"}, status_code=400)
    if not re.match(r"^[a-z0-9\-]+$", req.slug):
        return JSONResponse({"error": "invalid slug"}, status_code=400)

    ip = hash_ip(get_client_ip(request))
    now = datetime.now(timezone.utc).isoformat()
    db = await get_db()

    existing = await db.execute(
        "SELECT direction FROM votes WHERE slug = ? AND ip_hash = ?",
        (req.slug, ip),
    )
    row = await existing.fetchone()

    if row:
        if row["direction"] == req.direction:
            # Same vote again = remove vote
            await db.execute(
                "DELETE FROM votes WHERE slug = ? AND ip_hash = ?",
                (req.slug, ip),
            )
        else:
            # Change vote direction
            await db.execute(
                "UPDATE votes SET direction = ?, created_at = ? WHERE slug = ? AND ip_hash = ?",
                (req.direction, now, req.slug, ip),
            )
    else:
        await db.execute(
            "INSERT INTO votes (slug, ip_hash, direction, created_at) VALUES (?, ?, ?, ?)",
            (req.slug, ip, req.direction, now),
        )

    await db.commit()

    # Return updated counts
    counts = await _get_votes(db, req.slug)
    await db.close()
    return counts


async def _get_votes(db, slug: str) -> dict:
    cur = await db.execute(
        "SELECT direction, COUNT(*) as cnt FROM votes WHERE slug = ? GROUP BY direction",
        (slug,),
    )
    rows = await cur.fetchall()
    up = 0
    down = 0
    for r in rows:
        if r["direction"] == "up":
            up = r["cnt"]
        else:
            down = r["cnt"]
    return {"up": up, "down": down, "score": up - down}


@app.get("/api/votes/{slug}")
async def get_votes(slug: str, request: Request):
    if not re.match(r"^[a-z0-9\-]+$", slug):
        return JSONResponse({"error": "invalid slug"}, status_code=400)

    db = await get_db()
    counts = await _get_votes(db, slug)

    # Also return the current user's vote
    ip = hash_ip(get_client_ip(request))
    cur = await db.execute(
        "SELECT direction FROM votes WHERE slug = ? AND ip_hash = ?",
        (slug, ip),
    )
    row = await cur.fetchone()
    counts["user_vote"] = row["direction"] if row else None

    await db.close()
    return counts


@app.post("/api/subscribe")
async def subscribe(req: SubscribeRequest):
    email = req.email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return JSONResponse({"error": "invalid email"}, status_code=400)

    now = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO subscribers (email, created_at) VALUES (?, ?)",
            (email, now),
        )
        await db.commit()
    except aiosqlite.IntegrityError:
        pass  # Already subscribed
    await db.close()
    return {"ok": True}


@app.get("/api/subscribers")
async def list_subscribers(request: Request):
    # Only allow from localhost
    client = get_client_ip(request)
    if client not in ("127.0.0.1", "::1"):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    db = await get_db()
    cur = await db.execute("SELECT email, created_at FROM subscribers ORDER BY created_at DESC")
    rows = await cur.fetchall()
    await db.close()
    return [{"email": r["email"], "created_at": r["created_at"]} for r in rows]
