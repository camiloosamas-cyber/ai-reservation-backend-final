from fastapi import FastAPI, Request, WebSocket, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import json, os, asyncio
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
# TIMEZONE SETTINGS
# ---------------------------------------------------------
# Local timezone for display (WhatsApp + Dashboard)
LOCAL_TZ_NAME = os.getenv("LOCAL_TZ", "America/Bogota")
LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)

def _safe_fromiso(s: str) -> datetime | None:
    """Parse ISO string robustly; handle 'Z' and offsets."""
    try:
        if not s:
            return None
        # Accept both "....Z" and "....+00:00"
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except:
        return None

def _to_utc_iso(dt_str: str | None) -> str | None:
    """
    Parse any natural/ISO input and return UTC ISO string with 'Z'.
    - If user typed 'tomorrow 8pm', we assume LOCAL_TZ then convert to UTC.
    - If input already has timezone (like ISO from dashboard JS), we normalize to UTC.
    """
    if not dt_str:
        return None

    # If it looks like ISO already, try to parse as aware and convert to UTC
    dti = _safe_fromiso(dt_str)
    if dti:
        # If naive → assume local tz
        if dti.tzinfo is None:
            dti = dti.replace(tzinfo=LOCAL_TZ)
        dtu = dti.astimezone(timezone.utc)
        return dtu.isoformat().replace("+00:00", "Z")

    # Otherwise use dateparser (assume local tz if naive)
    try:
        parsed = dateparser.parse(
            dt_str,
            settings={
                "RETURN_AS_TIMEZONE_AWARE": True,
                "TIMEZONE": LOCAL_TZ_NAME,
                "TO_TIMEZONE": "UTC",
            },
        )
        if not parsed:
            return None
        # parsed is aware in UTC due to TO_TIMEZONE
        return parsed.isoformat().replace("+00:00", "Z")
    except:
        return None

def _utc_iso_to_local_iso(iso_utc: str | None) -> str | None:
    """Convert UTC ISO ('Z' or +00:00) to LOCAL_TZ ISO (without crashing)."""
    dtu = _safe_fromiso(iso_utc or "")
    if not dtu:
        return None
    if dtu.tzinfo is None:
        # Assume UTC if missing tz (safety)
        dtu = dtu.replace(tzinfo=timezone.utc)
    local_dt = dtu.astimezone(LOCAL_TZ)
    # Keep as ISO (no 'Z' because it's local time with offset)
    return local_dt.isoformat()

def _readable_local(iso_utc: str | None) -> str:
    """Nice readable local string for WhatsApp confirmation."""
    dtu = _safe_fromiso(iso_utc or "")
    if not dtu:
        return "Invalid time"
    if dtu.tzinfo is None:
        dtu = dtu.replace(tzinfo=timezone.utc)
    local_dt = dtu.astimezone(LOCAL_TZ)
    return local_dt.strftime("%A %I:%M %p")


# ---------------------------------------------------------
# SUPABASE INIT
# ---------------------------------------------------------
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE"),
)

TABLE_LIMIT = 10


def assign_table(iso_utc: str):
    """ Returns first available table at that UTC datetime (exact match). """
    booked = supabase.table("reservations") \
        .select("table_number") \
        .eq("datetime", iso_utc).execute()

    taken = {row["table_number"] for row in (booked.data or [])}

    for i in range(1, TABLE_LIMIT + 1):
        t = f"T{i}"
        if t not in taken:
            return t
    return None


def save_reservation(data: dict) -> str:
    """
    Save to DB:
      - Normalize input `datetime` -> UTC ISO 'Z'
      - Auto table assignment
      - Return readable confirmation using LOCAL_TZ
    """
    # Normalize to UTC
    iso_utc = data.get("datetime")
    iso_utc = _to_utc_iso(iso_utc)

    if not iso_utc:
        return "❌ Invalid date/time. Please specify date AND time."

    table = assign_table(iso_utc)
    if not table:
        return "❌ No tables available at that time."

    # Insert
    supabase.table("reservations").insert({
        "customer_name": data.get("customer_name", ""),
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
        f"👤 {data.get('customer_name','')}\n"
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

    # Build a view-model for the template with LOCAL time in the "datetime" field
    view_rows = []
    for r in reservations:
        row = dict(r)
        local_iso = _utc_iso_to_local_iso(r.get("datetime"))
        # Fallback if something weird is stored
        row["datetime"] = local_iso or r.get("datetime") or ""
        view_rows.append(row)

    # KPI analytics (safe)
    total = len(view_rows)
    cancelled = len([r for r in view_rows if (r.get("status") == "cancelled")])

    # compute "this week" using LOCAL time
    now_local = datetime.now(LOCAL_TZ)
    week_ago_local = now_local - timedelta(days=7)

    def _local_dt_or_none(r):
        d = _safe_fromiso(r.get("datetime", ""))
        # If template datetime is local with offset, great; if not, assume UTC then to local
        if not d:
            d = _safe_fromiso(r.get("datetime", "").replace("Z", "+00:00"))
        if not d:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=LOCAL_TZ)  # assume local if naive
        return d.astimezone(LOCAL_TZ)

    weekly_count = 0
    party_vals = []
    times = []

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
            "reservations": view_rows,   # IMPORTANT: datetime already in LOCAL time ISO
            "weekly_count": weekly_count,
            "avg_party_size": avg_party_size,
            "peak_time": peak_time,
            "cancel_rate": cancel_rate,
        },
    )


# ---------------------------------------------------------
# WHATSAPP AI WEBHOOK (time fixed)
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp_webhook(Body: str = Form(...)):
    print("📩 Incoming:", Body)
    resp = MessagingResponse()

    prompt = f"""
Extract reservation details and return valid JSON ONLY.
Convert any natural language date → ISO 8601.

Expected JSON:
{{
 "customer_name": "",
 "customer_email": "",
 "contact_phone": "",
 "party_size": "",
 "datetime": "",   // can be natural language; backend normalizes to UTC
 "notes": ""
}}

If ANYTHING is missing → return ONLY:
{{"ask":"<question>"}}
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

    # Save (this normalizes time to UTC and replies in LOCAL time)
    confirmation_msg = save_reservation(data)
    resp.message(confirmation_msg)

    asyncio.create_task(notify_refresh())
    return Response(content=str(resp), media_type="application/xml")


# ---------------------------------------------------------
# DASHBOARD API (Edit, create, cancel) — unchanged behavior
# ---------------------------------------------------------
@app.post("/createReservation")
async def create_reservation(payload: dict):
    msg = save_reservation(payload)  # normalizes time to UTC, returns readable LOCAL msg
    asyncio.create_task(notify_refresh())
    return {"success": True, "message": msg}


@app.post("/updateReservation")
async def update_reservation(update: dict):
    # If frontend passes ISO (usually UTC 'Z'), keep it; if local text, normalize to UTC.
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
    for ws in clients:
        try:
            await ws.send_text("refresh")
        except:
            pass
