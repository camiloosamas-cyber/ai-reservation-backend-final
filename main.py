from fastapi import FastAPI, Request, WebSocket, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta
from twilio.twiml.messaging_response import MessagingResponse  # ✅ REQUIRED
import json, os

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
# SUPABASE
# -------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE = os.getenv("SUPABASE_SERVICE_ROLE")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE)

TABLE_LIMIT = 10  # T1–T10


# -------------------------------------------------
# MODEL
# -------------------------------------------------
class Reservation(BaseModel):
    customer_name: str
    datetime: str
    party_size: int
    customer_email: Optional[str] = None
    contact_phone: Optional[str] = None
    notes: Optional[str] = None


# -------------------------------------------------
# HELPERS
# -------------------------------------------------
def assign_table(res_date: str):
    """Pick first available table"""
    existing = supabase.table("reservations").select("table_number").eq("datetime", res_date).execute()
    used = {row["table_number"] for row in existing.data}

    for n in range(1, TABLE_LIMIT + 1):
        if f"T{n}" not in used:
            return f"T{n}"

    return None


def readable(dt_str: str):
    dt = datetime.fromisoformat(dt_str.replace("Z", ""))
    return dt.strftime("%A — %I:%M %p")  # Saturday — 07:30 PM


# -------------------------------------------------
# DASHBOARD
# -------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    return "<h3>✅ Backend Running</h3>"


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    r = supabase.table("reservations").select("*").order("datetime", desc=True).execute()
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "reservations": r.data, "timedelta": timedelta},
    )


# -------------------------------------------------
# CREATE RESERVATION (used internally)
# -------------------------------------------------
async def save_reservation(data: Reservation):
    table_assigned = assign_table(data.datetime)
    if not table_assigned:
        return "❌ No tables available at that time."

    supabase.table("reservations").insert({
        "customer_name": data.customer_name,
        "customer_email": data.customer_email,
        "contact_phone": data.contact_phone,
        "datetime": data.datetime,
        "party_size": data.party_size,
        "notes": data.notes,
        "table_number": table_assigned,
        "status": "confirmed",
    }).execute()

    return (
        f"✅ *Reservation confirmed!*\n"
        f"👤 Name: {data.customer_name}\n"
        f"👥 People: {data.party_size}\n"
        f"🗓 Date: {readable(data.datetime)}\n"
        f"🍽 Table: {table_assigned}"
    )


# -------------------------------------------------
# WHATSAPP WEBHOOK (Twilio)
# -------------------------------------------------
@app.post("/whatsapp")
async def whatsapp(Body: str = Form(...)):

    print("Incoming WhatsApp:", Body)

    resp = MessagingResponse()   # ✅ Twilio XML response builder

    # Try to parse structured JSON from your chatbot
    try:
        data = json.loads(Body)
        reservation = Reservation(**data)
        message = await save_reservation(reservation)
        resp.message(message)
        return Response(content=str(resp), media_type="application/xml")

    except:
        # Normal convo, ask for formatted message
        resp.message(
            "✅ I can book your reservation.\n\n"
            "Send:\nName, date (YYYY-MM-DD), time, people\n\n"
            "Example:\nReservation under Daniel, 4 people tomorrow at 8pm"
        )
        return Response(content=str(resp), media_type="application/xml")
