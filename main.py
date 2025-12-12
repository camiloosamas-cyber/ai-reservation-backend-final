import os
import re
from datetime import datetime, timedelta
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

app = FastAPI(title="Oriental IPS WhatsApp Bot", version="2.0.0")

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
    "school_name",
    "age",
    "cedula",
    "package",
    "date",
    "time",
]

PACKAGE_INFO = {
    "esencial": {
        "price": "45.000",
        "label": "cuidado esencial",
        "full": "Paquete Cuidado Esencial — 45.000 COP\nIncluye Medicina General, Optometría y Audiometría.",
    },
    "activa": {
        "price": "60.000",
        "label": "salud activa",
        "full": "Paquete Salud Activa — 60.000 COP\nIncluye Medicina General, Optometría, Audiometría y Psicología.",
    },
    "bienestar": {
        "price": "75.000",
        "label": "bienestar total",
        "full": "Paquete Bienestar Total — 75.000 COP\nIncluye Medicina General, Optometría, Audiometría, Psicología y Odontología.",
    },
}

FAQ_RESPONSES = {
    "ubicación": "Estamos ubicados en Calle 31 #29–61, Yopal.",
    "pago": "Aceptamos Nequi y efectivo.",
    "duración": "El examen dura entre 30 y 45 minutos.",
    "llevar": "Debes traer el documento del estudiante.",
    "domingo": "Sí, atendemos todos los días de 6am a 8pm.",
}

RELEVANT_KEYWORDS = [
    "cita", "reserv", "paquete", "precio", "colegio", "estudiante",
    "examen", "fecha", "hora", "ubic", "nequi", "pago",
    "duración", "llevar", "domingo", "esencial", "activa", "bienestar",
]

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
            "school_name": None,
            "age": None,
            "cedula": None,
            "package": None,
            "date": None,
            "time": None,
        }
    return SESSIONS[phone]

def reset_session(phone: str):
    SESSIONS[phone] = {
        "booking_started": False,
        "student_name": None,
        "school_name": None,
        "age": None,
        "cedula": None,
        "package": None,
        "date": None,
        "time": None,
    }

def is_relevant(msg: str):
    m = msg.lower()
    return any(k in m for k in RELEVANT_KEYWORDS)

# ============================================================
# EXTRACTORS
# ============================================================

def extract_package(msg: str):
    m = msg.lower()
    if any(k in m for k in ["esencial", "45", "verde"]):
        return "esencial"
    if any(k in m for k in ["activa", "psico", "60", "azul"]):
        return "activa"
    if any(k in m for k in ["bienestar", "total", "75", "odont"]):
        return "bienestar"
    return None

SCHOOL_PATTERNS = [
    r"colegio ([a-zA-Záéíóúñ0-9\s]+)",
    r"gimnasio ([a-zA-Záéíóúñ0-9\s]+)",
    r"instituto ([a-zA-Záéíóúñ0-9\s]+)",
]

def extract_school(msg: str):
    text = msg.lower()
    for p in SCHOOL_PATTERNS:
        m = re.search(p, text)
        if m:
            return m.group(1).strip().title()
    return None

def extract_student_name(msg: str):
    t = msg.lower()
    if t.startswith("hola") or t.startswith("buenos"):
        return None
    m = re.search(r"(se llama|el nombre es)\s+([a-zA-Záéíóúñ\s]+)", t)
    if m:
        return m.group(2).strip().title()
    return None

def extract_age(msg: str):
    m = re.search(r"(\d{1,2})\s*años", msg.lower())
    if m:
        return m.group(1)
    return None

def extract_cedula(msg: str):
    m = re.search(r"\b(\d{5,12})\b", msg)
    return m.group(1) if m else None

