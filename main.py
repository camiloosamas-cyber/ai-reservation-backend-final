# ---------------------------------------------------------
# ✅ AI RESERVATION SYSTEM — FINAL | EMAIL OPTIONAL | WHATSAPP FIXED
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

# ✅ Supabase
from supabase import create_client, Client

# ✅ OpenAI
from openai import OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ✅ Twilio (Voice + WhatsApp)
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://ai-reservation-backend-final.onrender.com")

twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM") or (f"whatsapp:{TWILIO_PHONE_NUMBER}" if TWILIO_PHONE_NUMBER else None)

REMINDER_HOURS = float(os.getenv("REMINDER_HOURS", "2"))
REMINDER_GRACE_SEC = int(os.getenv("REMINDER_GRACE_SEC", "300"))  # 5 min window

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
# TIMEZONE HELPERS
# ---------------------------------------------------------
LOCAL_TZ_NAME = os.getenv("LOCAL_TZ", "America/Bogota")
LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)

def _safe_fromiso(s: str):
    try:
        if not s:
            return None
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except:
        return None

def _to_utc_iso(dt_str: str | None):
    if not dt_str:
        return None
    dti = _safe_fromiso(dt_str)
    if dti:
        if not dti.tzinfo:
            dti = dti.replace(tzinfo=LOCAL_TZ)
        dtu = dti.astimezone(timezone.utc)
        return dtu.isoformat().replace("+00:00", "Z")
    try:
        parsed = dateparser.parse(
            dt_str,
            settings={
                "PREFER_DATES_FROM": "future",
                "RETURN_AS_TIMEZONE_AWARE": True,
                "TIMEZONE": LOCAL_TZ_NAME,
                "TO_TIMEZONE": "UTC",
            },
        )
        if not parsed:
            return None
        return parsed.isoformat().replace("+00:00", "Z")
    except:
        return None

def _utc_iso_to_local_iso(iso_utc: str | None):
    dtu = _safe_fromiso(iso_utc or "")
    if not dtu:
        return None
    if not dtu.tzinfo:
        dtu = dtu.replace(tzinfo=timezone.utc)
    return dtu.astimezone(LOCAL_TZ).isoformat()

def _readable_local(iso_utc: str | None) -> str:
    dtu = _safe_fromiso(iso_utc or "")
    if not dtu:
        return "Invalid time"
    if not dtu.tzinfo:
        dtu = dtu.replace(tzinfo=timezone.utc)
    return dtu.astimezone(LOCAL_TZ).strftime("%A %I:%M %p")

# ---------------------------------------------------------
# NAME + DATETIME CLEANING
# ---------------------------------------------------------
def clean_name_input(text: str) -> str:
    text = text.lower()
    for p in ["my name is", "i am", "i'm", "its", "it's", "this is", "name is"]:
        text = text.replace(p, "")
    text = re.sub(r"[^a-zA-ZáéíóúüñÁÉÍÓÚÜÑ ]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.title()

def clean_datetime_input(text: str) -> str:
    text = text.lower()
    for f in ["around", "ish", "maybe", "let's do", "lets do", "mmm", "uh"]:
        text = text.replace(f, "")
    text = re.sub(r"\bat\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def gpt_extract_datetime(spoken: str) -> str | None:
    try:
        result = client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0,
            messages=[
                {"role": "system", "content": "Extract clean date & time only. Example: Friday at 7 PM"},
                {"role": "user", "content": spoken}
            ],
        )
        return result.choices[0].message.content.strip()
    except:
        return None

# ---------------------------------------------------------
# SUPABASE
# ---------------------------------------------------------
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE"),
)

TABLE_LIMIT = 10

_recent_keys = {}
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
    iso_utc = _to_utc_iso(data.get("datetime"))
    if not iso_utc:
        return "❌ Invalid time. Please specify date AND time."

    name = data.get("customer_name", "").strip() or "Guest"
    key = f"{name}|{iso_utc}"

    if _cache_check_and_add(key):
        return f"ℹ️ Already confirmed.\n👤 {name}"

    table = data.get("table_number") or assign_table(iso_utc)
    if not table:
        return "❌ No tables available at that time."

    supabase.table("reservations").insert({
        "customer_name": name,
        "customer_email": data.get("customer_email", "") or "",
        "contact_phone": data.get("contact_phone", ""),
        "datetime": iso_utc,
        "party_size": int(data.get("party_size", 1)),
        "table_number": table,
        "notes": data.get("notes", ""),
        "status": "confirmed",
    }).execute()

    readable = _readable_local(iso_utc)
    return f"✅ Reservation confirmed for {name}\n🗓 {readable}\n🍽 Table {table}"

# ---------------------------------------------------------
# HOMEPAGE
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    return "<h3>✅ Backend running</h3><p>Go to /dashboard</p>"

# ---------------------------------------------------------
# DASHBOARD ROUTE (unchanged)
# ---------------------------------------------------------
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    res = supabase.table("reservations").select("*").order("datetime", desc=True).execute()
    rows = res.data or []
    for r in rows:
        r["datetime"] = _utc_iso_to_local_iso(r.get("datetime"))
    return templates.TemplateResponse("dashboard.html", {"request": request, "reservations": rows})

