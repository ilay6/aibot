import os
import json
import time
import base64
import asyncio
import sqlite3
import hashlib
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from urllib.parse import quote
from fastapi import FastAPI, Header, Request
from fastapi.responses import StreamingResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

import httpx

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "AIgptchatII_bot")
PREMIUM_STARS = int(os.getenv("PREMIUM_STARS", "100"))
RATE_LIMIT_FREE = int(os.getenv("RATE_LIMIT_FREE", "30"))
RATE_LIMIT_WINDOW = 3600
DB = "data.db"

# ── LRU RESPONSE CACHE ──
class _LRUCache:
    def __init__(self, maxsize: int = 200, ttl: int = 3600):
        self._cache: OrderedDict = OrderedDict()
        self._maxsize = maxsize
        self._ttl = ttl

    def get(self, key: str):
        if key not in self._cache:
            return None
        val, ts = self._cache[key]
        if time.time() - ts > self._ttl:
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return val

    def set(self, key: str, val: str):
        self._cache[key] = (val, time.time())
        self._cache.move_to_end(key)
        if len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)

_cache = _LRUCache(200, 3600)

def _cache_key(model: str, messages: list) -> str | None:
    """Only cache single-turn text queries (no history, no images)."""
    non_sys = [m for m in messages if m.get("role") != "system"]
    if len(non_sys) != 1 or non_sys[0].get("role") != "user":
        return None
    content = non_sys[0].get("content", "")
    if not isinstance(content, str) or len(content) > 500:
        return None
    return hashlib.md5(f"{model}:{content}".encode()).hexdigest()

# ── HTTP CLIENT ──
_http: httpx.AsyncClient | None = None

def get_http() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(
            timeout=httpx.Timeout(90, connect=10),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=True,
        )
    return _http

