# ==========================================
#               PART 1 / 4
#      CLEAN — STABLE — SPANISH ONLY
#     A1 MODE (AI ONLY RETURNS JSON)
# ==========================================

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

# ---------- External Clients ----------
from supabase import create_client, Client
from openai import OpenAI
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse

# ---------- ENV ----------
OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY")
SUPABASE_URL          = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE = os.getenv("SUPABASE_SERVICE_ROLE")
TWILIO_ACCOUNT_SID    = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN     = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER   = os.getenv("TWILIO_PHONE_NUMBER")

# Default Render URL
PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL",
    "https://ai-reservation-backend-final.onrender.com"
)

LOCAL_TZ_NAME = os.getenv("LOCAL_TZ", "America/Bogota")

# ---------- Init ----------
client = OpenAI(api_key=OPENAI_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE)
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

app = FastAPI()
os.chdir(os.path.dirname(os.path.abspath(__file__)))
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
TABLE_LIMIT = 10

# -----------------------------------------------------
#                 TIME HELPERS
# -----------------------------------------------------
def _safe_fromiso(s: str):
    try:
        if not s:
            return None
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except:
        return None


def _explicit_year_in(text):
    return bool(text and re.search(r"\b20\d{2}\b", text))


def _to_utc_iso(dt_str):
    if not dt_str:
        return None

    dti = _safe_fromiso(dt_str)
    if dti:
        if dti.tzinfo is None:
            dti = dti.replace(tzinfo=LOCAL_TZ)
        return dti.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    now_local = datetime.now(LOCAL_TZ)
    current_year = datetime.now().year

    try:
        parsed = dateparser.parse(
            dt_str,
            settings={
                "PREFER_DATES_FROM": "future",
                "RETURN_AS_TIMEZONE_AWARE": True,
                "TIMEZONE": LOCAL_TZ_NAME,
                "TO_TIMEZONE": "UTC",
                "RELATIVE_BASE": now_local,
            },
        )
        if not parsed:
            return None

        if not _explicit_year_in(dt_str):
            parsed = parsed.replace(year=current_year)

        return parsed.isoformat().replace("+00:00", "Z")
    except:
        return None


def _utc_iso_to_local_iso(iso_utc):
    dtu = _safe_fromiso(iso_utc)
    if not dtu:
        return None
    if dtu.tzinfo is None:
        dtu = dtu.replace(tzinfo=timezone.utc)
    return dtu.astimezone(LOCAL_TZ).isoformat()


def _readable_local(iso_utc):
    dtu = _safe_fromiso(iso_utc)
    if not dtu:
        return "Hora inválida"
    if dtu.tzinfo is None:
        dtu = dtu.replace(tzinfo=timezone.utc)
    return dtu.astimezone(LOCAL_TZ).strftime("%A %I:%M %p")


def _norm_name(name):
    return (name or "").strip().casefold()

# -----------------------------------------------------
#         INPUT CLEANING HELPERS
# -----------------------------------------------------
def clean_name_input(text):
    text = (text or "").lower()
    for r in ["mi nombre es", "me llamo", "soy", "nombre es"]:
        text = text.replace(r, " ")
    text = re.sub(r"[^a-zA-ZáéíóúüñÁÉÍÓÚÜÑ ]", "", text)
    return re.sub(r"\s+", " ", text).strip().title()


def clean_datetime_input(text):
    text = (text or "").lower()
    for f in ["alrededor de", "más o menos", "mmm", "uh", "uhh"]:
        text = text.replace(f, " ")
    return re.sub(r"\s+", " ", text).strip()
# ==========================================
#               PART 2 / 4
#     MEMORY + DATABASE + SAVE RESERVATION
# ==========================================

# -----------------------------------------------------
#              CONVERSATION MEMORY
# -----------------------------------------------------
CONVO_MEMORY = {}  # phone → { name, datetime, party_size, datetime_utc }


def reset_memory_if_expired(phone):
    if phone not in CONVO_MEMORY:
        return
    mem = CONVO_MEMORY[phone]
    dt = mem.get("datetime_utc")
    if not dt:
        return
    dt_obj = _safe_fromiso(dt)
    if dt_obj and datetime.now(timezone.utc) > dt_obj:
        CONVO_MEMORY.pop(phone, None)


def memory_set(phone, key, value):
    if phone not in CONVO_MEMORY:
        CONVO_MEMORY[phone] = {}
    CONVO_MEMORY[phone][key] = value


def memory_clear(phone):
    if phone in CONVO_MEMORY:
        CONVO_MEMORY.pop(phone, None)


# -----------------------------------------------------
#              IDEMPOTENCY LOGIC
# -----------------------------------------------------
_recent_keys = {}
IDEMPOTENCY_TTL = 60  # 1 minuto


def _cache_check_and_add(key):
    now = time.time()
    exp = now + IDEMPOTENCY_TTL
    if key in _recent_keys and _recent_keys[key] > now:
        return True
    _recent_keys[key] = exp
    return False


