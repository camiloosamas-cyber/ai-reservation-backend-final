from fastapi import FastAPI, Request, WebSocket, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta
from supabase import create_client, Client
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

TABLE_LIMIT = 10  # ✅ restaurant has 10 tables (T1–T10)

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
        {
            "request": request,
            "reservations": result.data,
            "parse_dt": datetime.fromisoformat,
            "timedelta": timedelta
        },
    )

@app.post("/createReservation")
async def create_reservation(data: CreateReservation):

    table_assigned = assign_table(data.datetime)
    if not table_assigned:
        return {"message": "No tables available at that time 😞"}

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

    readable_date = format_datetime(data.datetime)

    reply = (
        f"✅ *Reservation confirmed!*\n"
        f"👤 *Name:* {data.customer_name}\n"
        f"👥 *People:* {data.party_size}\n"
        f"🗓 *Date:* {readable_date}\n"
        f"🍽 *Table:* {table_assigned}"
    )

    return {"message": reply, "status": "created"}

# -------------------------------------------------
# WHATSAPP WEBHOOK (TwiML response required)
# -------------------------------------------------

@app.post("/whatsapp")
async def whatsapp_webhook(Body: str = Form(...)):
    print("Incoming WhatsApp:", Body)

    try:
        # ✅ Attempt to parse JSON from Chatbase agent
        parsed = json.loads(Body)
        reservation = CreateReservation(**parsed)
        response = await create_reservation(reservation)
        reply_message = response["message"]

    except Exception:
        # ❌ Not JSON → it's just a WhatsApp message from customer
        reply_message = (
            "✅ I can book your reservation.\n\n"
            "Please send in *one message*:\n"
            "`Name, date (YYYY-MM-DD), time, people`\n\n"
            "Example:\n"
            "`Reservation under Daniel, 4 people Saturday 7:30pm`"
        )

    # ✅ WhatsApp/Twilio requires XML (TwiML), NOT JSON
    twilio_xml = f"""
<Response>
  <Message>{reply_message}</Message>
</Response>
""".strip()

    return Response(content=twilio_xml, media_type="application/xml")

# -------------------------------------------------
# WEBSOCKET REFRESH FOR DASHBOARD
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
