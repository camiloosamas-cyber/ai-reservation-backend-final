from fastapi import FastAPI, Request, WebSocket, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta
import json, os, asyncio
import dateparser

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
    """ Converts natural language datetime ('tomorrow at 8pm') → ISO 8601 """
    try:
        parsed = dateparser.parse(dt_str)
        return parsed.isoformat()
    except:
        return None


def assign_table(date_str: str):
    """ Returns first free table at that datetime """
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
    """ Save reservation to DB with auto table selecting """

    iso_dt = data.get("datetime")

    if not iso_dt or "T" not in iso_dt:
        iso_dt = to_iso(iso_dt)

    if not iso_dt:
        return "❌ Invalid date/time. Please specify a date AND time."

    # ✅ prevent double insert (only insert if NOT exists)
    existing = supabase.table("reservations") \
        .select("reservation_id") \
        .eq("customer_name", data["customer_name"]) \
        .eq("datetime", iso_dt) \
        .eq("party_size", data["party_size"]) \
        .execute()

    if existing.data:
        return "✅ Reservation already exists."

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
# ROUTES
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    return "<h3>✅ Backend running</h3><p>Go to /dashboard</p>"


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    res = supabase.table("reservations").select("*").order("datetime", desc=True).execute()
    reservations = res.data or []

    total = len(reservations)
    cancelled = len([r for r in reservations if r.get("status") == "cancelled"])
    week_ago = datetime.now() - timedelta(days=7)

    def valid_dt(r):
        try:
            return datetime.fromisoformat(r["datetime"])
        except:
            return None

    weekly_count = len([r for r in reservations if valid_dt(r) and valid_dt(r) > week_ago])

    party_sizes = [int(r["party_size"]) for r in reservations if r.get("party_size")]
    avg_party_size = round(sum(party_sizes) / len(party_sizes), 1) if party_sizes else 0

    times = []
    for r in reservations:
        dt = valid_dt(r)
        if dt:
            times.append(dt.strftime("%H:%M"))

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
# WHATSAPP AI RESERVATION WEBHOOK  ✅ FIXED PROMPT
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp_webhook(Body: str = Form(...)):
    print("📩 Incoming:", Body)

    resp = MessagingResponse()

    prompt = """
You are an information extractor. DO NOT be conversational.

Your ONLY task:
✅ Read user's message
✅ Extract reservation info
✅ Return ONE JSON object only

FORMAT (ALWAYS):
{
 "customer_name": "",
 "customer_email": "",
 "contact_phone": "",
 "party_size": "",
 "datetime": "",
 "notes": ""
}

Rules:
- Convert natural language date/time → ISO 8601 ("2025-01-24T20:00:00")
- If something is missing return ONLY:
  {"ask":"What is your <missing field>?"}
- NEVER ask for data that already exists
- NEVER send anything other than JSON
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

    except Exception as e:
        print("❌ JSON Parse Error:", e)
        resp.message("❌ Try again, I couldn't understand.")
        return Response(content=str(resp), media_type="application/xml")

    if "ask" in data:
        resp.message(data["ask"])
        return Response(content=str(resp), media_type="application/xml")

    confirmation_msg = save_reservation(data)
    resp.message(confirmation_msg)

    asyncio.create_task(notify_refresh())
    return Response(content=str(resp), media_type="application/xml")


# ---------------------------------------------------------
# DASHBOARD API
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
# WEBSOCKETS: LIVE AUTO REFRESH
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