# ── DATABASE ──
def init_db():
    con = sqlite3.connect(DB)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA cache_size=-8000")
    con.execute("""CREATE TABLE IF NOT EXISTS users (
        tg_id INTEGER PRIMARY KEY,
        username TEXT DEFAULT '',
        first_name TEXT DEFAULT '',
        joined_at TEXT,
        premium_until TEXT DEFAULT NULL,
        memory TEXT DEFAULT '',
        ref_count INTEGER DEFAULT 0
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER,
        text TEXT,
        ts TEXT,
        role TEXT DEFAULT 'user'
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS chats (
        tg_id INTEGER PRIMARY KEY,
        data TEXT DEFAULT '[]',
        updated_at TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS rate_limits (
        tg_id INTEGER PRIMARY KEY,
        count INTEGER DEFAULT 0,
        window_start TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_tg_id INTEGER,
        referred_tg_id INTEGER UNIQUE,
        created_at TEXT
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_msg_tg ON messages(tg_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_msg_id_desc ON messages(id DESC)")
    # Migrations for existing databases
    for sql in [
        "ALTER TABLE messages ADD COLUMN role TEXT DEFAULT 'user'",
        "ALTER TABLE users ADD COLUMN premium_until TEXT DEFAULT NULL",
        "ALTER TABLE users ADD COLUMN memory TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN ref_count INTEGER DEFAULT 0",
    ]:
        try:
            con.execute(sql)
        except Exception:
            pass
    con.commit()
    con.close()

init_db()

# ── RATE LIMITING ──
def _is_premium(tg_id: int) -> bool:
    con = sqlite3.connect(DB)
    row = con.execute("SELECT premium_until FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    con.close()
    if row and row[0]:
        try:
            return datetime.now() < datetime.fromisoformat(row[0])
        except Exception:
            pass
    return False

def _get_ref_count(tg_id: int) -> int:
    con = sqlite3.connect(DB)
    row = con.execute("SELECT COUNT(*) FROM referrals WHERE referrer_tg_id=?", (tg_id,)).fetchone()
    con.close()
    return row[0] if row else 0

def check_rate_limit(tg_id: int) -> tuple[bool, int]:
    """Returns (allowed, seconds_until_reset). Premium users always allowed."""
    if not tg_id:
        return True, 0
    if _is_premium(tg_id):
        return True, 0
    ref_count = _get_ref_count(tg_id)
    limit = RATE_LIMIT_FREE + ref_count * 5  # +5 per referral
    now = datetime.now()
    con = sqlite3.connect(DB)
    row = con.execute("SELECT count, window_start FROM rate_limits WHERE tg_id=?", (tg_id,)).fetchone()
    if row:
        count, ws = row
        try:
            window_start = datetime.fromisoformat(ws)
        except Exception:
            window_start = now
        elapsed = (now - window_start).total_seconds()
        if elapsed >= RATE_LIMIT_WINDOW:
            con.execute("UPDATE rate_limits SET count=1, window_start=? WHERE tg_id=?", (now.isoformat(), tg_id))
            con.commit()
            con.close()
            return True, 0
        if count >= limit:
            con.close()
            return False, int(RATE_LIMIT_WINDOW - elapsed)
        con.execute("UPDATE rate_limits SET count=count+1 WHERE tg_id=?", (tg_id,))
    else:
        con.execute("INSERT INTO rate_limits (tg_id, count, window_start) VALUES (?,1,?)", (tg_id, now.isoformat()))
    con.commit()
    con.close()
    return True, 0

# ── LIFESPAN ──
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: webhook or polling
    try:
        from bot import bot, dp
        if bot and WEBAPP_URL:
            await bot.set_webhook(f"{WEBAPP_URL}/webhook", drop_pending_updates=True)
            print(f"Webhook set: {WEBAPP_URL}/webhook")
        elif bot:
            from aiogram.methods import DeleteWebhook
            await bot(DeleteWebhook(drop_pending_updates=True))
            asyncio.create_task(dp.start_polling(bot))
            print("Bot polling started")
    except Exception as e:
        print(f"Bot startup warning: {e}")
    yield
    # Shutdown
    try:
        from bot import bot
        if bot:
            await bot.session.close()
    except Exception:
        pass
    if _http and not _http.is_closed:
        await _http.aclose()

app = FastAPI(lifespan=lifespan)

ALLOWED_MODELS = {"mistral-large-latest", "mistral-small-latest", "codestral-latest", "gpt", "pixtral-large-latest"}
POLLINATIONS_URL = "https://text.pollinations.ai/openai/chat/completions"


class ЗапросЧат(BaseModel):
    сообщения: list
    модель: str = "mistral-large-latest"
    tg_id: int = 0


class ЗапросКартинки(BaseModel):
    запрос: str


class TrackData(BaseModel):
    tg_id: int
    username: str = ""
    first_name: str = ""
    text: str = ""
    secret: str = ""
    role: str = "user"


def _get_api(модель: str):
    if модель == "gpt":
        return POLLINATIONS_URL, {}, "openai"
    return MISTRAL_URL, {"Authorization": f"Bearer {MISTRAL_API_KEY}"}, модель


def _fix_messages_for_gpt(msgs: list) -> list:
    fixed = []
    for m in msgs:
        if m.get("role") == "system":
            fixed.append({"role": "system", "content": "You are a helpful AI assistant. Reply in the same language the user writes in. Be concise and to the point. Format code in code blocks."})
        else:
            fixed.append(m)
    return fixed


@app.post("/api/chat")
async def чат(данные: ЗапросЧат):
    # Rate limit
    allowed, wait_secs = check_rate_limit(данные.tg_id)
    if not allowed:
        mins = wait_secs // 60 + 1
        return {"ответ": f"⏳ Лимит исчерпан (~{mins} мин.). Пригласи друзей /ref для +5 сообщений или купи Premium /premium в боте."}

    модель = данные.модель if данные.модель in ALLOWED_MODELS else "mistral-large-latest"
    url, headers, model_name = _get_api(модель)
    msgs = _fix_messages_for_gpt(данные.сообщения) if модель == "gpt" else данные.сообщения

    # Cache check (single-turn only)
    ck = _cache_key(model_name, msgs)
    if ck:
        cached = _cache.get(ck)
        if cached:
            return {"ответ": cached}

    payload = {"model": model_name, "messages": msgs}
    last_status = 0
    http = get_http()
    for attempt in range(3):
        try:
            r = await http.post(url, headers=headers, json=payload)
            last_status = r.status_code
            if r.status_code == 200:
                result = r.json()["choices"][0]["message"]["content"]
                if ck:
                    _cache.set(ck, result)
                return {"ответ": result}
            if r.status_code == 429:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            break
        except Exception:
            await asyncio.sleep(2)
    if last_status == 429:
        return {"ответ": "Слишком много запросов, подождите немного и попробуйте снова."}
    return {"ответ": "Сервер временно недоступен. Попробуйте позже."}


@app.post("/api/chat/stream")
async def чат_stream(данные: ЗапросЧат):
    # Rate limit
    allowed, wait_secs = check_rate_limit(данные.tg_id)
    if not allowed:
        mins = wait_secs // 60 + 1
        msg = f"⏳ Лимит исчерпан (~{mins} мин.). Пригласи друзей /ref для +5 сообщений или купи Premium /premium в боте."
        async def _rate_limited():
            yield f"data: {json.dumps({'d': msg})}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(_rate_limited(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    модель = данные.модель if данные.модель in ALLOWED_MODELS else "mistral-large-latest"
    url, headers, model_name = _get_api(модель)
    msgs = _fix_messages_for_gpt(данные.сообщения) if модель == "gpt" else данные.сообщения
    payload = {"model": model_name, "messages": msgs, "stream": True}

    async def generate():
        http = get_http()
        for attempt in range(3):
            try:
                async with http.stream("POST", url, headers=headers, json=payload) as r:
                    if r.status_code == 429:
                        await asyncio.sleep(2 * (attempt + 1))
                        continue
                    if r.status_code != 200:
                        if модель == "gpt" and attempt < 2:
                            await asyncio.sleep(1)
                            continue
                        yield f"data: {json.dumps({'d': 'Сервер временно недоступен. Попробуйте позже.'})}\n\n"
                        yield "data: [DONE]\n\n"
                        return
                    async for line in r.aiter_lines():
                        if line.startswith("data: "):
                            data = line[6:]
                            if data.strip() == "[DONE]":
                                yield "data: [DONE]\n\n"
                                return
                            try:
                                delta = json.loads(data)["choices"][0]["delta"]
                                content = delta.get("content") or ""
                                if content:
                                    yield f"data: {json.dumps({'d': content})}\n\n"
                            except Exception:
                                pass
                    yield "data: [DONE]\n\n"
                    return
            except Exception:
                await asyncio.sleep(2)
        # GPT fallback non-stream
        if модель == "gpt":
            try:
                fallback_payload = {"model": model_name, "messages": msgs}
                r = await http.post(url, headers=headers, json=fallback_payload)
                if r.status_code == 200:
                    text = r.json()["choices"][0]["message"]["content"]
                    yield f"data: {json.dumps({'d': text})}\n\n"
                    yield "data: [DONE]\n\n"
                    return
            except Exception:
                pass
        yield f"data: {json.dumps({'d': 'Слишком много запросов, подождите немного.'})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/image")
async def картинка(данные: ЗапросКартинки):
    eng_prompt = данные.запрос
    http = get_http()
    try:
        tr = await http.post(MISTRAL_URL,
            headers={"Authorization": f"Bearer {MISTRAL_API_KEY}"},
            json={"model": "mistral-small-latest", "messages": [
                {"role": "system", "content": "You are an image prompt engineer. Take the user's request and turn it into a detailed English prompt for an image generator (1-2 sentences). Be descriptive. Output ONLY the prompt."},
                {"role": "user", "content": данные.запрос}
            ]})
        eng_prompt = tr.json()["choices"][0]["message"]["content"].strip().strip('"')
    except Exception:
        pass

    prompt_encoded = quote(eng_prompt)
    url = f"https://image.pollinations.ai/prompt/{prompt_encoded}?width=768&height=768&nologo=true&enhance=true&seed={int(time.time())}"

    for attempt in range(3):
        try:
            r = await http.get(url, follow_redirects=True, timeout=httpx.Timeout(90, connect=15))
            ct = r.headers.get("content-type", "")
            if r.status_code == 200 and ct.startswith("image/"):
                b64 = base64.b64encode(r.content).decode()
                mime = ct.split(";")[0]
                return {"image": f"data:{mime};base64,{b64}"}
            if r.status_code == 429:
                await asyncio.sleep(5 * (attempt + 1))
                continue
        except Exception:
            pass
        await asyncio.sleep(3)

    return {"error": "Генерация временно недоступна. Попробуйте через минуту."}


@app.post("/api/track")
async def track(data: TrackData):
    if not ADMIN_SECRET or data.secret != ADMIN_SECRET:
        return {"ok": False}
    con = sqlite3.connect(DB)
    con.execute(
        """INSERT INTO users (tg_id, username, first_name, joined_at) VALUES (?,?,?,?)
           ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name""",
        (data.tg_id, data.username, data.first_name, datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    if data.text:
        con.execute(
            "INSERT INTO messages (tg_id, text, ts, role) VALUES (?,?,?,?)",
            (data.tg_id, data.text, datetime.now().strftime("%Y-%m-%d %H:%M"), data.role)
        )
    con.commit()
    con.close()
    return {"ok": True}


@app.get("/api/admin/stats")
async def admin_stats(x_admin_secret: str = Header(None)):
    if not ADMIN_SECRET or x_admin_secret != ADMIN_SECRET:
        return {"error": "forbidden"}
    con = sqlite3.connect(DB)
    total_users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_msgs = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    users = con.execute("""
        SELECT u.tg_id, u.username, u.first_name, u.joined_at,
               COUNT(m.id) as cnt, u.premium_until, u.ref_count
        FROM users u LEFT JOIN messages m ON u.tg_id = m.tg_id
        GROUP BY u.tg_id ORDER BY u.joined_at DESC
    """).fetchall()
    recent = con.execute("""
        SELECT u.first_name, u.username, m.text, m.ts, m.role
        FROM messages m LEFT JOIN users u ON m.tg_id = u.tg_id
        ORDER BY m.id DESC LIMIT 100
    """).fetchall()
    con.close()
    now = datetime.now()
    return {
        "total_users": total_users,
        "total_messages": total_msgs,
        "users": [{
            "tg_id": u[0], "username": u[1] or "", "first_name": u[2] or "",
            "joined": u[3], "messages": u[4],
            "is_premium": bool(u[5] and now < datetime.fromisoformat(u[5])),
            "ref_count": u[6] or 0
        } for u in users],
        "recent_messages": [{"name": m[0] or "?", "username": m[1] or "", "text": m[2], "ts": m[3], "role": m[4] or "user"} for m in recent]
    }


class SyncData(BaseModel):
    tg_id: int
    secret: str = ""
    chats: list = []


@app.post("/api/chats/save")
async def chats_save(data: SyncData):
    if not ADMIN_SECRET or data.secret != ADMIN_SECRET:
        return {"ok": False}
    con = sqlite3.connect(DB)
    trimmed = []
    for c in data.chats[:30]:
        msgs = []
        for m in c.get("сообщения", []):
            content = m.get("content", "")
            if isinstance(content, list):
                content = [p for p in content if p.get("type") != "image_url"]
            msgs.append({"role": m.get("role", "user"), "content": content, "time": m.get("time", "")})
        trimmed.append({"id": c.get("id"), "сообщения": msgs, "время": c.get("время"), "preview": c.get("preview", "")[:60]})
    con.execute(
        "INSERT OR REPLACE INTO chats (tg_id, data, updated_at) VALUES (?,?,?)",
        (data.tg_id, json.dumps(trimmed, ensure_ascii=False)[:500000], datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    con.commit()
    con.close()
    return {"ok": True}


@app.post("/api/chats/load")
async def chats_load(data: SyncData):
    if not ADMIN_SECRET or data.secret != ADMIN_SECRET:
        return {"chats": []}
    con = sqlite3.connect(DB)
    row = con.execute("SELECT data FROM chats WHERE tg_id=?", (data.tg_id,)).fetchone()
    con.close()
    if not row:
        return {"chats": []}
    try:
        return {"chats": json.loads(row[0])}
    except Exception:
        return {"chats": []}


class SearchQuery(BaseModel):
    query: str
    lang: str = "ru"


@app.post("/api/search")
async def web_search(data: SearchQuery):
    http = get_http()
    try:
        r = await http.get(
            f"https://html.duckduckgo.com/html/?q={quote(data.query)}",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=httpx.Timeout(15, connect=5)
        )
        if r.status_code != 200:
            return {"results": []}
        import re
        html = r.text
        results = []
        for m in re.finditer(r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</span>', html, re.DOTALL):
            url_raw, title, snippet = m.group(1), m.group(2), m.group(3)
            title = re.sub(r'<[^>]+>', '', title).strip()
            snippet = re.sub(r'<[^>]+>', '', snippet).strip()
            if 'uddg=' in url_raw:
                from urllib.parse import unquote
                url_clean = unquote(url_raw.split('uddg=')[1].split('&')[0])
            else:
                url_clean = url_raw
            if title and snippet:
                results.append({"title": title, "snippet": snippet, "url": url_clean})
            if len(results) >= 5:
                break
        return {"results": results}
    except Exception:
        return {"results": []}


@app.get("/api/tts")
async def tts(q: str = "", lang: str = "ru"):
    if not q or len(q) > 200:
        return {"error": "text too long or empty"}
    url = f"https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob&tl={lang}&q={quote(q)}"
    http = get_http()
    try:
        r = await http.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and "audio" in r.headers.get("content-type", ""):
            return Response(content=r.content, media_type="audio/mpeg",
                headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        pass
    return {"error": "TTS unavailable"}


# ── WEBHOOK ──
@app.post("/webhook")
async def process_webhook(request: Request):
    try:
        from bot import bot, dp
        from aiogram.types import Update
        if not bot:
            return {"ok": False}
        body = await request.body()
        update = Update.model_validate_json(body)
        await dp.feed_update(bot, update)
    except Exception as e:
        print(f"Webhook error: {e}")
    return {"ok": True}


# ── PREMIUM ──
class PremiumData(BaseModel):
    tg_id: int
    first_name: str = ""
    username: str = ""
    text: str = ""
    secret: str = ""


@app.post("/api/premium/grant")
async def grant_premium(data: PremiumData):
    if not ADMIN_SECRET or data.secret != ADMIN_SECRET:
        return {"ok": False}
    premium_until = (datetime.now() + timedelta(days=30)).isoformat()
    con = sqlite3.connect(DB)
    con.execute(
        """INSERT INTO users (tg_id, username, first_name, joined_at, premium_until) VALUES (?,?,?,?,?)
           ON CONFLICT(tg_id) DO UPDATE SET premium_until=excluded.premium_until,
           username=excluded.username, first_name=excluded.first_name""",
        (data.tg_id, data.username, data.first_name, datetime.now().strftime("%Y-%m-%d %H:%M"), premium_until)
    )
    con.commit()
    con.close()
    return {"ok": True, "premium_until": premium_until}


@app.get("/api/premium/status/{tg_id}")
async def premium_status(tg_id: int, x_admin_secret: str = Header(None)):
    if not ADMIN_SECRET or x_admin_secret != ADMIN_SECRET:
        return {"error": "forbidden"}
    is_prem = _is_premium(tg_id)
    con = sqlite3.connect(DB)
    row = con.execute("SELECT premium_until FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    con.close()
    return {"is_premium": is_prem, "premium_until": row[0] if row else None}


# ── REFERRAL ──
class ReferralData(BaseModel):
    referrer_tg_id: int
    referred_tg_id: int
    secret: str = ""


@app.post("/api/referral/add")
async def add_referral(data: ReferralData):
    if not ADMIN_SECRET or data.secret != ADMIN_SECRET:
        return {"ok": False}
    con = sqlite3.connect(DB)
    try:
        con.execute(
            "INSERT OR IGNORE INTO referrals (referrer_tg_id, referred_tg_id, created_at) VALUES (?,?,?)",
            (data.referrer_tg_id, data.referred_tg_id, datetime.now().strftime("%Y-%m-%d %H:%M"))
        )
        con.execute("UPDATE users SET ref_count = ref_count + 1 WHERE tg_id=?", (data.referrer_tg_id,))
        con.commit()
    except Exception:
        pass
    con.close()
    return {"ok": True}


@app.get("/api/referral/count/{tg_id}")
async def referral_count(tg_id: int, x_admin_secret: str = Header(None)):
    if not ADMIN_SECRET or x_admin_secret != ADMIN_SECRET:
        return {"error": "forbidden"}
    return {"count": _get_ref_count(tg_id)}


# ── USER MEMORY ──
class MemoryData(BaseModel):
    tg_id: int
    secret: str = ""
    memory: str = ""


@app.post("/api/memory/save")
async def save_memory(data: MemoryData):
    if not ADMIN_SECRET or data.secret != ADMIN_SECRET:
        return {"ok": False}
    con = sqlite3.connect(DB)
    con.execute("UPDATE users SET memory=? WHERE tg_id=?", (data.memory[:2000], data.tg_id))
    con.commit()
    con.close()
    return {"ok": True}


@app.post("/api/memory/load")
async def load_memory(data: MemoryData):
    if not ADMIN_SECRET or data.secret != ADMIN_SECRET:
        return {"memory": ""}
    con = sqlite3.connect(DB)
    row = con.execute("SELECT memory FROM users WHERE tg_id=?", (data.tg_id,)).fetchone()
    con.close()
    return {"memory": row[0] if row and row[0] else ""}


# ── USER STATS ──
@app.get("/api/stats/{tg_id}")
async def user_stats(tg_id: int, x_admin_secret: str = Header(None)):
    if not ADMIN_SECRET or x_admin_secret != ADMIN_SECRET:
        return {"error": "forbidden"}
    con = sqlite3.connect(DB)
    msg_count = con.execute("SELECT COUNT(*) FROM messages WHERE tg_id=? AND role='user'", (tg_id,)).fetchone()[0]
    user = con.execute("SELECT premium_until FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    ref_count = _get_ref_count(tg_id)
    con.close()
    is_prem = False
    if user and user[0]:
        try:
            is_prem = datetime.now() < datetime.fromisoformat(user[0])
        except Exception:
            pass
    limit = RATE_LIMIT_FREE + ref_count * 5
    return {"messages": msg_count, "referrals": ref_count, "is_premium": is_prem, "hourly_limit": limit}


# ── STATIC ──
@app.get("/")
async def index():
    return FileResponse("static/index.html", headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

app.mount("/", StaticFiles(directory="static", html=True), name="static")
