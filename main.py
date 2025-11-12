# ---------------------------------------------------------
# ✅ AI RESERVATION SYSTEM — Stable Build (calls+actions+dates fixed)
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
from twilio.twiml.voice_response import VoiceResponse

TWILIO_ACCOUNT_SID   = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN    = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER  = os.getenv("TWILIO_PHONE_NUMBER")
PUBLIC_BASE_URL      = os.getenv("PUBLIC_BASE_URL", "https://ai-reservation-backend-final.onrender.com")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM") or "whatsapp:+14155238886"

twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ---------- Reminders ----------
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
    for w in ["around", "ish", "mmm", "uh", "uhh", "uhhh"]:
        text = text.replace(w, " ")
    text = re.sub(r"\bat\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def _explicit_year_in(text: str) -> bool:
    return bool(re.search(r"\b20\d{2}\b", text))

def _to_utc_iso_current_year(dt_str: str | None) -> str | None:
    """
    Normalize to UTC ISO Z.
    If user didn't give a year -> force CURRENT YEAR (local).
    Anchors parsing to now to keep 'today/tomorrow/friday' correct.
    """
    if not dt_str:
        return None

    # Try ISO first
    dti = _safe_fromiso(dt_str)
    if dti:
        if dti.tzinfo is None:
            dti = dti.replace(tzinfo=LOCAL_TZ)
        # If year missing is impossible here; ISO had year. Keep as given.
        return dti.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

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

    # Force current year if user did not explicitly include a year
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

def _find_existing(utc_iso: str, name: str):
    rows = supabase.table("reservations").select("*").eq("datetime", utc_iso).execute().data or []
    n = _norm_name(name)
    for r in rows:
        if _norm_name(r.get("customer_name")) == n and r.get("status") not in ("cancelled", "archived"):
            return r
    return None

def save_reservation(data: dict) -> str:
    iso_utc = _to_utc_iso_current_year(data.get("datetime"))
    if not iso_utc:
        return "❌ Invalid time. Please specify date AND time."

    name = (data.get("customer_name") or "Guest").strip()
    key  = f"{_norm_name(name)}|{iso_utc}"

    if _cache_check_and_add(key):
        existing = _find_existing(iso_utc, name)
        if existing:
            return (
                "ℹ️ Already booked.\n"
                f"👤 {existing.get('customer_name','')}\n"
                f"👥 {existing.get('party_size','') } people\n"
                f"🗓 {_readable_local(existing.get('datetime'))}\n"
                f"🍽 Table: {existing.get('table_number') or '-'}"
            )

    existing = _find_existing(iso_utc, name)
    if existing:
        return (
            "ℹ️ Already booked.\n"
            f"👤 {existing.get('customer_name','')}\n"
            f"👥 {existing.get('party_size','') } people\n"
            f"🗓 {_readable_local(existing.get('datetime'))}\n"
            f"🍽 Table: {existing.get('table_number') or '-'}"
        )

    table = data.get("table_number") or assign_table(iso_utc)
    if not table:
        return "❌ No tables available at that time."

    supabase.table("reservations").insert({
        "customer_name": name,
        "customer_email": data.get("customer_email", "") or "",
        "contact_phone": data.get("contact_phone", "") or "",
        "datetime": iso_utc,  # stored UTC
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
# HOME + DASHBOARD (This Week metric restored)
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    return "<h3>✅ Backend running</h3><p>Go to /dashboard</p>"

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    rows = supabase.table("reservations").select("*").order("datetime", desc=True).execute().data or []

    view = []
    now_local   = datetime.now(LOCAL_TZ)
    week_start  = now_local - timedelta(days=7)
    weekly_count = 0

    for r in rows:
        row = dict(r)
        local_iso = _utc_iso_to_local_iso(r.get("datetime"))
        row["datetime"] = local_iso or ""
        view.append(row)

        dlocal = _safe_fromiso(row["datetime"])
        if dlocal and dlocal > week_start:
            weekly_count += 1

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "reservations": view,
        "weekly_count": weekly_count,
    })

# ---------------------------------------------------------
# ✅ WHATSAPP WEBHOOK (uses WaId → +…; current-year dates)
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    form = await request.form()
    Body = form.get("Body", "")

    # Prefer WaId (real customer number). If missing, fallback to From.
    wa_id = form.get("WaId", "").strip()
    if wa_id:
        phone = wa_id if wa_id.startswith("+") else f"+{wa_id}"
    else:
        phone = (form.get("From", "") or "").replace("whatsapp:", "").strip()

    resp = MessagingResponse()

    prompt = """
Return ONLY valid JSON:
{
 "customer_name": "",
 "party_size": "",
 "datetime": "",
 "notes": ""
}
- Keep relative words like "today", "tomorrow", "friday".
- Do not invent a year.
- Email is optional and should not be asked.
"""
    try:
        result = client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0,
            messages=[{"role": "system", "content": prompt},
                      {"role": "user", "content": Body}],
        )
        out = result.choices[0].message.content.strip()
        if out.startswith("```"):
            out = out.replace("```json", "").replace("```", "").strip()
        data = json.loads(out)
    except Exception as e:
        print("❌ WhatsApp extract error:", e)
        resp.message("❌ I didn’t understand that. Try again.")
        return Response(content=str(resp), media_type="application/xml")

    data["contact_phone"] = phone

    # If GPT gave no datetime, fallback to user's message
    dt_txt = data.get("datetime") or Body
    dt_txt = clean_datetime_input(dt_txt)
    data["datetime"] = dt_txt

    msg = save_reservation(data)
    resp.message(msg)
    asyncio.create_task(notify_refresh())
    return Response(content=str(resp), media_type="application/xml")

