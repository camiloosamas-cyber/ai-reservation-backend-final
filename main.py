from fastapi import FastAPI, Request, WebSocket, Form, Query
from fastapi.responses import HTMLResponse, Response, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import json, os, asyncio, time
import dateparser  # natural language datetime parser

# ✅ Supabase
from supabase import create_client, Client

# ✅ OpenAI (WhatsApp JSON extraction)
from openai import OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ✅ Twilio (WhatsApp + Voice + Outbound Calls)
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")  # The number you bought

twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)



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
# TIMEZONE
# ---------------------------------------------------------
LOCAL_TZ_NAME = os.getenv("LOCAL_TZ", "America/Bogota")
LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)


def safe_fromiso(val: str):
    try:
        if not val:
            return None
        if val.endswith("Z"):
            val = val.replace("Z", "+00:00")
        return datetime.fromisoformat(val)
    except:
        return None


def normalize_to_utc(dt_str: str | None) -> str | None:
    if not dt_str:
        return None

    parsed = dateparser.parse(
        dt_str,
        settings={
            "PREFER_DATES_FROM": "future",
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TIMEZONE": LOCAL_TZ_NAME,
            "TO_TIMEZONE": "UTC"
        }
    )

    if not parsed:
        iso_try = safe_fromiso(dt_str)
        if iso_try:
            parsed = iso_try

    if not parsed:
        return None

    dtu = parsed.astimezone(timezone.utc)
    return dtu.isoformat().replace("+00:00", "Z")


def utc_to_local(iso_utc: str | None) -> str:
    dtu = safe_fromiso(iso_utc or "")
    if not dtu:
        return ""
    return dtu.astimezone(LOCAL_TZ).isoformat()


def readable_local(iso_utc: str | None) -> str:
    dtu = safe_fromiso(iso_utc or "")
    if not dtu:
        return "Invalid time"
    return dtu.astimezone(LOCAL_TZ).strftime("%A %I:%M %p")



# ---------------------------------------------------------
# SUPABASE
# ---------------------------------------------------------
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE"),
)

TABLE_LIMIT = 10



# ---------------------------------------------------------
# SAVE RESERVATION LOGIC
# ---------------------------------------------------------
recent_keys = {}
TTL = 60  # seconds


def dedupe(key: str):
    now = time.time()
    for k in list(recent_keys.keys()):
        if recent_keys[k] <= now:
            del recent_keys[k]

    if key in recent_keys and recent_keys[key] > now:
        return True

    recent_keys[key] = now + TTL
    return False


def assign_table(iso_utc: str) -> str | None:
    booked = supabase.table("reservations").select("table_number").eq("datetime", iso_utc).execute()
    taken = {row["table_number"] for row in (booked.data or [])}

    for i in range(1, TABLE_LIMIT + 1):
        t = f"T{i}"
        if t not in taken:
            return t
    return None


def save_reservation(data: dict):
    iso_utc = normalize_to_utc(data.get("datetime"))
    if not iso_utc:
        return "❌ Invalid date/time. Please specify date AND time."

    name = data.get("customer_name", "").strip() or "Guest"
    dedupe_key = f"{name.lower()}|{iso_utc}"

    if dedupe(dedupe_key):
        return f"ℹ️ Already confirmed.\n👤 {name}"

    table = assign_table(iso_utc)
    if not table:
        return "❌ No tables available at that time."

    supabase.table("reservations").insert({
        "customer_name": name,
        "customer_email": data.get("customer_email", ""),
        "contact_phone": data.get("contact_phone", ""),
        "datetime": iso_utc,
        "party_size": int(data.get("party_size", 1)),
        "table_number": table,
        "notes": data.get("notes", ""),
        "status": "confirmed"
    }).execute()

    return (
        "✅ Reservation confirmed!\n"
        f"👤 {name}\n"
        f"👥 {data.get('party_size', 1)} people\n"
        f"🗓 {readable_local(iso_utc)}\n"
        f"🍽 Table: {table}"
    )



# ---------------------------------------------------------
# HOME + DASHBOARD
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    return "<h3>✅ Backend running</h3><p>Go to /dashboard</p>"


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    res = supabase.table("reservations").select("*").order("datetime", desc=True).execute()
    rows = res.data or []

    view = []
    for r in rows:
        r["datetime"] = utc_to_local(r.get("datetime"))
        view.append(r)

    total = len(view)
    cancelled = len([r for r in view if r.get("status") == "cancelled"])

    now = datetime.now(LOCAL_TZ)
    week_ago = now - timedelta(days=7)

    weekly_count = len([r for r in view if safe_fromiso(r.get("datetime") or "").astimezone(LOCAL_TZ) > week_ago])
    avg_party_size = round(sum(int(r.get("party_size", 0)) for r in view) / total, 1) if total else 0

    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "reservations": view, "weekly_count": weekly_count, "avg_party_size": avg_party_size}
    )



