# ---------------------------------------------------------
# ✅ AI RESERVATION SYSTEM — STABLE / TODAY-FIX / WHATSAPP-FIX
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
import dateparser  # natural language datetime parser

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

# WhatsApp sender config (if you have Sandbox, set TWILIO_WHATSAPP_FROM=whatsapp:+14155238886)
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM") or (f"whatsapp:{TWILIO_PHONE_NUMBER}" if TWILIO_PHONE_NUMBER else None)

# Reminder config (2 hours before, ±5 min)
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
    """Normalize inputs (ISO or natural language) to UTC ISO Z, anchored to NOW in local tz."""
    if not dt_str:
        return None

    # 1) Try ISO first
    dti = _safe_fromiso(dt_str)
    if dti:
        if dti.tzinfo is None:
            dti = dti.replace(tzinfo=LOCAL_TZ)
        return dti.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    # 2) Natural language with explicit RELATIVE_BASE (prevents random past year)
    try:
        parsed = dateparser.parse(
            dt_str,
            settings={
                "PREFER_DATES_FROM": "future",
                "PREFER_DAY_OF_MONTH": "current",
                "RELATIVE_BASE": datetime.now(LOCAL_TZ),
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
    # remove fillers but KEEP today/tomorrow/tonight
    fillers = ["around", "ish", "maybe", "let's do", "lets do", "mmm", "uh", "uhh", "uhhh"]
    for f in fillers:
        text = text.replace(f, " ")
    text = re.sub(r"\bat\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def gpt_extract_datetime(spoken: str) -> str | None:
    """Use GPT to clean ambiguous datetime; keep relative words (today/tomorrow)."""
    try:
        result = client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0,
            messages=[
                {"role": "system", "content":
                    "Extract ONLY the date/time phrase the user said.\n"
                    "Preserve words like 'today', 'tomorrow', 'tonight', 'mañana'.\n"
                    "Do NOT invent a year. Do NOT convert to weekday.\n"
                    "Return a short phrase like 'today 11 am' or 'tomorrow at 7 pm'."},
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

# Dedupe (60s)
_recent_keys: dict[str, float] = {}
IDEMPOTENCY_TTL = 60

def _cache_prune_now():
    now = time.time()
    for k in list(_recent_keys.keys()):
        if _recent_keys[k] <= now:
            _recent_keys.pop(k, None)

def _cache_check_and_add(key: str) -> bool:
    _cache_prune_now()
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
        return "❌ Invalid time. Please specify date AND time."

    name = (data.get("customer_name") or "Guest").strip()
    key = f"{_norm_name(name)}|{iso_utc}"

    # in-process dedupe
    if _cache_check_and_add(key):
        existing = _find_existing(iso_utc, name)
        if existing:
            readable = _readable_local(existing.get("datetime"))
            table = existing.get("table_number") or "-"
            return (
                "ℹ️ Already booked (dedup).\n"
                f"👤 {existing.get('customer_name','')}\n"
                f"👥 {existing.get('party_size','') } people\n"
                f"🗓 {readable}\n"
                f"🍽 Table: {table}"
            )

    # db-level dedupe
    existing = _find_existing(iso_utc, name)
    if existing:
        readable = _readable_local(existing.get("datetime"))
        table = existing.get("table_number") or "-"
        return (
            "ℹ️ Already booked.\n"
            f"👤 {existing.get('customer_name','')}\n"
            f"👥 {existing.get('party_size','') } people\n"
            f"🗓 {readable}\n"
            f"🍽 Table: {table}"
        )

    table = data.get("table_number") or assign_table(iso_utc)
    if not table:
        return "❌ No tables available at that time."

    supabase.table("reservations").insert({
        "customer_name": name,
        "customer_email": data.get("customer_email", "") or "",
        "contact_phone": data.get("contact_phone", ""),
        "datetime": iso_utc,  # UTC
        "party_size": int(data.get("party_size", 1)),
        "table_number": table,
        "notes": data.get("notes", "") or "",
        "status": "confirmed"
    }).execute()

    readable = _readable_local(iso_utc)
    return (
        "✅ Reservation confirmed!\n"
        f"👤 {name}\n"
        f"👥 {data.get('party_size', 1)} people\n"
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
# DASHBOARD  (unchanged shape)
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

    return templates.TemplateResponse("dashboard.html", {"request": request, "reservations": view_rows})

# ---------------------------------------------------------
# ✅ WHATSAPP BOOKING — EMAIL OPTIONAL, PHONE = From (customer), TODAY/TOMORROW FIX
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    form = await request.form()
    Body = form.get("Body", "")
    # IMPORTANT: In sandbox, From = customer; To = Twilio. We want the customer's phone.
    From = form.get("From", "")
    contact_phone = From.replace("whatsapp:", "").strip()

    resp = MessagingResponse()

    extraction_prompt = """
Return ONLY JSON with these fields (no extra keys, no prose):

{
 "customer_name": "",
 "party_size": "",
 "datetime": "",
 "notes": ""
}

Rules:
- Preserve words like 'today', 'tomorrow', 'tonight', 'mañana'. DO NOT convert them to a weekday. DO NOT invent a year.
- Phone is never asked (use metadata).
- Email is optional and must not be requested.
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
        if output.startswith("```"):
            output = output.replace("```json", "").replace("```", "").strip()
        data = json.loads(output)
    except Exception as e:
        print("❌ WhatsApp extract error:", e)
        resp.message("❌ I didn’t understand that. Try again.")
        return Response(content=str(resp), media_type="application/xml")

    # Auto-inject phone
    data["contact_phone"] = contact_phone

    # If GPT returned empty datetime, fall back to the user's raw message
    if not data.get("datetime"):
        data["datetime"] = Body

    # Clean/normalize datetime BEFORE save
    cleaned_dt = clean_datetime_input(data.get("datetime", ""))
    # If still vague, try GPT simplification (keeps today/tomorrow)
    if not _to_utc_iso(cleaned_dt):
        simplified = gpt_extract_datetime(cleaned_dt)
        if simplified:
            cleaned_dt = simplified
    data["datetime"] = cleaned_dt

    msg = save_reservation(data)
    resp.message(msg)
    asyncio.create_task(notify_refresh())
    return Response(content=str(resp), media_type="application/xml")

# ---------------------------------------------------------
# DASHBOARD API (Create / Update / Cancel)
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

    supabase.table("reservations") \
        .update({
            "datetime": normalized if normalized else new_dt,
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
# ✅ VOICE CALL FLOW — Joanna voice / smart NLU
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

def _gather(vr: VoiceResponse, url: str, prompt: str, timeout_sec=6):
    g = vr.gather(
        input="speech",
        speech_model="default",
        speech_timeout="auto",
        partial_results_callback="/voice/stream",
        profanity_filter="false",
        timeout=timeout_sec,
        action=url,
        method="POST",
    )
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
    name_raw = (form.get("SpeechResult") or "").strip()
    name = clean_name_input(name_raw)

    vr = VoiceResponse()
    _gather(vr, f"/voice/party?name={quote(name)}", f"Nice to meet you {name}. For how many people?")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/voice/party")
async def voice_party(request: Request, name: str):
    form = await request.form()
    speech = (form.get("SpeechResult") or "").lower().strip()

    numbers = {"one":"1","two":"2","three":"3","four":"4","for":"4","five":"5","six":"6","seven":"7","eight":"8","nine":"9","ten":"10"}
    party = next((token for token in speech.replace("-", " ").split() if token.isdigit()), None)
    if party is None:
        party = next((num for word, num in numbers.items() if word in speech), "1")

    vr = VoiceResponse()
    _gather(vr, f"/voice/datetime?name={quote(name)}&party={party}", "What date and time should I book?")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/voice/datetime")
async def voice_datetime(request: Request, name: str, party: str):
    form = await request.form()
    raw = (form.get("SpeechResult") or "").strip()
    cleaned = clean_datetime_input(raw)

    # If only day and no time, ask for exact time
    if not re.search(r"\d|pm|am", cleaned):
        vr = VoiceResponse()
        _gather(vr, f"/voice/datetime?name={quote(name)}&party={party}",
                "What time exactly?")
        return Response(content=str(vr), media_type="application/xml")

    iso = _to_utc_iso(cleaned)
    if not iso:
        cleaned_gpt = gpt_extract_datetime(raw)
        if cleaned_gpt:
            iso = _to_utc_iso(cleaned_gpt)
            cleaned = cleaned_gpt

    vr = VoiceResponse()
    if not iso:
        _gather(vr, f"/voice/datetime?name={quote(name)}&party={party}",
                "Sorry, I didn't catch that. Try saying Friday at 7 PM.")
        return Response(content=str(vr), media_type="application/xml")

    _gather(vr, f"/voice/notes?name={quote(name)}&party={party}&dt={quote(cleaned)}",
            "Any notes or preferences? Say none if no.")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/voice/notes")
async def voice_notes(request: Request, name: str, party: str, dt: str):
    form = await request.form()
    notes_speech = (form.get("SpeechResult") or "").strip()
    notes = "none" if any(x in notes_speech.lower() for x in ["none", "no", "nothing"]) else notes_speech

    payload = {"customer_name": name, "party_size": party, "datetime": dt, "notes": notes, "contact_phone": ""}

    vr = VoiceResponse()
    vr.say("Perfect, I’m booking your table now.", voice="Polly.Joanna-Neural", language="en-US")
    vr.say("Thank you. Goodbye.", voice="Polly.Joanna-Neural", language="en-US")
    vr.hangup()

    asyncio.create_task(async_save(payload))
    return Response(content=str(vr), media_type="application/xml")

@app.post("/voice/stream")
async def voice_stream(request: Request):
    try:
        _ = await request.form()
    except:
        pass
    return Response(content="OK", media_type="text/plain")

async def async_save(payload):
    await asyncio.sleep(2)
    save_reservation(payload)
    await notify_refresh()

# ---------------------------------------------------------
# 🔔 REMINDER SCHEDULER (WhatsApp, ~2 hours before)
# ---------------------------------------------------------
_reminded_ids: set[str] = set()

def _format_whatsapp(number: str | None) -> str | None:
    if not number:
        return None
    n = number.strip()
    if not n:
        return None
    if n.startswith("whatsapp:"):
        return n
    if n.startswith("+"):
        return f"whatsapp:{n}"
    digits = re.sub(r"\D", "", n)
    if len(digits) == 10:
        return f"whatsapp:+57{digits}"
    return None

def _can_send_whatsapp() -> bool:
    return bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM)

async def _send_whatsapp(to_whatsapp: str, body: str):
    try:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=to_whatsapp,
            body=body,
        )
    except Exception as e:
        print("❌ WhatsApp send error:", e)

async def reminder_loop():
    while True:
        try:
            if _can_send_whatsapp():
                now_utc = datetime.now(timezone.utc)
                # Query upcoming confirmed rows in a loose window
                window_end = now_utc + timedelta(hours=REMINDER_HOURS, minutes=15)
                res = supabase.table("reservations") \
                    .select("*") \
                    .in_("status", ["confirmed"]) \
                    .lte("datetime", window_end.isoformat().replace("+00:00", "Z")) \
                    .execute()

                rows = res.data or []
                for r in rows:
                    rid = str(r.get("reservation_id"))
                    if not rid or rid in _reminded_ids:
                        continue

                    dt_utc = _safe_fromiso(r.get("datetime") or "")
                    if not dt_utc:
                        continue
                    if dt_utc.tzinfo is None:
                        dt_utc = dt_utc.replace(tzinfo=timezone.utc)

                    delta_sec = (dt_utc - now_utc).total_seconds()
                    target_sec = REMINDER_HOURS * 3600
                    if (target_sec - REMINDER_GRACE_SEC) <= delta_sec <= (target_sec + REMINDER_GRACE_SEC):
                        to_wa = _format_whatsapp(r.get("contact_phone"))
                        if not to_wa:
                            _reminded_ids.add(rid)
                            continue

                        readable = _readable_local(r.get("datetime"))
                        name = r.get("customer_name") or "your reservation"
                        party = r.get("party_size") or ""
                        party_txt = f" for {party} " if party else " "

                        body = f"⏰ Reminder: {name}{party_txt}is today at {readable.split(' ',1)[1]}."
                        await _send_whatsapp(to_wa, body)
                        _reminded_ids.add(rid)
        except Exception as e:
            print("❌ Reminder loop error:", e)
        await asyncio.sleep(60)

@app.on_event("startup")
async def _on_startup():
    asyncio.create_task(reminder_loop())

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
