from fastapi import FastAPI, Request, WebSocket, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import json, os, asyncio, time
import dateparser

# ---------- Supabase ----------
from supabase import create_client, Client

# ---------- OpenAI ----------
from openai import OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ---------- Twilio ----------
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
        if dti.tzinfo is None:
            dti = dti.replace(tzinfo=LOCAL_TZ)
        dtu = dti.astimezone(timezone.utc)
        return dtu.isoformat().replace("+00:00", "Z")
    try:
        parsed = dateparser.parse(
            dt_str,
            settings={
                "RETURN_AS_TIMEZONE_AWARE": True,
                "TIMEZONE": LOCAL_TZ_NAME,
                "TO_TIMEZONE": "UTC",
            }
        )
        if not parsed:
            return None
        return parsed.isoformat().replace("+00:00", "Z")
    except:
        return None


def _utc_iso_to_local_iso(iso_utc):
    dtu = _safe_fromiso(iso_utc or "")
    if not dtu:
        return None
    if dtu.tzinfo is None:
        dtu = dtu.replace(tzinfo=timezone.utc)
    return dtu.astimezone(LOCAL_TZ).isoformat()


def _readable_local(iso_utc):
    dtu = _safe_fromiso(iso_utc)
    if not dtu:
        return "Horario inválido"
    if dtu.tzinfo is None:
        dtu = dtu.replace(tzinfo=timezone.utc)
    return dtu.astimezone(LOCAL_TZ).strftime("%A %d %B, %I:%M %p")


def _norm_name(name: str | None):
    return (name or "").strip().casefold()


# ---------------------------------------------------------
# SUPABASE
# ---------------------------------------------------------
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE")
)

TABLE_LIMIT = 10


# ---------------------------------------------------------
# IDEMPOTENCY CACHE
# ---------------------------------------------------------
_recent_keys: dict[str, float] = {}
IDEMPOTENCY_TTL = 60


def _cache_prune_now():
    now = time.time()
    for k, exp in list(_recent_keys.items()):
        if exp <= now:
            _recent_keys.pop(k, None)


def _cache_check_and_add(key: str) -> bool:
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
    booked = supabase.table("reservations").select("table_number").eq("datetime", iso_utc).execute()
    taken = {r["table_number"] for r in (booked.data or [])}
    for i in range(1, TABLE_LIMIT + 1):
        t = f"T{i}"
        if t not in taken:
            return t
    return None


def _find_existing(utc_iso: str, name: str):
    rows = (
        supabase.table("reservations")
        .select("*")
        .eq("datetime", utc_iso)
        .execute()
        .data
        or []
    )

    n = _norm_name(name)
    for r in rows:
        if _norm_name(r.get("customer_name")) == n and r.get("status") not in ("cancelled", "archived"):
            return r
    return None


def save_reservation(data: dict) -> str:
    iso_utc = _to_utc_iso(data.get("datetime"))
    if not iso_utc:
        return "❌ Fecha u hora inválida. Por favor incluye **fecha y hora exacta**."

    name = data.get("customer_name", "")
    key = f"{_norm_name(name)}|{iso_utc}"

    if _cache_check_and_add(key):
        existing = _find_existing(iso_utc, name)
        if existing:
            table = existing.get("table_number")
            readable = _readable_local(existing.get("datetime"))
            return (
                "ℹ️ Esta reserva ya estaba registrada.\n"
                f"👤 {existing.get('customer_name')}\n"
                f"👥 {existing.get('party_size')} personas\n"
                f"🗓 {readable}\n"
                f"🍽 Mesa: {table}"
            )

    existing = _find_existing(iso_utc, name)
    if existing:
        table = existing.get("table_number")
        readable = _readable_local(existing.get("datetime"))
        return (
            "ℹ️ Esta reserva ya existe.\n"
            f"👤 {existing.get('customer_name')}\n"
            f"👥 {existing.get('party_size')} personas\n"
            f"🗓 {readable}\n"
            f"🍽 Mesa: {table}"
        )

    table = assign_table(iso_utc)
    if not table:
        return "❌ No hay mesas disponibles para ese horario."

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
    return (
        "✅ *¡Reserva confirmada!*\n"
        f"👤 {name}\n"
        f"👥 {data.get('party_size', 1)} personas\n"
        f"🗓 {readable}\n"
        f"🍽 Mesa: {table}"
    )


# ---------------------------------------------------------
# HOME
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    return "<h3>Backend activo ✅</h3>"


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
        local = _utc_iso_to_local_iso(r.get("datetime"))
        row["datetime"] = local or r.get("datetime")
        view_rows.append(row)

    return templates.TemplateResponse("dashboard.html", {"request": request, "reservations": view_rows})


# ---------------------------------------------------------
# WHATSAPP AI WEBHOOK — SPANISH + GREETING
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp(Body: str = Form(...)):
    print("📩 WhatsApp:", Body)
    resp = MessagingResponse()

    lower = Body.lower().strip()
    user_id = "default_user"  # using one session for now

    # Initialize state
    if user_id not in session_state:
        session_state[user_id] = {"mode": "none"}

    mode = session_state[user_id]["mode"]

    # ---- GREETING BLOCK ----
    greeting_words = ["hola", "buenas", "buenos días", "buenas tardes", "buenas noches", "hey"]

    if any(word in lower for word in greeting_words) and mode == "none":
        resp.message("¡Hola! 😊 ¿En qué puedo ayudarte hoy? ¿Quieres información del restaurante o hacer una reserva?")
        return Response(str(resp), media_type="application/xml")

    # ---- If user expresses reservation intent ----
    if "reserv" in lower and mode != "reservation":
        session_state[user_id]["mode"] = "reservation"
        mode = "reservation"

    # ---- If we are NOT in reservation mode ----
    if mode != "reservation":
        resp.message("¿Te gustaría hacer una reserva? 😊")
        return Response(str(resp), media_type="application/xml")

    # ---- RESERVATION MODE (AI extraction) ----
    system_prompt = """
Eres un asistente que extrae detalles de reservas en español.
Responde SOLO con JSON.

Datos necesarios:
- customer_name
- party_size
- datetime
- contact_phone

Si falta información:
{"ask":"<pregunta>"}

Formato final:
{
 "customer_name": "",
 "customer_email": "",
 "contact_phone": "",
 "party_size": "",
 "datetime": "",
 "notes": ""
}
"""

    try:
        result = client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": Body}
            ]
        )

        output = result.choices[0].message.content.strip()
        if output.startswith("```"):
            output = output.replace("```json", "").replace("```", "").strip()

        data = json.loads(output)

    except Exception as e:
        print("❌ JSON ERROR:", e)
        resp.message("❌ No pude entender el mensaje. Intenta nuevamente.")
        return Response(str(resp), media_type="application/xml")

    # ---- If AI needs more info ----
    if "ask" in data:
        resp.message(data["ask"])
        return Response(str(resp), media_type="application/xml")

    # ---- Finalize reservation ----
    msg = save_reservation(data)
    resp.message(msg)

    # End reservation mode
    session_state[user_id]["mode"] = "none"

    asyncio.create_task(notify_refresh())
    return Response(str(resp), media_type="application/xml")


# ---------------------------------------------------------
# LIVE REFRESH
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
