import os
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

import dateparser
from supabase import create_client, Client, PostgrestAPIError
from openai import OpenAI
from twilio.twiml.messaging_response import MessagingResponse
from dateutil import parser as dateutil_parser

# ---------------------------------------------------------
# 1. CONFIGURATION & INITIALIZATION
# ---------------------------------------------------------

# Required for correct file paths on Render/Railway
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# VERSION STAMP
app = FastAPI(title="AI Reservation System", version="1.0.54")
print("🚀 AI Reservation System Loaded — Version 1.0.54 (Full Normalization Consistency Fix)")

# Timezone
try:
    LOCAL_TZ = ZoneInfo("America/Bogota")
except ZoneInfoNotFoundError:
    LOCAL_TZ = ZoneInfo("UTC")

# Static + Templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# CORS (safe default)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------
# 2. EXTERNAL SERVICE INITIALIZATION
# ---------------------------------------------------------

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE = os.getenv("SUPABASE_SERVICE_ROLE")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

supabase = None
openai_client = None

try:
    if SUPABASE_URL and SUPABASE_SERVICE_ROLE:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE)
    else:
        print("WARNING: Missing Supabase credentials.")

    if OPENAI_API_KEY:
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
    else:
        print("WARNING: Missing OpenAI API key.")

except Exception as e:
    print(f"FATAL ERROR loading external services: {e}")

# Business metadata
RESERVATION_TABLE = "reservations"
SESSION_TABLE = "sessions"
TABLE_LIMIT = 10
BUSINESS_ID = 2  # For this IPS

# ---------------------------------------------------------
# 3. SESSION MANAGEMENT
# ---------------------------------------------------------

DEFAULT_SESSION = {
    "phone": None,
    "student_name": None,
    "school": None,
    "package": None,
    "date": None,
    "time": None,
    "age": None,
    "cedula": None,
    "booking_started": False,
    "info_mode": False,
    "greeted": False,
    "awaiting_confirmation": False
}

def get_session(phone: str) -> dict:
    """Retrieve or create session for a phone number."""

    if not supabase:
        s = DEFAULT_SESSION.copy()
        s["phone"] = phone
        return s

    try:
        response = (
            supabase.table(SESSION_TABLE)
            .select("data")
            .eq("phone", phone)
            .maybe_single()
            .execute()
        )

        if not response or not response.data:
            s = DEFAULT_SESSION.copy()
            s["phone"] = phone
            return s

        if response.data.get("data") is None:
            s = DEFAULT_SESSION.copy()
            s["phone"] = phone
            return s

        stored = response.data["data"]
        stored["phone"] = phone
        return {**DEFAULT_SESSION, **stored}

    except Exception:
        s = DEFAULT_SESSION.copy()
        s["phone"] = phone
        return s


def save_session(session: dict):
    """Save updated session to Supabase."""
    if not supabase:
        return

    phone = session.get("phone")
    if not phone:
        return

    data = {k: v for k, v in session.items() if k != "phone"}

    try:
        supabase.table(SESSION_TABLE).upsert(
            {
                "phone": phone,
                "data": data,
                "last_updated": datetime.now(LOCAL_TZ).isoformat()
            }
        ).execute()
    except Exception as e:
        print(f"Error saving session for {phone}: {e}")
# ---------------------------------------------------------
# 4. NLP EXTRACTION ENGINE (STUDENT, SCHOOL, PACKAGE, DATE, TIME)
# ---------------------------------------------------------

def detect_package(msg: str) -> str | None:
    """Detects the IPS package based on keywords, colors, or prices."""
    text = msg.lower()

    # Esencial
    if any(w in text for w in ["esencial", "verde", "45k", "45 mil", "45000", "paquete esencial"]):
        return "Paquete Cuidado Esencial"

    # Activa (includes PSICO)
    if any(w in text for w in ["activa", "azul", "psico", "psicología", "psicologia",
                               "60k", "60 mil", "60000", "paquete activa"]):
        return "Paquete Salud Activa"

    # Total / Bienestar
    if any(w in text for w in ["total", "bienestar", "amarillo", "completo",
                               "75k", "75 mil", "75000", "bienestar total"]):
        return "Paquete Bienestar Total"

    return None


