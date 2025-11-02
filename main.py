from fastapi import FastAPI, Request, WebSocket, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import json, os, asyncio, time
import dateparser

from supabase import create_client, Client
from openai import OpenAI
from twilio.twiml.messaging_response import MessagingResponse

# ---------------------------------------------------------
# INIT
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

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE"),
)

# ---------------------------------------------------------
# TIMEZONE SETTINGS
# ---------------------------------------------------------
LOCAL_TZ_NAME = os.getenv("LOCAL_TZ", "America/Bogota")
LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)

TABLE_LIMIT = 10


# ---------------------------------------------------------
# UTILS
# ---------------------------------------------------------
def safe_fromiso(s: str):
    try:
        if not s:
            return None
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except:
        return None


def normalize_to_utc(dt_str: str | None) -> str | None:
    """ ALWAYS returns UTC ISO with time. Prevents dashboard crash """

    if not dt_str:
        return None

    # If ISO format
    dti = safe_fromiso(dt_str)
    if dti:
        if dti.tzinfo is None:
            dti = dti.replace(tzinfo=LOCAL_TZ)
        return dti.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    # Natural language ("tomorrow at 8pm")
    parsed = dateparser.parse(
        dt_str,
        settings={
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TIMEZONE": LOCAL_TZ_NAME,
            "TO_TIMEZONE": "UTC",
        },
    )

    if not parsed:
        return None

    # If GPT returned only date (without time), assume 8pm
    if parsed.hour == 0 and "pm" not in dt_str.lower() and "am" not in dt_str.lower():
        parsed = parsed.replace(hour=20)

    return parsed.isoformat().replace("+00:00", "Z")


def utc_to_local_readable(utc_iso):
    d = safe_fromiso(utc_iso)
    if not d:
        return "Invalid time"

    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)

    return d.astimezone(LOCAL_TZ).strftime("%A %I:%M %p")


def utc_to_local_iso(utc_iso):
    d = safe_fromiso(utc_iso)
    if not d:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(LOCAL_TZ).isoformat()


# ---------------------------------------------------------
# DE-DUPLICATION
# ---------------------------------------------------------
recent_keys = {}
TTL = 60

def idempotent(key):
    now = time.time()
    expired = [k for k, v in recent_keys.items() if v < now]
    for k in expired:
        recent_keys.pop(k, None)

    if key in recent_keys:
        return True

    recent_keys[key] = now + TTL
    return False


# ---------------------------------------------------------
# DATABASE / RESERVATION
# ---------------------------------------------------------
def assign_table(utc_iso):
    booked = supabase.table("reservations") \
        .select("table_number") \
        .eq("datetime", utc_iso).execute()

    taken = {r["table_number"] for r in (booked.data or [])}

    for i in range(1, TABLE_LIMIT + 1):
        tid = f"T{i}"
        if tid not in taken:
            return tid

    return None


def save_reservation(data: dict):
    """No duplicates - always correct time - never crashes"""

    name = data.get("customer_name", "").strip()

    utc_iso = normalize_to_utc(data.get("datetime"))
    if not utc_iso:
        return "❌ Invalid date/time. Please specify date + time."

    key = f"{name.lower()}|{utc_iso}"

    # idempotency lock
    if idempotent(key):
        return "ℹ️ Already processed."

    # Check DB if exists
    res = supabase.table("reservations") \
        .select("*") \
        .eq("datetime", utc_iso).execute()

    if res.data:
        return "ℹ️ Reservation already exists."

    table = assign_table(utc_iso)
    if not table:
        return "❌ No tables available at that time."

    supabase.table("reservations").insert({
        "customer_name": name,
        "customer_email": data.get("customer_email", ""),
        "contact_phone": data.get("contact_phone", ""),
        "datetime": utc_iso,
        "party_size": int(data.get("party_size", 1)),
        "table_number": table,
        "notes": data.get("notes", ""),
        "status": "confirmed"
    }).execute()

    readable = utc_to_local_readable(utc_iso)

    return f"""✅ Reservation confirmed!
👤 {name}
👥 {data.get('party_size', 1)} people
🗓 {readable}
🍽 Table: {table}
"""


# ---------------------------------------------------------
# ROUTES
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    return "<h3>✅ Backend running</h3><p>Go to /dashboard</p>"


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    rows = supabase.table("reservations").select("*").order("datetime", desc=True).execute().data or []

    # always convert DB UTC -> local
    for r in rows:
        r["datetime"] = utc_to_local_iso(r.get("datetime"))

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "reservations": rows,
        "weekly_count": len(rows),
        "avg_party_size": 0,
        "peak_time": "-",
        "cancel_rate": 0,
    })


@app.post("/whatsapp")
async def whatsapp_webhook(Body: str = Form(...)):
    print("📩 Incoming:", Body)
    resp = MessagingResponse()

    prompt = """
Extract ONLY JSON:
{
 "customer_name": "",
 "customer_email": "",
 "contact_phone": "",
 "party_size": "",
 "datetime": "",  
 "notes": ""
}
If missing info:
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
        resp.message("❌ I couldn’t understand that.")
        return Response(content=str(resp), media_type="application/xml")

    if "ask" in data:
        resp.message(data["ask"])
        return Response(content=str(resp), media_type="application/xml")

    resp.message(save_reservation(data))
    asyncio.create_task(notify_refresh())
    return Response(content=str(resp), media_type="application/xml")


@app.post("/createReservation")
async def create_reservation(payload: dict):
    msg = save_reservation(payload)
    asyncio.create_task(notify_refresh())
    return {"success": True, "message": msg}


@app.post("/updateReservation")
async def update_reservation(update: dict):
    new_dt = normalize_to_utc(update.get("datetime"))
    supabase.table("reservations").update({
        "datetime": new_dt,
        "party_size": update.get("party_size"),
        "table_number": update.get("table_number"),
        "notes": update.get("notes"),
        "status": "updated",
    }).eq("reservation_id", update["reservation_id"]).execute()

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
# WEBSOCKET REFRESH
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
