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
    """Normalize various inputs (ISO or natural language) to UTC ISO Z."""
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
# NEW: Natural language helpers (name + datetime)
# ---------------------------------------------------------
def clean_name_input(text: str) -> str:
    """Extract a clean full name: 'My name is David Martinez' -> 'David Martinez'."""
    text = text.lower()
    remove = ["my name is", "i am", "i'm", "its", "it's", "this is", "name is"]
    for r in remove:
        text = text.replace(r, " ")
    # keep letters and spaces (allow ñ/accents)
    text = re.sub(r"[^a-zA-ZáéíóúüñÁÉÍÓÚÜÑ ]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.title()

def clean_datetime_input(text: str) -> str:
    """Tolerate vague phrasing: 'around 7-ish Friday' -> 'friday 7 pm' (dateparser handles it)."""
    text = text.lower()
    fillers = ["around", "ish", "maybe", "let's do", "lets do", "mmm", "uh", "uhh", "uhhh"]
    for f in fillers:
        text = text.replace(f, " ")
    # soften strict 'at'
    text = re.sub(r"\bat\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def gpt_extract_datetime(spoken: str) -> str | None:
    """Fallback to GPT to clean ambiguous datetime when dateparser fails."""
    try:
        result = client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0,
            messages=[
                {"role": "system", "content":
                    "Extract ONLY the date and time mentioned. "
                    "Return like 'Friday at 7 PM' or 'Tomorrow at 9 PM'. No extra text."},
                {"role": "user", "content": spoken}
            ],
        )
        return result.choices[0].message.content.strip()
    except:
        return None

# ---------------------------------------------------------
# SUPABASE INIT
# ---------------------------------------------------------
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE"),
)

TABLE_LIMIT = 10

# ---------------------------------------------------------
# DEDUPE: in-memory idempotency cache for 60s
# ---------------------------------------------------------
# key: f"{norm_name}|{utc_iso}" -> expires_at (epoch seconds)
_recent_keys: dict[str, float] = {}
IDEMPOTENCY_TTL = 60  # seconds

def _cache_prune_now():
    now = time.time()
    to_del = [k for k, exp in _recent_keys.items() if exp <= now]
    for k in to_del:
        _recent_keys.pop(k, None)

def _cache_check_and_add(key: str) -> bool:
    """Return True if key already seen (dup), else add and return False."""
    _cache_prune_now()
    now = time.time()
    exp = now + IDEMPOTENCY_TTL
    if key in _recent_keys and _recent_keys[key] > now:
        return True
    _recent_keys[key] = exp
    return False

# ---------------------------------------------------------
# TABLE ASSIGN + SAVE
# ---------------------------------------------------------
def assign_table(iso_utc: str):
    booked = supabase.table("reservations") \
        .select("table_number") \
        .eq("datetime", iso_utc).execute()
    taken = {row["table_number"] for row in (booked.data or [])}
    for i in range(1, TABLE_LIMIT + 1):
        t = f"T{i}"
        if t not in taken:
            return t
    return None

def _find_existing(utc_iso: str, name: str):
    """Look for an active reservation with the same datetime and same name."""
    result = supabase.table("reservations") \
        .select("*") \
        .eq("datetime", utc_iso) \
        .execute()
    rows = result.data or []
    n = _norm_name(name)
    for r in rows:
        if _norm_name(r.get("customer_name")) == n and r.get("status") not in ("cancelled", "archived"):
            return r
    return None

