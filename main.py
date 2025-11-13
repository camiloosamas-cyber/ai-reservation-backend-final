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
from twilio.twiml.voice_response import VoiceResponse, Gather

# ---------- Env ----------
OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY")
SUPABASE_URL          = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE = os.getenv("SUPABASE_SERVICE_ROLE")
TWILIO_ACCOUNT_SID    = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN     = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER   = os.getenv("TWILIO_PHONE_NUMBER")
PUBLIC_BASE_URL       = os.getenv("PUBLIC_BASE_URL", "https://ai-reservation-backend-final.onrender.com")
LOCAL_TZ_NAME         = os.getenv("LOCAL_TZ", "America/Bogota")

# ---------- Init ----------
client        = OpenAI(api_key=OPENAI_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE)
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

app = FastAPI()
os.chdir(os.path.dirname(os.path.abspath(__file__)))
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
TABLE_LIMIT = 10


# ---------------- TIME HELPERS ----------------
def _safe_fromiso(s: str) -> datetime | None:
    try:
        if not s:
            return None
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except:
        return None


def _explicit_year_in(text: str | None) -> bool:
    return bool(text and re.search(r"\b20\d{2}\b", text))


def _to_utc_iso(dt_str: str | None) -> str | None:
    """
    STRICT YEAR MODE:
    - Never changes the year.
    - If the user says “viernes”, “mañana”, “el 20 de noviembre” → always interpret inside the current year.
    - Only accept future if explicitly given year.
    """
    if not dt_str:
        return None

    # If it's already ISO-like
    dti = _safe_fromiso(dt_str)
    if dti:
        if dti.tzinfo is None:
            dti = dti.replace(tzinfo=LOCAL_TZ)
        return dti.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    now_local = datetime.now(LOCAL_TZ)
    current_year = now_local.year  # 2025 today

    # Parse using dateparser
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

        # Strict rule: never change year unless the user explicitly says a year
        if not _explicit_year_in(dt_str):
            parsed = parsed.replace(year=current_year)

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
        return "Hora inválida"
    if dtu.tzinfo is None:
        dtu = dtu.replace(tzinfo=timezone.utc)
    return dtu.astimezone(LOCAL_TZ).strftime("%A %I:%M %p")


def _norm_name(name: str | None) -> str:
    return (name or "").strip().casefold()


# ---------------- CLEAN INPUT HELPERS ----------------
def clean_name_input(text: str) -> str:
    text = (text or "").lower()
    for r in ["my name is", "i am", "i'm", "its", "it's", "this is", "name is"]:
        text = text.replace(r, " ")
    text = re.sub(r"[^a-zA-ZáéíóúüñÁÉÍÓÚÜÑ ]", "", text)
    return re.sub(r"\s+", " ", text).strip().title()


def clean_datetime_input(text: str) -> str:
    text = (text or "").lower()
    replace_phrases = [
        "around", "ish", "maybe", "let's do", "lets do",
        "mmm", "uh", "uhh", "uhhh"
    ]
    for f in replace_phrases:
        text = text.replace(f, " ")
    return re.sub(r"\s+", " ", text).strip()


# ---------------- DEDUPE ----------------
_recent_keys: dict[str, float] = {}
IDEMPOTENCY_TTL = 60

def _cache_check_and_add(key: str) -> bool:
    now = time.time()
    exp = now + IDEMPOTENCY_TTL
    if key in _recent_keys and _recent_keys[key] > now:
        return True
    _recent_keys[key] = exp
    return False


# ---------------- DB HELPERS ----------------
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


