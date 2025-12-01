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

# --- 1. CONFIGURATION & INITIALIZATION ---

# Set up the environment (critical for external service access)
# This will ensure the script works regardless of where it is executed from.
os.chdir(os.path.dirname(os.path.abspath(__file__)))

app = FastAPI(title="AI Reservation System", version="1.0.0")

# Timezone: Must be explicitly defined and used consistently
try:
    LOCAL_TZ = ZoneInfo("America/Bogota")
except ZoneInfoNotFoundError:
    LOCAL_TZ = ZoneInfo("UTC") # Fallback

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# CORS setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# External Service Initialization (API Keys must be in environment variables)
try:
    # Use environment variables for secure access
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_SERVICE_ROLE = os.getenv("SUPABASE_SERVICE_ROLE")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

    if not all([SUPABASE_URL, SUPABASE_SERVICE_ROLE, OPENAI_API_KEY]):
        # WARNING: In a production environment like Render, these MUST be set.
        # This print statement is a warning for local testing.
        print("WARNING: Missing critical environment variables (SUPABASE_URL, SUPABASE_SERVICE_ROLE, OPENAI_API_KEY). Using NULL database clients.")
        # Raise an error if you want the app to fail hard on missing keys:
        # raise ValueError("Missing critical environment variables...")

    if SUPABASE_URL and SUPABASE_SERVICE_ROLE:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE)
    else:
        supabase = None # Safety measure

    if OPENAI_API_KEY:
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
    else:
        openai_client = None # Safety measure


except Exception as e:
    print(f"FATAL ERROR during external service initialization: {e}")
    supabase = None
    openai_client = None

TABLE_LIMIT = 10
RESERVATION_TABLE = "reservations"
SESSION_TABLE = "sessions"
BUSINESS_ID = 2 # Fixed for this specific business instance

# --- 2. DATABASE & SESSION MANAGEMENT (SCALABILITY FIX) ---

# CRITICAL FIX: Session management is now externalized to Supabase
DEFAULT_SESSION = {
    "phone": None,
    "student_name": None,
    "school": None,
    "package": None,
    "date": None, # YYYY-MM-DD
    "time": None, # HH:MM (24h format)
    "age": None,
    "cedula": None,
    "booking_started": False,
    "info_mode": False,
    "greeted": False,
}

def get_session(phone: str) -> dict:
    """Retrieves session state from Supabase or initializes a new one."""
    if not supabase: 
        # Emergency fallback for missing connection (sessions will NOT persist)
        new_session = DEFAULT_SESSION.copy()
        new_session['phone'] = phone
        return new_session

    try:
        response = supabase.table(SESSION_TABLE).select("data").eq("phone", phone).maybe_single().execute()
        if response.data and response.data.get('data'):
            # The data field stores the session dictionary as JSON
            session_data = response.data['data']
            session_data['phone'] = phone # Ensure phone is always present
            return session_data
        
        # New session
        new_session = DEFAULT_SESSION.copy()
        new_session['phone'] = phone
        return new_session
    except Exception as e:
        print(f"Error retrieving session for {phone}: {e}")
        new_session = DEFAULT_SESSION.copy()
        new_session['phone'] = phone
        return new_session

def save_session(session: dict):
    """Saves the current session state back to Supabase."""
    if not supabase: return # Cannot save without connection
    
    phone = session.get("phone")
    if not phone: return

    # Store the entire session dictionary under the 'data' column
    data_to_store = {k: v for k, v in session.items() if k != 'phone'}

    try:
        # Upsert: insert if phone doesn't exist, update if it does
        supabase.table(SESSION_TABLE).upsert({
            "phone": phone,
            "data": data_to_store,
            "last_updated": datetime.now(LOCAL_TZ).isoformat()
        }).execute()
    except Exception as e:
        print(f"Error saving session for {phone}: {e}")

# --- 3. RESERVATION HELPERS ---

