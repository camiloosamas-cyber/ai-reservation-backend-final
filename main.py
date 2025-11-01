from fastapi import FastAPI, Request, WebSocket, Form
from fastapi.responses import HTMLResponse, JSONResponse, Response
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
    dt = datetime.fromisoformat(dt_str.replace("Z",""))
    return dt.strftime("%A — %I:%M %p")  # Example: Saturday — 07:30 PM

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

    readable_date = format_datetime(data.datetime)

    reply = (
        f"✅ *Reservation confirmed!*\n"
        f"👤 *Name:* {data.customer_name}\n"
        f"👥 *People:* {data.party_size}\n"
        f"🗓 *Date:* {readable_date}\n"
        f"🍽 *Table:* {table_assigned}"
    )

    return {"message": reply, "status": "created"}


@app.post("/whatsapp")
async def whatsapp_webhook(Body: str = Form(...)):
    print("Incoming WhatsApp:", Body)

    try:
        # ✅ Try to parse JSON from Chatbase webhook
        parsed = json.loads(Body)

        reservation = CreateReservation(**parsed)
        response = await create_reservation(reservation)

        return JSONResponse(response)

    except Exception as e:
        print("Not JSON, Chat message instead:", e)

        # ✅ reply back to WhatsApp normally (NOT XML)
        return JSONResponse({
            "message": "Sure ✅ Please tell me:\n\n• Name\n• Date (YYYY-MM-DD)\n• Time\n• People"
        })


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
