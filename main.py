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

# ✅ OpenAI (used for WhatsApp JSON extraction)
from openai import OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ✅ Twilio (WhatsApp + Voice)
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse, Gather


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
# DEDUPE CACHE
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


# ---------------------------------------------------------
# SAVE RESERVATION (shared by WhatsApp + Voice)
# ---------------------------------------------------------
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
# ROUTES: HOME + DASHBOARD
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

    weekly_count = 0
    party_list = []
    time_list = []

    for r in view:
        if r.get("party_size"):
            party_list.append(int(r["party_size"]))

        dt = safe_fromiso(r.get("datetime"))
        if dt:
            dt = dt.astimezone(LOCAL_TZ)
            if dt > week_ago:
                weekly_count += 1
            time_list.append(dt.strftime("%H:%M"))

    avg_party_size = round(sum(party_list) / len(party_list), 1) if party_list else 0
    if time_list:
        # find peak time by frequency
        counts = {}
        for t in time_list:
            counts[t] = counts.get(t, 0) + 1
        peak_time = max(counts, key=counts.get)
    else:
        peak_time = "N/A"
    cancel_rate = round((cancelled / total) * 100, 1) if total else 0

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "reservations": view,
            "weekly_count": weekly_count,
            "avg_party_size": avg_party_size,
            "peak_time": peak_time,
            "cancel_rate": cancel_rate,
        }
    )


# ---------------------------------------------------------
# WHATSAPP WEBHOOK (already working)
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

Rules:
1. Fill fields ONLY with what user said.
2. Missing data stays "".
3. Required fields: customer_name, party_size, datetime.
4. If missing ANY required field, put ONE question into "ask".
   - Missing name → "May I have your name?"
   - Missing party_size → "For how many people?"
   - Missing datetime → "What date and time should I book it for?"
5. If all required fields exist, "ask" must be "".
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
        print("⚠️ JSON extraction error:", e)
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
# VOICE CALL BOOKING (NEW)
# ---------------------------------------------------------
def _say_and_gather(step: str, prompt: str, carry: dict, timeout_sec: int = 6) -> str:
    """
    Returns TwiML that asks a question and gathers speech.
    We carry previously collected fields as <Gather> action query params.
    """
    vr = VoiceResponse()
    with vr.gather(
        input="speech",
        action=f"/voice/collect?step={step}"
              f"&name={carry.get('name','')}"
              f"&party={carry.get('party','')}"
              f"&dt={carry.get('dt','')}"
              f"&notes={carry.get('notes','')}",
        method="POST",
        timeout=timeout_sec
    ) as g:
        g.say(prompt, voice="alice", language="en-US")
    # If no speech, repeat prompt
    vr.redirect(f"/voice/retry?step={step}"
                f"&name={carry.get('name','')}"
                f"&party={carry.get('party','')}"
                f"&dt={carry.get('dt','')}"
                f"&notes={carry.get('notes','')}")
    return str(vr)


@app.post("/voice", response_class=PlainTextResponse)
async def voice_welcome():
    """
    Twilio Voice webhook (Voice URL).
    Starts the flow by asking for the customer's name.
    """
    carry = {"name": "", "party": "", "dt": "", "notes": ""}
    twiml = _say_and_gather(
        step="name",
        prompt="Hi! I can book your table. What is your name?",
        carry=carry
    )
    return Response(content=twiml, media_type="application/xml")


@app.post("/voice/retry", response_class=PlainTextResponse)
async def voice_retry(request: Request,
                      step: str = Query("name"),
                      name: str = Query(""),
                      party: str = Query(""),
                      dt: str = Query(""),
                      notes: str = Query("")):
    """
    Fallback if Twilio didn't capture speech in time.
    """
    carry = {"name": name, "party": party, "dt": dt, "notes": notes}
    prompts = {
        "name": "Sorry, I didn't get that. What's your name?",
        "party": "Sorry, how many people is the reservation for?",
        "datetime": "Sorry, what date and time should I book it for?",
        "notes": "Any notes or preferences? You can say none."
    }
    twiml = _say_and_gather(step=step, prompt=prompts.get(step, "Please repeat."), carry=carry)
    return Response(content=twiml, media_type="application/xml")


@app.post("/voice/collect", response_class=PlainTextResponse)
async def voice_collect(
    request: Request,
    step: str = Query("name"),
    name: str = Query(""),
    party: str = Query(""),
    dt: str = Query(""),
    notes: str = Query("")
):
    """
    Handles each step, reads SpeechResult, advances flow, and finally saves.
    """
    form = await request.form()
    speech = (form.get("SpeechResult") or "").strip()
    carry = {"name": name, "party": party, "dt": dt, "notes": notes}

    # STEP LOGIC
    if step == "name":
        if not speech:
            twiml = _say_and_gather("name", "Sorry, I missed that. What's your name?", carry)
            return Response(content=twiml, media_type="application/xml")
        carry["name"] = speech
        twiml = _say_and_gather("party", f"Nice to meet you {carry['name']}. For how many people?", carry)
        return Response(content=twiml, media_type="application/xml")

    elif step == "party":
        # Try to parse an integer from speech
        parsed_party = None
        for token in speech.replace("-", " ").split():
            if token.isdigit():
                parsed_party = int(token)
                break
        if parsed_party is None:
            twiml = _say_and_gather("party", "Got it. Please say a number, like 2 or 4.", carry)
            return Response(content=twiml, media_type="application/xml")
        carry["party"] = str(max(1, parsed_party))
        twiml = _say_and_gather("datetime",
                                "Great. What date and time should I book? For example, Friday at 7 PM.",
                                carry, timeout_sec=8)
        return Response(content=twiml, media_type="application/xml")

    elif step == "datetime":
        # Validate with normalize_to_utc
        if not speech or not normalize_to_utc(speech):
            twiml = _say_and_gather("datetime",
                                    "I didn't catch the date and time. Try something like, "
                                    "Saturday, November 15th at 8 PM.",
                                    carry, timeout_sec=8)
            return Response(content=twiml, media_type="application/xml")
        carry["dt"] = speech
        twiml = _say_and_gather("notes",
                                "Any notes or preferences? You can say none.",
                                carry)
        return Response(content=twiml, media_type="application/xml")

    elif step == "notes":
        carry["notes"] = "none" if (not speech or speech.lower() in ["no", "none", "nope"]) else speech

        # Build payload and save
        payload = {
            "customer_name": carry["name"],
            "customer_email": "",
            "contact_phone": "",   # Twilio caller ID can be added if needed from request.form
            "party_size": carry["party"],
            "datetime": carry["dt"],
            "notes": carry["notes"]
        }
        msg = save_reservation(payload)

        vr = VoiceResponse()
        vr.say(msg.replace("\n", ". "), voice="alice", language="en-US")
        vr.say("Thank you! Goodbye.", voice="alice", language="en-US")
        vr.hangup()

        asyncio.create_task(notify_refresh())
        return Response(content=str(vr), media_type="application/xml")

    # Unknown step fallback
    vr = VoiceResponse()
    vr.redirect("/voice")
    return Response(content=str(vr), media_type="application/xml")


# ---------------------------------------------------------
# UPDATE / CANCEL (Dashboard actions)
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
# WEBSOCKET AUTO REFRESH
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
