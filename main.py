# ---------------------------------------------------------
# ✅ AI RESERVATION SYSTEM — Stable + FAST Voice Flow
# ---------------------------------------------------------

from fastapi import FastAPI, Request, WebSocket, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote
import json, os, asyncio, time, re
import dateparser

# ---------- Supabase ----------
from supabase import create_client, Client

# ---------- OpenAI ----------
from openai import OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ---------- Twilio ----------
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse, Gather

TWILIO_ACCOUNT_SID   = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN    = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER  = os.getenv("TWILIO_PHONE_NUMBER")
PUBLIC_BASE_URL      = os.getenv("PUBLIC_BASE_URL", "https://ai-reservation-backend-final.onrender.com")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM") or "whatsapp:+14155238886"

twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ---------- Reminder Config ----------
REMINDER_HOURS     = float(os.getenv("REMINDER_HOURS", "2"))
REMINDER_GRACE_SEC = int(os.getenv("REMINDER_GRACE_SEC", "300"))

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
# TIMEZONE + HELPERS
# ---------------------------------------------------------
LOCAL_TZ_NAME = os.getenv("LOCAL_TZ", "America/Bogota")
LOCAL_TZ      = ZoneInfo(LOCAL_TZ_NAME)

def _safe_fromiso(s: str):
    try:
        if not s:
            return None
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except:
        return None

def _utc_iso_to_local_iso(iso_utc: str | None):
    dtu = _safe_fromiso(iso_utc or "")
    if not dtu:
        return None
    if dtu.tzinfo is None:
        dtu = dtu.replace(tzinfo=timezone.utc)
    return dtu.astimezone(LOCAL_TZ).isoformat()

def _readable_local(iso_utc: str | None) -> str:
    dtu = _safe_fromiso(iso_utc or "")
    if not dtu:
        return "Invalid time"
    if dtu.tzinfo is None:
        dtu = dtu.replace(tzinfo=timezone.utc)
    return dtu.astimezone(LOCAL_TZ).strftime("%A %I:%M %p")

def _norm_name(name: str | None) -> str:
    return (name or "").strip().casefold()

