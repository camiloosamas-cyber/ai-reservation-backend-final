from fastapi import FastAPI, Request, WebSocket, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import json, os, asyncio, time, re
import dateparser  # natural language datetime parser

# ✅ Supabase
from supabase import create_client, Client

# ✅ OpenAI
from openai import OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ✅ Twilio
from twilio.twiml.messaging_response import MessagingResponse


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
# SAVE RESERVATION
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

    name = (data.get("customer_name") or "").strip()
    if not name:
        return "❌ Missing name."

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
# DASHBOARD
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
    peak_time = max(set(time_list), key=time_list.count) if time_list else "N/A"
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
# ✅ CONVERSATION MEMORY
# ---------------------------------------------------------
conversation_memory = {}  # key: sender (WaId or From), value: dict of fields


# ---------------------------------------------------------
# WHATSAPP WEBHOOK
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp_webhook(
    Body: str = Form(...),
    WaId: str = Form(None),
    From_: str = Form(None, alias="From")  # Twilio sends 'From' (e.g., 'whatsapp:+57...')
):
    resp = MessagingResponse()

    # Identify sender
    sender = WaId or From_ or "unknown"

    # Initialize memory
    if sender not in conversation_memory:
        conversation_memory[sender] = {
            "customer_name": None,
            "party_size": None,
            "datetime": None,
            "notes": None,
            "customer_email": None,
            "contact_phone": None,
        }
    mem = conversation_memory[sender]

    # Extraction prompt
    extraction_prompt = f"""
You are a reservation extractor. Update only the fields the user provides.
Never delete previously known values.

Known so far:
- name: {mem['customer_name']}
- party_size: {mem['party_size']}
- datetime: {mem['datetime']}
- notes: {mem['notes']}

Return ONLY raw JSON (no prose, no code fences). Example:
{{
  "customer_name": "",
  "party_size": "",
  "datetime": "",
  "notes": "",
  "ready": false
}}

Rules:
- If the user states one or more of name / party_size / datetime / notes, put them in those fields.
- If all of name + party_size + datetime are present (after combining with known values), set "ready": true. Otherwise false.
- Keep strings simple (no extra commentary).
"""

    # Call model (stable name)
    try:
        result = client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0,
            messages=[
                {"role": "system", "content": extraction_prompt},
                {"role": "user", "content": Body},
            ],
        )
        raw = result.choices[0].message.content.strip()
        # Unfence if needed
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw.replace("json\n", "").replace("JSON\n", "")
        # Fallback: extract first {...} block
        if not raw.startswith("{"):
            m = re.search(r"\{.*\}", raw, re.S)
            raw = m.group(0) if m else "{}"
        data = json.loads(raw)
    except Exception as e:
        print("⚠️ JSON extraction error:", e)
        resp.message("Sorry, could you repeat that?")
        return Response(content=str(resp), media_type="application/xml")

    # Merge new values into memory (only if present and truthy)
    for k in ("customer_name", "party_size", "datetime", "notes"):
        v = data.get(k)
        if v is not None and v != "":
            mem[k] = v

    # Decide next step
    ready = data.get("ready") is True or (
        bool(mem["customer_name"]) and bool(mem["party_size"]) and bool(mem["datetime"])
    )

    if ready:
        # Optional: attach sender phone for storage
        mem["contact_phone"] = sender.replace("whatsapp:", "") if isinstance(sender, str) else None
        msg = save_reservation(mem)
        # Clear memory after saving
        conversation_memory.pop(sender, None)
        resp.message(msg)
        asyncio.create_task(notify_refresh())
        return Response(content=str(resp), media_type="application/xml")

    # Ask only for the next missing field
    if not mem["customer_name"]:
        resp.message("May I have your name?")
    elif not mem["party_size"]:
        resp.message("For how many people?")
    elif not mem["datetime"]:
        resp.message("What date and time should I book it for?")
    else:
        resp.message("Any notes for the reservation?")

    return Response(content=str(resp), media_type="application/xml")


# ---------------------------------------------------------
# UPDATE / CANCEL
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
        clients.remove(websocket)


async def notify_refresh():
    for ws in clients:
        try:
            await ws.send_text("refresh")
        except:
            pass
