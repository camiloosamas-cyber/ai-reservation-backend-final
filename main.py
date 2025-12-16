# ============================================================
# ORIENTAL IPS WHATSAPP BOT - main.py (v3.0.1 - SYNTAX FIXED)
# RULE-BASED, HUMAN-TONE, 100% DETERMINISTIC
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
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE")

supabase: SupabaseClient = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(title="Oriental IPS WhatsApp Bot", version="3.0.1")

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
        "label": "Cuidado Esencial",
        "full": "Paquete Cuidado Esencial cuesta $45.000 COP.\nIncluye: Medicina General, Optometria y Audiometria."
    },
    "activa": {
        "price": "60.000",
        "label": "Salud Activa",
        "full": "Paquete Salud Activa cuesta $60.000 COP.\nIncluye: Medicina General, Optometria, Audiometria y Psicologia."
    },
    "bienestar": {
        "price": "75.000",
        "label": "Bienestar Total",
        "full": "Paquete Bienestar Total cuesta $75.000 COP.\nIncluye: Medicina General, Optometria, Audiometria, Psicologia y Odontologia."
    },
}

FAQ_RESPONSES = {
    "ubicados": "Estamos ubicados en Calle 31 #29-61, Yopal.",
    "pago": "Aceptamos Nequi y efectivo.",
    "duracion": "El examen dura entre 30 y 45 minutos.",
    "llevar": "Debes traer el documento de identidad del estudiante.",
    "domingo": "Si, atendemos todos los dias de 6am a 8pm.",
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
            "awaiting_confirmation": False,
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
        "awaiting_confirmation": False,
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
    "agendar", "confirmo", "confirmar",
]

def is_relevant(msg: str):
    m = msg.lower()
    return any(k in m for k in RELEVANT_KEYWORDS)

# ============================================================
# EXTRACCIÓN DE DATOS
# ============================================================

def extract_package(msg: str):
    m = msg.lower()
    if any(k in m for k in ["esencial", "verde", "45k", "45000", "45.000"]):
        return "esencial"
    if any(k in m for k in ["activa", "salud activa", "azul", "psico", "60000", "60.000"]):
        return "activa"
    if any(k in m for k in ["bienestar", "total", "75k", "75000", "75.000", "completo", "odont"]):
        return "bienestar"
    return None


def extract_school(msg: str):
    t = msg.strip()
    l = t.lower()

    patterns = [
        r"del colegio\s+([a-zA-Záéíóúñ0-9\s]+)",
        r"colegio\s+([a-zA-Záéíóúñ0-9\s]+)",
        r"gimnasio\s+([a-zA-Záéíóúñ0-9\s]+)",
        r"instituto\s+([a-zA-Záéíóúñ0-9\s]+)",
        r"liceo\s+([a-zA-Záéíóúñ0-9\s]+)",
    ]

    for pat in patterns:
        m = re.search(pat, l)
        if m:
            school_name = m.group(1).strip()
            school_name = re.sub(r'\s+(es|son|tiene|años?).*$', '', school_name)
            return school_name.title()

    if any(k in l for k in ["gimnasio", "instituto", "liceo", "comfacasanare"]):
        clean = re.sub(r'\s+(es|son|tiene|años?).*$', '', t)
        return clean.strip().title()

    return None


def extract_student_name(msg: str):
    t = msg.strip()
    l = t.lower()

    if l in ["hola", "buenos días", "buenas tardes", "buenas noches", "buenas"]:
        return None

    if any(k in l for k in ["paquete", "precio", "cuesta", "incluye"]):
        return None

    patterns = [
        r"se llama\s+([a-zA-Záéíóúñ\s]+)",
        r"el nombre es\s+([a-zA-Záéíóúñ\s]+)",
        r"nombre:\s*([a-zA-Záéíóúñ\s]+)",
        r"para (mi|el|la)\s+(hijo|hija|niño|niña|estudiante)\s+([a-zA-Záéíóúñ\s]+)",
        r"mi\s+(hijo|hija|niño|niña)\s+([a-zA-Záéíóúñ\s]+)",
    ]

    for pat in patterns:
        m = re.search(pat, l)
        if m:
            name = m.groups()[-1].strip()
            name = re.sub(r'\s+(de|del|tiene|años?).*$', '', name)
            return name.title()

    words = t.split()
    if len(words) >= 2:
        valid_name_words = []
        for w in words:
            if len(w) >= 2 and re.match(r'^[a-zA-Záéíóúñ]+$', w):
                valid_name_words.append(w)
        
        if len(valid_name_words) >= 2:
            combined = " ".join(valid_name_words).lower()
            if not any(k in combined for k in ["buenos", "buenas", "gracias", "confirmo", "cita", "paquete"]):
                return " ".join(valid_name_words).title()

    return None


