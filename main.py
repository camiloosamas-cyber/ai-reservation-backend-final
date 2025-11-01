from fastapi import FastAPI, Request, WebSocket, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta
from supabase import create_client, Client
import urllib.parse
import json
import os

# -------------------------------------------------
# INIT
# -------------------------------------------------
app = FastAPI()

os.chdir(os.path.dirname(os.path.abspath(__file__)))

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------------------------------
# SUPABASE CLIENT
# -------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE = os.getenv("SUPABASE_SERVICE_ROLE")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE)

TABLE_LIMIT = 10  # ✅ 10 tables (T1–T10)


# -------------------------------------------------
# MODELS
# -------------------------------------------------
class CreateReservation(BaseModel):
    customer_name: str
    customer_email: Optional[str] = None
    contact_phone: Optional[str] = None
    datetime: str
    party_size: int
    notes: Optional[str] = None


# -------------------------------------------------
# HELPERS
# -------------------------------------------------
def assign_table(res_date: str):
    existing = supabase.table("reservations") \
        .select("table_number") \
        .eq("datetime", res_date).execute()

    used = {row["table_number"] for row in existing.data}

    for i in range(1, TABLE_LIMIT + 1):
        table_name = f"T{i}"
        if table_name not in used:
            return table_name

    return None  # fully booked


def format_datetime(dt_str: str):
    dt = datetime.fromisoformat(dt_str.replace("Z", ""))
    return dt.strftime("%A — %I:%M %p")


# -------------------------------------------------
# ROUTES
# -------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    return "<h3>✅ Backend Running</h3><p>Open <a href='/dashboard'>/dashboard</a></p>"


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    result = supabase.table("reservations").select("*").order("datetime", desc=True).execute()
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "reservations": result.data, "parse_dt": datetime.fromisoformat}
    )


@app.post("/createReservation")
async def create_reservation(data: CreateReservation):

    table_assigned = assign_table(data.datetime)

    if not table_assigned:
        return JSONResponse({"message": "No tables available at that time 😞"})

    supabase.table("reservations").insert({
        "customer_name": data.customer_name,
        "customer_email": data.customer_email,
        "contact_phone": data.contact_phone,
        "datetime": data.datetime,
        "party_size": data.party_size,
        "table_number": table_assigned,
        "notes": data.notes,
        "status": "confirmed"
    }).execute()

    await notify({"type": "refresh"})

    readable = format_datetime(data.datetime)

    reply = (
        f"✅ *Reservation confirmed!*\n"
        f"👤 *Name:* {data.customer_name}\n"
        f"👥 *People:* {data.party_size}\n"
        f"🗓 *Date:* {readable}\n"
        f"🍽 *Table:* {table_assigned}"
    )

    return {"message": reply, "status": "created"}


# -------------------------------------------------
# ✅ FIXED WHATSAPP / CHATBASE ENDPOINT
# -------------------------------------------------
@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    body = (await request.body()).decode("utf-8")
    print("Incoming WhatsApp:", body)

    parsed = None

    # 🔹 Case 1: JSON
    try:
        parsed = json.loads(body)
    except:
        pass

    # 🔹 Case 2: x-www-form-urlencoded (Chatbase / Twilio style)
    if not parsed and "=" in body:
        form = urllib.parse.parse_qs(body)
        if "data" in form:  # Chatbase uses a field named 'data'
            try:
                parsed = json.loads(form["data"][0])
            except:
                pass

    # No JSON detected → fallback response
    if not parsed:
        return JSONResponse({
            "message": "✅ I can book your reservation.\nSend: Name, date (YYYY-MM-DD), time, people"
        })

    # Now create reservation
    data = CreateReservation(**parsed)
    response = await create_reservation(data)
    return JSONResponse(response)


# -------------------------------------------------
# LIVE DASHBOARD UPDATE (Websocket)
# -------------------------------------------------
clients = []

@app.websocket("/ws")
async def ws(websocket: WebSocket):
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