# ---------------------------------------------------------
# STUDENT NAME EXTRACTION
# ---------------------------------------------------------

def extract_student_name(msg: str, current_name: str | None) -> str | None:
    """
    Extracts student name, preventing overwrite if a name exists,
    and ignoring school-related phrases.
    """
    text = msg.lower()

    # Do not overwrite name unless explicit intent to change is clear
    if current_name and not any(k in text for k in ["cambiar nombre", "otro nombre"]):
        return current_name
    
    # Ignore any message containing clear school/institution keywords
    school_keywords = ["colegio", "sede", "institución", "institucion", "school", "liceo", "gimnasio"]
    if any(k in text for k in school_keywords):
        return None 

    raw = None

    # Pattern 1: "el nombre es X", "se llama X", "para mi hijo/hija X"
    prefix = r"(?:el\s+nombre\s+es|se\s+llama|para\s+mi\s+hijo|para\s+mi\s+hija)\s+([a-záéíóúñ ]+)"
    m = re.search(prefix, text)
    if m:
        raw = m.group(1).strip()
    else:
        # Pattern 2: Try extracting 2–4 capitalized words (alpha-only)
        # Use re.search to find the name anywhere in the message.
        m2 = re.search(r"([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+){1,3})", msg)
        if m2:
            raw = m2.group(1)
            
    if not raw:
        return None

    # Cut off trailing info (cedula, time, date) - simplified from original
    raw = re.split(r"(cedula|cédula|edad|años|a las|\d{1,2}:\d{2})", raw)[0].strip()

    # Clean and format
    parts = raw.split()
    
    # Validation: 1 to 4 words, alpha-only (no digits, no non-name junk)
    if 1 <= len(parts) <= 4 and all(w.isalpha() for w in parts):
        return " ".join(w.capitalize() for w in parts)

    return None


# ---------------------------------------------------------
# SCHOOL EXTRACTION
# ---------------------------------------------------------

def extract_school_name(msg: str) -> str | None:
    text = msg.lower()

    patterns = [
        r"(?:colegio|gimnasio|liceo|instituto|escuela)\s+([a-záéíóúñ0-9 ]+)",
        r"(?:del\s+colegio|del\s+gimnasio|del\s+liceo|de\s+la\s+escuela)\s+([a-záéíóúñ0-9 ]+)"
    ]

    for p in patterns:
        m = re.search(p, text)
        if m:
            name = m.group(1).strip()
            name = re.split(r"[.,!?]", name)[0].strip()
            if len(name) > 1:
                return name.title()

    return None


# ---------------------------------------------------------
# AGE & CEDULA EXTRACTION
# ---------------------------------------------------------

def extract_age_cedula(msg: str, session: dict):
    text = msg.lower()

    # AGE
    if not session.get("age"):
        m = re.search(r"(?:edad\s+(\d{1,2}))|(\d{1,2})\s*(años|anos)", text)
        if m:
            age_val = m.group(1) or m.group(2)
            if age_val:
                age = int(age_val)
                if 5 <= age <= 25:  # Reasonable school age
                    session["age"] = age

    # CEDULA
    if not session.get("cedula"):
        # Pattern: exactly 5-12 digits, often isolated by word boundaries
        m2 = re.search(r"\b\d{5,12}\b", msg)
        if m2:
            session["cedula"] = m2.group(0)


# ---------------------------------------------------------
# DATE & TIME EXTRACTION + INVALID TIME VALIDATION
# ---------------------------------------------------------