def extract_age(msg: str):
    t = msg.lower()
    
    m = re.search(r'(\d{1,2})\s*años?', t)
    if m:
        age = int(m.group(1))
        if 3 <= age <= 18:
            return str(age)
    
    m = re.search(r'(?:edad|tiene)\s*(\d{1,2})', t)
    if m:
        age = int(m.group(1))
        if 3 <= age <= 18:
            return str(age)
    
    if t.strip().isdigit():
        age = int(t.strip())
        if 3 <= age <= 18:
            return str(age)
    
    return None


def extract_cedula(msg: str):
    m = re.search(r'\b(\d{7,12})\b', msg)
    if m:
        return m.group(1)
    return None


def extract_date(msg: str):
    d = dateparser.parse(msg, settings={
        "TIMEZONE": "America/Bogota",
        "TO_TIMEZONE": "America/Bogota",
        "PREFER_DATES_FROM": "future",
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
    
    m = re.search(r'(\d{1,2})(?:[:\.](\d{2}))?\s*(am|pm)', t)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        ampm = m.group(3)

        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0

        if 6 <= hour <= 20:
            return f"{hour:02d}:{minute:02d}"
    
    m = re.search(r'(?:las|a las)\s+(\d{1,2})', t)
    if m:
        hour = int(m.group(1))
        if 6 <= hour <= 20:
            return f"{hour:02d}:00"

    return None

# ============================================================
# FAQ HANDLING
# ============================================================

def check_faq(msg: str):
    t = msg.lower()
    if "ubicad" in t or "direcc" in t or "dónde" in t or "donde" in t:
        return FAQ_RESPONSES["ubicados"]
    if "pago" in t or "nequi" in t or "efectivo" in t:
        return FAQ_RESPONSES["pago"]
    if "dur" in t or "demora" in t:
        return FAQ_RESPONSES["duracion"]
    if "llevar" in t or "traer" in t or "documento" in t:
        return FAQ_RESPONSES["llevar"]
    if "domingo" in t or "horario" in t:
        return FAQ_RESPONSES["domingo"]
    return None

# ============================================================
# PACKAGE INFO
# ============================================================

def package_information(pkg: str):
    return PACKAGE_DATA[pkg]["full"] + "\n\nDeseas agendar una cita?"

# ============================================================
# INTENCIÓN DE AGENDAR
# ============================================================

def detect_booking_intent(msg: str):
    t = msg.lower()
    return any(k in t for k in ["agendar", "reservar", "reserv", "cita", "quiero agendar", "apartar", "sacar cita"])

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


def get_field_prompt(field: str):
    prompts = {
        "student_name": "Cual es el nombre completo del estudiante?",
        "school": "De que colegio es?",
        "age": "Que edad tiene?",
        "cedula": "Cual es el numero de cedula del estudiante?",
        "package": "Perfecto. Tenemos 3 paquetes:\n\nCuidado Esencial - $45.000\n(Medicina General, Optometria, Audiometria)\n\nSalud Activa - $60.000\n(Esencial + Psicologia)\n\nBienestar Total - $75.000\n(Salud Activa + Odontologia)\n\nCual paquete deseas?",
        "date": "Para que fecha deseas la cita? (ejemplo: 15 de enero)",
        "time": "A que hora prefieres? (ejemplo: 10am o 3pm)"
    }
    return prompts.get(field, "")


def acknowledge_field(field: str, value: str):
    responses = {
        "student_name": f"Perfecto, {value}.",
        "school": f"Entendido, {value}.",
        "age": f"Ok, {value} años.",
        "cedula": f"Cedula {value} registrada.",
        "package": "Paquete seleccionado.",
        "date": "Fecha anotada.",
        "time": "Hora confirmada.",
    }
    return responses.get(field, "")

# ============================================================
# SUMMARY MESSAGE
# ============================================================

def build_summary(session: dict):
    pkg = session["package"]
    label = PACKAGE_DATA[pkg]["label"]
    price = PACKAGE_DATA[pkg]["price"]

    return (
        "Perfecto, ya tengo toda la informacion:\n\n"
        f"Estudiante: {session['student_name']}\n"
        f"Colegio: {session['school']}\n"
        f"Edad: {session['age']} años\n"
        f"Cedula: {session['cedula']}\n"
        f"Paquete: {label} (${price})\n"
        f"Fecha: {session['date']}\n"
        f"Hora: {session['time']}\n\n"
        "Deseas confirmar esta cita? Responde Confirmo para agendar."
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
            "age": int(session["age"]),
            "cedula": session["cedula"],
            "business_id": 2,
            "table_number": table,
            "status": "confirmado"
        }).execute()

        return True, table

    except Exception as e:
        print(f"Error inserting reservation: {e}")
        return False, str(e)


# ============================================================
# MAIN CONVERSATION LOGIC
# ============================================================

def handle_message(phone: str, msg: str):
    session = get_session(phone)
    t = msg.strip()
    l = t.lower()

    is_greeting = any(l.startswith(g) for g in ["hola", "buenos", "buenas", "buen día"])

    if not session["booking_started"] and not is_relevant(t) and not is_greeting:
        return ""

    if not session["booking_started"] and is_greeting and not is_relevant(t):
        return "Buenos dias, estas comunicado con Oriental IPS. En que te podemos ayudar?"

    if not session["booking_started"]:
        ans = check_faq(t)
        if ans:
            return ans

    pkg = extract_package(t)
    if pkg and not session["booking_started"]:
        if detect_booking_intent(t):
            session["package"] = pkg
            session["booking_started"] = True
            missing = missing_fields(session)
            if missing:
                return get_field_prompt(missing,[object Object],)
        return package_information(pkg)

    if detect_booking_intent(t) and not session["booking_started"]:
        session["booking_started"] = True
        missing = missing_fields(session)
        if missing:
            return "Perfecto, voy a ayudarte a agendar la cita. " + get_field_prompt(missing,[object Object],)

    updated = update_session_with_extracted_data(t, session)

    if "confirmo" in l or "confirmar" in l or l == "si" or l == "sí":
        if session.get("awaiting_confirmation"):
            missing = missing_fields(session)
            if not missing:
                ok, table = insert_reservation(phone, session)
                if ok:
                    name = session["student_name"]
                    pkg_label = PACKAGE_DATA[session["package"]]["label"]
                    date = session["date"]
                    time = session["time"]
                    reset_session(phone)
                    return (
                        f"Cita confirmada!\n\n"
                        f"El estudiante {name} tiene su cita para el paquete {pkg_label}.\n"
                        f"Fecha: {date}\n"
                        f"Hora: {time}\n"
                        f"Mesa: {table}\n\n"
                        f"Te esperamos en Oriental IPS!"
                    )
                return "Hubo un error registrando la cita. Por favor intenta nuevamente."

    if updated:
        ack = acknowledge_field(updated,[object Object],, session[updated,[object Object],])
        missing = missing_fields(session)
        
        if not missing:
            session["awaiting_confirmation"] = True
            if ack:
                return f"{ack}\n\n{build_summary(session)}"
            return build_summary(session)
        else:
            next_prompt = get_field_prompt(missing,[object Object],)
            if ack:
                return f"{ack} {next_prompt}"
            return next_prompt

    if session["booking_started"]:
        missing = missing_fields(session)
        if missing:
            return get_field_prompt(missing,[object Object],)

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

    if reply == "":
        if TEST_MODE:
            return Response(content="", media_type="text/plain")
        return Response(content=str(MessagingResponse()), media_type="application/xml")

    return twiml(reply)


# ============================================================
# ROOT
# ============================================================

@app.get("/")
def root():
    return {
        "status": "Oriental IPS WhatsApp Bot running",
        "version": "3.0.1",
        "fixes": ["All syntax errors fixed", "Removed special characters causing issues"]
    }


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "supabase_connected": supabase is not None,
        "active_sessions": len(SESSIONS)
    }
