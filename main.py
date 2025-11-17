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

PUBLIC_BASE_URL       = os.getenv("PUBLIC_BASE_URL", "")
LOCAL_TZ_NAME         = os.getenv("LOCAL_TZ", "America/Bogota")

# ---------- Init ----------
client        = OpenAI(api_key=OPENAI_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE)

app = FastAPI()
os.chdir(os.path.dirname(os.path.abspath(__file__)))
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
app.add_middleware(CORSORMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
TABLE_LIMIT = 10

# -----------------------------------------------------
#                     TIME HELPERS
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

    # If it's already an ISO datetime
    existing = _safe_fromiso(dt_str)
    if existing:
        if existing.tzinfo is None:
            existing = existing.replace(tzinfo=LOCAL_TZ)
        return existing.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

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

def _readable_local(iso_utc):
    dtu = _safe_fromiso(iso_utc)
    if not dtu:
        return "Fecha inválida"
    if dtu.tzinfo is None:
        dtu = dtu.replace(tzinfo=timezone.utc)
    return dtu.astimezone(LOCAL_TZ).strftime("%A %I:%M %p")

# -----------------------------------------------------
#              CONVERSATION MEMORY
# -----------------------------------------------------
CONVO_MEMORY = {}  

def reset_memory_if_expired(phone):
    if phone not in CONVO_MEMORY:
        return
    dt = CONVO_MEMORY[phone].get("datetime_utc")
    if dt:
        dt_obj = _safe_fromiso(dt)
        if dt_obj and datetime.now(timezone.utc) > dt_obj:
            CONVO_MEMORY.pop(phone, None)

def memory_clear(phone):
    if phone in CONVO_MEMORY:
        CONVO_MEMORY.pop(phone, None)

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

def save_reservation(data):
    iso_utc = _to_utc_iso(data.get("datetime"))
    if not iso_utc:
        return "❌ La fecha u hora no es válida."

    name  = data.get("customer_name") or ""
    party = data.get("party_size")   or ""
    phone = data.get("contact_phone", "")

    table = assign_table(iso_utc)
    if not table:
        return "❌ No hay mesas disponibles para ese horario."

    supabase.table("reservations").insert({
        "customer_name": name,
        "contact_phone": phone,
        "datetime": iso_utc,
        "party_size": int(party),
        "table_number": table,
        "status": "confirmed"
    }).execute()

    readable = _readable_local(iso_utc)

    return (
        "✅ ¡Listo! Tu reserva está confirmada\n"
        f"👤 {name}\n"
        f"👥 {party} personas\n"
        f"🗓 {readable}\n"
        f"🍽 Mesa: {table}"
    )

# -----------------------------------------------------
#        WHATSAPP WEBHOOK — SPANISH ONLY
# -----------------------------------------------------
@app.post("/whatsapp")
async def whatsapp_webhook(
    Body: str = Form(...),
    WaId: str = Form(None),
    From: str = Form(None)
):
    resp = MessagingResponse()

    phone = ""
    if WaId:
        phone = WaId if WaId.startswith("+") else f"+{WaId}"
    elif From:
        phone = From.replace("whatsapp:", "")

    reset_memory_if_expired(phone)

    text   = Body.strip()
    lower  = text.lower()

    # Greeting
    if any(lower.startswith(g) for g in ["hola", "buenas", "holi"]):
        resp.message("¡Hola! 😊 ¿En qué puedo ayudarte hoy? ¿Quieres información o hacer una reserva?")
        return Response(str(resp), media_type="application/xml")

    # --------------- SYSTEM PROMPT ---------------------
    system_prompt = """
Eres un asistente de reservas para restaurante, extremadamente preciso.

REGLAS IMPORTANTES:

1. SOLO debes extraer:
   - nombre del cliente
   - fecha y hora exacta
   - número de personas

2. NUNCA respondas en inglés. SOLO español.

3. NO repitas información que el cliente ya dio.

4. Si falta uno o más datos:
   - Si falta todo → pregunta: "¿Podrías indicarme la fecha, hora y cuántas personas serían?"
   - Si falta nombre      → pregunta solo por el nombre.
   - Si falta fecha/hora  → pregunta solo por la fecha/hora.
   - Si falta número      → pregunta por cuántas personas.
   - Si faltan dos datos  → pregunta ambos en una sola frase.

5. FECHAS AMBIGUAS:
   Si el usuario dice cosas como:
   - "el 20"
   - "el viernes"
   - "la próxima semana"
   - "más tarde"
   NO des JSON.
   Pide la fecha exacta: "¿Podrías confirmarme la fecha exacta de la reserva?"

6. SOLO debes dar JSON cuando:
   ✔ nombre está completo
   ✔ fecha y hora son exactas y parseables
   ✔ número de personas es claro

7. El JSON FINAL debe ser en este formato EXACTO:

{
 "customer_name": "",
 "party_size": "",
 "datetime": "",
 "contact_phone": ""
}

8. SI NO tienes los 3 datos correctos → NO des JSON.
"""

    # ---------------- CALL OPENAI -----------------------
    try:
        ai = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": text}
            ]
        )
        ai_msg = ai.choices[0].message.content.strip()
    except:
        resp.message("Error procesando el mensaje.")
        return Response(str(resp), media_type="application/xml")

    # If it's NOT JSON → send it as-is
    if not ai_msg.startswith("{"):
        resp.message(ai_msg)
        return Response(str(resp), media_type="application/xml")

    # Parse JSON safely
    try:
        data = json.loads(ai_msg)
    except:
        resp.message("No entendí bien la fecha/hora, ¿podrías repetirla?")
        return Response(str(resp), media_type="application/xml")

    # Validate JSON
    if not data.get("customer_name") or not data.get("party_size") or not data.get("datetime"):
        resp.message("Me faltan algunos datos, ¿puedes confirmarlos?")
        return Response(str(resp), media_type="application/xml")

    data["contact_phone"] = phone

    # Save reservation
    message = save_reservation(data)
    resp.message(message)

    memory_clear(phone)
    return Response(str(resp), media_type="application/xml")