# ---------------------------------------------------------
# ✅ WHATSApp BOOKING (EMAIL OPTIONAL, AUTO-PHONE)
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    form = await request.form()
    Body = form.get("Body", "")
    From = form.get("From", "")  # phone number from WhatsApp
    resp = MessagingResponse()

    # Always store WhatsApp number as phone automatically
    contact_phone = From.replace("whatsapp:", "")

    prompt = """
Extract JSON. REQUIRED FIELDS:

{
 "customer_name": "",
 "party_size": "",
 "datetime": "",
 "notes": ""
}

DO NOT ASK FOR EMAIL.
DO NOT RETURN ask FOR EMAIL.
Never ask for phone — use metadata.
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
        resp.message("❌ I didn’t understand that. Try again.")
        return Response(content=str(resp), media_type="application/xml")

    # Auto-inject WhatsApp phone
    data["contact_phone"] = contact_phone

    msg = save_reservation(data)
    resp.message(msg)
    asyncio.create_task(notify_refresh())
    return Response(content=str(resp), media_type="application/xml")

# ---------------------------------------------------------
# ✅ VOICE (unchanged)
# ---------------------------------------------------------
@app.get("/call")
async def make_test_call(to: str):
    call = twilio_client.calls.create(
        to=to,
        from_=TWILIO_PHONE_NUMBER,
        url=f"{PUBLIC_BASE_URL}/voice",
    )
    return {"status": "queued", "sid": call.sid}

@app.post("/voice")
async def voice_welcome():
    vr = VoiceResponse()
    g = vr.gather(input="speech", action="/voice/name")
    g.say("Hi! I can book your table. What is your name?", voice="Polly.Joanna-Neural")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/voice/name")
async def voice_name(request: Request):
    form = await request.form()
    name = clean_name_input(form.get("SpeechResult") or "")
    vr = VoiceResponse()
    g = vr.gather(input="speech", action=f"/voice/party?name={quote(name)}")
    g.say(f"Nice to meet you {name}. For how many people?", voice="Polly.Joanna-Neural")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/voice/party")
async def voice_party(request: Request, name: str):
    form = await request.form()
    speech = form.get("SpeechResult", "").lower()
    numbers = {"one":"1","two":"2","for":"4"}
    party = next((word for word in speech.split() if word.isdigit()), None) or \
            next((numbers[w] for w in numbers if w in speech), "1")
    vr = VoiceResponse()
    g = vr.gather(input="speech", action=f"/voice/datetime?name={quote(name)}&party={party}")
    g.say("What date and time should I book?", voice="Polly.Joanna-Neural")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/voice/datetime")
async def voice_datetime(request: Request, name: str, party: str):
    form = await request.form()
    raw = form.get("SpeechResult", "").strip()
    cleaned = clean_datetime_input(raw)

    if not re.search(r"\d|pm|am", cleaned):
        vr = VoiceResponse()
        g = vr.gather(input="speech", action=f"/voice/datetime?name={quote(name)}&party={party}")
        g.say("What time exactly?", voice="Polly.Joanna-Neural")
        return Response(content=str(vr), media_type="application/xml")

    iso = _to_utc_iso(cleaned) or _to_utc_iso(gpt_extract_datetime(raw))

    if not iso:
        vr = VoiceResponse()
        g = vr.gather(input="speech", action=f"/voice/datetime?name={quote(name)}&party={party}")
        g.say("Sorry, I couldn’t understand. Try Friday at 7 PM.", voice="Polly.Joanna-Neural")
        return Response(content=str(vr), media_type="application/xml")

    vr = VoiceResponse()
    g = vr.gather(input="speech", action=f"/voice/notes?name={quote(name)}&party={party}&dt={quote(cleaned)}")
    g.say("Any notes or preferences? Say none if no.", voice="Polly.Joanna-Neural")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/voice/notes")
async def voice_notes(request: Request, name: str, party: str, dt: str):
    form = await request.form()
    notes = form.get("SpeechResult", "").strip()
    if notes.lower() in ["none", "no", "nothing"]:
        notes = ""
    payload = {"customer_name": name, "party_size": party, "datetime": dt, "notes": notes, "contact_phone": ""}
    vr = VoiceResponse()
    vr.say("Perfect, booking now.", voice="Polly.Joanna-Neural")
    vr.hangup()
    asyncio.create_task(async_save(payload))
    return Response(content=str(vr), media_type="application/xml")

async def async_save(payload):
    await asyncio.sleep(2)
    save_reservation(payload)
    await notify_refresh()

# ---------------------------------------------------------
# ✅ REMINDER SCHEDULER (unchanged)
# ---------------------------------------------------------
_reminded = set()

async def reminder_loop():
    while True:
        try:
            now = datetime.now(timezone.utc)
            window_end = now + timedelta(hours=REMINDER_HOURS)
            res = supabase.table("reservations").select("*").eq("status","confirmed").execute()
            for r in res.data or []:
                rid = r.get("reservation_id")
                if rid in _reminded:
                    continue
                dt = _safe_fromiso(r.get("datetime") or "")
                if not dt:
                    continue
                secs = (dt - now).total_seconds()
                target = REMINDER_HOURS * 3600
                if (target - REMINDER_GRACE_SEC) <= secs <= (target + REMINDER_GRACE_SEC):
                    phone = r.get("contact_phone","")
                    if phone:
                        twilio_client.messages.create(
                            from_=TWILIO_WHATSAPP_FROM,
                            to=f"whatsapp:{phone}",
                            body=f"⏰ Reminder: Your reservation is today at {_readable_local(r.get('datetime'))}",
                        )
                    _reminded.add(rid)
        except Exception as e:
            print("Reminder error:", e)
        await asyncio.sleep(60)

@app.on_event("startup")
async def start_scheduler():
    asyncio.create_task(reminder_loop())

# ---------------------------------------------------------
# ✅ WEBSOCKET REFRESH
# ---------------------------------------------------------
clients = []

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    clients.append(ws)
    try:
        while True:
            await ws.receive_text()
    except:
        clients.remove(ws)

async def notify_refresh():
    for ws in list(clients):
        try:
            await ws.send_text("refresh")
        except:
            clients.remove(ws)