def extract_datetime_info(msg: str) -> tuple[str, str]:
    """
    Extracts date and time.
    Returns (date_str, "INVALID") if time is found but invalid.
    """
    text = msg.lower()
    today = datetime.now(LOCAL_TZ).date()

    date_str = ""
    time_str = ""

    # ---------- DATE DETECTION ----------
    if "mañana" in text:
        date_str = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    elif "pasado mañana" in text:
        date_str = (today + timedelta(days=2)).strftime("%Y-%m-%d")
    elif "hoy" in text:
        date_str = today.strftime("%Y-%m-%d")

    # Fallback: dateparser for “el viernes”, “10/12”, "domingo"
    dt = dateparser.parse(
        msg,
        languages=["es"],
        settings={
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TIMEZONE": LOCAL_TZ.key,
            "TO_TIMEZONE": LOCAL_TZ.key,
            "PREFER_DATES_FROM": "future"
        }
    )
    
    if dt:
        date_str_parsed = dt.strftime("%Y-%m-%d")
        if not date_str or date_str_parsed != today.strftime("%Y-%m-%d"):
            date_str = date_str_parsed

    # Reject past dates
    if date_str:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            if d < today:
                date_str = "" 
        except:
            date_str = ""
            pass

    if not date_str:
        return "", "" # No date found

    # ---------- TIME DETECTION ----------
    # Anchor the regex to detect time context only ("a las" or "am/pm")
    explicit = re.search(
        r"(?:a\s+las\s+)?(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)\b"
        r"|a\s+las\s+(\d{1,2})(?:[.:](\d{2}))?\b",
        text
    )

    if explicit:
        # Determine which capture groups (1-3 or 4-5) matched
        if explicit.group(1): # Matches "a las 11am" or "11:30 pm"
            hour = int(explicit.group(1))
            minute = int(explicit.group(2) or 0) 
            marker = explicit.group(3)
        elif explicit.group(4): # Matches "a las 11"
            hour = int(explicit.group(4))
            minute = int(explicit.group(5) or 0)
            marker = None 
        else:
            # Safety net: regex matched but didn't fit expected groups
            return date_str, "INVALID" 

        # INVALID TIMES 
        if hour > 23 or minute > 59:
            return date_str, "INVALID" 

        # Convert am/pm
        if marker:
            marker = marker.lower()
            if "pm" in marker and hour != 12:
                hour += 12
            if "am" in marker and hour == 12:
                hour = 0
            
        # Heuristic for hours without am/pm (e.g. "a las 3")
        elif explicit.group(4):
            if 1 <= hour <= 6: 
                 hour += 12 # Assume 3 means 3 PM (15:00)
            elif hour == 12: 
                hour = 12 
            elif 7 <= hour <= 11:
                pass 

        # Time window restriction → 7am (7) to 5pm (17)
        if not (7 <= hour <= 17):
            return date_str, "INVALID"

        time_str = f"{hour:02d}:{minute:02d}"
        return date_str, time_str

    # ---------- VAGUE TIME RULES ----------
    if "mañana en la tarde" in text or "en la tarde" in text:
        return date_str, "15:00"

    if "mañana en la mañana" in text or "en la mañana" in text:
        return date_str, "09:00"

    if "en la noche" in text:
        # Outside window, but clear intent.
        return date_str, "19:00" 

    # If dateparser detected a time by itself (last resort)
    if dt and dt.date().strftime("%Y-%m-%d") == date_str:
        t = dt.time()
        if 7 <= t.hour <= 17:
            return date_str, t.strftime("%H:%M")

    return date_str, ""  # Missing but valid date found


# ---------------------------------------------------------
# SESSION UPDATE (CENTRAL EXTRACTOR)
# ---------------------------------------------------------

def update_session_with_info(msg: str, session: dict):
    
    new_name = extract_student_name(msg, session.get("student_name"))
    
    new_school = extract_school_name(msg)
    new_package = detect_package(msg)
    new_date, new_time = extract_datetime_info(msg)

    extract_age_cedula(msg, session)

    if new_name:
        session["student_name"] = new_name

    if new_school:
        session["school"] = new_school

    if new_package:
        session["package"] = new_package

    if new_date:
        session["date"] = new_date

    # Time validation (invalid → bot should ask again)
    if new_time == "":
        pass
    elif new_time == "INVALID":
        # Don't set the session time, let process_message handle the error response
        pass
    elif new_time:
        session["time"] = new_time

    save_session(session)
    return new_date, new_time
# ---------------------------------------------------------
# 5. INTENT DEFINITIONS
# ---------------------------------------------------------

