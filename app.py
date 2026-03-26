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
HF_TOKEN = os.getenv("HF_TOKEN", "")  # Hugging Face бесплатный токен для генерации картинок
MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "aibot_secret")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "AIgptchatII_bot")
PREMIUM_STARS = int(os.getenv("PREMIUM_STARS", "100"))
FREE_MESSAGES = int(os.getenv("FREE_MESSAGES", "7"))   # free messages per day
FREE_IMAGES   = int(os.getenv("FREE_IMAGES",   "3"))   # free images per day
DB = os.getenv("DB_PATH", "data.db")

DATABASE_URL = os.getenv("DATABASE_URL", "")  # Railway PostgreSQL
IS_PG = bool(DATABASE_URL)

# ── DB ABSTRACTION (SQLite locally, PostgreSQL on Railway) ──
class _PgConn:
    """Thin psycopg2 wrapper matching sqlite3 Connection interface."""
    def __init__(self, url: str):
        import psycopg2
        self._c = psycopg2.connect(url)

    def execute(self, sql: str, params=()):
        sql = sql.replace("?", "%s")
        cur = self._c.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self):   self._c.commit()
    def rollback(self): self._c.rollback()
    def close(self):    self._c.close()

def db_connect():
    if IS_PG:
        return _PgConn(DATABASE_URL)
    con = sqlite3.connect(DB)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA cache_size=-8000")
    return con

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
    con = db_connect()
    pk  = "SERIAL PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    int_t = "BIGINT" if IS_PG else "INTEGER"
    con.execute(f"""CREATE TABLE IF NOT EXISTS users (
        tg_id {int_t} PRIMARY KEY, username TEXT DEFAULT '',
        first_name TEXT DEFAULT '', joined_at TEXT,
        premium_until TEXT DEFAULT NULL, memory TEXT DEFAULT '',
        ref_count INTEGER DEFAULT 0)""")
    con.execute(f"""CREATE TABLE IF NOT EXISTS messages (
        id {pk}, tg_id {int_t}, text TEXT, ts TEXT, role TEXT DEFAULT 'user')""")
    con.execute(f"""CREATE TABLE IF NOT EXISTS chats (
        tg_id {int_t} PRIMARY KEY, data TEXT DEFAULT '[]', updated_at TEXT)""")
    con.execute(f"""CREATE TABLE IF NOT EXISTS rate_limits (
        tg_id {int_t} PRIMARY KEY, count INTEGER DEFAULT 0, window_start TEXT)""")
    con.execute(f"""CREATE TABLE IF NOT EXISTS img_rate_limits (
        tg_id {int_t} PRIMARY KEY, count INTEGER DEFAULT 0, window_start TEXT)""")
    con.execute(f"""CREATE TABLE IF NOT EXISTS referrals (
        id {pk}, referrer_tg_id {int_t},
        referred_tg_id {int_t} UNIQUE, created_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS whitelist (
        username TEXT PRIMARY KEY, added_at TEXT)""")
    con.execute(f"""CREATE TABLE IF NOT EXISTS promo_codes (
        code TEXT PRIMARY KEY, free_msgs INTEGER DEFAULT 50,
        max_uses INTEGER DEFAULT 100, used_count INTEGER DEFAULT 0,
        created_at TEXT)""")
    pk = "SERIAL PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    int_t = "BIGINT" if IS_PG else "INTEGER"
    con.execute(f"""CREATE TABLE IF NOT EXISTS image_history (
        id {pk}, tg_id {int_t}, prompt TEXT, eng_prompt TEXT,
        seed INTEGER, created_at TEXT)""")
    con.execute(f"""CREATE TABLE IF NOT EXISTS daily_bonus (
        tg_id {int_t} PRIMARY KEY, last_claimed TEXT)""")
    for idx in [
        "CREATE INDEX IF NOT EXISTS idx_msg_tg ON messages(tg_id)",
        "CREATE INDEX IF NOT EXISTS idx_msg_id_desc ON messages(id DESC)",
    ]:
        try: con.execute(idx)
        except Exception:
            if IS_PG: con.rollback()
    con.commit()
    # Migrations for existing SQLite databases
    for sql in [
        "ALTER TABLE messages ADD COLUMN role TEXT DEFAULT 'user'",
        "ALTER TABLE users ADD COLUMN premium_until TEXT DEFAULT NULL",
        "ALTER TABLE users ADD COLUMN memory TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN ref_count INTEGER DEFAULT 0",
    ]:
        try:
            con.execute(sql)
            con.commit()
        except Exception:
            if IS_PG: con.rollback()
    con.close()

