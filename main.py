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
from twilio.twiml.messaging_response import MessagingResponse

# ---------- Env ----------
OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY")
SUPABASE_URL          = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE = os.getenv("SUPABASE_SERVICE_ROLE")
PUBLIC_BASE_URL       = os.getenv(
    "PUBLIC_BASE_URL",
    "https://ai-reservation-backend-final.onrender.com"
)
LOCAL_TZ_NAME         = os.getenv("LOCAL_TZ", "America/Bogota")

# ---------- Init ----------
client        = OpenAI(api_key=OPENAI_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE)

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
#                   TIME HELPERS
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
    current_year = now_local.year

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
#              CONVERSATION MEMORY
# -----------------------------------------------------
CONVO_MEMORY = {}  # phone → {name, datetime, party_size, datetime_utc}

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

def memory_clear(phone):
    if phone in CONVO_MEMORY:
        CONVO_MEMORY.pop(phone, None)

# -----------------------------------------------------
#              IDEMPOTENCY LOGIC
# -----------------------------------------------------
_recent_keys = {}
IDEMPOTENCY_TTL = 60

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
    booked = supabase.table("reservations").select("table_number").eq("datetime", iso_utc).execute()
    taken = {row["table_number"] for row in (booked.data or [])}
    for i in range(1, TABLE_LIMIT + 1):
        t = f"T{i}"
        if t not in taken:
            return t
    return None

# -----------------------------------------------------
#         SAVE RESERVATION (SPANISH ONLY)
# -----------------------------------------------------
def save_reservation(data):
    iso_utc = _to_utc_iso(data.get("datetime"))
    if not iso_utc:
        return {"es": "❌ La fecha u hora no es válida."}

    name = data.get("customer_name", "")
    key = f"{_norm_name(name)}|{iso_utc}"

    # Idempotency
    if _cache_check_and_add(key):
        return {"es": "ℹ️ Esta reserva ya existe."}

    table = data.get("table_number") or assign_table(iso_utc)
    if not table:
        return {"es": "❌ No hay mesas disponibles."}

    supabase.table("reservations").insert({
        "customer_name": name,
        "customer_email": "",
        "contact_phone": data.get("contact_phone", ""),
        "datetime": iso_utc,
        "party_size": int(data.get("party_size", 1)),
        "table_number": table,
        "notes": "",
        "status": "confirmed"
    }).execute()

    readable = _readable_local(iso_utc)

    return {
        "es": (
            f"✅ ¡Listo! Tu reserva está confirmada 😊\n"
            f"👤 {name}\n"
            f"👥 {data.get('party_size', 1)} personas\n"
            f"🗓 {readable}\n"
            f"🍽 Mesa: {table}"
        )
    }

# -----------------------------------------------------
#                     ROUTES
# -----------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    return "<h3>Backend running</h3>"

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    try:
        res = supabase.table("reservations").select("*").order("datetime", desc=True).execute()
        reservations = res.data or []

        view_rows = []
        for r in reservations:
            local_iso = _utc_iso_to_local_iso(r.get("datetime"))
            row = dict(r)
            row["datetime"] = local_iso or r.get("datetime")
            view_rows.append(row)

        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "reservations": view_rows,
        })

    except:
        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "reservations": []
        })

# -----------------------------------------------------
#        SMART SPANISH-ONLY WHATSAPP ROUTE (FINAL)
# -----------------------------------------------------
@app.post("/whatsapp")
async def whatsapp_webhook(
    Body: str = Form(...),
    WaId: str = Form(None),
    From: str = Form(None)
):
    resp = MessagingResponse()

    # Normalize phone
    phone = ""
    if WaId:
        phone = WaId if WaId.startswith("+") else f"+{WaId}"
    elif From:
        phone = From.replace("whatsapp:", "")

    reset_memory_if_expired(phone)

    text = Body.strip()
    lower = text.lower()

    # STRONG Spanish detection
    is_spanish = any([
        re.search(r"[áéíóúñ¿¡]", lower),
        "hola" in lower,
        "buenas" in lower,
        "quiero" in lower,
        "reserv" in lower,
        "personas" in lower,
        "mañana" in lower,
        "nombre" in lower,
        "hora" in lower,
        "mesa" in lower
    ])

    lang = "es"

    # Greeting with emoji
    if lower.startswith(("hola", "buenas", "holi")):
        resp.message("¡Hola! 😊 ¿En qué puedo ayudarte hoy? ¿Quieres información del restaurante o hacer una reserva?")
        return Response(str(resp), media_type="application/xml")

    # SYSTEM PROMPT — SPANISH ONLY
    system_prompt = """
Eres un asistente de reservas de restaurante extremadamente inteligente.
Siempre responde SOLO en español.

Tu trabajo:

1. Extraer SOLO:
   - customer_name
   - datetime
   - party_size

2. NUNCA repetir información que el usuario ya dijo.

3. Si falta información:
   - Si falta TODO → "¿Podrías indicarme la fecha, hora y cuántas personas serían?"
   - Si falta nombre → "¿A nombre de quién sería la reserva?"
   - Si falta fecha/hora → "¿Para qué fecha y hora te gustaría la reserva?"
   - Si falta número de personas → "¿Para cuántas personas sería la reserva?"
   - Si faltan dos → preguntar ambas en un solo mensaje.

4. Cuando la información está COMPLETA:
   → RESPONDE SOLO CON ESTE JSON (sin texto extra):

{
 "customer_name": "",
 "party_size": "",
 "datetime": "",
 "customer_email": "",
 "contact_phone": "",
 "notes": ""
}

5. No uses emojis.
6. No pidas teléfono ni email.
7. Nunca cambies de idioma.
"""

    # CALL OPENAI
    try:
        ai = client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0.4,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
        )
        ai_msg = ai.choices[0].message.content.strip()
    except:
        resp.message("Hubo un error procesando tu mensaje.")
        return Response(str(resp), media_type="application/xml")

    # CASE A: text question
    if not ai_msg.startswith("{"):
        resp.message(ai_msg)
        return Response(str(resp), media_type="application/xml")

    # CASE B: JSON detected
    try:
        data = json.loads(ai_msg)
    except:
        resp.message("No entendí la fecha/hora.")
        return Response(str(resp), media_type="application/xml")

    # Attach phone
    data["contact_phone"] = phone

    # Save reservation
    result = save_reservation(data)

    resp.message(result["es"])

    # Clear memory after finishing
    memory_clear(phone)

    asyncio.create_task(notify_refresh())
    return Response(str(resp), media_type="application/xml")

# -----------------------------------------------------
#              WEBSOCKET REFRESH
# -----------------------------------------------------
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
