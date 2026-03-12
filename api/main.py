import hashlib
import hmac
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import resend
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "laz.db")
BASE_URL = os.environ.get("BASE_URL", "https://laz.dev")
UNSUBSCRIBE_SECRET = os.environ.get("UNSUBSCRIBE_SECRET", "dev-secret-change-me")
resend.api_key = os.environ.get("RESEND_API_KEY", "")

app = FastAPI()

# ---------------------------------------------------------------------------
# Rate limiting — simple in-memory, per-IP timestamps
# ---------------------------------------------------------------------------
_rate_limits: dict[str, dict[str, list[float]]] = defaultdict(
    lambda: defaultdict(list)
)

RATE_RULES: dict[str, tuple[int, int]] = {
    # action: (max_calls, window_seconds)
    "vote": (10, 60),
    "subscribe": (3, 60),
    "notify": (5, 3600),
}


def _check_rate(ip: str, action: str) -> bool:
    """Return True if the request is allowed, False if rate-limited."""
    max_calls, window = RATE_RULES[action]
    now = time.monotonic()
    bucket = _rate_limits[ip][action]
    # Prune old entries
    cutoff = now - window
    _rate_limits[ip][action] = bucket = [t for t in bucket if t > cutoff]
    if len(bucket) >= max_calls:
        return False
    bucket.append(now)
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


def hash_ip(ip: str) -> str:
    return hashlib.sha256(ip.encode()).hexdigest()


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host


def _is_localhost(request: Request) -> bool:
    client = get_client_ip(request)
    return client in ("127.0.0.1", "::1")


def make_unsubscribe_token(email: str) -> str:
    return hmac.new(
        UNSUBSCRIBE_SECRET.encode(), email.encode(), hashlib.sha256
    ).hexdigest()


def make_unsubscribe_url(email: str) -> str:
    token = make_unsubscribe_token(email)
    return f"{BASE_URL}/api/unsubscribe?token={token}"


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

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
    await db.execute("""
        CREATE TABLE IF NOT EXISTS sent_notifications (
            slug TEXT NOT NULL,
            email TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY (slug, email)
        )
    """)
    await db.commit()
    await db.close()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class VoteRequest(BaseModel):
    slug: str
    direction: str  # "up" or "down"


class SubscribeRequest(BaseModel):
    email: str
    website: Optional[str] = None  # honeypot — hidden field, bots fill it


class NotifyRequest(BaseModel):
    slug: str
    title: str
    excerpt: str


# ---------------------------------------------------------------------------
# Vote endpoints (unchanged logic)
# ---------------------------------------------------------------------------

@app.post("/api/vote")
async def vote(req: VoteRequest, request: Request):
    if req.direction not in ("up", "down"):
        return JSONResponse({"error": "direction must be 'up' or 'down'"}, status_code=400)
    if not re.match(r"^[a-z0-9\-]+$", req.slug):
        return JSONResponse({"error": "invalid slug"}, status_code=400)

    client_ip = get_client_ip(request)
    if not _check_rate(client_ip, "vote"):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    ip = hash_ip(client_ip)
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


# ---------------------------------------------------------------------------
# Subscribe endpoint (+ honeypot)
# ---------------------------------------------------------------------------

@app.post("/api/subscribe")
async def subscribe(req: SubscribeRequest, request: Request):
    # Honeypot: if the hidden field is filled, silently succeed
    if req.website:
        return {"ok": True}

    client_ip = get_client_ip(request)
    if not _check_rate(client_ip, "subscribe"):
        return JSONResponse({"error": "rate limited"}, status_code=429)

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


# ---------------------------------------------------------------------------
# Subscribers list (localhost only, unchanged)
# ---------------------------------------------------------------------------

@app.get("/api/subscribers")
async def list_subscribers(request: Request):
    if not _is_localhost(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    db = await get_db()
    cur = await db.execute("SELECT email, created_at FROM subscribers ORDER BY created_at DESC")
    rows = await cur.fetchall()
    await db.close()
    return [{"email": r["email"], "created_at": r["created_at"]} for r in rows]


# ---------------------------------------------------------------------------
# Unsubscribe (token-based)
# ---------------------------------------------------------------------------

@app.get("/api/unsubscribe")
async def unsubscribe(token: str):
    # We need to find which email this token belongs to
    db = await get_db()
    cur = await db.execute("SELECT email FROM subscribers")
    rows = await cur.fetchall()

    target_email = None
    for r in rows:
        if hmac.compare_digest(make_unsubscribe_token(r["email"]), token):
            target_email = r["email"]
            break

    if not target_email:
        await db.close()
        return HTMLResponse(
            "<html><body><h2>Invalid or expired unsubscribe link.</h2></body></html>",
            status_code=400,
        )

    await db.execute("DELETE FROM subscribers WHERE email = ?", (target_email,))
    await db.commit()
    await db.close()

    return HTMLResponse(
        "<html><body><h2>You've been unsubscribed.</h2>"
        "<p>You won't receive any more emails from laz.dev.</p></body></html>"
    )


# ---------------------------------------------------------------------------
# Notify endpoint (localhost only, sends emails via Resend)
# ---------------------------------------------------------------------------

@app.post("/api/notify")
async def notify(req: NotifyRequest, request: Request):
    if not _is_localhost(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    client_ip = get_client_ip(request)
    if not _check_rate(client_ip, "notify"):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    if not resend.api_key:
        return JSONResponse({"error": "RESEND_API_KEY not configured"}, status_code=500)

    if not re.match(r"^[a-z0-9\-]+$", req.slug):
        return JSONResponse({"error": "invalid slug"}, status_code=400)

    article_url = f"{BASE_URL}/articles/{req.slug}.html"

    db = await get_db()
    cur = await db.execute("SELECT email FROM subscribers")
    rows = await cur.fetchall()
    all_emails = [r["email"] for r in rows]

    # Skip emails already notified for this slug
    cur2 = await db.execute(
        "SELECT email FROM sent_notifications WHERE slug = ?", (req.slug,)
    )
    already_sent = {r["email"] for r in await cur2.fetchall()}

    to_send = [e for e in all_emails if e not in already_sent]

    if not to_send:
        await db.close()
        return {"sent": 0, "skipped": len(already_sent)}

    now = datetime.now(timezone.utc).isoformat()
    sent_count = 0
    errors = []

    for email in to_send:
        unsub_url = make_unsubscribe_url(email)
        body = (
            f"New post on laz.dev:\n\n"
            f"{req.title}\n\n"
            f"{req.excerpt}\n\n"
            f"Read it: {article_url}\n\n"
            f"---\n"
            f"Unsubscribe: {unsub_url}\n"
        )
        try:
            resend.Emails.send({
                "from": "noreply@laz.dev",
                "to": email,
                "subject": f"New post: {req.title}",
                "text": body,
                "headers": {
                    "List-Unsubscribe": f"<{unsub_url}>",
                },
            })
            await db.execute(
                "INSERT OR IGNORE INTO sent_notifications (slug, email, sent_at) VALUES (?, ?, ?)",
                (req.slug, email, now),
            )
            sent_count += 1
        except Exception as e:
            errors.append({"email": email, "error": str(e)})

    await db.commit()
    await db.close()

    result = {"sent": sent_count, "skipped": len(already_sent)}
    if errors:
        result["errors"] = errors
    return result
