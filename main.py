from fastapi import FastAPI, Request, WebSocket, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote
import json, os, asyncio, time, re
import dateparser  # natural language datetime parser

# ✅ Supabase
from supabase import create_client, Client

# ✅ OpenAI
from openai import OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ✅ Twilio
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://ai-reservation-backend-final.onrender.com")
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
# TIMEZONE SETTINGS
# ---------------------------------------------------------
LOCAL_TZ_NAME = os.getenv("LOCAL_TZ", "America/Bogota")
LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)

def _safe_fromiso(s: str) -> datetime | None:
    try:
        if not s:
            return None
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except:
        return None

def _to_utc_iso(dt_str: str | None) -> str | None:
    if not dt_str:
        return None
    dti = _safe_fromiso(dt_str)
    if dti:
        if dti.tzinfo is None:
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
        parsed = parsed.replace(year=datetime.now().year)
        return parsed.isoformat().replace("+00:00", "Z")
    except:
        return None

def _utc_iso_to_local_iso(iso_utc: str | None) -> str | None:
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
# NATURAL LANGUAGE HELPERS (VOICE INPUT)
# ---------------------------------------------------------
def clean_name_input(text: str) -> str:
    text = text.lower()
    remove = ["my name is", "i am", "i'm", "its", "it's", "this is", "name is"]
    for r in remove:
        text = text.replace(r, " ")
    text = re.sub(r"[^a-zA-ZáéíóúüñÁÉÍÓÚÜÑ ]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.title()

def clean_datetime_input(text: str) -> str:
    text = text.lower()
    fillers = ["around", "ish", "maybe", "let's do", "lets do", "mmm", "uh", "uhh"]
    for f in fillers:
        text = text.replace(f, " ")
    text = re.sub(r"\bat\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

# ---------------------------------------------------------
# SUPABASE INIT
# ---------------------------------------------------------
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE"),
)

TABLE_LIMIT = 10

# ---------------------------------------------------------
# DEDUPE (MEMORY)
# ---------------------------------------------------------
_recent_keys: dict[str, float] = {}
IDEMPOTENCY_TTL = 60  # seconds

def _cache_check_and_add(key: str) -> bool:
    now = time.time()
    if key in _recent_keys and _recent_keys[key] > now:
        return True
    _recent_keys[key] = now + IDEMPOTENCY_TTL
    return False

# ---------------------------------------------------------
# TABLE ASSIGN + SAVE
# ---------------------------------------------------------
def assign_table(iso_utc: str):
    booked = supabase.table("reservations").select("table_number").eq("datetime", iso_utc).execute()
    taken = {row["table_number"] for row in (booked.data or [])}
    for i in range(1, TABLE_LIMIT + 1):
        t = f"T{i}"
        if t not in taken:
            return t
    return None

def _find_existing(utc_iso: str, name: str):
    result = supabase.table("reservations").select("*").eq("datetime", utc_iso).execute()
    rows = result.data or []
    n = _norm_name(name)
    for r in rows:
        if _norm_name(r.get("customer_name")) == n and r.get("status") not in ("cancelled", "archived"):
            return r
    return None

def save_reservation(data: dict) -> str:
    iso_utc = _to_utc_iso(data.get("datetime"))
    if not iso_utc:
        return "❌ Invalid date/time. Please specify date AND time."

    name = data.get("customer_name", "")
    key = f"{_norm_name(name)}|{iso_utc}"

    if _cache_check_and_add(key):
        existing = _find_existing(iso_utc, name)
        if existing:
            readable = _readable_local(existing.get("datetime"))
            return f"ℹ️ Already booked.\n👤 {name}\n🗓 {readable}"

    existing = _find_existing(iso_utc, name)
    if existing:
        readable = _readable_local(existing.get("datetime"))
        return f"ℹ️ Already booked.\n👤 {name}\n🗓 {readable}"

    table = assign_table(iso_utc)
    if not table:
        return "❌ No tables available at that time."

    supabase.table("reservations").insert({
        "customer_name": name,
        "customer_email": data.get("customer_email", "") or "",
        "contact_phone": data.get("contact_phone", "") or "",
        "datetime": iso_utc,
        "party_size": int(data.get("party_size", 1)),
        "table_number": table,
        "notes": data.get("notes", "") or "",
        "status": "confirmed"
    }).execute()

    readable = _readable_local(iso_utc)
    return f"✅ Reservation confirmed!\n👤 {name}\n👥 {data.get('party_size', 1)} people\n🗓 {readable}\n🍽 Table: {table}"

# ---------------------------------------------------------
# HOME
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    return "<h3>✅ Backend running</h3><p>Go to /dashboard</p>"

# ---------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    res = supabase.table("reservations").select("*").order("datetime", desc=True).execute()
    reservations = res.data or []

    view_rows = []
    for r in reservations:
        row = dict(r)
        local_iso = _utc_iso_to_local_iso(r.get("datetime"))
        row["datetime"] = local_iso or r.get("datetime") or ""
        view_rows.append(row)

    now_local = datetime.now(LOCAL_TZ)
    week_ago_local = now_local - timedelta(days=7)

    weekly_count = len([r for r in view_rows if _safe_fromiso(r["datetime"]) and _safe_fromiso(r["datetime"]) > week_ago_local])
    avg_party_size = round(sum(int(r.get("party_size", 0)) for r in view_rows) / len(view_rows), 1) if view_rows else 0
    times = [_safe_fromiso(r["datetime"]).strftime("%H:%M") for r in view_rows if _safe_fromiso(r["datetime"])]
    peak_time = max(set(times), key=times.count) if times else "N/A"
    cancelled = len([r for r in view_rows if r.get("status") == "cancelled"])
    cancel_rate = round((cancelled / len(view_rows)) * 100, 1) if view_rows else 0

    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "reservations": view_rows, "weekly_count": weekly_count,
         "avg_party_size": avg_party_size, "peak_time": peak_time, "cancel_rate": cancel_rate},
    )