# ---------------------------------------------------------
# INPUT CLEANERS
# ---------------------------------------------------------
def clean_name_input(text: str) -> str:
    text = text.lower()
    for phrase in ["my name is", "i am", "i'm", "this is", "name is"]:
        text = text.replace(phrase, "")
    text = re.sub(r"[^a-zA-ZáéíóúüñÁÉÍÓÚÜÑ ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.title()

def clean_datetime_input(text: str) -> str:
    text = text.lower()
    text = re.sub(r"\bat\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def _explicit_year_in(text: str) -> bool:
    return bool(re.search(r"\b20\d{2}\b", text))

def _to_utc_iso_current_year(dt_str: str | None) -> str | None:
    if not dt_str:
        return None

    now_local = datetime.now(LOCAL_TZ)

    parsed = dateparser.parse(
        dt_str,
        settings={
            "PREFER_DATES_FROM": "future",
            "PREFER_DAY_OF_MONTH": "current",
            "RELATIVE_BASE": now_local,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TIMEZONE": LOCAL_TZ_NAME,
            "TO_TIMEZONE": "UTC",
        },
    )
    if not parsed:
        return None

    if not _explicit_year_in(dt_str):
        parsed = parsed.replace(year=now_local.year)

    return parsed.isoformat().replace("+00:00", "Z")

# ---------------------------------------------------------
# SUPABASE
# ---------------------------------------------------------
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE"))

TABLE_LIMIT = 10
_recent_keys: dict[str, float] = {}
IDEMPOTENCY_TTL = 60

def _cache_check_and_add(key: str) -> bool:
    now = time.time()
    if key in _recent_keys and _recent_keys[key] > now:
        return True
    _recent_keys[key] = now + IDEMPOTENCY_TTL
    return False

def assign_table(iso_utc: str):
    booked = supabase.table("reservations").select("table_number").eq("datetime", iso_utc).execute()
    taken = {row["table_number"] for row in (booked.data or [])}
    for i in range(1, TABLE_LIMIT + 1):
        t = f"T{i}"
        if t not in taken:
            return t
    return None

def save_reservation(data: dict) -> str:
    iso_utc = _to_utc_iso_current_year(data.get("datetime"))
    if not iso_utc:
        return "❌ Invalid time. Please specify date AND time."

    name = (data.get("customer_name") or "Guest").strip()
    key  = f"{_norm_name(name)}|{iso_utc}"

    if _cache_check_and_add(key):
        return f"ℹ️ Already booked."

    table = data.get("table_number") or assign_table(iso_utc)
    if not table:
        return "❌ No tables available."

    supabase.table("reservations").insert({
        "customer_name": name,
        "contact_phone": data.get("contact_phone", ""),
        "datetime": iso_utc,
        "party_size": int(data.get("party_size", 1)),
        "table_number": table,
        "notes": data.get("notes", "") or "",
        "status": "confirmed"
    }).execute()

    return (
        "✅ Reservation confirmed!\n"
        f"👤 {name}\n"
        f"👥 {data.get('party_size', 1)} people\n"
        f"🗓 {_readable_local(iso_utc)}\n"
        f"🍽 Table: {table}"
    )

# ---------------------------------------------------------
# DASHBOARD (unchanged)
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    return "<h3>✅ Backend running</h3><p>Go to /dashboard</p>"

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    rows = supabase.table("reservations").select("*").order("datetime", desc=True).execute().data or []
    return templates.TemplateResponse("dashboard.html", {"request": request, "reservations": rows})

# ---------------------------------------------------------
# ✅ WHATSAPP WEBHOOK (unchanged)
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    form = await request.form()
    Body = form.get("Body", "")
    wa_id = form.get("WaId", "").strip()

    phone = wa_id if wa_id.startswith("+") else f"+{wa_id}"

    resp = MessagingResponse()

    data = {
        "customer_name": "Guest",
        "party_size": "1",
        "datetime": clean_datetime_input(Body),
        "contact_phone": phone,
        "notes": ""
    }

    msg = save_reservation(data)
    resp.message(msg)
    asyncio.create_task(notify_refresh())
    return Response(content=str(resp), media_type="application/xml")

# ---------------------------------------------------------
# ✅ FAST VOICE CALL FLOW (FIXED)
# ---------------------------------------------------------
@app.get("/call")
async def make_test_call(to: str):
    try:
        call = twilio_client.calls.create(
            to=to,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{PUBLIC_BASE_URL}/voice",
        )
        return {"status": "queued", "sid": call.sid}
    except Exception as e:
        return {"error": str(e)}


@app.post("/voice")
async def voice_welcome():
    vr = VoiceResponse()
    gather = Gather(
        input="speech",
        speech_timeout="auto",
        action="/voice/name",
        method="POST"
    )
    gather.say("Hi! I can book your table. What is your name?", voice="Polly.Joanna-Neural", language="en-US")
    vr.append(gather)
    return Response(str(vr), media_type="application/xml")


@app.post("/voice/name")
async def voice_name(request: Request):
    form = await request.form()
    caller = form.get("Caller", "")
    phone = caller if caller.startswith("+") else f"+{caller}" if caller else ""

    name = clean_name_input(form.get("SpeechResult") or "Guest")

    vr = VoiceResponse()
    gather = Gather(
        input="speech",
        speech_timeout="auto",
        action=f"/voice/party?name={quote(name)}&phone={quote(phone)}",
        method="POST"
    )
    gather.say(f"Nice to meet you {name}. How many people should I book the table for?", voice="Polly.Joanna-Neural")
    vr.append(gather)
    return Response(str(vr), media_type="application/xml")


@app.post("/voice/party")
async def voice_party(request: Request, name: str, phone: str):
    form = await request.form()
    spoken = (form.get("SpeechResult") or "").lower()

    numbers = {"one":"1","two":"2","three":"3","four":"4","five":"5","six":"6","seven":"7","eight":"8","nine":"9","ten":"10"}
    party = "1"
    for word, num in numbers.items():
        if word in spoken:
            party = num

    vr = VoiceResponse()
    gather = Gather(
        input="speech",
        speech_timeout="auto",
        action=f"/voice/datetime?name={quote(name)}&phone={quote(phone)}&party={party}",
        method="POST"
    )
    gather.say("What date and time should I book?", voice="Polly.Joanna-Neural")
    vr.append(gather)
    return Response(str(vr), media_type="application/xml")


@app.post("/voice/datetime")
async def voice_datetime(request: Request, name: str, phone: str, party: str):
    form = await request.form()
    spoken = clean_datetime_input(form.get("SpeechResult") or "")
    iso = _to_utc_iso_current_year(spoken)

    if not iso:
        vr = VoiceResponse()
        gather = Gather(
            input="speech",
            speech_timeout="auto",
            action=f"/voice/datetime?name={quote(name)}&phone={quote(phone)}&party={party}",
            method="POST"
        )
        gather.say("Sorry, I didn’t catch that. Try again.", voice="Polly.Joanna-Neural")
        vr.append(gather)
        return Response(str(vr), media_type="application/xml")

    vr = VoiceResponse()
    gather = Gather(
        input="speech",
        speech_timeout="auto",
        action=f"/voice/notes?name={quote(name)}&phone={quote(phone)}&party={party}&dt={quote(spoken)}",
        method="POST"
    )
    gather.say("Any notes or preferences? Say none if no.", voice="Polly.Joanna-Neural")
    vr.append(gather)
    return Response(str(vr), media_type="application/xml")


@app.post("/voice/notes")
async def voice_notes(request: Request, name: str, phone: str, party: str, dt: str):
    form = await request.form()
    notes_speech = form.get("SpeechResult") or ""
    notes = "none" if notes_speech.lower() in ["none", "no", "nothing"] else notes_speech

    payload = {
        "customer_name": name,
        "party_size": party,
        "datetime": dt,
        "notes": notes,
        "contact_phone": phone
    }

    save_reservation(payload)

    vr = VoiceResponse()
    vr.say("Perfect, I'm booking your table now.", voice="Polly.Joanna-Neural")
    vr.say("Thank you, goodbye.", voice="Polly.Joanna-Neural")
    vr.hangup()
    asyncio.create_task(notify_refresh())
    return Response(str(vr), media_type="application/xml")

# ---------------------------------------------------------
# WEBSOCKET REFRESH
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