# ---------------------------------------------------------
# WHATSAPP WEBHOOK (working)
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp_webhook(Body: str = Form(...)):
    resp = MessagingResponse()

    extraction_prompt = """
You are an AI reservation assistant. Extract structured reservation data.

⬇️ RETURN ONLY JSON (no text around it)
{
 "customer_name": "",
 "customer_email": "",
 "contact_phone": "",
 "party_size": "",
 "datetime": "",
 "notes": "",
 "ask": ""
}
"""

    try:
        result = client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0,
            messages=[{"role": "system", "content": extraction_prompt},
                      {"role": "user", "content": Body}]
        )

        output = result.choices[0].message.content.strip()
        if output.startswith("```"):
            output = output.replace("```json", "").replace("```", "").strip()

        data = json.loads(output)

    except:
        resp.message("❌ Sorry, I couldn't understand that. Try again.")
        return Response(content=str(resp), media_type="application/xml")

    if data.get("ask"):
        resp.message(data["ask"])
        return Response(content=str(resp), media_type="application/xml")

    msg = save_reservation(data)
    resp.message(msg)

    asyncio.create_task(notify_refresh())
    return Response(content=str(resp), media_type="application/xml")



# ---------------------------------------------------------
# ✅ OUTBOUND CALL TRIGGER (NEW)
# ---------------------------------------------------------
@app.get("/call")
async def make_test_call(to: str):
    """Trigger a call from your Twilio number to a given number."""
    try:
        call = twilio_client.calls.create(
            to=to,
            from_=TWILIO_PHONE_NUMBER,
            url="https://ai-reservation-backend-final.onrender.com/voice"
        )
        return {"status": "queued", "sid": call.sid}

    except Exception as e:
        return {"error": str(e)}



# ---------------------------------------------------------
# VOICE FLOW — CALL BOOKING
# ---------------------------------------------------------
@app.post("/voice", response_class=PlainTextResponse)
async def voice_welcome():
    vr = VoiceResponse()
    vr.say("Hi! I can book your table. What is your name?", voice="alice", language="en-US")
    vr.gather(input="speech", action="/voice/name", method="POST")
    return Response(content=str(vr), media_type="application/xml")


@app.post("/voice/name")
async def voice_name(request: Request):
    form = await request.form()
    name = form.get("SpeechResult", "Guest")
    vr = VoiceResponse()
    vr.gather(input="speech",
              action=f"/voice/party?name={name}",
              method="POST").say(f"Nice to meet you {name}. For how many people?")
    return Response(content=str(vr), media_type="application/xml")


@app.post("/voice/party")
async def voice_party(request: Request, name: str):
    form = await request.form()
    party = form.get("SpeechResult", "1")
    vr = VoiceResponse()
    vr.gather(input="speech",
              action=f"/voice/datetime?name={name}&party={party}",
              method="POST").say("What date and time should I book?")
    return Response(content=str(vr), media_type="application/xml")


@app.post("/voice/datetime")
async def voice_datetime(request: Request, name: str, party: str):
    form = await request.form()
    dt = form.get("SpeechResult", "")
    payload = {"customer_name": name, "party_size": party, "datetime": dt}
    msg = save_reservation(payload)

    vr = VoiceResponse()
    vr.say(msg.replace("\n", ". "))
    vr.say("Thanks, goodbye.")
    vr.hangup()

    asyncio.create_task(notify_refresh())
    return Response(content=str(vr), media_type="application/xml")



# ---------------------------------------------------------
# UPDATE / CANCEL (Dashboard)
# ---------------------------------------------------------
@app.post("/updateReservation")
async def update_reservation(update: dict):
    normalized = normalize_to_utc(update.get("datetime"))
    supabase.table("reservations").update({
        "datetime": normalized if normalized else update.get("datetime"),
        "party_size": update.get("party_size"),
        "table_number": update.get("table_number"),
        "notes": update.get("notes"),
        "status": update.get("status", "updated"),
    }).eq("reservation_id", update["reservation_id"]).execute()

    asyncio.create_task(notify_refresh())
    return {"success": True}


@app.post("/cancelReservation")
async def cancel_reservation(update: dict):
    supabase.table("reservations").update({"status": "cancelled"}).eq(
        "reservation_id", update["reservation_id"]
    ).execute()

    asyncio.create_task(notify_refresh())
    return {"success": True}



# ---------------------------------------------------------
# REALTIME REFRESH
# ---------------------------------------------------------
clients = []


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except:
        if websocket in clients:
            clients.remove(websocket)


async def notify_refresh():
    for ws in list(clients):
        try:
            await ws.send_text("refresh")
        except:
            try:
                clients.remove(ws)
            except:
                pass
