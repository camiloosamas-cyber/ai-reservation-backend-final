# ============================================================
# ORIENTAL IPS WHATSAPP BOT - main.py (v2.1.1)
# COMPLETELY RULE-BASED, HUMAN-TONE, 100% DETERMINISTIC
# ============================================================

import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from twilio.twiml.messaging_response import MessagingResponse

import dateparser
from supabase import create_client, Client as SupabaseClient

# ============================================================
# CONFIGURACIÓN INICIAL
# ============================================================

os.chdir(os.path.dirname(os.path.abspath(__file__)))
TEST_MODE = os.getenv("TEST_MODE") == "1"

try:
    LOCAL_TZ = ZoneInfo("America/Bogota")
except ZoneInfoNotFoundError:
    LOCAL_TZ = ZoneInfo("UTC")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: SupabaseClient = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(title="Oriental IPS WhatsApp Bot", version="2.1.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

# ============================================================
# SESIONES EN MEMORIA
# ============================================================

SESSIONS = {}

REQUIRED_FIELDS = [
    "student_name",
    "school",
    "age",
    "cedula",
    "package",
    "date",
    "time",
]

PACKAGE_DATA = {
    "esencial": {
        "price": "45.000",
        "label": "cuidado esencial",
        "full":
            "🧾 Paquete Cuidado Esencial cuesta 45.000 COP.\n"
            "Incluye: Medicina General, Optometría y Audiometría."
    },
    "activa": {
        "price": "60.000",
        "label": "salud activa",
        "full":
            "🧾 Paquete Salud Activa cuesta 60.000 COP.\n"
            "Incluye: Esencial + Psicología."
    },
    "bienestar": {
        "price": "75.000",
        "label": "bienestar total",
        "full":
            "🧾 Paquete Bienestar Total cuesta 75.000 COP.\n"
            "Incluye: Salud Activa + Odontología."
    },
}

FAQ_RESPONSES = {
    "ubicados": "Estamos ubicados en Calle 31 #29–61, Yopal.",
    "pago": "Aceptamos Nequi y efectivo.",
    "duracion": "El examen dura entre 30 y 45 minutos.",
    "llevar": "Debes traer el documento del estudiante.",
    "domingo": "Sí, atendemos todos los días de 6am a 8pm.",
}

# ============================================================
# UTILIDADES
# ============================================================

def twiml(message: str):
    if TEST_MODE:
        return Response(content=message, media_type="text/plain")
    r = MessagingResponse()
    r.message(message)
    return Response(content=str(r), media_type="application/xml")


def get_session(phone: str):
    if phone not in SESSIONS:
        SESSIONS[phone] = {
            "booking_started": False,
            "student_name": None,
            "school": None,
            "age": None,
            "cedula": None,
            "package": None,
            "date": None,
            "time": None,
            "emoji_used": False,
            "sent_full_prompt": False,
            "sent_summary": False,
        }
    return SESSIONS[phone]


def reset_session(phone: str):
    SESSIONS[phone] = {
        "booking_started": False,
        "student_name": None,
        "school": None,
        "age": None,
        "cedula": None,
        "package": None,
        "date": None,
        "time": None,
        "emoji_used": False,
        "sent_full_prompt": False,
        "sent_summary": False,
    }

# ============================================================
# DETECCIÓN DE RELEVANCIA
# ============================================================

RELEVANT_KEYWORDS = [
    "cita", "reserv", "paquete", "precio", "precios",
    "colegio", "estudiante", "examenes", "exámenes",
    "fecha", "hora", "ubicados", "pago", "nequi",
    "duración", "llevar", "domingo",
    "esencial", "activa", "bienestar",
    "psico", "odont", "optometr", "audio", "medicina",
]

def is_relevant(msg: str):
    m = msg.lower()
    return any(k in m for k in RELEVANT_KEYWORDS)
# ============================================================
# EXTRACCIÓN DE DATOS
# ============================================================

def extract_package(msg: str):
    m = msg.lower()
    if any(k in m for k in ["esencial", "verde", "45k", "45000", "kit escolar"]):
        return "esencial"
    if any(k in m for k in ["activa", "salud activa", "azul", "psico", "60000"]):
        return "activa"
    if any(k in m for k in ["bienestar", "total", "75k", "completo", "odont"]):
        return "bienestar"
    return None


def extract_school(msg: str):
    t = msg.lower()

    patterns = [
        r"del colegio\s+([a-zA-Záéíóúñ0-9\s]+)",
        r"colegio\s+([a-zA-Záéíóúñ0-9\s]+)",
        r"gimnasio\s+([a-zA-Záéíóúñ0-9\s]+)",
        r"instituto\s+([a-zA-Záéíóúñ0-9\s]+)",
    ]

    for pat in patterns:
        m = re.search(pat, t)
        if m:
            return m.group(1).strip().title()

    return None


def extract_student_name(msg: str):
    t = msg.lower()

    if t.startswith("hola") or t.startswith("buenos") or t.startswith("buenas"):
        return None

    patterns = [
        r"se llama\s+([a-zA-Záéíóúñ\s]+)",
        r"el nombre es\s+([a-zA-Záéíóúñ\s]+)",
        r"para (mi|el|la)\s+(hijo|hija|niño|niña|estudiante)\s+([a-zA-Záéíóúñ\s]+)",
        r"mi\s+(hijo|hija|niño|niña)\s+([a-zA-Záéíóúñ\s]+)",
        r"para\s+([a-zA-Záéíóúñ\s]+)\s+del colegio",
    ]

    for pat in patterns:
        m = re.search(pat, t)
        if m:
            # last group always name
            return m.groups()[-1].strip().title()

    return None


def extract_age(msg: str):
    t = msg.lower()
    m = re.search(r"(\d{1,2})\s*años", t)
    if m:
        return m.group(1)
    m = re.search(r"edad\s*(\d{1,2})", t)
    if m:
        return m.group(1)
    return None


def extract_cedula(msg: str):
    m = re.search(r"\b(\d{5,12})\b", msg)
    return m.group(1) if m else None


def extract_date(msg: str):
    d = dateparser.parse(msg, settings={
        "TIMEZONE": "America/Bogota",
        "TO_TIMEZONE": "America/Bogota",
    })
    if not d:
        return None
    d = d.astimezone(LOCAL_TZ)

    today = datetime.now(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    if d < today:
        return None

    return d.strftime("%Y-%m-%d")


def extract_time(msg: str):
    t = msg.lower()
    m = re.search(r"(\d{1,2})(?:[:\.](\d{2}))?\s*(am|pm)?", t)
    if not m:
        return None

    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    ampm = m.group(3)

    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0

    if hour < 6 or hour > 20:
        return None

    return f"{hour:02d}:{minute:02d}"

# ============================================================
# FAQ HANDLING
# ============================================================

def check_faq(msg: str):
    t = msg.lower()
    if "ubicad" in t or "direcc" in t:
        return FAQ_RESPONSES["ubicados"]
    if "pago" in t or "nequi" in t:
        return FAQ_RESPONSES["pago"]
    if "dur" in t:
        return FAQ_RESPONSES["duracion"]
    if "llevar" in t:
        return FAQ_RESPONSES["llevar"]
    if "domingo" in t:
        return FAQ_RESPONSES["domingo"]
    return None

# ============================================================
# PACKAGE INFO
# ============================================================

def package_information(pkg: str, ask=True):
    info = PACKAGE_DATA[pkg]["full"]
    if ask:
        return f"{info}\n\n¿Deseas agendar una cita?"
    return info

# ============================================================
# INTENCIÓN DE AGENDAR
# ============================================================

def detect_booking_intent(msg: str):
    t = msg.lower()
    return any(k in t for k in ["agendar", "reservar", "reserv", "cita", "quiero agendar"])

# ============================================================
# UPDATE SESSION WITH EXTRACTED FIELDS
# ============================================================

def update_session_with_extracted_data(msg: str, session: dict):
    updated = []

    extractors = [
        ("package", extract_package),
        ("student_name", extract_student_name),
        ("school", extract_school),
        ("age", extract_age),
        ("cedula", extract_cedula),
        ("date", extract_date),
        ("time", extract_time),
    ]

    for field, fn in extractors:
        if not session[field]:
            val = fn(msg)
            if val:
                session[field] = val
                session["booking_started"] = True
                updated.append(field)

    return updated

# ============================================================
# MISSING FIELDS & PROMPTS
# ============================================================

def missing_fields(session: dict):
    return [f for f in REQUIRED_FIELDS if not session.get(f)]


FIELD_LABELS = {
    "student_name": "el nombre del estudiante",
    "school": "el colegio",
    "age": "la edad",
    "cedula": "la cédula",
    "package": "el paquete",
    "date": "la fecha",
    "time": "la hora",
}

def build_human_missing_prompt(session: dict):
    missing = missing_fields(session)
    if not missing:
        return None

    # Construcción humana
    text = ", ".join(FIELD_LABELS[f] for f in missing)

    if not session["emoji_used"]:
        session["emoji_used"] = True
        session["sent_full_prompt"] = True
        return f"Claro, mira 😊, solo necesito {text}."

    return f"Solo necesito {text}."

# ============================================================
# SUMMARY MESSAGE
# ============================================================

def build_summary(session: dict):
    pkg = session["package"]
    label = PACKAGE_DATA[pkg]["label"]
    price = PACKAGE_DATA[pkg]["price"]

    return (
        "Ya tengo toda la información:\n\n"
        f"👤 Estudiante: {session['student_name']}\n"
        f"🎒 Colegio: {session['school']}\n"
        f"📦 Paquete: {label} {price}\n"
        f"📅 Fecha: {session['date']}\n"
        f"⏰ Hora: {session['time']}\n"
        f"🧒 Edad: {session['age']}\n"
        f"🪪 Cédula: {session['cedula']}\n\n"
        "¿Deseas confirmar esta cita? (Responde \"Confirmo\")"
    )
# ============================================================
# SUPABASE INSERT & TABLE ASSIGNMENT
# ============================================================

def assign_table():
    if not supabase:
        return "T1"
    try:
        res = supabase.table("reservations").select("table_number").eq("business_id", 2).execute()
        nums = [r["table_number"] for r in res.data]
        return f"T{len(nums) + 1}"
    except:
        return "T1"


def insert_reservation(phone: str, session: dict):
    if not supabase:
        return True, "T1"

    table = assign_table()

    dt = datetime.strptime(
        f"{session['date']} {session['time']}",
        "%Y-%m-%d %H:%M"
    )
    dt_iso = dt.astimezone(LOCAL_TZ).isoformat()

    try:
        supabase.table("reservations").insert({
            "student_name": session["student_name"],
            "phone": phone,
            "datetime": dt_iso,
            "school": session["school"],
            "package": session["package"],
            "age": session["age"],
            "cedula": session["cedula"],
            "business_id": 2,
            "table_number": table,
            "status": "confirmado"
        }).execute()

        return True, table

    except Exception as e:
        return False, str(e)


# ============================================================
# MAIN CONVERSATION LOGIC
# ============================================================

def handle_message(phone: str, msg: str):
    session = get_session(phone)
    t = msg.strip()
    l = t.lower()

    # Greeting detection
    is_greeting = l.startswith("hola") or l.startswith("buenos") or l.startswith("buenas")

    # 1. Silence if irrelevant, not greeting, and not booking
    if not session["booking_started"] and not is_relevant(t) and not is_greeting:
        return ""

    # 2. Greeting
    if not session["booking_started"] and is_greeting:
        return "Buenos días, estás comunicado con Oriental IPS. ¿En qué te podemos ayudar?"

    # 3. FAQs
    if not session["booking_started"]:
        ans = check_faq(t)
        if ans:
            return ans

    # 4. Package information
    pkg = extract_package(t)
    if pkg and not session["booking_started"]:
        if detect_booking_intent(t):
            session["package"] = pkg
            session["booking_started"] = True
            return build_human_missing_prompt(session)

        return package_information(pkg, ask=True)

    # 5. Detect booking intent
    if detect_booking_intent(t):
        session["booking_started"] = True

    # 6. Extraction
    updated = update_session_with_extracted_data(t, session)

    if not updated and not detect_booking_intent(t) and not is_relevant(t):
        return ""

    # 7. Confirm appointment
    if l == "confirmo":
        if all(session.get(f) for f in REQUIRED_FIELDS):
            ok, table = insert_reservation(phone, session)
            if ok:
                name = session["student_name"]
                pkg = session["package"]
                label = PACKAGE_DATA[pkg]["label"]
                date = session["date"]
                time = session["time"]
                reset_session(phone)
                return (
                    f"✅ ¡Cita confirmada!\n"
                    f"El estudiante {name} tiene su cita para el paquete {label}.\n"
                    f"Fecha: {date} a las {time}.\n"
                    f"Te atenderemos en la mesa {table}.\n"
                    f"¡Te esperamos!"
                )
            return "Hubo un error registrando la cita. Intenta nuevamente."
        return ""

    # 8. Missing fields
    missing = missing_fields(session)

    if missing:
        if len(missing) > 1 and not session["sent_full_prompt"]:
            return build_human_missing_prompt(session)
        return build_human_missing_prompt(session)

    # 9. Send summary only once
    if not session["sent_summary"]:
        session["sent_summary"] = True
        return build_summary(session)

    return ""

# ============================================================
# TWILIO WEBHOOK
# ============================================================

@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    form = await request.form()
    text = form.get("Body", "")
    phone = form.get("From", "").replace("whatsapp:", "")

    reply = handle_message(phone, text)

    # SILENCIO TOTAL
    if reply == "":
        if TEST_MODE:
            return Response(content="", media_type="text/plain")
        return Response(content=str(MessagingResponse()), media_type="application/xml")

    # RESPUESTA NORMAL
    return twiml(reply)


# ============================================================
# ROOT
# ============================================================

@app.get("/")
def root():
    return {"status": "Oriental IPS WhatsApp Bot running", "version": "2.1.1"}