def save_reservation(data: dict) -> str:
    """
    Save to DB with:
      - UTC normalization
      - de-duplication (DB lookup + 60s idempotency cache)
      - auto table assignment
      - readable LOCAL confirmation
    """
    # Normalize to UTC
    iso_utc = _to_utc_iso(data.get("datetime"))
    if not iso_utc:
        return "❌ Invalid date/time. Please specify date AND time."

    name = data.get("customer_name", "")
    key = f"{_norm_name(name)}|{iso_utc}"

    # Quick in-process dedupe
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

    # DB-level dedupe
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

    # Assign table + insert
    table = assign_table(iso_utc)
    if not table:
        return "❌ No tables available at that time."

    supabase.table("reservations").insert({
        "customer_name": name,
        "customer_email": data.get("customer_email", "") or "",
        "contact_phone": data.get("contact_phone", "") or "",
        "datetime": iso_utc,  # stored in UTC
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
# DASHBOARD (timezone-correct & crash-proof)
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

    total = len(view_rows)
    cancelled = len([r for r in view_rows if (r.get("status") == "cancelled")])

    now_local = datetime.now(LOCAL_TZ)
    week_ago_local = now_local - timedelta(days=7)

    def _local_dt_or_none(r):
        d = _safe_fromiso(r.get("datetime", ""))
        if not d:
            d = _safe_fromiso(r.get("datetime", "").replace("Z", "+00:00"))
        if not d:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=LOCAL_TZ)
        return d.astimezone(LOCAL_TZ)

    weekly_count = 0
    party_vals, times = [], []

    for r in view_rows:
        if r.get("party_size"):
            try:
                party_vals.append(int(r["party_size"]))
            except:
                pass
        ldt = _local_dt_or_none(r)
        if ldt:
            if ldt > week_ago_local:
                weekly_count += 1
            times.append(ldt.strftime("%H:%M"))

    avg_party_size = round(sum(party_vals) / len(party_vals), 1) if party_vals else 0
    peak_time = max(set(times), key=times.count) if times else "N/A"
    cancel_rate = round((cancelled / total) * 100, 1) if total else 0

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "reservations": view_rows,   # datetime already LOCAL ISO
            "weekly_count": weekly_count,
            "avg_party_size": avg_party_size,
            "peak_time": peak_time,
            "cancel_rate": cancel_rate,
        },
    )

# ---------------------------------------------------------
# WHATSAPP AI WEBHOOK
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp_webhook(Body: str = Form(...)):
    print("📩 Incoming:", Body)
    resp = MessagingResponse()

    prompt = """
Extract reservation details and return valid JSON ONLY.
Convert any natural language date → ISO 8601.

{
 "customer_name": "",
 "customer_email": "",
 "contact_phone": "",
 "party_size": "",
 "datetime": "",   // can be natural language; backend normalizes to UTC
 "notes": ""
}

If ANYTHING is missing → return ONLY:
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

    except Exception as e:
        print("❌ AI/JSON error:", e)
        resp.message("❌ I couldn’t understand that. Try again.")
        return Response(content=str(resp), media_type="application/xml")

    if "ask" in data:
        resp.message(data["ask"])
        return Response(content=str(resp), media_type="application/xml")

    msg = save_reservation(data)  # includes dedupe
    resp.message(msg)

    asyncio.create_task(notify_refresh())
    return Response(content=str(resp), media_type="application/xml")

# ---------------------------------------------------------
# DASHBOARD API (Create / Update / Cancel) — shapes unchanged
# ---------------------------------------------------------
@app.post("/createReservation")
async def create_reservation(payload: dict):
    msg = save_reservation(payload)   # dedupe + time normalization
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
# ✅ VOICE CALL FLOW (Twilio) — natural voice + fast + smarter NLU
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

    # If user gives day only ("Friday night"), ask for time
    contains_time = bool(re.search(r"\d|pm|am", cleaned))
    if not contains_time:
        vr = VoiceResponse()
        _gather(vr, f"/voice/datetime?name={quote(name)}&party={party}",
                "What time exactly?")
        return Response(content=str(vr), media_type="application/xml")

    # Parse datetime (auto-fixes “around 7pm” → 7PM)
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

    # async save (non-blocking for the call)
    asyncio.create_task(async_save(payload))
    return Response(content=str(vr), media_type="application/xml")

# Twilio partial speech hook (we just ACK for speed; keeps latency low)
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
