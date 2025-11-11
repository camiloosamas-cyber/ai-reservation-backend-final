# ---------------------------------------------------------
# ✅ AI RESERVATION SYSTEM — WHATSAPP PHONE FIX
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

# ✅ Twilio
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://ai-reservation-backend-final.onrender.com")

twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

REMINDER_HOURS = float(os.getenv("REMINDER_HOURS", "2"))
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
        return dti.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

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
    return parsed.isoformat().replace("+00:00", "Z") if parsed else None

def _utc_iso_to_local_iso(iso_utc: str | None):
    dtu = _safe_fromiso(iso_utc or "")
    if not dtu:
        return None
    return dtu.astimezone(LOCAL_TZ).isoformat()

def _readable_local(iso_utc: str | None) -> str:
    dtu = _safe_fromiso(iso_utc or "")
    if not dtu:
        return "Invalid time"
    return dtu.astimezone(LOCAL_TZ).strftime("%A %I:%M %p")

def clean_name_input(text: str) -> str:
    text = text.lower()
    for p in ["my name is", "i am", "i'm", "its", "it's", "this is", "name is"]:
        text = text.replace(p, "")
    text = re.sub(r"[^a-zA-ZáéíóúüñÁÉÍÓÚÜÑ ]", "", text)
    return re.sub(r"\s+", " ", text).strip().title()

def clean_datetime_input(text: str) -> str:
    text = text.lower()
    for f in ["around", "ish", "maybe", "let's do", "lets do", "mmm", "uh"]:
        text = text.replace(f, "")
    return re.sub(r"\s+", " ", text).strip()

# ---------------------------------------------------------
# SUPABASE DB
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
    taken = {r["table_number"] for r in (booked.data or [])}
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
        readable = _readable_local(iso_utc)
        return f"ℹ️ Already confirmed.\n👤 {name}\n🗓 {readable}"

    table = data.get("table_number") or assign_table(iso_utc)
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
        "status": "confirmed",
    }).execute()

    readable = _readable_local(iso_utc)
    return f"✅ Reservation confirmed for {name}\n🗓 {readable}\n🍽 Table {table}"

# ---------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    return "<h3>✅ Backend running</h3><p>Go to /dashboard</p>"

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    res = supabase.table("reservations").select("*").order("datetime", desc=True).execute()
    rows = res.data or []
    for r in rows:
        r["datetime"] = _utc_iso_to_local_iso(r.get("datetime"))
    return templates.TemplateResponse("dashboard.html", {"request": request, "reservations": rows})

# ---------------------------------------------------------
# ✅ WHATSAPP webhook (FIXED: saves customer's phone)
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    form = await request.form()
    Body = form.get("Body", "")

    # ✅ This is the correct customer phone number
    From = form.get("From", "")  # <-- FIXED
    contact_phone = From.replace("whatsapp:", "").strip()

    resp = MessagingResponse()

    extraction_prompt = """
Return ONLY JSON:
{
 "customer_name": "",
 "party_size": "",
 "datetime": "",
 "notes": ""
}
Email is optional. NEVER ask for it.
"""

    try:
        result = client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0,
            messages=[
                {"role": "system", "content": extraction_prompt},
                {"role": "user", "content": Body},
            ]
        )
        output = result.choices[0].message.content.strip()
        data = json.loads(output.replace("```json", "").replace("```", ""))
    except:
        resp.message("❌ I didn't understand that. Try again.")
        return Response(content=str(resp), media_type="application/xml")

    data["contact_phone"] = contact_phone

    msg = save_reservation(data)
    resp.message(msg)
    asyncio.create_task(notify_refresh())
    return Response(content=str(resp), media_type="application/xml")

# ---------------------------------------------------------
# VOICE BOOKING (Joanna voice)
# ---------------------------------------------------------
@app.get("/call")
async def make_test_call(to: str):
    call = twilio_client.calls.create(
        to=to,
        from_=TWILIO_PHONE_NUMBER,
        url=f"{PUBLIC_BASE_URL}/voice",
    )
    return {"status": "queued", "sid": call.sid}

def _gather(vr: VoiceResponse, url: str, prompt: str):
    g = vr.gather(input="speech", action=url, method="POST")
    g.say(prompt, voice="Polly.Joanna-Neural")
    return vr

@app.post("/voice")
async def voice_welcome():
    vr = VoiceResponse()
    _gather(vr, "/voice/name", "Hi! I can book your table. What is your name?")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/voice/name")
async def voice_name(request: Request):
    form = await request.form()
    name = clean_name_input(form.get("SpeechResult") or "")
    vr = VoiceResponse()
    _gather(vr, f"/voice/party?name={quote(name)}", f"Nice to meet you {name}. For how many people?")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/voice/party")
async def voice_party(request: Request, name: str):
    form = await request.form()
    speech = (form.get("SpeechResult") or "").lower()
    numbers = {"one":"1","two":"2","three":"3","four":"4","for":"4","five":"5"}
    party = next((w for w in speech.split() if w.isdigit()), None)
    if not party:
        party = next((numbers[w] for w in numbers if w in speech), "1")
    vr = VoiceResponse()
    _gather(vr, f"/voice/datetime?name={quote(name)}&party={party}", "What date and time?")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/voice/datetime")
async def voice_datetime(request: Request, name: str, party: str):
    form = await request.form()
    raw = (form.get("SpeechResult") or "").strip()
    iso = _to_utc_iso(clean_datetime_input(raw))
    if not iso:
        return Response(str(_gather(VoiceResponse(), f"/voice/datetime?name={quote(name)}&party={party}", "Sorry, try saying Friday at 7 PM.")), media_type="application/xml")
    vr = VoiceResponse()
    _gather(vr, f"/voice/notes?name={quote(name)}&party={party}&dt={quote(raw)}", "Any notes? Say none if no.")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/voice/notes")
async def voice_notes(request: Request, name: str, party: str, dt: str):
    form = await request.form()
    notes = form.get("SpeechResult") or ""
    payload = {"customer_name": name, "party_size": party, "datetime": dt, "notes": notes, "contact_phone": ""}
    vr = VoiceResponse()
    vr.say("Perfect, I’m booking your table now.", voice="Polly.Joanna-Neural")
    vr.hangup()
    asyncio.create_task(async_save(payload))
    return Response(content=str(vr), media_type="application/xml")

async def async_save(payload):
    await asyncio.sleep(2)
    save_reservation(payload)
    await notify_refresh()

# ---------------------------------------------------------
# REMINDER LOOP
# ---------------------------------------------------------
_reminded = set()

async def reminder_loop():
    while True:
        try:
            now = datetime.now(timezone.utc)
            res = supabase.table("reservations").select("*").eq("status","confirmed").execute()
            for r in res.data or []:
                rid = r.get("reservation_id")
                if rid in _reminded:
                    continue

                dt = _safe_fromiso(r.get("datetime"))
                if not dt:
                    continue

                delta = (dt - now).total_seconds()
                if abs(delta - REMINDER_HOURS * 3600) <= REMINDER_GRACE_SEC:
                    phone = r.get("contact_phone")
                    if phone:
                        twilio_client.messages.create(
                            from_="whatsapp:" + TWILIO_PHONE_NUMBER,
                            to="whatsapp:" + phone,
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
# WEBSOCKET REFRESH
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