def assign_table(iso_local: str):
    """Assigns the first available table for a specific datetime."""
    try:
        # Query for all tables booked for the exact time slot
        booked = supabase.table(RESERVATION_TABLE).select("table_number").eq("datetime", iso_local).execute()
        taken = {r["table_number"] for r in (booked.data or [])}
        
        for i in range(1, TABLE_LIMIT + 1):
            t = f"T{i}"
            if t not in taken:
                return t
        return None
    except Exception as e:
        print(f"Database error during table assignment: {e}")
        return None

def save_reservation(data: dict):
    """
    Saves the final confirmed reservation.
    Timezone FIX: Ensures the ISO string is timezone-aware and in Bogotá time.
    """
    if not supabase: return "❌ Error de conexión con la base de datos."

    try:
        # Convert YYYY-MM-DD HH:MM string back to a timezone-aware datetime object
        dt_text = f"{data['date']} {data['time']}"
        # Parse the string and attach the LOCAL_TZ (Bogotá)
        dt_local = datetime.strptime(dt_text, "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ)
        iso_to_store = dt_local.isoformat()
    except Exception:
        return "❌ Error interno procesando la fecha final."

    table = data.get("table_number") or assign_table(iso_to_store)
    if not table:
        return "❌ No hay mesas disponibles para ese horario."

    try:
        supabase.table(RESERVATION_TABLE).insert({
            "customer_name": data["student_name"],
            "contact_phone": data["phone"], # Use the user's phone for contact
            "datetime": iso_to_store,
            "party_size": int(data.get("party_size") or 1), # Default to 1 if not extracted
            "table_number": table,
            "status": "confirmado",
            "business_id": BUSINESS_ID,
            "package": data.get("package", ""),
            "school_name": data.get("school", ""),
            "age": data.get("age", None),
            "cedula": data.get("cedula", None),
        }).execute()

        # SUCCESSFUL RESERVATION MESSAGE
        return (
            "✅ *¡Reserva confirmada!*\n"
            f"👤 Estudiante: {data['student_name']}\n"
            f"🎒 Colegio: {data.get('school', 'N/A')}\n"
            f"📦 Paquete: {data.get('package','N/A')}\n"
            f"📅 Fecha/Hora: {dt_local.strftime('%Y-%m-%d %H:%M')} ({LOCAL_TZ.key.split('/')[-1]})"
        )
    except PostgrestAPIError as e:
        print(f"Supabase error inserting reservation: {e}")
        return "❌ Error al guardar la reserva en la base de datos."
    except Exception as e:
        print(f"Unknown error in save_reservation: {e}")
        return "❌ Error inesperado al confirmar la reserva."

# --- 4. DATA EXTRACTION & NLP (ROBUSTNESS FIX) ---

PACKAGE_MAPPING = {
    "cuidado esencial": "Paquete Cuidado Esencial", # $45.000 (verde)
    "salud activa": "Paquete Salud Activa",        # $60.000 (azul)
    "bienestar total": "Paquete Bienestar Total",  # $75.000 (amarillo)
}

def detect_package(msg: str) -> str | None:
    """Robustly detects the package based on keywords or price."""
    msg = msg.lower().strip()
    
    # 1. Direct name match (using whole word boundaries)
    for key, pkg_name in PACKAGE_MAPPING.items():
        if re.search(r"\b" + key.replace(' ', r'\s*') + r"\b", msg):
            return pkg_name

    # 2. Price match
    if any(re.search(p, msg) for p in [r'\b45k\b', r'\b45\s*mil\b', r'\b45\.?000\b']):
        return PACKAGE_MAPPING["cuidado esencial"]
    if any(re.search(p, msg) for p in [r'\b60k\b', r'\b60\s*mil\b', r'\b60\.?000\b']):
        return PACKAGE_MAPPING["salud activa"]
    if any(re.search(p, msg) for p in [r'\b75k\b', r'\b75\s*mil\b', r'\b75\.?000\b']):
        return PACKAGE_MAPPING["bienestar total"]
        
    # 3. Component/Color match (less reliable, but kept from original)
    if "odont" in msg or "amarillo" in msg or "total" in msg:
        return PACKAGE_MAPPING["bienestar total"]
    if "psico" in msg or "azul" in msg or "activa" in msg:
        return PACKAGE_MAPPING["salud activa"]
    if any(w in msg for w in ["audio", "optometr", "medicina", "verde", "esencial"]):
        return PACKAGE_MAPPING["cuidado esencial"]

    return None

def extract_datetime_info(msg: str) -> tuple[str, str]:
    """
    Uses dateparser to extract date and time.
    FIX: Removed costly GPT-4o-mini call for this simple extraction.
    """
    dt_local = dateparser.parse(
        msg,
        settings={
            "TIMEZONE": LOCAL_TZ.key, # Use Bogotá timezone
            "TO_TIMEZONE": LOCAL_TZ.key, # Ensure output is in Bogotá timezone
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
            "STRICT_PARSING": False, # Allow flexible parsing
            "PREFER_DAY_OF_MONTH": "first"
        }
    )

    if dt_local:
        # Apply normalization: If only date is provided, default to a reasonable time (e.g., 9:00 AM)
        if not re.search(r'\d{1,2}(:\d{2})?\s*(am|pm|a\.m|p\.m|mañana|tarde|noche)', msg.lower()):
             # If no time is explicitly mentioned, set to 9:00 AM for safety, checking if it is in the past
            dt_local = dt_local.replace(hour=9, minute=0, second=0, microsecond=0)
            if dt_local < datetime.now(LOCAL_TZ) - timedelta(minutes=5):
                 # If 9 AM today is in the past, propose 9 AM tomorrow
                dt_local += timedelta(days=1)
                dt_local = dt_local.replace(hour=9, minute=0)

        # Final check to ensure it's in the correct timezone (Bogota)
        dt_local = dt_local.astimezone(LOCAL_TZ)
        return dt_local.strftime("%Y-%m-%d"), dt_local.strftime("%H:%M")
    
    return "", ""

def extract_school_name(msg: str) -> str | None:
    """Robustly extracts school name using multiple patterns."""
    msg_clean = msg.lower()
    
    # Prioritize 'colegio X' over raw names
    patterns = [
        r"(del\s+|de\s+|la\s+)?(colegio|gimnasio|liceo|instituto|escuela)\s+([a-záéíóúñ0-9\s]+)",
        r"(colegio|gimnasio|liceo|instituto|escuela)\s+([a-záéíóúñ0-9\s]+)",
    ]

    for p in patterns:
        m = re.search(p, msg_clean)
        if m:
            # Group 3 is usually the name part after the school type
            if len(m.groups()) == 3:
                name = m.group(3).strip()
            else:
                # Handle pattern without prefix group
                name = m.group(2).strip()

            # Clean up the end of the name (remove trailing punctuation or common words)
            name = re.split(r"[,.!?\n]| a las | a la | mañana | hoy | pasado mañana", name)[0]
            if name and len(name.split()) > 1: # Require at least 2 words or a non-trivial name
                return name.title().strip()
    return None

def extract_age_cedula(msg: str, session: dict):
    """Extracts age and cedula if they are reasonable numbers."""
    
    # AGE DETECTION (must be 1-2 digits, 1-20 range is a good heuristic for student exams)
    if not session["age"]:
        age_match = re.search(r"\b(\d{1,2})\s*(años|anos|año|ano)?\b", msg.lower())
        if age_match:
            age_num = int(age_match.group(1))
            if 1 <= age_num <= 20:
                session["age"] = age_num

    # CEDULA DETECTION (must be 5-12 digits, often isolated)
    if not session["cedula"]:
        # Find 5-12 digits that are NOT part of a time (HH:MM or H:MM) or phone number (prefixed)
        ced_match = re.search(r"(?<!:)(\b\d{5,12}\b)(?!:)", msg)
        if ced_match:
            session["cedula"] = ced_match.group(1)

def extract_student_name(msg: str) -> str | None:
    """Uses a cleaned message to find a likely student name."""
    msg_lower = msg.lower()
    
    # Common stop/noise words to ignore when looking for a standalone name
    noise_words = [
        "quiero", "cita", "reservar", "agendar", "necesito", "la", "el", "una", "un", "hora", "fecha",
        "dia", "día", "por", "favor", "gracias", "me", "referia", "refería", "perdon", "perdón", "mejor",
        "si", "sí", "ok", "dale", "listo", "perfecto", "super", "claro", "de una", "bueno", "mañana",
        "tarde", "noche", "am", "pm"
    ]
    
    # 1. Pattern matching (e.g., 'es para mi hijo X')
    name_patterns = [
        r"(mi\s+(hijo|hija)\s+(es\s+)?|se\s+llama|es\s+para)\s+([a-záéíóúñ ]+)",
    ]
    for p in name_patterns:
        m = re.search(p, msg_lower)
        if m and len(m.groups()) >= 4:
            raw_name = m.group(4).strip()
            if raw_name:
                # Clean name: remove trailing stop words or punctuation
                cleaned = raw_name.split(",")[0].strip()
                words = [w for w in cleaned.split() if w not in noise_words]
                if 1 <= len(words) <= 3:
                    return " ".join(words).title()

    # 2. Heuristic check: If the message is only 1-3 capitalized words, it might be a name.
    words = msg.strip().split()
    if 1 <= len(words) <= 3 and all(word[0].isupper() for word in words):
        if all(c.isalpha() or c.isspace() for c in msg.strip()):
             return msg.strip()

    return None

def detect_correction(msg: str) -> bool:
    """Simple check if the user is explicitly correcting previous data."""
    t = msg.lower().strip()
    return any(w in t for w in [
        "no es", "no era", "no perdón", "no perdon", "quise decir", 
        "me equivoqué", "cambiar", "reagendar"
    ])

def apply_correction(session: dict):
    """Resets key fields if a correction is detected to prompt for new values."""
    session["student_name"] = None
    session["school"] = None
    session["package"] = None
    session["date"] = None
    session["time"] = None
    session["age"] = None
    session["cedula"] = None
    session["booking_started"] = False # Revert to initial state to collect data cleanly

def update_session_with_info(msg: str, session: dict):
    """
    Master function to extract and update all session parameters.
    FIX: Refactored for clarity and robustness.
    """
    text = msg.lower().strip()
    
    # 1. Check for correction intent first
    if detect_correction(msg):
        apply_correction(session)
        # Note: We still attempt to extract new info below, but we've cleared the old state.

    # 2. Data extraction using refactored functions
    new_name = extract_student_name(msg)
    new_school = extract_school_name(msg)
    new_package = detect_package(msg)
    new_date, new_time = extract_datetime_info(msg)
    extract_age_cedula(msg, session) # Updates session in place

    # 3. Apply extracted data (only if field is currently empty OR a correction was just applied)
    if new_name and (session["student_name"] is None or detect_correction(msg)):
        session["student_name"] = new_name
    
    if new_school and (session["school"] is None or detect_correction(msg)):
        session["school"] = new_school

    if new_package and (session["package"] is None or detect_correction(msg)):
        session["package"] = new_package

    if new_date and new_time and (session["date"] is None or detect_correction(msg)):
        session["date"] = new_date
        session["time"] = new_time
    
    # Always set booking_started if any key piece of info is provided
    if new_name or new_school or new_package or new_date:
        session["booking_started"] = True

    # 4. Save the updated session state
    save_session(session)


# --- 5. INTENT & CONTEXTUAL HANDLING ---

INTENTS = {
    "greeting": {"patterns": [], "handler": "handle_greeting"},
    "package_info": {"patterns": [], "handler": "handle_package_info"},
    "booking_request": {"patterns": [], "handler": "handle_booking_request"},
    "modify": {"patterns": [], "handler": "handle_modify"},
    "cancel": {"patterns": [], "handler": "handle_cancel"},
    "confirmation": {"patterns": [], "handler": "handle_confirmation"},
}

INTENTS["greeting"]["patterns"] = ["hola","buenas","buenos dias","buen dia","buenas tardes","buenas noches","disculpa","una pregunta","consulta","informacion","quisiera saber"]
INTENTS["package_info"]["patterns"] = ["cuanto vale","cuánto vale","cuanto cuesta","precio","valor","paquete","kit escolar","psicologia","odontologia","el verde","el azul","el amarillo","45k","60k","75k","esencial","salud activa","bienestar total"]
INTENTS["booking_request"]["patterns"] = ["quiero reservar","quiero una cita","quiero agendar","necesito una cita","quiero el examen","me pueden reservar","agendar cita","reservar examen","separar cita"]
INTENTS["modify"]["patterns"] = ["cambiar cita","cambiar la cita","quiero cambiar","cambiar hora","cambiar fecha","mover cita","reagendar"]
INTENTS["cancel"]["patterns"] = ["cancelar","cancelar cita","anular","quitar la cita","ya no quiero la cita"]
INTENTS["confirmation"]["patterns"] = ["confirmo","sí confirmo","si confirmo","confirmar","confirmada","confirmado","si","sí","ok","dale","listo","perfecto","super","claro","de una","por supuesto","está bien","esta bien","si está bien","sí está bien"]

def detect_explicit_intent(msg: str) -> str | None:
    """Detects explicit intent based on keywords."""
    msg = msg.lower()
    # Prioritize disruptive or high-value intents
    priority = ["cancel", "modify", "confirmation", "booking_request", "package_info", "greeting"]
    for intent in priority:
        for p in INTENTS[intent]["patterns"]:
            if p in msg:
                # Disambiguate simple 'si/no' confirmations unless booking is in progress
                if intent == "confirmation" and not get_session(msg).get("booking_started"):
                    continue
                return intent
    return None

def build_missing_fields_message(session: dict) -> str | None:
    """Generates a friendly message listing only the required missing fields."""
    missing = []
    if not session["student_name"]: missing.append("el *nombre* del estudiante")
    if not session["school"]: missing.append("el *colegio*")
    if not session["package"]: missing.append("el *paquete* (ej: Esencial, Activa, Total)")
    if not session["date"] or not session["time"]: missing.append("la *fecha* y *hora* de la cita")
    if not session["age"]: missing.append("la *edad* del estudiante")
    if not session["cedula"]: missing.append("la *cédula* del estudiante")

    if not missing:
        return None

    if len(missing) == 1:
        return f"Listo, solo me falta {missing[0]}. ¿Me lo compartes porfa? 🙏"
    
    # Standard list construction
    joined = ", ".join(missing[:-1]) + " y " + missing[-1]
    return f"¡Perfecto! Para continuar, necesito estos datos: {joined}. ¿Me los colaboras? 🙏"

def finish_booking_summary(session: dict) -> str:
    """Generates the confirmation summary message before final database save."""
    return (
        f"Listo, ya tengo toda la información:\n\n"
        f"👤 Estudiante: {session.get('student_name', 'N/A')}\n"
        f"🎒 Colegio: {session.get('school', 'N/A')}\n"
        f"📦 Paquete: {session.get('package', 'N/A')}\n"
        f"📅 Fecha: {session.get('date', 'N/A')}\n"
        f"⏰ Hora: {session.get('time', 'N/A')}\n"
        f"🧒 Edad: {session.get('age', 'N/A')}\n"
        f"🪪 Cédula: {session.get('cedula', 'N/A')}\n\n"
        f"¿*Deseas confirmar la cita* con estos datos? (Responde *Sí* o *Confirmar*)"
    )

# --- 6. HANDLERS (State Machine Steps) ---

def handle_greeting(msg, session):
    if not session["greeted"]:
        session["greeted"] = True
        return "¡Hola! Soy tu asistente de agendamiento 🤖. Claro que sí, ¿en qué te puedo ayudar hoy? 😊"
    return "Claro que sí, ¿en qué te puedo ayudar?"

def handle_package_info(msg, session):
    session["info_mode"] = True
    pkg = detect_package(msg)

    prices = {
        "Paquete Cuidado Esencial": "45.000 COP",
        "Paquete Salud Activa": "60.000 COP",
        "Paquete Bienestar Total": "75.000 COP",
    }
    details = {
        "Paquete Cuidado Esencial": "Medicina General, Optometría y Audiometría.",
        "Paquete Salud Activa": "Medicina General, Optometría, Audiometría y Psicología.",
        "Paquete Bienestar Total": "Medicina General, Optometría, Audiometría, Psicología y Odontología.",
    }

    if pkg:
        return (
            f"Claro 😊\n"
            f"*{pkg}* cuesta *${prices[pkg]}*.\n\n"
            f"📋 *Incluye:*\n{details[pkg]}\n\n"
            "¿Te gustaría agendar una cita?"
        )

    # General package list if no specific package was detected
    return (
        "Claro. Ofrecemos tres paquetes de exámenes escolares:\n\n"
        "• *Cuidado Esencial* (Verde) — $45.000 COP\n"
        "  _Incluye: Medicina, Optometría, Audiometría_\n\n"
        "• *Salud Activa* (Azul) — $60.000 COP\n"
        "  _Incluye: Paquete Esencial + Psicología_\n\n"
        "• *Bienestar Total* (Amarillo) — $75.000 COP\n"
        "  _Incluye: Paquete Activa + Odontología_\n\n"
        "¿Cuál te interesa o quieres agendar?"
    )

def handle_booking_request(msg, session):
    session["booking_started"] = True
    session["info_mode"] = False
    
    # Try to extract info from the same message
    update_session_with_info(msg, session)

    missing_message = build_missing_fields_message(session)
    if not missing_message:
        # If all fields are present, go directly to summary
        return finish_booking_summary(session)

    # If info is missing, ask for it using the structured prompt
    return missing_message


def handle_confirmation(msg, session):
    # This handler is only called when all checks pass in process_message
    
    # 1. Final check for completeness
    required = [session.get(f) for f in ["student_name", "school", "package", "date", "time", "age", "cedula"]]
    if not all(required):
        # Should not happen if flow is followed, but handles edge case
        return "Disculpa, necesito toda la información antes de confirmar. ¿Me la puedes completar?"

    # 2. Save reservation to the database
    response_msg = save_reservation(session)

    # 3. Clean up the session only if reservation was successful (starts with '✅')
    if response_msg.startswith("✅"):
        # Reset to default session data, keeping only the phone number.
        reset_session = {k: v for k, v in DEFAULT_SESSION.items() if k != "phone"}
        reset_session["phone"] = session["phone"]
        save_session(reset_session)

    return response_msg

def handle_modify(msg, session):
    # For a real system, you would check the database for existing reservations
    return "Entendido. ¿Me indicas la *nueva fecha y hora* que deseas para la cita? Por ejemplo: _'el martes a las 10 am'_"

def handle_cancel(msg, session):
    # For a real system, you would check the database and confirm the reservation ID
    # After confirmation, you would delete it from the 'reservations' table.
    return "Perfecto, ¿confirmas que deseas *cancelar* completamente la cita agendada? (Responde *Sí* si estás seguro)"

def handle_contextual(msg: str, session: dict) -> str | None:
    """Handles non-booking questions (hours, location, process)."""
    text = msg.lower().strip()
    
    if any(x in text for x in ["atienden", "abren", "horario", "horarios", "sábados", "sabados"]):
        return "Nuestros horarios son de lunes a viernes de 7:00 AM a 5:00 PM y sábados de 7:00 AM a 1:00 PM 😊"
    
    if any(x in text for x in ["donde queda", "ubicados", "direccion", "dirección"]):
        return "Estamos ubicados en Bogotá, en la calle 75 #20-36. Te envío la ubicación exacta por mensaje. 📍"
    
    if any(x in text for x in ["como funciona", "cómo funciona", "proceso", "examen", "dura"]):
        return (
            "Claro 😊 El examen escolar se hace en *aproximadamente 30–45 minutos* e incluye:\n"
            "• Historia clínica y revisión general\n"
            "• Pruebas del paquete que elijas\n"
            "• Entrega inmediata del certificado escolar\n\n"
            "¿Te gustaría agendar una cita?"
        )
    
    if any(x in text for x in ["puedes repetir", "puede repetir", "repiteme", "repite"]):
        missing = build_missing_fields_message(session)
        if missing:
            return missing
        if session["booking_started"]:
            return finish_booking_summary(session)
        
    if any(x in text for x in ["espera", "un momento", "dame un segundo", "ya te escribo"]):
        return "Claro, aquí te espero 😊."
        
    if any(x in text for x in ["gracias", "muchas gracias", "bueno gracias"]):
        return "Con gusto. ¡Que tengas un excelente día! 😊"
        
    return None

def natural_tone(text: str) -> str:
    """Adds emojis for a friendlier, Latin American tone."""
    replacements = {
        "Perfecto,": "Perfecto 😊,",
        "Listo,": "Listo 😊,",
        "Claro,": "Claro que sí 😊,",
        "Por supuesto.": "Por supuesto, ya te ayudo 😊.",
        "Entendido.": "Entendido 😊.",
        "De acuerdo.": "Listo 😊.",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)

    # Add a closing emoji if the message ends in a question and isn't already emotional
    if text.strip().endswith("?") and "😊" not in text and "🙏" not in text:
        text = text.rstrip("?") + " 😊?"

    return text

# --- 7. MAIN MESSAGE PROCESSING FLOW ---

def process_message(msg: str, session: dict) -> str:
    """The central state machine logic."""
    
    # 1. Handle contextual/general questions (always priority)
    contextual_response = handle_contextual(msg, session)
    if contextual_response:
        return natural_tone(contextual_response)

    # 2. Extract and update all info from the incoming message
    update_session_with_info(msg, session)
    
    # 3. Check for completion or next missing step (BOOKING FLOW PRIORITY)
    if session["booking_started"]:
        missing_message = build_missing_fields_message(session)
        
        # A. ALL FIELDS COMPLETE -> Go to FINAL SUMMARY
        if not missing_message:
            # If user explicitly confirms AND all fields are full, process the booking
            if detect_explicit_intent(msg) == "confirmation":
                return natural_tone(handle_confirmation(msg, session))
            # Otherwise, show the final summary and ask for confirmation
            return natural_tone(finish_booking_summary(session))
        
        # B. FIELDS MISSING -> Ask for the next missing piece
        # This prevents asking generic booking intro questions when data is still needed
        return natural_tone(missing_message)

    # 4. Handle explicit non-booking intents
    intent = detect_explicit_intent(msg)

    if intent and intent in INTENTS and intent not in ["confirmation"]:
        handler = globals()[INTENTS[intent]["handler"]]
        resp = handler(msg, session)
        return natural_tone(resp)
    
    # 5. Default Response (If not greeted, send greeting, otherwise ask what they need)
    if not session["greeted"]:
        return natural_tone(handle_greeting(msg, session))

    # 6. Fallback (If the message was not clear enough for any action)
    return "Disculpa, no entendí bien. ¿Me lo repites o me indicas si quieres *agendar una cita* o saber sobre los *paquetes*? 😊"


# --- 8. FASTAPI ENDPOINTS ---

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Simple redirect to the dashboard for quick access."""
    # Ensure there's a 'templates' directory and 'dashboard.html' in it
    return templates.TemplateResponse("dashboard.html", {"request": request, "reservations": [], "weekly_count": 0, "total_reservations": 0, "message": "Cargando datos..."})

@app.post("/whatsapp")
async def whatsapp_reply(request: Request):
    """Twilio Webhook for incoming WhatsApp messages."""
    form = await request.form()
    incoming_msg = form.get("Body", "").strip()
    phone = form.get("From", "").replace("whatsapp:", "").strip() # Use raw phone as the key

    # 1. Retrieve the persistent session
    session = get_session(phone)
    
    # 2. Process the message using the state machine
    response_text = process_message(incoming_msg, session)

    # 3. Send response back to Twilio
    if not response_text:
        # Should not happen often with a robust process_message, but safety fallback
        response_text = "Disculpa, no entendí bien. ¿Me lo repites por favor?"

    twilio_resp = MessagingResponse()
    twilio_resp.message(response_text)

    # Note: save_session is called within update_session_with_info and handle_confirmation
    # This ensures the session is saved every time new data is extracted or the booking is finalized.

    return Response(content=str(twilio_resp), media_type="application/xml")

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Displays the list of reservations."""
    if not supabase: 
        return templates.TemplateResponse("dashboard.html", {"request": request, "reservations": [], "weekly_count": 0, "total_reservations": 0, "message": "ERROR: Database connection failed. Check SUPABASE_URL and SUPABASE_SERVICE_ROLE."})

    try:
        # Fetch all reservations ordered by date (most recent first)
        res = supabase.table(RESERVATION_TABLE).select("*").order("datetime", desc=True).limit(50).execute()
        rows = res.data or []
    except Exception as e:
        print(f"Error fetching dashboard data: {e}")
        rows = []

    fixed = []
    weekly_count = 0
    now = datetime.now(LOCAL_TZ)
    week_ago = now - timedelta(days=7)

    for r in rows:
        iso = r.get("datetime")
        row = r.copy()
        if iso:
            # Use dateutil_parser for robust ISO string handling
            dt = dateutil_parser.isoparse(iso).astimezone(LOCAL_TZ)
            row["date"] = dt.strftime("%Y-%m-%d")
            row["time"] = dt.strftime("%H:%M")
            row["iso_datetime"] = dt.isoformat() # Helpful for sorting/display
            
            # Calculate weekly count
            if dt >= week_ago:
                weekly_count += 1
        else:
            row["date"], row["time"], row["iso_datetime"] = "-", "-", "-"
            
        fixed.append(row)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "reservations": fixed,
        "weekly_count": weekly_count,
        "total_reservations": len(rows),
    })

# The original update_reservation endpoint was incomplete, but included here for completeness
@app.post("/updateReservation")
async def update_reservation(update: dict):
    if not supabase: 
        return {"success": False, "message": "Database connection failed."}

    rid = update.get("reservation_id")
    if not rid:
        return {"success": False, "message": "Missing reservation ID."}

    # Clean the dictionary to only contain fields to update
    fields_to_update = {
        k: v for k, v in update.items()
        if k != "reservation_id" and v not in ["", None] and k in [
            "customer_name", "contact_phone", "party_size", 
            "notes", "status", "package", "school_name", "age", "cedula"
        ]
    }
    
    # Handle datetime update if present (ensure timezone-awareness)
    if 'datetime' in update:
        try:
            dt = dateutil_parser.isoparse(update['datetime']).astimezone(LOCAL_TZ)
            fields_to_update['datetime'] = dt.isoformat()
        except Exception:
            return {"success": False, "message": "Invalid datetime format."}

    try:
        supabase.table(RESERVATION_TABLE).update(fields_to_update).eq("id", rid).execute()
        return {"success": True, "message": f"Reservation {rid} updated successfully."}
    except Exception as e:
        print(f"Error updating reservation {rid}: {e}")
        return {"success": False, "message": f"Database error: {e}"}