# ---------------- SAVE RESERVATION ----------------
def save_reservation(data: dict):
    iso_utc = _to_utc_iso(data.get("datetime"))
    if not iso_utc:
        return {
            "es": "❌ La fecha u hora no es válida. Recuerda incluir fecha **y** hora.",
            "en": "❌ Invalid date/time. Please specify date **and** time."
        }

    name = data.get("customer_name", "")
    key = f"{_norm_name(name)}|{iso_utc}"

    if _cache_check_and_add(key):
        existing = _find_existing(iso_utc, name)
        if existing:
            readable = _readable_local(existing["datetime"])
            table = existing["table_number"] or "-"
            return {
                "es": f"ℹ️ Esta reserva ya existe.\n👤 {existing['customer_name']}\n👥 {existing['party_size']} personas\n🗓 {readable}\n🍽 Mesa: {table}",
                "en": f"ℹ️ This reservation already exists.\n👤 {existing['customer_name']}\n👥 {existing['party_size']} people\n🗓 {readable}\n🍽 Table: {table}"
            }

    existing = _find_existing(iso_utc, name)
    if existing:
        readable = _readable_local(existing["datetime"])
        table = existing["table_number"] or "-"
        return {
            "es": f"ℹ️ Esta reserva ya existe.\n👤 {existing['customer_name']}\n👥 {existing['party_size']} personas\n🗓 {readable}\n🍽 Mesa: {table}",
            "en": f"ℹ️ This reservation already exists.\n👤 {existing['customer_name']}\n👥 {existing['party_size']} people\n🗓 {readable}\n🍽 Table: {table}"
        }

    table = data.get("table_number") or assign_table(iso_utc)
    if not table:
        return {
            "es": "❌ No hay mesas disponibles para esa hora.",
            "en": "❌ No tables available for that time."
        }

    supabase.table("reservations").insert({
        "customer_name": name,
        "customer_email": data.get("customer_email", "") or "",
        "contact_phone": data.get("contact_phone", "") or "",
        "datetime": iso_utc,
        "party_size": int(data.get("party_size", 1)),
        "table_number": table,
        "notes": data.get("notes", "") or "",
        "status": "confirmed"
    }).execute()

    readable = _readable_local(iso_utc)
    return {
        "es": f"✅ ¡Reserva confirmada! 😊\n👤 {name}\n👥 {data.get('party_size', 1)} personas\n🗓 {readable}\n🍽 Mesa: {table}",
        "en": f"✅ Reservation confirmed! 😊\n👤 {name}\n👥 {data.get('party_size', 1)} people\n🗓 {readable}\n🍽 Table: {table}"
    }


# ---------------- ROUTES ----------------
@app.get("/", response_class=HTMLResponse)
def home():
    return "<h3>✅ Backend running</h3><p>Go to /dashboard</p>"


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    try:
        res = supabase.table("reservations").select("*").order("datetime", desc=True).execute()
        reservations = res.data or []

        view_rows = []
        for r in reservations:
            row = dict(r)
            local_iso = _utc_iso_to_local_iso(r.get("datetime"))
            row["datetime"] = local_iso or r.get("datetime") or ""
            view_rows.append(row)

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

        avg_party_size = round(sum(party_vals) / len(party_vals), 1) if party_vals else 0
        peak_time = max(set(times), key=times.count) if times else "N/A"
        cancelled = len([rr for rr in view_rows if rr["status"] == "cancelled"])
        total = len(view_rows)
        cancel_rate = round((cancelled / total) * 100, 1) if total else 0

        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "reservations": view_rows,
            "weekly_count": weekly_count,
            "avg_party_size": avg_party_size,
            "peak_time": peak_time,
            "cancel_rate": cancel_rate,
        })

    except:
        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "reservations": [],
            "weekly_count": 0,
            "avg_party_size": 0,
            "peak_time": "N/A",
            "cancel_rate": 0,
        })