INTENTS = {
    "greeting": {
        "patterns": [
            "hola", "buenos dias", "buenas", "buenas tardes",
            "buenas noches", "disculpe", "una pregunta",
            "informacion", "consulta"
        ],
        "handler": "handle_greeting"
    },

    # Package info patterns
    "package_info": {
        "patterns": [
            "cuanto vale", "cuánto vale", "precio", "valor",
            "psico", "psicología", "psicologia", "odontologia",
            "odontología", "paquete", "kit escolar", "esencial",
            "activa", "total", "bienestar", "el verde",
            "el azul", "el amarillo", "45k", "60k", "75k",
            "que paquetes ofrecen", "¿qué paquetes ofrecen", 
            "paquetes ofrecen", "paquetes"
        ],
        "handler": "handle_package_info"
    },

    "booking_request": {
        "patterns": [
            "quiero reservar", "quiero agendar", "necesito una cita",
            "me pueden reservar", "reservar cita", "reservar examen",
            "separar cita", "cita para", "para el examen",
            "para una cita", "para un paquete", "para mañana",
            "para hoy", "quiero el examen"
        ],
        "handler": "handle_booking_request"
    },

    "modify": {
        "patterns": [
            "cambiar cita", "cambiar la cita", "quiero cambiar",
            "mover cita", "reagendar", "cambiar hora",
            "cambiar fecha"
        ],
        "handler": "handle_modify"
    },

    "cancel": {
        "patterns": [
            "cancelar", "cancelar cita", "anular",
            "quitar la cita", "ya no quiero la cita"
        ],
        "handler": "handle_cancel"
    },

    "confirmation": {
        "patterns": ["confirmo", "sí confirmo", "si confirmo", "confirmar"],
        "handler": "handle_confirmation"
    }
}


# ---------------------------------------------------------
# 6. INTENT DETECTION (GUARDED DURING BOOKING FLOW)
# ---------------------------------------------------------

def detect_explicit_intent(msg: str, session: dict) -> str | None:
    msg_lower = msg.lower().strip()
    
    # ⚠️ FIX #2: Normalize for boundary issues in Spanish question marks (critical for matching)
    msg_lower = msg_lower.replace("¿", "")
    # Maintain consistency in the original message
    msg = msg.replace("¿", "") 

    # Work on a copy — never mutate global INTENTS
    local_intents = json.loads(json.dumps(INTENTS))

    if session.get("booking_started"):
        # Inside booking, ignore greeting intent
        local_intents["greeting"]["patterns"] = []
        # BUT allow package_info always (user may ask prices mid-flow)
        
    priority = ["cancel", "confirmation", "modify", "booking_request", "package_info", "greeting"]

    for intent in priority:
        for p in local_intents[intent]["patterns"]:
            if intent == "confirmation":
                if msg_lower == p and session.get("awaiting_confirmation"):
                    return intent
                continue

            # Use whole-word matching using regex boundary \b
            if re.search(rf"\b{re.escape(p)}\b", msg_lower):
                return intent

    return None


# ---------------------------------------------------------
# 7. MISSING-FIELDS ENGINE (ASK ONLY WHAT IS MISSING)
# ---------------------------------------------------------

def build_missing_fields_message(session: dict) -> str | None:
    missing = []

    if not session["student_name"]: missing.append("el *nombre* del estudiante")
    if not session["school"]: missing.append("el *colegio*")
    if not session["package"]: missing.append("el *paquete* (Esencial, Activa o Total)")
    if not session["date"] or not session["time"]: missing.append("la *fecha y hora*")
    if not session["age"]: missing.append("la *edad*")
    if not session["cedula"]: missing.append("la *cédula*")

    if not missing:
        return None

    if len(missing) == 1:
        return f"Perfecto 😊, solo me falta {missing[0]}. ¿Me lo compartes porfa?"

    items = ", ".join(missing[:-1]) + " y " + missing[-1]
    return f"Perfecto 😊, solo necesito {items}."


# ---------------------------------------------------------
# 8. BOOKING SUMMARY (BEFORE CONFIRMATION)
# ---------------------------------------------------------