init_db()

# ── RATE LIMITING ──
def _is_whitelisted(tg_id: int) -> bool:
    """Check if user's username is in the whitelist."""
    con = db_connect()
    row = con.execute("SELECT username FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    con.close()
    if not row or not row[0]:
        return False
    username = row[0].lstrip("@").lower()
    con = db_connect()
    wl = con.execute("SELECT 1 FROM whitelist WHERE LOWER(username)=?", (username,)).fetchone()
    con.close()
    return wl is not None


def _is_premium(tg_id: int) -> bool:
    con = db_connect()
    row = con.execute("SELECT premium_until FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    con.close()
    if row and row[0]:
        try:
            return datetime.now() < datetime.fromisoformat(row[0])
        except Exception:
            pass
    return False

def _get_ref_count(tg_id: int) -> int:
    con = db_connect()
    row = con.execute("SELECT COUNT(*) FROM referrals WHERE referrer_tg_id=?", (tg_id,)).fetchone()
    con.close()
    return row[0] if row else 0

def check_rate_limit(tg_id: int) -> tuple[bool, int]:
    """Returns (allowed, remaining). Daily window. Premium/whitelist = unlimited."""
    if not tg_id:
        return True, 9999
    if _is_premium(tg_id):
        return True, 9999
    if _is_whitelisted(tg_id):
        return True, 9999
    ref_count = _get_ref_count(tg_id)
    daily_limit = FREE_MESSAGES + ref_count * 3  # +3 per referral
    today = datetime.now().strftime("%Y-%m-%d")
    con = db_connect()
    row = con.execute("SELECT count, window_start FROM rate_limits WHERE tg_id=?", (tg_id,)).fetchone()
    used = row[0] if (row and row[1] and str(row[1])[:10] == today) else 0
    if used >= daily_limit:
        con.close()
        return False, 0
    if row and row[1] and str(row[1])[:10] == today:
        con.execute("UPDATE rate_limits SET count=count+1 WHERE tg_id=?", (tg_id,))
    else:
        con.execute(
            """INSERT INTO rate_limits (tg_id, count, window_start) VALUES (?,1,?)
               ON CONFLICT (tg_id) DO UPDATE SET count=1, window_start=EXCLUDED.window_start""",
            (tg_id, datetime.now().isoformat())
        )
    con.commit()
    con.close()
    return True, max(0, daily_limit - used - 1)

def check_img_rate_limit(tg_id: int, increment: bool = True) -> tuple[bool, int]:
    """Returns (allowed, remaining) for image generation. Daily window."""
    if not tg_id:
        return True, 9999
    if _is_premium(tg_id):
        return True, 9999
    if _is_whitelisted(tg_id):
        return True, 9999
    today = datetime.now().strftime("%Y-%m-%d")
    con = db_connect()
    row = con.execute("SELECT count, window_start FROM img_rate_limits WHERE tg_id=?", (tg_id,)).fetchone()
    used = row[0] if (row and row[1] and str(row[1])[:10] == today) else 0
    if used >= FREE_IMAGES:
        con.close()
        return False, 0
    if increment:
        if row and row[1] and str(row[1])[:10] == today:
            con.execute("UPDATE img_rate_limits SET count=count+1 WHERE tg_id=?", (tg_id,))
        else:
            con.execute(
                """INSERT INTO img_rate_limits (tg_id, count, window_start) VALUES (?,1,?)
                   ON CONFLICT (tg_id) DO UPDATE SET count=1, window_start=EXCLUDED.window_start""",
                (tg_id, datetime.now().isoformat())
            )
        con.commit()
    con.close()
    return True, max(0, FREE_IMAGES - used - (1 if increment else 0))

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
POLLINATIONS_URL = "https://gen.pollinations.ai/v1/chat/completions"
POLLINATIONS_API_KEY = os.getenv("POLLINATIONS_API_KEY", "")


class ЗапросЧат(BaseModel):
    сообщения: list
    модель: str = "mistral-large-latest"
    tg_id: int = 0


class ЗапросКартинки(BaseModel):
    запрос: str
    tg_id: int = 0
    seed: int = 0  # 0 = random
    width: int = 768
    height: int = 768


class TrackData(BaseModel):
    tg_id: int
    username: str = ""
    first_name: str = ""
    text: str = ""
    secret: str = ""
    role: str = "user"


def _get_api(модель: str):
    if модель == "gpt":
        headers = {}
        if POLLINATIONS_API_KEY:
            headers["Authorization"] = f"Bearer {POLLINATIONS_API_KEY}"
        return POLLINATIONS_URL, headers, "openai"
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
    allowed, remaining = check_rate_limit(данные.tg_id)
    if not allowed:
        return {"ответ": "", "paywall": True}

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
    allowed, remaining = check_rate_limit(данные.tg_id)
    if not allowed:
        async def _paywall():
            yield f"data: {json.dumps({'paywall': True})}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(_paywall(), media_type="text/event-stream",
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
        # GPT fallback: non-streaming request to Pollinations
        if модель == "gpt":
            try:
                fallback_payload = {"model": model_name, "messages": msgs}
                r = await http.post(POLLINATIONS_URL, headers=headers, json=fallback_payload,
                                   timeout=httpx.Timeout(30, connect=8))
                if r.status_code == 200:
                    text = r.json()["choices"][0]["message"]["content"]
                    yield f"data: {json.dumps({'d': text})}\n\n"
                    yield "data: [DONE]\n\n"
                    return
            except Exception:
                pass
        yield f"data: {json.dumps({'d': 'GPT временно недоступен. Попробуйте Mistral Small — он работает стабильнее.'})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})



@app.post("/api/daily-bonus/{tg_id}")
async def daily_bonus(tg_id: int):
    if not tg_id:
        return {"ok": False}
    today = datetime.now().strftime("%Y-%m-%d")
    con = db_connect()
    row = con.execute("SELECT last_claimed FROM daily_bonus WHERE tg_id=?", (tg_id,)).fetchone()
    if row and row[0] == today:
        con.close()
        return {"ok": False, "already_claimed": True}
    # Дать +2 сообщения (уменьшить счётчик rate_limits)
    r = con.execute("SELECT count, window_start FROM rate_limits WHERE tg_id=?", (tg_id,)).fetchone()
    if r and r[1] and str(r[1])[:10] == today:
        con.execute("UPDATE rate_limits SET count=? WHERE tg_id=?", (max(0, r[0] - 2), tg_id))
    con.execute(
        """INSERT INTO daily_bonus (tg_id, last_claimed) VALUES (?,?)
           ON CONFLICT (tg_id) DO UPDATE SET last_claimed=EXCLUDED.last_claimed""",
        (tg_id, today)
    )
    con.commit(); con.close()
    return {"ok": True, "bonus": 2}


@app.get("/api/image/remaining/{tg_id}")
async def img_remaining(tg_id: int):
    if _is_premium(tg_id) or _is_whitelisted(tg_id):
        return {"remaining": 9999, "limit": FREE_IMAGES, "is_premium": True}
    today = datetime.now().strftime("%Y-%m-%d")
    con = db_connect()
    row = con.execute("SELECT count, window_start FROM img_rate_limits WHERE tg_id=?", (tg_id,)).fetchone()
    con.close()
    used = row[0] if (row and row[1] and str(row[1])[:10] == today) else 0
    return {"remaining": max(0, FREE_IMAGES - used), "limit": FREE_IMAGES, "is_premium": False}


@app.post("/api/image")
async def картинка(данные: ЗапросКартинки):
    # Rate limit check (only for real generation, not seed replay)
    if данные.tg_id and not данные.seed:
        allowed, remaining = check_img_rate_limit(данные.tg_id, increment=True)
        if not allowed:
            return {"error": "paywall", "paywall": True}
    eng_prompt = данные.запрос
    http = get_http()
    # Translate to English for better image quality
    if MISTRAL_API_KEY:
        try:
            tr = await http.post(MISTRAL_URL,
                headers={"Authorization": f"Bearer {MISTRAL_API_KEY}"},
                json={"model": "mistral-small-latest", "messages": [
                    {"role": "system", "content": "Translate to English image generation prompt (1-2 sentences, detailed, descriptive). Output ONLY the prompt, no quotes."},
                    {"role": "user", "content": данные.запрос}
                ]}, timeout=httpx.Timeout(20, connect=5))
            eng_prompt = tr.json()["choices"][0]["message"]["content"].strip().strip('"')
        except Exception:
            pass

    seed = данные.seed if данные.seed else int(time.time())

    async def _save_history(img_b64: str, mime: str):
        if данные.tg_id:
            try:
                con = db_connect()
                con.execute(
                    "INSERT INTO image_history (tg_id, prompt, eng_prompt, seed, created_at) VALUES (?,?,?,?,?)",
                    (данные.tg_id, данные.запрос, eng_prompt, seed, datetime.now().strftime("%Y-%m-%d %H:%M"))
                )
                con.execute("""DELETE FROM image_history WHERE tg_id=? AND id NOT IN (
                    SELECT id FROM image_history WHERE tg_id=? ORDER BY id DESC LIMIT 50)""",
                    (данные.tg_id, данные.tg_id))
                con.commit(); con.close()
            except Exception: pass
        rem = 9999
        if данные.tg_id and not данные.seed:
            _, rem = check_img_rate_limit(данные.tg_id, increment=False)
        return {"image": f"data:{mime};base64,{img_b64}", "seed": seed, "eng_prompt": eng_prompt, "remaining": rem}

    errors = []
    _hf_token = os.getenv("HF_TOKEN", "").strip()


    # ── Hugging Face (бесплатно) ──
    if not _hf_token:
        errors.append("HF_TOKEN не задан")
    else:
        hf_models = [
            "black-forest-labs/FLUX.1-schnell",
            "black-forest-labs/FLUX.1-dev",
            "stabilityai/stable-diffusion-xl-base-1.0",
        ]
        hf_hdrs = {"Authorization": f"Bearer {_hf_token}", "Content-Type": "application/json"}
        for model in hf_models:
            for attempt in range(3):
                try:
                    r = await http.post(
                        f"https://router.huggingface.co/hf-inference/models/{model}",
                        headers=hf_hdrs,
                        json={"inputs": eng_prompt, "parameters": {"width": данные.width, "height": данные.height}},
                        timeout=httpx.Timeout(120, connect=15)
                    )
                    ct = r.headers.get("content-type", "")
                    if r.status_code == 200 and "image" in ct:
                        b64 = base64.b64encode(r.content).decode()
                        mime = ct.split(";")[0].strip() or "image/jpeg"
                        return await _save_history(b64, mime)
                    if r.status_code == 503:
                        wait = int(r.headers.get("X-Wait-For-Model", "20"))
                        await asyncio.sleep(min(wait, 30))
                        continue
                    errors.append(f"HF/{model.split('/')[1]}: {r.status_code} {r.text[:100]}")
                    break
                except Exception as e:
                    errors.append(f"HF/{model.split('/')[1]}: {e}")
                    break

    # ── Fallback: Pollinations (бесплатный режим) ──
    prompt_encoded = quote(eng_prompt)
    free_hdrs = {"User-Agent": "Mozilla/5.0 (compatible; AIchatBot/1.0)"}
    poll_urls = [
        f"https://image.pollinations.ai/prompt/{prompt_encoded}?width=768&height=768&model=flux&seed={seed}",
        f"https://image.pollinations.ai/prompt/{prompt_encoded}?width=512&height=512&seed={seed}",
    ]
    for url in poll_urls:
        try:
            r = await http.get(url, headers=free_hdrs, timeout=httpx.Timeout(120, connect=15))
            ct = r.headers.get("content-type", "")
            if r.status_code == 200 and "image" in ct:
                b64 = base64.b64encode(r.content).decode()
                mime = ct.split(";")[0].strip()
                return await _save_history(b64, mime)
            errors.append(f"Pollinations: {r.status_code}")
            if r.status_code == 401:
                break  # дальше пробовать бесполезно
        except Exception as e:
            errors.append(f"Pollinations: {e}")
        await asyncio.sleep(1)

    return {"error": f"Ошибка: {' | '.join(errors)}"}


@app.get("/api/images/history/{tg_id}")
async def images_history(tg_id: int, x_admin_secret: str = Header(None)):
    if not ADMIN_SECRET or x_admin_secret != ADMIN_SECRET:
        return {"error": "forbidden"}
    con = db_connect()
    rows = con.execute(
        "SELECT id, prompt, eng_prompt, seed, created_at FROM image_history WHERE tg_id=? ORDER BY id DESC LIMIT 50",
        (tg_id,)
    ).fetchall()
    con.close()
    return {"history": [{"id": r[0], "prompt": r[1], "eng_prompt": r[2], "seed": r[3], "created_at": r[4]} for r in rows]}


@app.post("/api/track")
async def track(data: TrackData):
    if not ADMIN_SECRET or data.secret != ADMIN_SECRET:
        return {"ok": False}
    con = db_connect()
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
    con = db_connect()
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
    con = db_connect()
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
        """INSERT INTO chats (tg_id, data, updated_at) VALUES (?,?,?)
           ON CONFLICT (tg_id) DO UPDATE SET data=EXCLUDED.data, updated_at=EXCLUDED.updated_at""",
        (data.tg_id, json.dumps(trimmed, ensure_ascii=False)[:500000], datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    con.commit()
    con.close()
    return {"ok": True}


@app.post("/api/chats/load")
async def chats_load(data: SyncData):
    if not ADMIN_SECRET or data.secret != ADMIN_SECRET:
        return {"chats": []}
    con = db_connect()
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


class InvoiceRequest(BaseModel):
    tg_id: int
    secret: str = ""


@app.post("/api/user/status")
async def user_status(data: InvoiceRequest):
    if not ADMIN_SECRET or data.secret != ADMIN_SECRET:
        return {"error": "forbidden"}
    is_prem = _is_premium(data.tg_id)
    is_wl = _is_whitelisted(data.tg_id)
    ref_count = _get_ref_count(data.tg_id)
    limit = FREE_MESSAGES + ref_count * 3
    today = datetime.now().strftime("%Y-%m-%d")
    con = db_connect()
    row = con.execute("SELECT count, window_start FROM rate_limits WHERE tg_id=?", (data.tg_id,)).fetchone()
    con.close()
    used = row[0] if (row and row[1] and str(row[1])[:10] == today) else 0
    unlimited = is_prem or is_wl
    remaining = 9999 if unlimited else max(0, limit - used)
    return {"is_premium": is_prem or is_wl, "used": used, "limit": limit, "remaining": remaining, "referrals": ref_count}


@app.post("/api/premium/invoice")
async def create_invoice(data: InvoiceRequest):
    if not ADMIN_SECRET or data.secret != ADMIN_SECRET:
        return {"error": "forbidden"}
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return {"error": "no bot token"}
    http = get_http()
    try:
        r = await http.post(
            f"https://api.telegram.org/bot{token}/createInvoiceLink",
            json={
                "title": "⭐ Premium подписка",
                "description": "Безлимитный AI-чат на 30 дней. Без ограничений на количество сообщений.",
                "payload": f"premium_{data.tg_id}",
                "provider_token": "",
                "currency": "XTR",
                "prices": [{"label": "Premium 30 дней", "amount": PREMIUM_STARS}]
            }
        )
        result = r.json()
        if r.status_code == 200 and result.get("ok"):
            return {"url": result["result"]}
        return {"error": result.get("description", "failed")}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/premium/grant")
async def grant_premium(data: PremiumData):
    if not ADMIN_SECRET or data.secret != ADMIN_SECRET:
        return {"ok": False}
    premium_until = (datetime.now() + timedelta(days=30)).isoformat()
    con = db_connect()
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
    con = db_connect()
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
    con = db_connect()
    try:
        con.execute(
            """INSERT INTO referrals (referrer_tg_id, referred_tg_id, created_at) VALUES (?,?,?)
               ON CONFLICT DO NOTHING""",
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
    con = db_connect()
    con.execute("UPDATE users SET memory=? WHERE tg_id=?", (data.memory[:2000], data.tg_id))
    con.commit()
    con.close()
    return {"ok": True}


@app.post("/api/memory/load")
async def load_memory(data: MemoryData):
    if not ADMIN_SECRET or data.secret != ADMIN_SECRET:
        return {"memory": ""}
    con = db_connect()
    row = con.execute("SELECT memory FROM users WHERE tg_id=?", (data.tg_id,)).fetchone()
    con.close()
    return {"memory": row[0] if row and row[0] else ""}


# ── USER PHOTO ──
@app.get("/api/user/photo/{tg_id}")
async def user_photo(tg_id: int):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return Response(status_code=404)
    http = get_http()
    try:
        r = await http.get(
            f"https://api.telegram.org/bot{token}/getUserProfilePhotos",
            params={"user_id": tg_id, "limit": 1}
        )
        data = r.json()
        photos = data.get("result", {}).get("photos", [])
        if not photos:
            return Response(status_code=404)
        # pick largest size
        file_id = photos[0][-1]["file_id"]
        r2 = await http.get(
            f"https://api.telegram.org/bot{token}/getFile",
            params={"file_id": file_id}
        )
        file_path = r2.json().get("result", {}).get("file_path", "")
        if not file_path:
            return Response(status_code=404)
        photo_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        r3 = await http.get(photo_url)
        if r3.status_code == 200:
            return Response(content=r3.content, media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=3600"})
    except Exception:
        pass
    return Response(status_code=404)


# ── USER STATS ──
@app.get("/api/stats/{tg_id}")
async def user_stats(tg_id: int, x_admin_secret: str = Header(None)):
    if not ADMIN_SECRET or x_admin_secret != ADMIN_SECRET:
        return {"error": "forbidden"}
    con = db_connect()
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
    limit = FREE_MESSAGES + ref_count * 3
    return {"messages": msg_count, "referrals": ref_count, "is_premium": is_prem, "hourly_limit": limit}


# ── WHITELIST ──
class WhitelistData(BaseModel):
    username: str
    secret: str = ""


@app.get("/api/admin/whitelist")
async def get_whitelist(x_admin_secret: str = Header(None)):
    if not ADMIN_SECRET or x_admin_secret != ADMIN_SECRET:
        return {"error": "forbidden"}
    con = db_connect()
    rows = con.execute("SELECT username, added_at FROM whitelist ORDER BY added_at DESC").fetchall()
    con.close()
    return {"whitelist": [{"username": r[0], "added_at": r[1]} for r in rows]}


@app.post("/api/admin/whitelist/add")
async def add_whitelist(data: WhitelistData):
    if not ADMIN_SECRET or data.secret != ADMIN_SECRET:
        return {"error": "forbidden"}
    username = data.username.strip().lstrip("@").lower()
    if not username:
        return {"error": "empty username"}
    con = db_connect()
    con.execute(
        """INSERT INTO whitelist (username, added_at) VALUES (?,?)
           ON CONFLICT DO NOTHING""",
        (username, datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    con.commit()
    con.close()
    return {"ok": True, "username": username}


@app.post("/api/admin/whitelist/remove")
async def remove_whitelist(data: WhitelistData):
    if not ADMIN_SECRET or data.secret != ADMIN_SECRET:
        return {"error": "forbidden"}
    username = data.username.strip().lstrip("@").lower()
    con = db_connect()
    con.execute("DELETE FROM whitelist WHERE LOWER(username)=?", (username,))
    con.commit()
    con.close()
    return {"ok": True}


# ── GIFT PREMIUM ──
class GiftData(BaseModel):
    from_tg_id: int
    to_username: str
    secret: str = ""

@app.post("/api/premium/gift")
async def gift_premium(data: GiftData):
    if not ADMIN_SECRET or data.secret != ADMIN_SECRET:
        return {"error": "forbidden"}
    username = data.to_username.strip().lstrip("@").lower()
    if not username:
        return {"error": "empty username"}
    con = db_connect()
    row = con.execute("SELECT tg_id FROM users WHERE LOWER(username)=?", (username,)).fetchone()
    if not row:
        con.close()
        return {"error": "Пользователь не найден. Он должен открыть бота хотя бы раз."}
    to_tg_id = row[0]
    premium_until = (datetime.now() + timedelta(days=30)).isoformat()
    con.execute(
        """INSERT INTO users (tg_id, premium_until) VALUES (?,?)
           ON CONFLICT (tg_id) DO UPDATE SET premium_until=EXCLUDED.premium_until""",
        (to_tg_id, premium_until)
    )
    con.commit()
    con.close()
    return {"ok": True}


# ── PROMO CODES ──
class PromoRedeemData(BaseModel):
    tg_id: int
    code: str
    secret: str = ""

class PromoCreateData(BaseModel):
    code: str
    free_msgs: int = 50
    max_uses: int = 100
    secret: str = ""

@app.post("/api/promo/redeem")
async def redeem_promo(data: PromoRedeemData):
    if not ADMIN_SECRET or data.secret != ADMIN_SECRET:
        return {"error": "forbidden"}
    code = data.code.strip().upper()
    con = db_connect()
    row = con.execute(
        "SELECT free_msgs, max_uses, used_count FROM promo_codes WHERE code=?", (code,)
    ).fetchone()
    if not row:
        con.close()
        return {"error": "Промокод не найден"}
    free_msgs, max_uses, used_count = row
    if used_count >= max_uses:
        con.close()
        return {"error": "Промокод уже исчерпан"}
    today = datetime.now().strftime("%Y-%m-%d")
    r = con.execute("SELECT count, window_start FROM rate_limits WHERE tg_id=?", (data.tg_id,)).fetchone()
    if r and r[1] and str(r[1])[:10] == today:
        new_count = max(0, r[0] - free_msgs)
        con.execute("UPDATE rate_limits SET count=? WHERE tg_id=?", (new_count, data.tg_id))
    con.execute("UPDATE promo_codes SET used_count=used_count+1 WHERE code=?", (code,))
    con.commit()
    con.close()
    return {"ok": True, "free_msgs": free_msgs}

@app.get("/api/promo/list")
async def list_promos(x_admin_secret: str = Header(None)):
    if not ADMIN_SECRET or x_admin_secret != ADMIN_SECRET:
        return {"error": "forbidden"}
    con = db_connect()
    rows = con.execute(
        "SELECT code, free_msgs, max_uses, used_count, created_at FROM promo_codes ORDER BY created_at DESC"
    ).fetchall()
    con.close()
    return {"promos": [{"code": r[0], "free_msgs": r[1], "max_uses": r[2], "used_count": r[3], "created_at": r[4]} for r in rows]}


@app.post("/api/promo/delete")
async def delete_promo(data: WhitelistData):
    if not ADMIN_SECRET or data.secret != ADMIN_SECRET:
        return {"error": "forbidden"}
    code = data.username.strip().upper()  # reuse WhitelistData model
    con = db_connect()
    con.execute("DELETE FROM promo_codes WHERE code=?", (code,))
    con.commit()
    con.close()
    return {"ok": True}


@app.post("/api/promo/create")
async def create_promo(data: PromoCreateData):
    if not ADMIN_SECRET or data.secret != ADMIN_SECRET:
        return {"error": "forbidden"}
    code = data.code.strip().upper()
    if not code:
        return {"error": "empty code"}
    con = db_connect()
    con.execute(
        """INSERT INTO promo_codes (code, free_msgs, max_uses, used_count, created_at) VALUES (?,?,?,0,?)
           ON CONFLICT DO NOTHING""",
        (code, data.free_msgs, data.max_uses, datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    con.commit()
    con.close()
    return {"ok": True, "code": code}


# ── STATIC ──
@app.get("/")
async def index():
    return FileResponse("static/index.html", headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

app.mount("/", StaticFiles(directory="static", html=True), name="static")