# -----------------------------------------------------
#              DATABASE HELPERS
# -----------------------------------------------------
def assign_table(iso_utc):
    booked = (
        supabase.table("reservations")
        .select("table_number")
        .eq("datetime", iso_utc)
        .execute()
    )
    taken = {row["table_number"] for row in (booked.data or [])}
    for i in range(1, TABLE_LIMIT + 1):
        t = f"T{i}"
        if t not in taken:
            return t
    return None


def _find_existing(utc_iso, name):
    result = (
        supabase.table("reservations")
        .select("*")
        .eq("datetime", utc_iso)
        .execute()
    )
    rows = result.data or []
    n = _norm_name(name)
    for r in rows:
        if (
            _norm_name(r.get("customer_name")) == n
            and r.get("status") not in ("cancelled", "archived")
        ):
            return r
    return None


# -----------------------------------------------------
#                   SAVE RESERVATION
# -----------------------------------------------------
def save_reservation(data):
    iso_utc = _to_utc_iso(data.get("datetime"))
    if not iso_utc:
        return {
            "msg": "❌ La fecha u hora no es válida."
        }

    name = data.get("customer_name", "")
    key = f"{_norm_name(name)}|{iso_utc}"

    # Idempotencia
    if _cache_check_and_add(key):
        existing = _find_existing(iso_utc, name)
        if existing:
            readable = _readable_local(existing.get("datetime"))
            table = existing.get("table_number") or "-"
            return {
                "msg": f"ℹ️ Esta reserva ya existe.\n"
                       f"👤 {existing['customer_name']}\n"
                       f"👥 {existing['party_size']} personas\n"
                       f"🗓 {readable}\n"
                       f"🍽 Mesa: {table}"
            }

    # Asignar mesa
    table = data.get("table_number") or assign_table(iso_utc)
    if not table:
        return {
            "msg": "❌ No hay mesas disponibles para esa hora."
        }

    # Guardar en la base de datos
    supabase.table("reservations").insert(
        {
            "customer_name": name,
            "customer_email": data.get("customer_email", ""),
            "contact_phone": data.get("contact_phone", ""),
            "datetime": iso_utc,
            "party_size": int(data.get("party_size", 1)),
            "table_number": table,
            "notes": data.get("notes", ""),
            "status": "confirmed",
        }
    ).execute()

    readable = _readable_local(iso_utc)

    return {
        "msg": f"✅ ¡Listo! Tu reserva está confirmada 😊\n"
               f"👤 {name}\n"
               f"👥 {data.get('party_size', 1)} personas\n"
               f"🗓 {readable}\n"
               f"🍽 Mesa: {table}"
    }
# ==========================================
#               PART 3 / 4
#         WHATSAPP RESERVATION ENGINE
# ==========================================

@app.post("/whatsapp")
async def whatsapp_webhook(
    Body: str = Form(...),
    WaId: str = Form(None),
    From: str = Form(None),
):
    resp = MessagingResponse()

    # -----------------------------------------
    # Normalize phone
    # -----------------------------------------
    phone = ""
    if WaId:
        phone = WaId if WaId.startswith("+") else f"+{WaId}"
    elif From:
        phone = From.replace("whatsapp:", "")

    reset_memory_if_expired(phone)

    text = Body.strip()
    low = text.lower()

    # -----------------------------------------
    # Detect Spanish (always true for now)
    # -----------------------------------------
    lang = "es"

    # -----------------------------------------
    # GREETING (only place where emoji is allowed)
    # -----------------------------------------
    if any(low.startswith(g) for g in ["hola", "buenas", "holi", "hello", "hi", "hey"]):
        resp.message(
            "¡Hola! 😊 ¿En qué puedo ayudarte hoy? ¿Quieres información del restaurante o hacer una reserva?"
        )
        return Response(str(resp), media_type="application/xml")

    # -----------------------------------------
    # SYSTEM PROMPT — manages missing info logic
    # -----------------------------------------
    system_prompt = """
Eres un asistente de reservas extremadamente inteligente y natural del restaurante.
Siempre respondes SOLO en español.

Tu objetivo: obtener ESTOS 3 datos, sin repetir lo que ya dijo el cliente:

1. customer_name (nombre)
2. datetime (fecha y hora)
3. party_size (cantidad de personas)

REGLAS:

- Si el cliente ya dio un dato, NO lo vuelvas a pedir.
- Solo pregunta por la información que falta.
- Haz UNA sola pregunta a la vez, incluyendo todos los datos que falten.

Ejemplos de lógica (solo ejemplos, tú decides según el mensaje del usuario):

• Si falta TODO:
  “¿Podrías indicarme la fecha, hora y cuántas personas serían para la reserva?”

• Si solo falta el nombre:
  “¿A nombre de quién sería la reserva?”

• Si solo falta fecha/hora:
  “¿Para qué fecha y hora te gustaría la reserva?”

• Si solo falta cantidad de personas:
  “¿Para cuántas personas sería la reserva?”

• Si faltan dos cosas:
  Pregunta ambas juntas sin repetir lo que él ya dijo.

Cuando YA tengas los 3 datos → responde SOLO con un JSON EXACTO así:

{
 "customer_name": "",
 "party_size": "",
 "datetime": "",
 "customer_email": "",
 "contact_phone": "",
 "notes": ""
}

NO agregues texto fuera del JSON.
NO uses emojis excepto el saludo inicial (ya gestionado).
NO pidas teléfono ni email.
Habla natural, claro, como ChatGPT.
"""

    # -----------------------------------------
    # CALL OPENAI
    # -----------------------------------------
    try:
        ai = client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0.3,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
        )
        ai_msg = ai.choices[0].message.content.strip()
    except:
        resp.message("Hubo un error procesando tu mensaje.")
        return Response(str(resp), media_type="application/xml")

    # -----------------------------------------
    # CASE A — assistant returns a question
    # -----------------------------------------
    if not ai_msg.startswith("{"):
        resp.message(ai_msg)
        return Response(str(resp), media_type="application/xml")

    # -----------------------------------------
    # CASE B — assistant returned JSON
    # -----------------------------------------
    try:
        data = json.loads(ai_msg)
    except:
        resp.message("No entendí la fecha u hora. ¿Podrías repetirla?")
        return Response(str(resp), media_type="application/xml")

    # Auto attach phone
    if phone and not data.get("contact_phone"):
        data["contact_phone"] = phone

    # Save reservation
    result = save_reservation(data)

    # Store memory (for possible modifications)
    CONVO_MEMORY[phone] = {
        "name": data.get("customer_name"),
        "party": data.get("party_size"),
        "datetime": data.get("datetime"),
        "datetime_utc": _to_utc_iso(data.get("datetime")),
    }

    # Send confirmation message
    resp.message(result["msg"])

    # Clear memory AFTER confirm
    memory_clear(phone)

    asyncio.create_task(notify_refresh())
    return Response(str(resp), media_type="application/xml")
