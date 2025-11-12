# ---------------------------------------------------------
# ✅ AI RESERVATION SYSTEM — FINAL FIXES APPLIED
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

TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM") or "whatsapp:+14155238886"

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

    # 1) Try ISO first
    iso_try = _safe_fromiso(dt_str)
    if iso_try:
        if iso_try.tzinfo is None:
            iso_try = iso_try.replace(tzinfo=LOCAL_TZ)
        return iso_try.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    # 2) Natural language + relative to now
    parsed = dateparser.parse(
        dt_str,
        settings={
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": datetime.now(LOCAL_TZ),
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TIMEZONE": LOCAL_TZ_NAME,
            "TO_TIMEZONE": "UTC",
        },
    )
    if not parsed:
        return None
    return parsed.isoformat().replace("+00:00", "Z")

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
    text = re.sub(r"[^a-zA-ZáéíóúüñÁÉÍÓÚÜÑ ]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.title()

def clean_datetime_input(text: str) -> str:
    text = text.lower()
    for word in ["around", "ish", "mmm", "uh", "uhh", "uhhh"]:
        text = text.replace(word, " ")
    text = text.replace(" at ", " ")
    return re.sub(r"\s+", " ", text).strip()

# ---------------------------------------------------------
# SUPABASE
# ---------------------------------------------------------
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE"),
)

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
        if f"T{i}" not in taken:
            return f"T{i}"
    return None

def save_reservation(data: dict) -> str:
    iso_utc = _to_utc_iso(data.get("datetime"))
    if not iso_utc:
        return "❌ Invalid date/time."

    name = (data.get("customer_name") or "Guest").strip()
    key = f"{_norm_name(name)}|{iso_utc}"

    if _cache_check_and_add(key):
        return "ℹ️ Already booked."

    table = data.get("table_number") or assign_table(iso_utc)
    if not table:
        return "❌ No tables available."

    supabase.table("reservations").insert({
        "customer_name": name,
        "customer_email": data.get("customer_email", ""),
        "contact_phone": data.get("contact_phone", ""),
        "datetime": iso_utc,
        "party_size": int(data.get("party_size", 1)),
        "table_number": table,
        "notes": data.get("notes", ""),
        "status": "confirmed",
    }).execute()

    return f"✅ Reservation confirmed!\n👤 {name}\n🗓 {_readable_local(iso_utc)}\n🍽 Table: {table}"

# ---------------------------------------------------------
# DASHBOARD (This Week FIXED)
# ---------------------------------------------------------
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    res = supabase.table("reservations").select("*").order("datetime", desc=True).execute()
    rows = res.data or []

    view = []
    now_local = datetime.now(LOCAL_TZ)
    week_start = now_local - timedelta(days=7)
    week_count = 0

    for r in rows:
        row = dict(r)
        local = _utc_iso_to_local_iso(r.get("datetime"))
        row["datetime"] = local or ""
        view.append(row)

        dt = _safe_fromiso(row["datetime"])
        if dt and dt > week_start:
            week_count += 1

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "reservations": view,
        "weekly_count": week_count,
    })

# ---------------------------------------------------------
# ✅ WHATSApp BOOKING (REAL PHONE VIA WaId)
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    form = await request.form()
    Body = form.get("Body", "")

    # ✅ BEST: WaId has real phone (Twilio sandbox bug fix)
    wa_id = form.get("WaId", "")
    if wa_id:
        contact_phone = "+" + wa_id if not wa_id.startswith("+") else wa_id
    else:
        From = form.get("From", "").replace("whatsapp:", "")
        contact_phone = From

    resp = MessagingResponse()

    prompt = """
    Return ONLY JSON with:
    {
     "customer_name": "",
     "party_size": "",
     "datetime": "",
     "notes": ""
    }
    """

    result = client.chat.completions.create(
        model="gpt-4.1-mini",
        temperature=0,
        messages=[{"role": "system", "content": prompt}, {"role": "user", "content": Body}],
    )

    output = result.choices[0].message.content.strip()
    if output.startswith("```"):
        output = output.replace("```json", "").replace("```", "").strip()

    data = json.loads(output)
    data["contact_phone"] = contact_phone

    msg = save_reservation(data)
    resp.message(msg)
    return Response(content=str(resp), media_type="application/xml")

# ---------------------------------------------------------
# ✅ VOICE PHONE NUMBER FIXED (USE Caller)
# ---------------------------------------------------------
@app.post("/voice")
async def voice_welcome():
    vr = VoiceResponse()
    vr.gather(
        input="speech",
        speech_timeout="auto",
        action="/voice/name",
        method="POST",
    ).say("Hi! What name should I put the reservation under?", voice="Polly.Joanna-Neural")
    return Response(str(vr), media_type="application/xml")

@app.post("/voice/name")
async def voice_name(request: Request):
    form = await request.form()
    caller = form.get("Caller", "")
    phone = caller if caller.startswith("+") else f"+{caller}"

    raw_name = clean_name_input(form.get("SpeechResult") or "Guest")
    vr = VoiceResponse()
    vr.gather(
        input="speech",
        speech_timeout="auto",
        action=f"/voice/datetime?name={quote(raw_name)}&phone={phone}",
        method="POST",
    ).say(f"Nice to meet you {raw_name}. What date and time?", voice="Polly.Joanna-Neural")
    return Response(str(vr), media_type="application/xml")

@app.post("/voice/datetime")
async def voice_datetime(request: Request, name: str, phone: str):
    form = await request.form()
    cleaned = clean_datetime_input(form.get("SpeechResult") or "")

    vr = VoiceResponse()
    vr.say("Booking your table.", voice="Polly.Joanna-Neural")
    save_reservation({"customer_name": name, "contact_phone": phone, "datetime": cleaned})
    vr.say("Done. Goodbye.", voice="Polly.Joanna-Neural")
    vr.hangup()
    return Response(str(vr), media_type="application/xml")

# ---------------------------------------------------------
# WEBSOCKET AUTO REFRESH
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
    for ws in list(clients):
        try:
            await ws.send_text("refresh")
        except:
            clients.remove(ws)