def finish_booking_summary(session: dict) -> str:
    
    session["awaiting_confirmation"] = True
    save_session(session)

    # Explicitly map all fields for clean output, using "N/A" for missing data
    student_name = session.get('student_name', 'N/A')
    school = session.get('school', 'N/A')
    package = session.get('package', 'N/A')
    date = session.get('date', 'N/A')
    time = session.get('time', 'N/A')
    age = session.get('age', 'N/A')
    cedula = session.get('cedula', 'N/A')

    # Format Age and Cedula lines only if present
    age_line = f"🧒 Edad: {age}\n" if age != 'N/A' else ""
    ced_line = f"🪪 Cédula: {cedula}\n" if cedula != 'N/A' else ""
    
    # Removed the trailing newline from the summary body
    return (
        "Ya tengo toda la información:\n\n"
        f"👤 Estudiante: {student_name}\n"
        f"🎒 Colegio: {school}\n"
        f"📦 Paquete: {package}\n"
        f"📅 Fecha: {date}\n"
        f"⏰ Hora: {time}\n"
        f"{age_line}"
        f"{ced_line}"
        "\n¿Deseas confirmar esta cita? (Responde *Confirmo*)"
    )


# ---------------------------------------------------------
# 9. HANDLERS
# ---------------------------------------------------------

# GREETING (Final IPS version)
def handle_greeting(msg, session):
    session["greeted"] = True
    return "Buenos días 😊. Estás comunicado(a) con Oriental IPS. ¿En qué te podemos ayudar?"


# PACKAGE INFO HANDLER
def handle_package_info(msg, session):
    session["awaiting_confirmation"] = False

    pkg = detect_package(msg) or session.get("package")

    prices = {
        "Paquete Cuidado Esencial": "45.000",
        "Paquete Salud Activa": "60.000",
        "Paquete Bienestar Total": "75.000",
    }

    details = {
        "Paquete Cuidado Esencial": "Medicina General, Optometría y Audiometría.",
        "Paquete Salud Activa": "Esencial + Psicología.",
        "Paquete Bienestar Total": "Activa + Odontología.",
    }

    if pkg:
        session["package"] = pkg
        save_session(session)

        if session.get("booking_started"):
            missing = build_missing_fields_message(session)
            if missing:
                return (
                    f"📋 {pkg} cuesta {prices[pkg]} COP.\n"
                    f"Incluye: {details[pkg]}\n\n"
                    f"{missing}"
                )
            return finish_booking_summary(session)

        return (
            f"📋 {pkg} cuesta {prices[pkg]} COP.\n"
            f"Incluye: {details[pkg]}\n\n"
            "¿Te gustaría agendar una cita? 😊"
        )

    return (
        "Ofrecemos tres paquetes:\n"
        "• Esencial — 45.000\n"
        "• Salud Activa — 60.000\n"
        "• Bienestar Total — 75.000\n\n"
        "¿Cuál te interesa?"
    )


# BOOKING REQUEST
def handle_booking_request(msg, session):
    # NOTE: session["booking_started"] is set at the start of process_message if detected
    session["awaiting_confirmation"] = False

    missing = build_missing_fields_message(session)
    if missing:
        return missing

    return finish_booking_summary(session)


# MODIFY REQUEST
def handle_modify(msg, session):
    session["booking_started"] = True
    session["awaiting_confirmation"] = False

    req = ["student_name", "school", "package", "date", "time", "age", "cedula"]
    if not all(session.get(f) for f in req):
        return "Perfecto 😊, indícame el nuevo dato que deseas cambiar."

    return finish_booking_summary(session)


# CANCEL REQUEST
def handle_cancel(msg, session):
    session["awaiting_confirmation"] = False
    return "¿Confirmas que deseas *cancelar* la cita? (Responde *Confirmo*)"