# ---------------- ADVANCED WHATSAPP AI ROUTE ----------------
@app.post("/whatsapp")
async def whatsapp_webhook(Body: str = Form(...), WaId: str = Form(None), From: str = Form(None)):
    """
    WhatsApp Smart Agent:
    - Conversación natural estilo ChatGPT
    - Responde en ES/EN dependiendo del usuario
    - Solo crea una reserva si el modelo devuelve JSON válido completo
    - Usa strict-year date parsing (2025 fijo)
    """

    resp = MessagingResponse()

    # Normalize phone number
    phone = ""
    if WaId:
        phone = WaId if WaId.startswith("+") else f"+{WaId}"
    elif From:
        phone = (From or "").replace("whatsapp:", "").strip()

    # ---------------- SMART SYSTEM PROMPT ----------------
    system_prompt = """
Eres un asistente de reservas para un restaurante. Funcionas de dos maneras:

──────────────────────────
🧠 1) MODO CONVERSACIONAL
──────────────────────────
- Habla como un humano amable e inteligente.
- Responde preguntas, saluda, da información, etc.
- Si el usuario solo conversa, NO intentes reservar.
- Usa SIEMPRE el idioma del usuario.
- No inventes políticas, precios ni horarios.

──────────────────────────
🍽️ 2) MODO RESERVA
──────────────────────────
Si el usuario menciona algo relacionado con reservar
(fecha, hora, día, personas, mesa, “quiero reservar”, etc.)
INTENTA detectar automáticamente estos campos:

- customer_name
- party_size
- datetime (fecha y hora)
- notes (opcional)
- customer_email (solo si la dicen explícitamente)
- contact_phone (solo si lo dicen)

REGLAS IMPORTANTES:
- Si la fecha u hora parecen incompletas, IGUAL intenta interpretarlas.
- NO pidas email. NO pidas número.
- SI NO puedes completar una reserva → responde con un mensaje normal (modo conversación).
- SI SÍ puedes completar una reserva → responde **SOLO el JSON**:

{
 "customer_name": "",
 "party_size": "",
 "datetime": "",
 "customer_email": "",
 "contact_phone": "",
 "notes": ""
}

NO agregues texto fuera del JSON.
NO uses comillas raras.
"""

    # ---------------- CALL OPENAI ----------------
    try:
        result = client.chat.completions.create(
            model="gpt-4.1",
            temperature=0.5,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": Body}
            ],
        )
        ai_response = result.choices[0].message.content.strip()

    except Exception as e:
        resp.message("❌ No pude procesar tu mensaje en este momento.")
        return Response(str(resp), media_type="application/xml")

    # ---------------------------------------------------------
    # CASE A: regular conversation (not JSON)
    # ---------------------------------------------------------
    if not ai_response.startswith("{"):
        # Respond normally (same language)
        if re.search(r"[áéíóúñ¿¡]|hola|buenas|gracias|reserv", Body.lower()):
            resp.message(ai_response)
        else:
            resp.message(ai_response)
        return Response(str(resp), media_type="application/xml")

    # ---------------------------------------------------------
    # CASE B: JSON detected → try to parse reservation
    # ---------------------------------------------------------
    try:
        data = json.loads(ai_response)
    except:
        resp.message("❌ No entendí bien tu fecha/hora. Intenta escribirla diferente.")
        return Response(str(resp), media_type="application/xml")

    # Fill phone automatically
    if phone and not data.get("contact_phone"):
        data["contact_phone"] = phone

    # Ensure required fields exist
    required = ["customer_name", "party_size", "datetime"]
    if not all(data.get(f) for f in required):
        # Missing data → treat it like normal conversation
        if re.search(r"[áéíóúñ¿¡]|hola|buenas|gracias|reserv", Body.lower()):
            resp.message("¿Podrías repetir la información? Creo que faltó la fecha, el nombre o el número de personas.")
        else:
            resp.message("Can you repeat the date, name, or party size? I think something was missing.")
        return Response(str(resp), media_type="application/xml")

    # Save reservation
    reply = save_reservation(data)

    # Detect user language
    user_lang = "es" if re.search(r"[áéíóúñ¿¡]|hola|buenas|quiero|reserv|mesa", Body.lower()) else "en"

    # Send reservation confirmation
    resp.message(reply[user_lang])

    # Trigger dashboard refresh
    asyncio.create_task(notify_refresh())

    return Response(str(resp), media_type="application/xml")
# ---------------- DASHBOARD API (Spanish by default) ----------------
@app.post("/createReservation")
async def create_reservation(payload: dict):
    msg = save_reservation(payload)
    return {"success": True, "message": msg["es"]}


@app.post("/updateReservation")
async def update_reservation(update: dict):
    new_dt = update.get("datetime")
    normalized = _to_utc_iso(new_dt) if new_dt else None

    supabase.table("reservations").update({
        "datetime": normalized if normalized else new_dt,
        "party_size": update.get("party_size"),
        "table_number": update.get("table_number"),
        "notes": update.get("notes"),
        "status": update.get("status", "updated"),
    }).eq("reservation_id", update["reservation_id"]).execute()

    asyncio.create_task(notify_refresh())
    return {"success": True}