# ---------------------------------------------------------
# WHATSAPP (email optional — NEVER ASK FOR IT)
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp_webhook(Body: str = Form(...)):
    resp = MessagingResponse()

    prompt = """
Extract reservation details and return ONLY valid JSON.

REQUIRED:
- customer_name
- party_size
- datetime

OPTIONAL:
- notes (only if user mentions something extra)
- customer_email (ONLY include if user explicitly mentions an email)
- contact_phone (leave empty)

NEVER ASK FOR MISSING EMAIL. NEVER request additional info.

Format exactly:
{
 "customer_name": "",
 "party_size": "",
 "datetime": "",
 "customer_email": "",
 "contact_phone": "",
 "notes": ""
}
"""

    try:
        result = client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0,
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": Body}],
        )
        output = result.choices[0].message.content.strip()
        if output.startswith("```"):
            output = output.replace("```json", "").replace("```", "").strip()
        data = json.loads(output)
    except:
        resp.message("❌ I couldn’t understand that. Try again.")
        return Response(content=str(resp), media_type="application/xml")

    data["contact_phone"] = ""  # no phone from WhatsApp text mode

    msg = save_reservation(data)
    resp.message(msg)
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
    new_dt = update.get("datetime")
    normalized = _to_utc_iso(new_dt) if new_dt else None
    supabase.table("reservations").update({
        "datetime": normalized if normalized else new_dt,
        "party_size": update.get("party_size"),
        "table_number": update.get("table_number"),
        "notes": update.get("notes"),
        "status": update.get("status", "updated"),
    }).eq("reservation_id", update["reservation_id"]).execute()
    asyncio.create_task(notify_refresh())
    return {"success": True}

