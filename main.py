from fastapi import FastAPI, Request, WebSocket, Form
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta
from pydantic import BaseModel
from supabase import create_client, Client
import os
import json
import re

app = FastAPI()

# STATIC + TEMPLATES -------------------------------------------------------
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# SUPABASE INIT ------------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE = os.getenv("SUPABASE_SERVICE_ROLE")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE)

TABLE_LIMIT = 10  # Number of tables available

# MODELS -------------------------------------------------------------------
class Reservation(BaseModel):
    customer_name: str
    datetime: str
    party_size: int
    customer_email: str = None
    contact_phone: str = None
    notes: str = None

def parse_whatsapp_message(msg: str):
    """Extract name, date, time, and party size using regex."""
    name = None
    party = None

    # Detect party size
    party_match = re.search(r"(\d+)\s*(people|person|pax)?", msg, re.IGNORECASE)
    if party_match:
        party = int(party_match.group(1))

    # Detect date (YYYY-MM-DD or "tomorrow" or weekday name)
    if "tomorrow" in msg.lower():
        date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        date_match = re.search(r"\d{4}-\d{2}-\d{2}", msg)
        date = date_match.group(0) if date_match else None

    # Detect time
    time_match = re.search(r"(\d{1,2}(:\d{2})?\s?(am|pm)?)", msg, re.IGNORECASE)
    time = time_match.group(1) if time_match else None

    # Detect name after "under"
    name_match = re.search(r"under\s+([A-Za-z]+)", msg, re.IGNORECASE)
    if name_match:
        name = name_match.group(1)

    if not (name and date and time and party):
        return None

    # Convert time to 24h and build datetime string
    dt_obj = datetime.strptime(f"{date} {time}", "%Y-%m-%d %I:%M%p")
    formatted_dt = dt_obj.isoformat()

    return {
        "customer_name": name,
        "datetime": formatted_dt,
        "party_size": party,
        "contact_phone": None,
        "customer_email": None,
        "notes": None
    }

def assign_table(date_time: str):
    """Assign first available table (T1–T10)."""
    result = supabase.table("reservations") \
        .select("table_number") \
        .eq("datetime", date_time).execute()

    used = {row["table_number"] for row in result.data}

    for i in range(1, TABLE_LIMIT + 1):
        table = f"T{i}"
        if table not in used:
            return table
    return None

@app.get("/", response_class=HTMLResponse)
def home():
    return "<h3>✅ Backend Running</h3><p>Open <a href='/dashboard'>/dashboard</a></p>"

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    reservations = supabase.table("reservations").select("*").order("datetime", desc=True).execute().data

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "reservations": reservations,
        "parse_dt": datetime.fromisoformat,
        "timedelta": timedelta
    })

# WEBSOCKET ----------------------------------------------------------------
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

# WHATSAPP WEBHOOK ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    data = await request.form()
    msg = data.get("Body", "")

    parsed = parse_whatsapp_message(msg)

    if not parsed:
        return JSONResponse({"message": "✅ I can book your reservation.\n\nSend:\nName, date (YYYY-MM-DD), time, people"})

    table = assign_table(parsed["datetime"])
    if not table:
        return JSONResponse({"message": "❌ No tables available at that time."})

    parsed["table_number"] = table
    parsed["status"] = "confirmed"

    supabase.table("reservations").insert(parsed).execute()
    await notify({"type": "refresh"})

    return JSONResponse({"message": f"✅ Reservation confirmed!\n👤 {parsed['customer_name']}\n👥 {parsed['party_size']} people\n🕒 {parsed['datetime']}\n🍽 Table {table}"})


print("✅ TWILIO-ONLY RESERVATION BOT IS READY")