# ---------------------------------------------------------
# ✅ DASHBOARD API (3-dot actions work)
# ---------------------------------------------------------
@app.post("/createReservation")
async def create_reservation(payload: dict):
    msg = save_reservation(payload)
    asyncio.create_task(notify_refresh())
    return {"success": True, "message": msg}

@app.post("/updateReservation")
async def update_reservation(update: dict):
    rid = update.get("reservation_id")
    if not rid:
        return {"success": False, "error": "reservation_id required"}

    patch = {}
    if update.get("datetime"):
        norm = _to_utc_iso_current_year(update["datetime"])
        if norm:
            patch["datetime"] = norm
    for k in ["party_size", "table_number", "notes", "status", "customer_name", "customer_email", "contact_phone"]:
        if k in update and update[k] not in [None, "", "undefined"]:
            patch[k] = update[k]

    if not patch:
        return {"success": False, "error": "no fields to update"}

    supabase.table("reservations").update(patch).eq("reservation_id", rid).execute()
    asyncio.create_task(notify_refresh())
    return {"success": True}

@app.post("/cancelReservation")
async def cancel_reservation(update: dict):
    rid = update.get("reservation_id")
    if not rid:
        return {"success": False, "error": "reservation_id required"}
    supabase.table("reservations").update({"status": "cancelled"}).eq("reservation_id", rid).execute()
    asyncio.create_task(notify_refresh())
    return {"success": True}

# ---------------------------------------------------------
# ✅ VOICE CALL FLOW (Polly.Joanna-Neural + caller phone)
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
        speech_timeout="auto",
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
    caller = form.get("Caller", "")
    phone  = caller if caller.startswith("+") else f"+{caller}" if caller else ""
    name   = clean_name_input(form.get("SpeechResult") or "Guest")
    vr = VoiceResponse()
    _gather(vr, f"/voice/datetime?name={quote(name)}&phone={quote(phone)}",
            f"Nice to meet you {name}. What date and time?")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/voice/datetime")
async def voice_datetime(request: Request, name: str, phone: str):
    form = await request.form()
    spoken = (form.get("SpeechResult") or "").strip()
    cleaned = clean_datetime_input(spoken)
    iso = _to_utc_iso_current_year(cleaned)

    vr = VoiceResponse()
    if not iso:
        _gather(vr, f"/voice/datetime?name={quote(name)}&phone={quote(phone)}",
                "Sorry, I didn't catch that. Try saying Friday at 7 PM.")
        return Response(content=str(vr), media_type="application/xml")

    payload = {"customer_name": name, "party_size": 1, "datetime": cleaned, "notes": "", "contact_phone": phone}
    vr.say("Perfect, I’m booking your table now.", voice="Polly.Joanna-Neural")
    vr.say("Thank you. Goodbye.", voice="Polly.Joanna-Neural")
    vr.hangup()
    asyncio.create_task(async_save(payload))
    return Response(content=str(vr), media_type="application/xml")

async def async_save(payload):
    await asyncio.sleep(1.5)
    save_reservation(payload)
    await notify_refresh()

# ---------------------------------------------------------
# 🔔 REMINDER LOOP (unchanged)
# ---------------------------------------------------------
_reminded = set()

async def reminder_loop():
    while True:
        try:
            now = datetime.now(timezone.utc)
            res = supabase.table("reservations").select("*").eq("status", "confirmed").execute()
            for r in res.data or []:
                rid = r.get("reservation_id")
                if rid in _reminded:
                    continue
                dt = _safe_fromiso(r.get("datetime"))
                if not dt:
                    continue
                delta = (dt - now).total_seconds()
                target = REMINDER_HOURS * 3600
                if abs(delta - target) <= REMINDER_GRACE_SEC:
                    phone = r.get("contact_phone")
                    if phone:
                        twilio_client.messages.create(
                            from_=TWILIO_WHATSAPP_FROM,
                            to=f"whatsapp:{phone}" if not phone.startswith("whatsapp:") else phone,
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
