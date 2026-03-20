import os
import json
import time
import base64
import asyncio
import sqlite3
import httpx
from datetime import datetime
from urllib.parse import quote
from fastapi import FastAPI, Header
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
load_dotenv()
app = FastAPI()

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")
DB = "data.db"

# Global HTTP client — reuses TCP/TLS connections across requests
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

@app.on_event("shutdown")
async def _shutdown():
    if _http and not _http.is_closed:
        await _http.aclose()

def init_db():
    con = sqlite3.connect(DB)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA cache_size=-8000")
    con.execute("""CREATE TABLE IF NOT EXISTS users (
        tg_id INTEGER PRIMARY KEY,
        username TEXT DEFAULT '',
        first_name TEXT DEFAULT '',
        joined_at TEXT
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
    con.execute("CREATE INDEX IF NOT EXISTS idx_msg_tg ON messages(tg_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_msg_id_desc ON messages(id DESC)")
    # Migrate: add role column if missing
    try:
        con.execute("ALTER TABLE messages ADD COLUMN role TEXT DEFAULT 'user'")
    except Exception:
        pass
    con.commit()
    con.close()

init_db()

ALLOWED_MODELS = {"mistral-large-latest", "mistral-small-latest", "codestral-latest", "gpt", "pixtral-large-latest"}
POLLINATIONS_URL = "https://text.pollinations.ai/openai/chat/completions"

class ЗапросЧат(BaseModel):
    сообщения: list
    модель: str = "mistral-large-latest"

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
    """Return (url, headers, model_name) based on selected model."""
    if модель == "gpt":
        return POLLINATIONS_URL, {}, "openai"
    return MISTRAL_URL, {"Authorization": f"Bearer {MISTRAL_API_KEY}"}, модель

def _fix_messages_for_gpt(msgs: list) -> list:
    """Pollinations API mangles Cyrillic in system prompts. Replace with English equivalent."""
    fixed = []
    for m in msgs:
        if m.get("role") == "system":
            fixed.append({"role": "system", "content": "You are a helpful AI assistant. Reply in the same language the user writes in. Be concise and to the point. Format code in code blocks."})
        else:
            fixed.append(m)
    return fixed

@app.post("/api/chat")
async def чат(данные: ЗапросЧат):
    модель = данные.модель if данные.модель in ALLOWED_MODELS else "mistral-large-latest"
    url, headers, model_name = _get_api(модель)
    msgs = _fix_messages_for_gpt(данные.сообщения) if модель == "gpt" else данные.сообщения
    payload = {"model": model_name, "messages": msgs}
    last_status = 0
    http = get_http()
    for attempt in range(3):
        try:
            r = await http.post(url, headers=headers, json=payload)
            last_status = r.status_code
            if r.status_code == 200:
                return {"ответ": r.json()["choices"][0]["message"]["content"]}
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
                        # Stream failed — try non-stream fallback for GPT
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
        # All stream attempts failed — try non-stream as last resort
        if модель == "gpt":
            try:
                fallback_payload = {"model": model_name, "messages": данные.сообщения}
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
    # Expand and translate prompt to English via Mistral
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
    url = f"https://image.pollinations.ai/prompt/{prompt_encoded}?model=flux&width=768&height=768&nologo=true&enhance=true&seed={int(time.time())}"

    # Try up to 3 times with backoff
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
        SELECT u.tg_id, u.username, u.first_name, u.joined_at, COUNT(m.id) as cnt
        FROM users u LEFT JOIN messages m ON u.tg_id = m.tg_id
        GROUP BY u.tg_id ORDER BY u.joined_at DESC
    """).fetchall()
    recent = con.execute("""
        SELECT u.first_name, u.username, m.text, m.ts, m.role
        FROM messages m LEFT JOIN users u ON m.tg_id = u.tg_id
        ORDER BY m.id DESC LIMIT 100
    """).fetchall()
    con.close()
    return {
        "total_users": total_users,
        "total_messages": total_msgs,
        "users": [{"tg_id": u[0], "username": u[1] or "", "first_name": u[2] or "", "joined": u[3], "messages": u[4]} for u in users],
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
    # Keep only last 30 chats, strip image data to save space
    trimmed = []
    for c in data.chats[:30]:
        msgs = []
        for m in c.get("сообщения", []):
            content = m.get("content", "")
            if isinstance(content, list):
                content = [p for p in content if p.get("type") != "image_url"]
            msgs.append({"role": m.get("role","user"), "content": content, "time": m.get("time","")})
        trimmed.append({"id": c.get("id"), "сообщения": msgs, "время": c.get("время"), "preview": c.get("preview","")[:60]})
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

@app.get("/api/tts")
async def tts(q: str = "", lang: str = "ru"):
    """Proxy Google TTS to avoid CORS issues in Telegram WebView."""
    if not q or len(q) > 200:
        return {"error": "text too long or empty"}
    url = f"https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob&tl={lang}&q={quote(q)}"
    http = get_http()
    try:
        r = await http.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and "audio" in r.headers.get("content-type", ""):
            from fastapi.responses import Response
            return Response(content=r.content, media_type="audio/mpeg",
                headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        pass
    return {"error": "TTS unavailable"}

@app.get("/")
async def index():
    return FileResponse("static/index.html", headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

app.mount("/", StaticFiles(directory="static", html=True), name="static")
