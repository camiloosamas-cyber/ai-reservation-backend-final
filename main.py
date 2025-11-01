from fastapi import FastAPI, Request, WebSocket, Form
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta
from supabase import create_client, Client
from twilio.twiml.messaging_response import MessagingResponse  # ✅ REQUIRED FOR WHATSAPP REPLY
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

TABLE_LIMIT = 10  # T1–T10

# -------------------------------------------------
# HELPERS
# -------------------------------------------------
def format_datetime(dt_str: str):
    dt = datetime.fromisoformat(dt_str.replace("Z", ""))
    return dt.strftime("%A — %I:%M %p")


def assign_table(res_date: str):
    """Returns first available table number T1-T10 for the given datetime"""
    existing = supabase.table("reservations") \
        .select("table_number") \
        .eq("datetime", res_date).execute()

    used_tables = {row["table_number"] for row in existing.data}

    for i in range(1, TABLE_LIMIT + 1):
        table_name = f"T{i}"
        if table_name not in used_tables:
            return table_name

    return None  # fully booked


def parse_whatsapp_message(raw: str):
    """Extracts reservation info from a sentence like:
       'Reservation under Daniel, 4 people tomorrow at 8pm'
    """
    raw = raw.lower()

    if "reservation" not in raw:
        return None

    try:
        # Basic parsing, enough to extract core info
        parts = raw.replace("reservation under", "").strip().split(" ")

        name = parts[0].capitalize()
        people = int(parts[2])

        # convert date/time
        today = datetime.now()
        if "tomorrow" in raw:
            date = today + timedelta(days=1)
        else:
            date = today

        # extract time
        time_str = parts[-1].replace("pm", "").replace("am", "")
        hour = int(time_str)
        if "pm" in raw:
            hour += 12

        final_dt = date.replace(hour=hour, minute=0, second=0, microsecond=0)

        return {
            "customer_name": name,
            "customer_email": None,
            "contact_phone": None,
            "datetime": final_dt.isoformat(),
            "party_size": people,
            "notes": None
        }

    except Exception:
        return None


# -------------------------------------------------
# ROUTES
# -------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home():
    return "<h3>✅ Backend Running</h3><p>Open <a href='/dashboard'>/dashboard</a></p>"


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    result = supabase.table("reservations").select("*").order("datetime", desc=True).execute()
    reservations = result.data

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "reservations": reservations,
            "parse_dt": datetime.fromisoformat,
            "timedelta": timedelta
        },
    )


@app.post("/whatsapp")  # ✅ Twilio points HERE
async def whatsapp_webhook(request: Request):
    data = await request.form()
    message = data.get("Body", "")

    print("Incoming WhatsApp:", message)

    parsed = parse_whatsapp_message(message)

    if not parsed:
        reply = (
            "✅ I can book your reservation.\n\n"
            "Send:\nName, date (YYYY-MM-DD), time, people\n\n"
            "Example:\nReservation under Daniel, 4 people tomorrow at 8pm"
        )
    else:
        table = assign_table(parsed["datetime"])
        if not table:
            reply = "❌ No tables available at that time."
        else:
            parsed["table_number"] = table
            parsed["status"] = "confirmed"

            supabase.table("reservations").insert(parsed).execute()
            await notify({"type": "refresh"})

            readable = format_datetime(parsed["datetime"])

            reply = (
                f"✅ Reservation confirmed!\n"
                f"👤 {parsed['customer_name']}\n"
                f"👥 {parsed['party_size']} people\n"
                f"🗓 {readable}\n"
                f"🍽 Table {table}"
            )

    twiml = MessagingResponse()
    twiml.message(reply)
    return Response(content=str(twiml), media_type="application/xml")  # ✅ REQUIRED FOR TWILIO


# -------------------------------------------------
# WEBSOCKET REFRESH
# -------------------------------------------------
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