# CONFIRMATION HANDLER
def handle_confirmation(msg, session):
    from datetime import datetime

    # Build datetime
    final_dt = f"{session['date']} {session['time']}"
    dt_local = datetime.strptime(final_dt, "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ)

    # Table assignment
    if not supabase:
        table = "T1"
    else:
        booked = (
            supabase.table(RESERVATION_TABLE)
            .select("table_number")
            .eq("datetime", dt_local.isoformat())
            .execute()
        )
        taken = {r["table_number"] for r in booked.data or []}
        table = None
        for i in range(1, TABLE_LIMIT + 1):
            t = f"T{i}"
            if t not in taken:
                table = t
                break
        if not table:
            return "❌ No hay cupos disponibles para ese horario."

    # Insert reservation
    if supabase:
        supabase.table(RESERVATION_TABLE).insert({
            "customer_name": session["student_name"],
            "contact_phone": session["phone"],
            "datetime": dt_local.isoformat(),
            "table_number": table,
            "status": "confirmado",
            "business_id": BUSINESS_ID,
            "package": session["package"],
            "school_name": session["school"],
            "age": session["age"],
            "cedula": session["cedula"],
        }).execute()

    # Build success message
    result = (
        "✅ *¡Cita confirmada!*\n\n"
        f"👤 Estudiante: {session['student_name']}\n"
        f"🎒 Colegio: {session['school']}\n"
        f"📦 Paquete: {session['package']}\n"
        f"📅 Fecha: {session['date']}\n"
        f"⏰ Hora: {session['time']}\n"
        f"🪪 Cédula: {session['cedula']}\n"
        f"📍 Oriental IPS — Calle 16 #28-57, Yopal (Centro)\n"
    )

    # RESET session after confirming
    reset = DEFAULT_SESSION.copy()
    reset["phone"] = session["phone"]
    reset["greeted"] = True
    save_session(reset)

    return result


# ---------------------------------------------------------
# 10. CONTEXTUAL FALLBACKS (INFO OUTSIDE BOOKING)
# ---------------------------------------------------------

def handle_contextual(msg: str, session: dict) -> str | None:
    text = msg.lower()

    # Address
    if any(w in text for w in ["donde", "ubicados", "dirección", "direccion"]):
        return "Estamos ubicados en Yopal, Calle 16 #28-57, barrio Centro. 📍"

    # Working hours
    if any(w in text for w in ["horario", "abren", "cierran"]):
        return "Atendemos de lunes a sábado de 7:00 am a 5:00 pm. 😊"

    # Duration
    if "cuánto dura" in text or "duración" in text:
        return "El examen dura entre 30 y 45 minutos. 😊"

    return None
# ---------------------------------------------------------
# 11. MAIN MESSAGE PROCESSOR (STATE MACHINE)
# ---------------------------------------------------------

def natural_tone(text: str) -> str:
    """
    Light tone adjustments, IPS-friendly.
    """
    if text.strip().endswith("?") and "😊" not in text:
        return text.rstrip("?") + " 😊?"

    return text


def process_message(msg: str, session: dict) -> str:
    # ⚠️ FIX #1: Normalize smart quotes in the original message
    msg = msg.replace("“", "\"").replace("”", "\"")
    
    msg_lower = msg.lower().strip()
    msg_lower = msg_lower.replace("“", "\"").replace("”", "\"") # Re-clean msg_lower to be safe
    
    # ⚠️ FIX #1 (Cont.): Normalize inverted question mark for extractors
    msg = msg.replace("¿", "")
    msg_lower = msg_lower.replace("¿", "")
    
    # -----------------------------------------------------
    # 1. AUTO-ENABLE BOOKING MODE WHEN USER PROVIDES INFO
    # -----------------------------------------------------
    info_keywords = ["se llama", "nombre", "colegio", "edad", "años", "cedula", "cédula", "documento"]
    if any(k in msg_lower for k in info_keywords):
        session["booking_started"] = True

    if any(session.get(f) for f in ["student_name", "school", "package", "date", "time", "age", "cedula"]):
        session["booking_started"] = True

    # -----------------------------------------------------
    # 2. DETECT INTENT (GUARDED)
    # -----------------------------------------------------
    # Pass the now-normalized msg to the intent detector
    intent = detect_explicit_intent(msg, session) 

    # If user explicitly requested a booking, lock booking mode immediately
    if intent == "booking_request":
        session["booking_started"] = True

    # Save old date/time before extraction (for modification detection)
    old_date = session.get("date")
    old_time = session.get("time")

    # -----------------------------------------------------
    # 3. PERFORM NLP EXTRACTION (unless it's a pure confirmation)
    # -----------------------------------------------------
    confirmation_words = INTENTS["confirmation"]["patterns"]
    is_pure_confirmation = msg_lower in confirmation_words and session.get("awaiting_confirmation")

    if not is_pure_confirmation:
        # Extractor functions now safely receive normalized 'msg'
        new_date, new_time = update_session_with_info(msg, session)
    else:
        new_date, new_time = "", ""

    # Intercept "INVALID" time status
    if new_time == "INVALID":
        return natural_tone("La hora no es válida 😊. ¿Me confirmas la hora porfa?")

    # -----------------------------------------------------
    # 4. MISSING FIELDS CHECK
    # -----------------------------------------------------
    required = ["student_name", "school", "package", "date", "time", "age", "cedula"]
    all_fields_complete = all(session.get(f) for f in required)

    # -----------------------------------------------------
    # 5. AUTO-MODIFY BEHAVIOR
    # -----------------------------------------------------
    auto_modify_allowed = (
        session.get("booking_started") and
        (old_date or old_time or session.get("awaiting_confirmation"))
    )

    if (
        auto_modify_allowed and
        (new_date or new_time) and
        all_fields_complete
    ):
        session["awaiting_confirmation"] = True
        save_session(session)
        return natural_tone("Perfecto 😊, ya actualicé la información.\n\n" + finish_booking_summary(session))

    # -----------------------------------------------------
    # 6. HANDLE INTENT RESPONSE
    # -----------------------------------------------------
    if intent and intent in INTENTS:
        handler = globals()[INTENTS[intent]["handler"]]

        if intent == "confirmation" and session.get("awaiting_confirmation"):
            return natural_tone(handler(msg, session))

        if intent == "cancel":
            return natural_tone(handler(msg, session))

        if intent in ["modify", "booking_request", "package_info"]:
            return natural_tone(handler(msg, session))

        if intent == "greeting" and not session.get("booking_started"):
            return natural_tone(handler(msg, session))

    # -----------------------------------------------------
    # 7. BOOKING FLOW CONTINUATION
    # -----------------------------------------------------
    if session["booking_started"]:
        missing = build_missing_fields_message(session)

        # A. Everything complete → Ask to confirm
        if all_fields_complete:
            session["awaiting_confirmation"] = True
            save_session(session)
            return natural_tone(finish_booking_summary(session))

        # B. Something missing → Ask for the missing fields
        if missing:
            return natural_tone(missing)

    # -----------------------------------------------------
    # 8. CONTEXTUAL FALLBACK
    # -----------------------------------------------------
    contextual = handle_contextual(msg, session)
    if contextual:
        return natural_tone(contextual)

    # -----------------------------------------------------
    # 9. DEFAULT RESPONSE
    # -----------------------------------------------------
    if not session["greeted"] and not session.get("booking_started") and intent is None:
        return natural_tone(handle_greeting(msg, session))

    return natural_tone(
        "No entendí bien 😊. ¿Deseas *agendar una cita*, consultar *precios*, o tienes otra pregunta?"
    )


# ---------------------------------------------------------
# 12. TWILIO WHATSAPP WEBHOOK
# ---------------------------------------------------------

@app.post("/whatsapp", response_class=Response)
async def whatsapp_webhook(
    request: Request,
    WaId: str = Form(...),
    Body: str = Form(...),
):
    phone = WaId.split(":")[-1].strip()
    user_msg = Body.strip()

    session = get_session(phone)
    response_text = process_message(user_msg, session)

    twiml = MessagingResponse()
    twiml.message(response_text)

    return Response(content=str(twiml), media_type="application/xml")


# ---------------------------------------------------------
# 13. HEALTH CHECK
# ---------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return (
        "<h1>AI Reservation System (IPS v1.0.54 - Full Normalization Consistency Fix)</h1>"
        f"<p>Timezone: {LOCAL_TZ.key}</p>"
        f"<p>Supabase: {'Connected' if supabase else 'Disconnected'}</p>"
    )