def extract_date(msg: str):
    text = msg.lower()
    if "mañana" in text:
        return (datetime.now(LOCAL_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
    d = dateparser.parse(msg, languages=["es"])
    if not d:
        return None
    d = d.replace(tzinfo=LOCAL_TZ)
    today = datetime.now(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    if d < today:
        return None
    return d.strftime("%Y-%m-%d")

def extract_time(msg: str):
    m = re.search(r"(?:a las )?(\d{1,2})(?:[:\.](\d{2}))?\s*(am|pm)?", msg.lower())
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    ampm = m.group(3)

    if ampm == "pm" and hour != 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0

    if hour < 6 or hour > 20:
        return None

    return f"{hour:02d}:{minute:02d}"

# ============================================================
# FAQ
# ============================================================

def check_faq(msg: str):
    m = msg.lower()
    if "ubic" in m or "direccion" in m or "dirección" in m:
        return FAQ_RESPONSES["ubicación"]
    if "nequi" in m or "pago" in m:
        return FAQ_RESPONSES["pago"]
    if "dur" in m:
        return FAQ_RESPONSES["duración"]
    if "llevar" in m:
        return FAQ_RESPONSES["llevar"]
    if "domingo" in m:
        return FAQ_RESPONSES["domingo"]
    return None

# ============================================================
# PACKAGE INFO
# ============================================================

def package_information(pkg: str):
    info = PACKAGE_INFO[pkg]["full"]
    return f"{info}\n\n¿Deseas agendar una cita?"

# ============================================================
# BOOKING LOGIC
# ============================================================

def detect_booking_intent(msg: str):
    m = msg.lower()
    return any(k in m for k in ["agendar", "reserv", "cita", "agenda"])

def update_session(msg: str, session: dict):
    updated = []

    pkg = extract_package(msg)
    if pkg and not session["package"]:
        session["package"] = pkg
        session["booking_started"] = True

    sn = extract_student_name(msg)
    if sn and not session["student_name"]:
        session["student_name"] = sn
        session["booking_started"] = True

    sc = extract_school(msg)
    if sc and not session["school_name"]:
        session["school_name"] = sc
        session["booking_started"] = True

    age = extract_age(msg)
    if age and not session["age"]:
        session["age"] = age
        session["booking_started"] = True

    ced = extract_cedula(msg)
    if ced and not session["cedula"]:
        session["cedula"] = ced
        session["booking_started"] = True

    dt = extract_date(msg)
    if dt and not session["date"]:
        session["date"] = dt
        session["booking_started"] = True

    tm = extract_time(msg)
    if tm and not session["time"]:
        session["time"] = tm
        session["booking_started"] = True

def missing_fields(session: dict):
    return [f for f in REQUIRED_FIELDS if not session.get(f)]

def ask_missing(session: dict):
    missing = missing_fields(session)
    if not missing:
        return None

    pkg = session.get("package")
    if pkg:
        p_label = PACKAGE_INFO[pkg]["label"]
        p_price = PACKAGE_INFO[pkg]["price"]
    else:
        p_label = ""
        p_price = ""

    if missing == ["date"]:
        return f"perfecto 😊, {p_label} {p_price}, solo necesito la fecha ..."

    if "student_name" in missing:
        return "¿Cuál es el nombre completo del estudiante?"
    if "school_name" in missing:
        return "¿De qué colegio es el estudiante?"
    if "age" in missing:
        return "¿Qué edad tiene el estudiante?"
    if "cedula" in missing:
        return "Por favor indícame la cédula del estudiante."
    if "package" in missing:
        return "¿Qué paquete deseas? Esencial, Salud Activa o Bienestar Total."
    if "date" in missing:
        return "¿Qué fecha deseas agendar?"
    if "time" in missing:
        return "¿A qué hora deseas agendar? (entre 6am y 8pm)"

def build_summary(session: dict):
    pkg = session["package"]
    label = PACKAGE_INFO[pkg]["label"]
    price = PACKAGE_INFO[pkg]["price"]

    return (
        "Ya tengo toda la información:\n\n"
        f"👤 Estudiante: {session['student_name']}\n"
        f"🎒 Colegio: {session['school_name']}\n"
        f"📦 Paquete: {label} {price}\n"
        f"📅 Fecha: {session['date']}\n"
        f"⏰ Hora: {session['time']}\n"
        f"🧒 Edad: {session['age']}\n"
        f"🪪 Cédula: {session['cedula']}\n\n"
        "¿Deseas confirmar esta cita? (Responde \"Confirmo\")"
    )

# ============================================================
# SUPABASE INSERT
# ============================================================

def insert_reservation(phone: str, session: dict):
    if not supabase:
        return True

    try:
        supabase.table("reservations").insert({
            "student_name": session["student_name"],
            "school_name": session["school_name"],
            "date": session["date"],
            "time": session["time"],
            "age": session["age"],
            "cedula": session["cedula"],
            "package": session["package"],
        }).execute()
        return True
    except Exception as e:
        print("DB ERROR:", e)
        return False

# ============================================================
# STATE MACHINE
# ============================================================

def handle_message(phone: str, msg: str):
    session = get_session(phone)
    t = msg.strip()

    # IGNORE IRRELEVANT
    if not is_relevant(t) and not session["booking_started"]:
        return ""

    # GREETING
    if t.lower().startswith(("hola", "buenos días", "buenas", "buenos dias")):
        if not session["booking_started"]:
            return "Buenos días, estás comunicado con Oriental IPS. ¿En qué te podemos ayudar?"

    # FAQ
    faq = check_faq(t)
    if faq and not session["booking_started"]:
        return faq

    # PACKAGE INFO
    pkg = extract_package(t)
    if pkg and not session["booking_started"]:
        return package_information(pkg)

    # BOOKING INTENT
    if detect_booking_intent(t):
        session["booking_started"] = True

    # UPDATE SESSION
    update_session(t, session)

    # CONFIRMATION
    if t.lower() == "confirmo":
        if all(session.get(f) for f in REQUIRED_FIELDS):
            ok = insert_reservation(phone, session)
            if not ok:
                return "Hubo un error registrando la cita. Intenta nuevamente."

            resp = (
                f"✅ ¡Cita confirmada!\n"
                f"El estudiante {session['student_name']} tiene su cita para el paquete {PACKAGE_INFO[session['package']]['label']}.\n"
                f"Fecha: {session['date']} a las {session['time']}.\n"
                f"¡Te esperamos! 😊"
            )

            reset_session(phone)
            return resp

        return "Aún faltan datos para poder confirmar la cita."

    # ASK MISSING
    missing_msg = ask_missing(session)
    if missing_msg:
        return missing_msg

    # SUMMARY
    if all(session.get(f) for f in REQUIRED_FIELDS):
        return build_summary(session)

    return ""

# ============================================================
# WEBHOOK
# ============================================================

@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    form = await request.form()
    msg = form.get("Body", "")
    phone = form.get("From", "").replace("whatsapp:", "")

    reply = handle_message(phone, msg)

    if reply == "":
        if TEST_MODE:
            return Response(content="", media_type="text/plain")
        r = MessagingResponse()
        return Response(content=str(r), media_type="application/xml")

    return twiml(reply)

@app.get("/")
def root():
    return {"status": "Oriental IPS Bot - OK"}

