from fastapi import FastAPI, Request, WebSocket, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta
import json, os, asyncio
import dateparser  # natural language datetime parser

# ✅ Supabase
from supabase import create_client, Client

# ✅ OpenAI
from openai import OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ✅ Twilio
from twilio.twiml.messaging_response import MessagingResponse


# ---------------------------------------------------------
# APP INIT
# ---------------------------------------------------------
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

# ---------------------------------------------------------
# SUPABASE INIT
# ---------------------------------------------------------
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE"),
)

TABLE_LIMIT = 10


def to_iso(dt_str: str):
    """ Converts natural language datetime ('tomorrow at 8pm') → ISO """
    try:
        parsed = dateparser.parse(dt_str)
        return parsed.isoformat()
    except:
        return None


def assign_table(date_str: str):
    """ Returns first available table at datetime """
    booked = supabase.table("reservations") \
        .select("table_number") \
        .eq("datetime", date_str).execute()

    taken = {row["table_number"] for row in booked.data}

    for i in range(1, TABLE_LIMIT + 1):
        t = f"T{i}"
        if t not in taken:
            return t

    return None


def save_reservation(data):
    """ Save to DB with automatic table selection """

    iso_dt = data.get("datetime")

    if not iso_dt or "T" not in iso_dt:
        iso_dt = to_iso(iso_dt)

    if not iso_dt:
        return "❌ Invalid date/time. Please specify date AND time."

    table = assign_table(iso_dt)
    if not table:
        return "❌ No tables available at that time."

    supabase.table("reservations").insert({
        "customer_name": data["customer_name"],
        "customer_email": data.get("customer_email", ""),
        "contact_phone": data.get("contact_phone", ""),
        "datetime": iso_dt,
        "party_size": int(data["party_size"]),
        "table_number": table,
        "notes": data.get("notes", ""),
        "status": "confirmed"
    }).execute()

    readable = datetime.fromisoformat(iso_dt).strftime("%A %I:%M %p")

    return (
        f"✅ Reservation confirmed!\n"
        f"👤 {data['customer_name']}\n"
        f"👥 {data['party_size']} people\n"
        f"🗓 {readable}\n"
        f"🍽 Table: {table}"
    )


# ---------------------------------------------------------
# HOMEPAGE
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    return "<h3>✅ Backend running</h3><p>Go to /dashboard</p>"


# ---------------------------------------------------------
# DASHBOARD (with analytics + crash proof)
# ---------------------------------------------------------
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    res = supabase.table("reservations").select("*").order("datetime", desc=True).execute()
    reservations = res.data or []

    # KPI analytics
    total = len(reservations)
    cancelled = len([r for r in reservations if r.get("status") == "cancelled"])
    week_ago = datetime.now() - timedelta(days=7)
    weekly_count = len([r for r in reservations if r.get("datetime") and datetime.fromisoformat(r["datetime"]) > week_ago])

    party_sizes = [int(r["party_size"]) for r in reservations if r.get("party_size")]
    avg_party_size = round(sum(party_sizes) / len(party_sizes), 1) if party_sizes else 0

    times = []
    for r in reservations:
        try:
            dt = datetime.fromisoformat(r["datetime"])
            times.append(dt.strftime("%H:%M"))
        except:
            pass

    peak_time = max(set(times), key=times.count) if times else "N/A"
    cancel_rate = round((cancelled / total) * 100, 1) if total else 0

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "reservations": reservations,
            "weekly_count": weekly_count,
            "avg_party_size": avg_party_size,
            "peak_time": peak_time,
            "cancel_rate": cancel_rate,
        },
    )


# ---------------------------------------------------------
# WHATSAPP AI WEBHOOK
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp_webhook(Body: str = Form(...)):

    print("📩 Incoming:", Body)

    resp = MessagingResponse()

    prompt = """
Extract reservation details and return valid JSON ONLY.
Convert natural language date → ISO 8601.

{
 "customer_name": "",
 "customer_email": "",
 "contact_phone": "",
 "party_size": "",
 "datetime": "",
 "notes": ""
}

If ANYTHING missing → return ONLY:
{"ask":"<question>"}
"""

    try:
        result = client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": Body},
            ]
        )

        output = result.choices[0].message.content.strip()
        if output.startswith("```"):
            output = output.replace("```json", "").replace("```", "").strip()

        data = json.loads(output)

    except:
        resp.message("❌ I couldn’t understand that. Try again.")
        return Response(content=str(resp), media_type="application/xml")

    if "ask" in data:
        resp.message(data["ask"])
        return Response(content=str(resp), media_type="application/xml")

    confirmation_msg = save_reservation(data)
    resp.message(confirmation_msg)

    asyncio.create_task(notify_refresh())
    return Response(content=str(resp), media_type="application/xml")


# ---------------------------------------------------------
# DASHBOARD API (Edit, create, cancel)
# ---------------------------------------------------------
@app.post("/createReservation")
async def create_reservation(payload: dict):
    msg = save_reservation(payload)
    asyncio.create_task(notify_refresh())
    return {"success": True, "message": msg}


@app.post("/updateReservation")
async def update_reservation(update: dict):

    supabase.table("reservations") \
        .update({
            "datetime": update.get("datetime"),
            "party_size": update.get("party_size"),
            "table_number": update.get("table_number"),
            "notes": update.get("notes"),
            "status": update.get("status", "updated"),
        }) \
        .eq("reservation_id", update["reservation_id"]) \
        .execute()

    asyncio.create_task(notify_refresh())
    return {"success": True}


@app.post("/cancelReservation")
async def cancel(update: dict):

    supabase.table("reservations") \
        .update({"status": "cancelled"}) \
        .eq("reservation_id", update["reservation_id"]) \
        .execute()

    asyncio.create_task(notify_refresh())
    return {"success": True}


# ---------------------------------------------------------
# WEBSOCKET LIVE REFRESH
# ---------------------------------------------------------
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


async def notify_refresh():
    for ws in clients:
        try:
            await ws.send_text("refresh")
        except:
            pass
