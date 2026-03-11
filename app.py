import os
import json
import base64
import sqlite3
import httpx
from datetime import datetime
from urllib.parse import quote
from fastapi import FastAPI, Header
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()
app = FastAPI()
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")
DB = "data.db"

def init_db():
    con = sqlite3.connect(DB)
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
        ts TEXT
    )""")
    con.commit()
    con.close()

init_db()

ALLOWED_MODELS = {"mistral-large-latest", "mistral-small-latest", "codestral-latest", "mistral-medium-latest"}

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

@app.post("/api/chat")
async def чат(данные: ЗапросЧат):
    модель = данные.модель if данные.модель in ALLOWED_MODELS else "mistral-large-latest"
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.post(MISTRAL_URL, headers={"Authorization": f"Bearer {MISTRAL_API_KEY}"}, json={"model": модель, "messages": данные.сообщения})
    try:
        return {"ответ": r.json()["choices"][0]["message"]["content"]}
    except Exception:
        return {"ответ": f"[DEBUG] Статус: {r.status_code} | Ответ: {r.text[:300]}"}

@app.post("/api/chat/stream")
async def чат_stream(данные: ЗапросЧат):
    модель = данные.модель if данные.модель in ALLOWED_MODELS else "mistral-large-latest"
    payload = {"model": модель, "messages": данные.сообщения, "stream": True}
    headers = {"Authorization": f"Bearer {MISTRAL_API_KEY}"}
    async def generate():
        async with httpx.AsyncClient(timeout=60) as http:
            async with http.stream("POST", MISTRAL_URL, headers=headers, json=payload) as r:
                async for line in r.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            yield "data: [DONE]\n\n"
                            break
                        try:
                            delta = json.loads(data)["choices"][0]["delta"].get("content", "")
                            if delta:
                                yield f"data: {json.dumps({'d': delta})}\n\n"
                        except Exception:
                            pass
    return StreamingResponse(generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.post("/api/image")
async def картинка(данные: ЗапросКартинки):
    текст = quote(данные.запрос, safe='')
    url = f"https://image.pollinations.ai/prompt/{текст}?width=512&height=512&nologo=true"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    async with httpx.AsyncClient(timeout=90, follow_redirects=True) as http:
        resp = await http.get(url, headers=headers)
    if resp.status_code != 200 or "image" not in resp.headers.get("content-type", ""):
        return {"error": "Ошибка генерации"}
    img_b64 = base64.b64encode(resp.content).decode()
    return {"url": f"data:image/jpeg;base64,{img_b64}"}

@app.post("/api/track")
async def track(data: TrackData):
    if not ADMIN_SECRET or data.secret != ADMIN_SECRET:
        return {"ok": False}
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT OR IGNORE INTO users (tg_id, username, first_name, joined_at) VALUES (?,?,?,?)",
        (data.tg_id, data.username, data.first_name, datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    con.execute(
        "UPDATE users SET username=?, first_name=? WHERE tg_id=?",
        (data.username, data.first_name, data.tg_id)
    )
    if data.text:
        con.execute(
            "INSERT INTO messages (tg_id, text, ts) VALUES (?,?,?)",
            (data.tg_id, data.text, datetime.now().strftime("%Y-%m-%d %H:%M"))
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
        SELECT u.first_name, u.username, m.text, m.ts
        FROM messages m LEFT JOIN users u ON m.tg_id = u.tg_id
        ORDER BY m.ts DESC LIMIT 50
    """).fetchall()
    con.close()
    return {
        "total_users": total_users,
        "total_messages": total_msgs,
        "users": [{"tg_id": u[0], "username": u[1] or "", "first_name": u[2] or "", "joined": u[3], "messages": u[4]} for u in users],
        "recent_messages": [{"name": m[0] or "?", "username": m[1] or "", "text": m[2], "ts": m[3]} for m in recent]
    }

app.mount("/", StaticFiles(directory="static", html=True), name="static")
