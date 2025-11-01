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

# ✅ Correct OpenAI SDK import
from openai import OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


app = FastAPI()

# ✅ Always work inside backend folder
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Static + templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- DB ----------------
DB_PATH = os.path.join(os.getcwd(), "reservations.db")


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=3000;")
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
        status TEXT DEFAULT 'pending'
    )
    """)
    conn.commit()
    conn.close()


init_db()

# ---------------- Models ----------------
class UpdateReservation(BaseModel):
    reservation_id: int
    datetime: Optional[str] = None
    party_size: Optional[int] = None
    table_number: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None


class CancelReservation(BaseModel):
    reservation_id: int


# ---------- Util / analytics ----------
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
    now, week_ago = datetime.now(), datetime.now() - timedelta(days=7)

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


# ---------------- Routes ----------------
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


@app.post("/createReservation")
async def create_reservation(request: Request):
    data = await request.json()
    party_size = int(data.get("party_size") or 0)

    conn = get_db()
    conn.execute("""
      INSERT INTO reservations (
        customer_name, customer_email, contact_phone,
        datetime, party_size, table_number, notes, status
      ) VALUES (?,?,?,?,?,?,?, 'confirmed')
    """, (
        data.get("customer_name"),
        data.get("customer_email"),
        data.get("contact_phone"),
        data.get("datetime"),
        party_size,
        data.get("table_number"),
        data.get("notes")
    ))
    conn.commit()
    conn.close()

    await notify({"type": "refresh"})
    return {"message": "created"}


@app.post("/updateReservation")
async def update_reservation(data: UpdateReservation):
    fields, values = [], []

    if data.datetime: fields.append("datetime = ?"); values.append(data.datetime)
    if data.party_size: fields.append("party_size = ?"); values.append(int(data.party_size))
    if data.table_number: fields.append("table_number = ?"); values.append(data.table_number)
    if data.notes is not None: fields.append("notes = ?"); values.append(data.notes)
    if data.status: fields.append("status = ?"); values.append(data.status)

    if not fields:
        fields.append("status = ?")
        values.append("updated")

    values.append(data.reservation_id)

    conn = get_db()
    conn.execute(f"UPDATE reservations SET {', '.join(fields)} WHERE reservation_id = ?", tuple(values))
    conn.commit()
    conn.close()

    await notify({"type": "refresh"})
    return {"message": "updated"}


@app.post("/cancelReservation")
async def cancel_reservation(data: CancelReservation):
    conn = get_db()
    conn.execute("UPDATE reservations SET status='cancelled' WHERE reservation_id=?", (data.reservation_id,))
    conn.commit()
    conn.close()

    await notify({"type": "refresh"})
    return {"message": "cancelled"}


@app.post("/resetReservations")
async def reset_reservations():
    conn = get_db()
    conn.execute("DELETE FROM reservations")
    conn.commit()
    conn.close()
    return {"message": "✅ all reservations cleared"}


# ---------------- WhatsApp webhook → AI → DB ----------------
@app.post("/whatsapp")
async def whatsapp_webhook(Body: str = Form(...)):
    print("📩 Incoming WhatsApp:", Body)

    prompt = """
You are an AI restaurant reservation assistant.
Extract a reservation and return ONLY JSON.
If missing info, return ONLY: {"ask":"<question>"}.
JSON fields: customer_name, customer_email, contact_phone, party_size, datetime, table_number, notes.
"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": Body}
            ]
        )

        output = resp.choices[0].message.content.strip()
        if output.startswith("```"):
            output = output.replace("```json", "").replace("```", "").strip()

        data = json.loads(output)

    except Exception as e:
        print("❌ Parsing error:", e)
        return Response(
            content="<Response><Message>Sorry, I didn’t catch that. Can you repeat?</Message></Response>",
            media_type="application/xml"
        )

    if "ask" in data:
        return Response(
            content=f"<Response><Message>{data['ask']}</Message></Response>",
            media_type="application/xml"
        )

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


# ---------------- WebSocket (dashboard live refresh) ----------------
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
