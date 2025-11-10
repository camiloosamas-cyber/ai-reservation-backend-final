from fastapi import FastAPI, Request, WebSocket, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote
import json, os, asyncio, time
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
# TIMEZONE + DATE HELPERS
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
            "TO_TIMEZONE": "UTC",
        },
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
# SAVE RESERVATION
# ---------------------------------------------------------
recent_keys = {}
TTL = 60

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

    table = data.get("table_number")
    if table:
        table = table.strip()
    else:
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
        "status": "confirmed",
    }).execute()

    return (
        "✅ Reservation confirmed!\n"
        f"👤 {name}\n"
        f"👥 {data.get('party_size', 1)} people\n"
        f"🗓 {readable_local(iso_utc)}\n"
        f"🍽 Table: {table}"
    )


# ---------------------------------------------------------
# DASHBOARD + HOME
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
    now = datetime.now(LOCAL_TZ)
    week_ago = now - timedelta(days=7)

    weekly_count = len([r for r in view if safe_fromiso(r.get("datetime") or "").astimezone(LOCAL_TZ) > week_ago])
    avg_party_size = round(sum(int(r.get("party_size", 0)) for r in view) / total, 1) if total else 0

    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "reservations": view, "weekly_count": weekly_count, "avg_party_size": avg_party_size},
    )

# ---------------------------------------------------------
# ✅ WHATSAPP WEBHOOK
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp_webhook(Body: str = Form(...)):
    resp = MessagingResponse()

    extraction_prompt = """
You are an AI reservation assistant. Extract structured reservation data.

RETURN ONLY JSON:
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
                      {"role": "user", "content": Body}],
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
# ✅ CALL FLOW (voice booking with faster response & human voice)
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

# ✅ Faster gather with partial speech detection
def gather(vr: VoiceResponse, url: str, prompt: str, timeout_sec=6):
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
    gather(vr, "/voice/name", "Hi! I can book your table. What is your name?")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/voice/name")
async def voice_name(request: Request):
    form = await request.form()
    name = (form.get("SpeechResult") or "Guest").strip()

    vr = VoiceResponse()
    gather(vr, f"/voice/party?name={quote(name)}", f"Nice to meet you {name}. For how many people?")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/voice/party")
async def voice_party(request: Request, name: str):
    form = await request.form()
    speech = (form.get("SpeechResult") or "").lower().strip()

    numbers = {"one":"1","two":"2","three":"3","four":"4","for":"4","five":"5","six":"6","seven":"7","eight":"8","nine":"9","ten":"10"}
    party = None

    for token in speech.replace("-", " ").split():
        if token.isdigit():
            party = token
            break

    if party is None:
        for word, num in numbers.items():
            if word in speech:
                party = num
                break

    if party is None:
        party = "1"

    vr = VoiceResponse()
    gather(vr, f"/voice/datetime?name={quote(name)}&party={party}", "What date and time should I book?")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/voice/datetime")
async def voice_datetime(request: Request, name: str, party: str):
    form = await request.form()
    spoken = (form.get("SpeechResult") or "").strip()

    iso = normalize_to_utc(spoken)
    vr = VoiceResponse()

    if not iso:
        gather(vr, f"/voice/datetime?name={quote(name)}&party={party}", "Sorry, I didn't catch that. Try saying Friday at 7 PM.")
        return Response(content=str(vr), media_type="application/xml")

    gather(vr, f"/voice/notes?name={quote(name)}&party={party}&dt={quote(spoken)}", "Any notes or preferences? Say none if no.")
    return Response(content=str(vr), media_type="application/xml")

@app.post("/voice/notes")
async def voice_notes(request: Request, name: str, party: str, dt: str):
    form = await request.form()
    notes_speech = (form.get("SpeechResult") or "").strip()
    text = notes_speech.lower()

    notes = "none" if any(word in text for word in ["no", "none", "nope", "nothing", "that's it", "no thank"]) else notes_speech

    payload = {"customer_name": name, "party_size": party, "datetime": dt, "notes": notes, "contact_phone": ""}

    vr = VoiceResponse()
    vr.say("Perfect, I’m booking your table now.", voice="Polly.Joanna-Neural", language="en-US")
    vr.say("Thank you. Goodbye.", voice="Polly.Joanna-Neural", language="en-US")
    vr.hangup()

    asyncio.create_task(async_save(payload))
    return Response(content=str(vr), media_type="application/xml")

# ✅ NEW ✅ partial transcripts (lower latency)
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
# DASHBOARD ACTIONS (edit/cancel)
# ---------------------------------------------------------
@app.post("/createReservation")
async def create_reservation(payload: dict):
    msg = save_reservation(payload)
    asyncio.create_task(notify_refresh())
    return {"ok": True, "message": msg}

@app.post("/updateReservation")
async def update_reservation(update: dict):
    reservation_id = update.get("reservation_id")
    if not reservation_id:
        return {"success": False, "error": "reservation_id required"}

    patch = {}

    if "datetime" in update and update["datetime"]:
        norm = normalize_to_utc(update["datetime"])
        if norm:
            patch["datetime"] = norm

    for key in ["party_size", "table_number", "notes", "status"]:
        if key in update and update[key] not in [None, "", "undefined"]:
            patch[key] = update[key]

    if not patch:
        return {"success": False, "error": "no fields to update"}

    supabase.table("reservations").update(patch).eq("reservation_id", reservation_id).execute()
    asyncio.create_task(notify_refresh())
    return {"success": True}

@app.post("/cancelReservation")
async def cancel_reservation(update: dict):
    supabase.table("reservations").update({"status": "cancelled"}).eq("reservation_id", update.get("reservation_id")).execute()
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
