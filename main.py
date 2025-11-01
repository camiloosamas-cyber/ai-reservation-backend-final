from fastapi import FastAPI, Request, WebSocket, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import sqlite3
from datetime import datetime, timedelta
import json
import os

# ✅ OpenAI SDK
from openai import OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = FastAPI()

# ✅ Ensure script always runs relative to backend folder
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Static + templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ✅ Allow dashboard/frontend to connect
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- DATABASE ----------------
DB_PATH = os.path.join(os.getcwd(), "reservations.db")

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
    CREATE TABLE IF NOT EXISTS reservations (
        reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_name TEXT,
        customer_email TEXT,
        contact_phone TEXT,
        datetime TEXT,
        party_size INTEGER,
        table_number TEXT,
        notes TEXT,
        status TEXT DEFAULT 'confirmed'
    )
    """)
    conn.commit()
    conn.close()

init_db()

# ---------------- MODELS ----------------
class UpdateReservation(BaseModel):
    reservation_id: int
    datetime: Optional[str] = None
    party_size: Optional[int] = None
    table_number: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None

class CancelReservation(BaseModel):
    reservation_id: int


# ---------------- ANALYTICS UTILS ----------------
def parse_dt(s: str) -> Optional[datetime]:
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except:
        return None

def get_reservations():
    conn = get_db()
    rows = conn.execute("SELECT * FROM reservations ORDER BY datetime DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_analytics():
    conn = get_db()
    rows = conn.execute("SELECT * FROM reservations").fetchall()
    conn.close()

    if not rows:
        return {"weekly_count": 0, "avg_party_size": 0, "peak_time": "N/A", "cancel_rate": 0}

    reservations = [dict(r) for r in rows]
    now = datetime.now()
    week_ago = now - timedelta(days=7)

    weekly, cancelled = 0, 0
    times, party_vals = [], []

    for r in reservations:
        if r.get("party_size"):
            party_vals.append(int(r["party_size"]))

        dt = parse_dt(r.get("datetime", ""))
        if dt:
            if dt > week_ago:
                weekly += 1
            times.append(dt.strftime("%H:%M"))

        if r.get("status") == "cancelled":
            cancelled += 1

    avg = round(sum(party_vals) / len(party_vals), 1) if party_vals else 0
    peak = max(set(times), key=times.count) if times else "N/A"
    cancel_rate = round((cancelled / len(reservations)) * 100, 1)

    return {
        "weekly_count": weekly,
        "avg_party_size": avg,
        "peak_time": peak,
        "cancel_rate": cancel_rate,
    }


# ---------------- ROUTES ----------------
@app.get("/", response_class=HTMLResponse)
def home():
    return "<h3>✅ Backend Running</h3><p>Open <a href='/dashboard'>/dashboard</a></p>"

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "reservations": get_reservations(),
            **get_analytics(),
            "parse_dt": parse_dt,
            "timedelta": timedelta,
        },
    )


# ---------------- WHATSAPP → AI → DB ----------------
@app.post("/whatsapp")
async def whatsapp_webhook(Body: str = Form(...)):
    print("📩 Incoming WhatsApp:", Body)

    prompt = """
You are an AI restaurant reservation assistant.

RULES:
- ALWAYS return valid JSON.
- NEVER repeat questions already answered.
- If missing data, return ONLY: {"ask":"<question>"}.
- KEEP previously inferred values unless overwritten.

VALID JSON FORMAT:

{
 "customer_name": "",
 "customer_email": "",
 "contact_phone": "",
 "party_size": "",
 "datetime": "",
 "table_number": "",
 "notes": ""
}

Fix common email mistakes (extra dots, missing @, etc.)
"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": Body}
            ]
        )

        output = resp.choices[0].message.content.strip()

        if output.startswith("```"):
            output = output.replace("```json", "").replace("```", "").strip()

        print("🔍 AI OUTPUT:", output)
        data = json.loads(output)

    except Exception as e:
        print("❌ Parsing error:", e)
        return Response(
            content="<Response><Message>Sorry, try again.</Message></Response>",
            media_type="application/xml"
        )

    # Missing information? Ask it.
    if "ask" in data:
        return Response(
            content=f"<Response><Message>{data['ask']}</Message></Response>",
            media_type="application/xml"
        )

    # Save into DB
    conn = get_db()
    conn.execute("""
        INSERT INTO reservations (customer_name, customer_email, contact_phone,
        datetime, party_size, table_number, notes, status)
        VALUES (?,?,?,?,?,?,?, 'confirmed')
    """, (
        data.get("customer_name"),
        data.get("customer_email") or "",
        data.get("contact_phone") or "",
        data.get("datetime"),
        int(data.get("party_size")),
        data.get("table_number") or "",
        data.get("notes") or "",
    ))
    conn.commit()
    conn.close()

    await notify({"type": "refresh"})
    return Response(
        content=f"<Response><Message>✅ Reservation created for {data['customer_name']} on {data['datetime']}.</Message></Response>",
        media_type="application/xml"
    )


# ---------------- DASHBOARD AUTO-REFRESH ----------------
clients = []

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except:
        clients.remove(websocket)

async def notify(message: dict):
    for ws in clients:
        try:
            await ws.send_text(json.dumps(message))
        except:
            pass