@app.post("/cancelReservation")
async def cancel_reservation(update: dict):
    supabase.table("reservations").update({"status": "cancelled"}).eq("reservation_id", update["reservation_id"]).execute()
    asyncio.create_task(notify_refresh())
    return {"success": True}


# ---------------- VOICE (unchanged, English only for now) ----------------
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


def _gather_xml(prompt: str, action_url: str) -> str:
    vr = VoiceResponse()
    g = Gather(
        input="speech",
        speech_timeout="auto",
        timeout=5,
        action=action_url,
        method="POST"
    )
    g.say(prompt, voice="Polly.Joanna-Neural", language="en-US")
    vr.append(g)
    return str(vr)


@app.post("/voice")
async def voice_welcome():
    return Response(
        _gather_xml("Hi! I can book your table. What is your name?", "/voice/name"),
        media_type="application/xml"
    )


@app.post("/voice/name")
async def voice_name(request: Request):
    form = await request.form()
    caller = form.get("Caller", "")
    phone = caller if caller.startswith("+") else f"+{caller}" if caller else ""
    name = clean_name_input(form.get("SpeechResult") or "Guest")

    return Response(
        _gather_xml(
            f"Nice to meet you {name}. For how many people should I book the table?",
            f"/voice/party?name={quote(name)}&phone={quote(phone)}"
        ),
        media_type="application/xml"
    )


@app.post("/voice/party")
async def voice_party(request: Request, name: str, phone: str):
    form = await request.form()
    speech = (form.get("SpeechResult") or "").lower()

    numbers = {
        "one": "1", "two": "2", "three": "3", "four": "4", "for": "4",
        "five": "5", "six": "6", "seven": "7", "eight": "8",
        "nine": "9", "ten": "10"
    }

    party = next((tok for tok in speech.replace("-", " ").split() if tok.isdigit()), None)
    if not party:
        for w, n in numbers.items():
            if w in speech:
                party = n
                break

    if not party:
        party = "1"

    return Response(
        _gather_xml(
            "What date and time should I book?",
            f"/voice/datetime?name={quote(name)}&phone={quote(phone)}&party={party}"
        ),
        media_type="application/xml"
    )


@app.post("/voice/datetime")
async def voice_datetime(request: Request, name: str, phone: str, party: str):
    form = await request.form()
    raw = (form.get("SpeechResult") or "").strip()
    cleaned = clean_datetime_input(raw)

    iso = _to_utc_iso(cleaned)
    if not iso:
        return Response(
            _gather_xml(
                "Sorry, I didn’t catch that. Please say: Friday at 7 PM.",
                f"/voice/datetime?name={quote(name)}&phone={quote(phone)}&party={party}"
            ),
            media_type="application/xml"
        )

    return Response(
        _gather_xml(
            "Any notes or preferences? Say none if no.",
            f"/voice/notes?name={quote(name)}&phone={quote(phone)}&party={party}&dt={quote(cleaned)}"
        ),
        media_type="application/xml"
    )


@app.post("/voice/notes")
async def voice_notes(request: Request, name: str, phone: str, party: str, dt: str):
    form = await request.form()
    notes_speech = (form.get("SpeechResult") or "").strip()

    notes = (
        "none"
        if any(x in notes_speech.lower() for x in ["none", "no", "nothing", "no notes"])
        else notes_speech
    )

    payload = {
        "customer_name": name,
        "party_size": party,
        "datetime": dt,
        "notes": notes,
        "contact_phone": phone
    }

    save_reservation(payload)

    vr = VoiceResponse()
    vr.say("Perfect, I'm booking your table now.", voice="Polly.Joanna-Neural", language="en-US")
    vr.say("Thank you, goodbye.", voice="Polly.Joanna-Neural", language="en-US")
    vr.hangup()

    asyncio.create_task(notify_refresh())
    return Response(str(vr), media_type="application/xml")


# ---------------- WEBSOCKET AUTO-REFRESH ----------------
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


# ---------------- END OF FILE ----------------
pass