# ==========================================
#               PART 4 / 4
#     DASHBOARD + API REST + WEBSOCKETS
# ==========================================

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    try:
        res = (
            supabase.table("reservations")
            .select("*")
            .order("datetime", desc=True)
            .execute()
        )
        reservations = res.data or []

        # Convert UTC → local
        view_rows = []
        for r in reservations:
            local_iso = _utc_iso_to_local_iso(r.get("datetime"))
            row = dict(r)
            row["datetime"] = local_iso or r.get("datetime")
            view_rows.append(row)

        # Analytics
        now_local = datetime.now(LOCAL_TZ)
        week_ago = now_local - timedelta(days=7)

        def _to_local_dt(row):
            d = _safe_fromiso(row.get("datetime", ""))
            if not d:
                return None
            if d.tzinfo is None:
                d = d.replace(tzinfo=LOCAL_TZ)
            return d.astimezone(LOCAL_TZ)

        weekly_count = 0
        party_vals = []
        times = []

        for r in view_rows:
            ldt = _to_local_dt(r)
            if ldt:
                if ldt > week_ago:
                    weekly_count += 1
                times.append(ldt.strftime("%H:%M"))
            try:
                party_vals.append(int(r.get("party_size") or 0))
            except:
                pass

        avg_party_size = (
            round(sum(party_vals) / len(party_vals), 1) if party_vals else 0
        )
        peak_time = max(set(times), key=times.count) if times else "N/A"
        cancelled = len([rr for rr in view_rows if rr["status"] == "cancelled"])
        total = len(view_rows)
        cancel_rate = round((cancelled / total) * 100, 1) if total else 0

        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "reservations": view_rows,
                "weekly_count": weekly_count,
                "avg_party_size": avg_party_size,
                "peak_time": peak_time,
                "cancel_rate": cancel_rate,
            },
        )
    except:
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "reservations": [],
                "weekly_count": 0,
                "avg_party_size": 0,
                "peak_time": "N/A",
                "cancel_rate": 0,
            },
        )


# ==========================================
#               DASHBOARD API
# ==========================================

@app.post("/createReservation")
async def create_reservation(payload: dict):
    msg = save_reservation(payload)
    return {"success": True, "message": msg["es"]}


@app.post("/updateReservation")
async def update_reservation(update: dict):
    new_dt = update.get("datetime")
    normalized = _to_utc_iso(new_dt) if new_dt else None

    supabase.table("reservations").update({
        "datetime": normalized or new_dt,
        "party_size": update.get("party_size"),
        "table_number": update.get("table_number"),
        "notes": update.get("notes"),
        "status": update.get("status", "updated"),
    }).eq("reservation_id", update["reservation_id"]).execute()

    asyncio.create_task(notify_refresh())
    return {"success": True}


@app.post("/cancelReservation")
async def cancel_reservation(update: dict):
    supabase.table("reservations").update({
        "status": "cancelled"
    }).eq("reservation_id", update["reservation_id"]).execute()

    asyncio.create_task(notify_refresh())
    return {"success": True}


# ==========================================
#             WEBSOCKET AUTO-REFRESH
# ==========================================

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


# ==========================
#     END OF FINAL FILE
# ==========================