@app.post("/cancelReservation")
async def cancel(update: dict):
    supabase.table("reservations").update({"status": "cancelled"}).eq("reservation_id", update["reservation_id"]).execute()
    asyncio.create_task(notify_refresh())
    return {"success": True}

# ---------------------------------------------------------
# ✅ VOICE CALL FLOW (FAST, NATURAL, NO DELAYS, STORES CALLER PHONE)
# ---------------------------------------------------------
@app.get("/call")
async def make_call(to: str):
    try:
        call = twilio_client.calls.create(
            to=to,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{PUBLIC_BASE_URL}/voice",
        )
        return {"status": "queued", "sid": call.sid}
    except Exception as e:
        return {"error": str(e)}

def _gather(vr: VoiceResponse, url: str, prompt: str):
    g = vr.gather(input="speech", speech_timeout="auto", timeout=4, action=url, method="POST")
    g.say(prompt, voice="Polly.Joanna-Neural", language="en-US")
    return vr

@app.post("/voice")
async def voice_welcome():
    vr = VoiceResponse()
    _gather(vr, "/voice/name", "Hi! I can book your table. What is your name?")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/voice/name")
async def voice_name(request: Request):
    form = await request.form()
    name = clean_name_input(form.get("SpeechResult") or "Guest")
    vr = VoiceResponse()
    _gather(vr, f"/voice/party?name={quote(name)}", f"Nice to meet you {name}. For how many people?")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/voice/party")
async def voice_party(request: Request, name: str):
    form = await request.form()
    speech = (form.get("SpeechResult") or "").lower()
    numbers = {"one":"1","two":"2","three":"3","four":"4","five":"5","six":"6","seven":"7","eight":"8","nine":"9","ten":"10"}
    party = next((token for token in speech.split() if token.isdigit()), None) or \
            next((num for word, num in numbers.items() if word in speech), "1")
    vr = VoiceResponse()
    _gather(vr, f"/voice/datetime?name={quote(name)}&party={party}", "What date and time should I book?")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/voice/datetime")
async def voice_datetime(request: Request, name: str, party: str):
    form = await request.form()
    raw = (form.get("SpeechResult") or "").strip()
    cleaned = clean_datetime_input(raw)

    contains_time = bool(re.search(r"\d|pm|am", cleaned))
    if not contains_time:
        vr = VoiceResponse()
        _gather(vr, f"/voice/datetime?name={quote(name)}&party={party}", "What time exactly?")
        return Response(content=str(vr), media_type="application/xml")

    iso = _to_utc_iso(cleaned)
    vr = VoiceResponse()
    if not iso:
        _gather(vr, f"/voice/datetime?name={quote(name)}&party={party}", "Sorry, try again. Example: Friday at 7 PM.")
        return Response(content=str(vr), media_type="application/xml")

    _gather(vr, f"/voice/notes?name={quote(name)}&party={party}&dt={quote(cleaned)}", "Any notes or preferences?")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/voice/notes")
async def voice_notes(request: Request, name: str, party: str, dt: str):
    form = await request.form()
    notes_speech = (form.get("SpeechResult") or "").strip()
    notes = "none" if any(x in notes_speech.lower() for x in ["none", "no"]) else notes_speech

    payload = {"customer_name": name, "party_size": party, "datetime": dt, "notes": notes, "contact_phone": ""}

    vr = VoiceResponse()
    vr.say("Perfect, I’m booking your table now.", voice="Polly.Joanna-Neural", language="en-US")
    vr.say("Thank you. Goodbye.", voice="Polly.Joanna-Neural", language="en-US")
    vr.hangup()

    asyncio.create_task(async_save(payload))
    return Response(content=str(vr), media_type="application/xml")

async def async_save(payload):
    await asyncio.sleep(1)
    save_reservation(payload)
    await notify_refresh()

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
    for ws in list(clients):
        try:
            await ws.send_text("refresh")
        except:
            try:
                clients.remove(ws)
            except:
                pass
