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
        # This will print in your live server logs if variables are missing
        print("WARNING: Missing critical environment variables (SUPABASE_URL, SUPABASE_SERVICE_ROLE, OPENAI_API_KEY). Using NULL database clients.")

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
    "awaiting_confirmation": False, # FIX 7: New state flag
}

def get_session(phone: str) -> dict:
    """Retrieves session state from Supabase or initializes a new one."""
    if not supabase: 
        new_session = DEFAULT_SESSION.copy()
        new_session['phone'] = phone
        return new_session

    try:
        response = supabase.table(SESSION_TABLE).select("data").eq("phone", phone).maybe_single().execute()
        if response.data and response.data.get('data'):
            session_data = response.data['data']
            session_data['phone'] = phone # Ensure phone is always present
            # Merge with default to ensure all keys are present for new flags (like awaiting_confirmation)
            return {**DEFAULT_SESSION, **session_data} 
        
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
    if not supabase: return
    
    phone = session.get("phone")
    if not phone: return

    # Store the entire session dictionary under the 'data' column
    # We must exclude 'phone' from the 'data' object as it's the PK in the table
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

# --- 3. RESERVATION HELPERS (No changes needed here) ---

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
    """Saves the final confirmed reservation."""
    if not supabase: return "❌ Error de conexión con la base de datos."

    try:
        dt_text = f"{data['date']} {data['time']}"
        # Parse without relying on dateutil_parser for the initial string as H:M is 24h format
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
            "contact_phone": data["phone"],
            "datetime": iso_to_store,
            "party_size": int(data.get("party_size") or 1),
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

# --- 4. DATA EXTRACTION & NLP ---

PACKAGE_MAPPING = {
    "cuidado esencial": "Paquete Cuidado Esencial",
    "salud activa": "Paquete Salud Activa",
    "bienestar total": "Paquete Bienestar Total",
}

def detect_package(msg: str) -> str | None:
    """
    Robustly detects the package based on keywords or price.
    FIX 3: Limited matching to full words using \b (word boundary).
    """
    msg = msg.lower().strip()
    
    # 1. Direct name match (using whole word boundaries \b)
    for key, pkg_name in PACKAGE_MAPPING.items():
        # Use regex to enforce full word match for key (e.g., prevents 'total' from matching 'totalmente')
        if re.search(r"\b" + key.replace(' ', r'\s*') + r"\b", msg):
            return pkg_name

    # 2. Price match (using whole word boundaries \b)
    if any(re.search(p, msg) for p in [r'\b45k\b', r'\b45\s*mil\b', r'\b45\.?000\b', r'\bcuarenta\s*y\s*cinco\b']):
        return PACKAGE_MAPPING["cuidado esencial"]
    if any(re.search(p, msg) for p in [r'\b60k\b', r'\b60\s*mil\b', r'\b60\.?000\b', r'\bsesenta\b']):
        return PACKAGE_MAPPING["salud activa"]
    if any(re.search(p, msg) for p in [r'\b75k\b', r'\b75\s*mil\b', r'\b75\.?000\b', r'\bsetenta\s*y\s*cinco\b']):
        return PACKAGE_MAPPING["bienestar total"]
        
    return None

def extract_datetime_info(msg: str) -> tuple[str, str]:
    """
    Uses dateparser to extract date and time.
    FIX 4: If no time is found, time is returned as empty string ("").
    
    Note: Refined logic to check for explicit time mentioned or if the time is NOT the default 9 AM.
    """
    dt_local = dateparser.parse(
        msg,
        settings={
            "TIMEZONE": LOCAL_TZ.key,
            "TO_TIMEZONE": LOCAL_TZ.key,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
            "STRICT_PARSING": False,
        }
    )

    date_str = ""
    time_str = ""
    
    if dt_local:
        
        # Check if time was explicitly mentioned (H:MM or HH:MM followed by AM/PM or words)
        explicit_time_found = re.search(r'\d{1,2}(:\d{2})?\s*(am|pm|a\.m|p\.m|mañana|tarde|noche|hr|hrs|h)\b', msg.lower())
        
        # If a date was found
        dt_local = dt_local.astimezone(LOCAL_TZ)
        date_str = dt_local.strftime("%Y-%m-%d")
        
        # Check if the extracted date is in the past
        if dt_local.date() < datetime.now(LOCAL_TZ).date():
             # If only a past date was provided (e.g. "yesterday"), return empty date/time to re-prompt.
             return "", ""

        # FIX 4 REFINEMENT: Only assign time if it was explicit OR if dateparser found a non-default hour.
        # We assume 9 AM is the most common default hour when only a date is mentioned.
        if explicit_time_found or dt_local.hour != 9:
            time_str = dt_local.strftime("%H:%M")
        else:
            time_str = "" # Leave time empty, requiring the bot to prompt for it.
            
    return date_str, time_str

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
                name = m.group(2).strip()

            name = re.split(r"[,.!?\n]| a las | a la | mañana | hoy | pasado mañana", name)[0]
            if name and len(name.split()) > 1:
                return name.title().strip()
    return None

def extract_age_cedula(msg: str, session: dict):
    """Extracts age and cedula if they are reasonable numbers."""
    
    # AGE DETECTION (1-2 digits, 1-20 range)
    if not session["age"]:
        age_match = re.search(r"\b(\d{1,2})\s*(años|anos|año|ano)?\b", msg.lower())
        if age_match:
            age_num = int(age_match.group(1))
            if 1 <= age_num <= 20:
                session["age"] = age_num

    # CEDULA DETECTION (5-12 digits)
    if not session["cedula"]:
        ced_match = re.search(r"(?<!:)(\b\d{5,12}\b)(?!:)", msg)
        if ced_match:
            session["cedula"] = ced_match.group(1)

def extract_student_name(msg: str) -> str | None:
    """
    Finds a likely student name.
    FIX 2: Removed unsafe uppercase-based name detection heuristic, kept pattern matching.
    """
    msg_lower = msg.lower()
    
    noise_words = [
        "quiero", "cita", "reservar", "agendar", "necesito", "la", "el", "una", "un", "hora", "fecha",
        "dia", "día", "por", "favor", "gracias", "me", "referia", "refería", "perdon", "perdón", "mejor",
        "si", "sí", "ok", "dale", "listo", "perfecto", "super", "claro", "de una", "bueno", "mañana",
        "tarde", "noche", "am", "pm", "es para", "para mi", "hijo", "hija", "mi", "se llama"
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
                cleaned = raw_name.split(",")[0].strip()
                words = [w for w in cleaned.split() if w not in noise_words]
                if 1 <= len(words) <= 3:
                    return " ".join(words).title()

    # 2. If the user only sends a name (e.g., "Juan Perez"), try to capture it if it looks like a name
    # We enforce a simple rule: 2-3 capitalized words, not matching common noise words.
    words = [w for w in msg.split() if w.lower() not in noise_words]
    if 2 <= len(words) <= 3 and all(w[0].isupper() for w in words):
          return " ".join(words).title()

    return None

def update_session_with_info(msg: str, session: dict):
    """
    Master function to extract and update all session parameters.
    FIX 5: Overwriting is always enabled if a new value is found. 
    """
    
    # 1. Data extraction
    new_name = extract_student_name(msg)
    new_school = extract_school_name(msg)
    new_package = detect_package(msg)
    new_date, new_time = extract_datetime_info(msg)
    extract_age_cedula(msg, session) # Updates session in place

    # 2. Apply extracted data: ALWAYS overwrite if a valid value is extracted (Fix 5)
    # This automatically handles corrections, as the new, corrected value overwrites the old one.
    if new_name:
        session["student_name"] = new_name
    
    if new_school:
        session["school"] = new_school

    if new_package:
        session["package"] = new_package

    if new_date:
        session["date"] = new_date
        # Only overwrite time if a valid time was extracted OR if the date extraction forced time to be empty ("")
        if new_time is not None: 
            session["time"] = new_time
    
    # 3. Save the updated session state
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

def detect_explicit_intent(msg: str, session: dict) -> str | None:
    """Detects explicit intent based on keywords."""
    msg = msg.lower()
    # Prioritize disruptive or high-value intents
    priority = ["cancel", "modify", "confirmation", "booking_request", "package_info", "greeting"]
    for intent in priority:
        for p in INTENTS[intent]["patterns"]:
            if p in msg:
                # FIX 7: Confirmation is now only accepted if awaiting_confirmation is True
                if intent == "confirmation" and not session.get("awaiting_confirmation"):
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
    # Age and Cedula are less critical, but still required for final booking
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
    """
    Generates the confirmation summary message and sets the 'awaiting_confirmation' flag.
    FIX 7: Sets session["awaiting_confirmation"] = True.
    """
    session["awaiting_confirmation"] = True
    save_session(session) # Save state before returning response

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
    # Reset confirmation flag if user asks for info mid-flow
    session["awaiting_confirmation"] = False
    
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
            f"¿Te gustaría agendar una cita?"
        )

    # General package list if no specific package was detected
    return (
        "Claro. Ofrecemos tres paquetes de exámenes escolares:\n\n"
        "• *Cuidado Esencial* (Verde) — $45.000 COP\n"
        "  _Incluye: Medicina, Optometría, Audiometría_\n\n"
        "• *Salud Activa* (Azul) — $60.000 COP\n"
        "  _Incluye: Paquete Esencial + Psicología_\n\n"
        "• *Bienestar Total* (Amarillo) — $75.000 COP\n"
        "  _Incluye: Paquete Activa + Odontología_\n\n"
        "¿Cuál te interesa o quieres agendar?"
    )

def handle_booking_request(msg, session):
    # FIX 6: booking_started is only set here, by explicit intent
    session["booking_started"] = True
    session["info_mode"] = False
    session["awaiting_confirmation"] = False # Ensure we are not awaiting confirmation yet
    
    # Try to extract info from the same message (Already done in process_message step 1)

    missing_message = build_missing_fields_message(session)
    if not missing_message:
        # If all fields are present, go directly to summary
        return finish_booking_summary(session)

    # If info is missing, ask for it using the structured prompt
    return missing_message


def handle_confirmation(msg, session):
    # FIX 7: This handler should only be called if awaiting_confirmation is True
    
    # 1. Final check for completeness (redundant but safe)
    required = [session.get(f) for f in ["student_name", "school", "package", "date", "time", "age", "cedula"]]
    if not all(required):
        session["awaiting_confirmation"] = False # Reset flag if data is somehow missing
        save_session(session)
        return "Disculpa, parece que falta información clave. ¿Me la puedes completar?"

    # 2. Save reservation to the database
    response_msg = save_reservation(session)

    # 3. Clean up the session 
    # Reset all key fields and flags, regardless of save success (start fresh flow)
    reset_session = {k: v for k, v in DEFAULT_SESSION.items() if k != "phone"}
    reset_session["phone"] = session["phone"]
    # Keep greeted state
    reset_session["greeted"] = session["greeted"]
    save_session(reset_session)

    return response_msg

def handle_modify(msg, session):
    session["awaiting_confirmation"] = False
    # If not already booking, start the flow
    if not session["booking_started"]:
        session["booking_started"] = True
    # If the user provided a date/time in the modify message, the update_session_with_info 
    # (in process_message) already captured it. Now check what's missing.
    missing = build_missing_fields_message(session)
    if missing:
        # If changing date/time was the goal, and it's still missing:
        if "fecha" in missing:
            return "Entendido. ¿Me indicas la *nueva fecha y hora* que deseas para la cita? Por ejemplo: _'el martes a las 10 am'_"
        # If other fields are missing (e.g., package got wiped)
        return missing

    # If all fields are present after extraction (including a new date/time)
    return finish_booking_summary(session)


def handle_cancel(msg, session):
    session["awaiting_confirmation"] = False
    # In a real app, this would check the DB for an active booking.
    # For now, it just prompts for confirmation.
    return "Perfecto, ¿confirmas que deseas *cancelar* completamente la cita agendada? (Responde *Sí* si estás seguro)"

def handle_contextual(msg: str, session: dict) -> str | None:
    """Handles non-booking questions (hours, location, process)."""
    text = msg.lower().strip()
    
    # If a contextual question is asked, stop any ongoing confirmation loop
    if text not in INTENTS["confirmation"]["patterns"] and session["awaiting_confirmation"]:
        session["awaiting_confirmation"] = False
        save_session(session)
    
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
        # If in booking flow, repeat the missing fields prompt
        if session["booking_started"]:
            missing = build_missing_fields_message(session)
            if missing:
                return missing
            # If all are filled, repeat the summary
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

    if text.strip().endswith("?") and "😊" not in text and "🙏" not in text:
        text = text.rstrip("?") + " 😊?"

    return text

# --- 7. MAIN MESSAGE PROCESSING FLOW (FIXED) ---

def process_message(msg: str, session: dict) -> str:
    """
    The central state machine logic.
    FIX: Prioritized high-level intents (Confirmation, Modify, Cancel) over 
    the general booking flow check (session["booking_started"]), resolving the deadlock.
    """
    
    # 1. Extract and update all info from the incoming message (allows corrections to overwrite data)
    update_session_with_info(msg, session)
    
    # 2. Handle contextual/general questions (e.g., 'What are your hours?')
    contextual_response = handle_contextual(msg, session)
    if contextual_response:
        return natural_tone(contextual_response)

    # 3. Detect Explicit Intent (Must use the updated session for confirmation check)
    intent = detect_explicit_intent(msg, session)
    
    # 4. Handle high-level, disruptive intents (CONFIRMATION, CANCEL, MODIFY, BOOKING REQUEST, PACKAGE INFO)
    # This must happen BEFORE the general 'booking_started' flow to prevent deadlock.
    if intent and intent in INTENTS:
        handler = globals()[INTENTS[intent]["handler"]]
        
        # A. Handle Confirmation (Only if state flag is set)
        if intent == "confirmation" and session.get("awaiting_confirmation"):
            return natural_tone(handler(msg, session))
        
        # B. Handle other high-level, non-confirmation intents
        if intent in ["cancel", "modify", "booking_request", "package_info", "greeting"]:
            # NOTE: handle_booking_request resets flags and starts the flow.
            return natural_tone(handler(msg, session))
        
    # 5. Handle Booking/Continuation Flow
    if session["booking_started"]:
        missing_message = build_missing_fields_message(session)
        
        # A. ALL FIELDS COMPLETE -> Show Final Summary (sets awaiting_confirmation=True)
        if not missing_message:
            # Re-prompts for confirmation if the user hasn't confirmed yet (awaiting_confirmation=True)
            # or if awaiting_confirmation=False (e.g., they just finished filling all fields).
            return natural_tone(finish_booking_summary(session))
        
        # B. FIELDS MISSING -> Ask for the next missing piece
        return natural_tone(missing_message)

    # 6. Default Response (Greeting or general prompt)
    if not session["greeted"]:
        return natural_tone(handle_greeting(msg, session))

    # 7. Fallback
    return "Disculpa, no entendí bien. ¿Me lo repites o me indicas si quieres *agendar una cita* o saber sobre los *paquetes*? 😊"


# --- 8. TWILIO WEBHOOK ENDPOINT (THE MISSING PIECE) ---

@app.post("/whatsapp", response_class=Response)
async def whatsapp_webhook(
    request: Request,
    WaId: str = Form(...), # Sender's WhatsApp ID (phone number)
    Body: str = Form(...), # Message content
):
    """
    Handles incoming POST requests from the Twilio WhatsApp webhook.
    This is the critical entry point that connects Twilio to your bot logic.
    """
    
    # Twilio sends the phone number in the format 'whatsapp:+1234567890'
    # We clean it to just the digits for our database key.
    phone = WaId.split(":")[-1].strip()
    user_message = Body.strip()

    # 1. Retrieve the user's current session state
    session = get_session(phone)

    # 2. Process the message and get the bot's response
    bot_response_text = process_message(user_message, session)

    # 3. Prepare the Twilio Messaging Response (TwiML)
    twiml = MessagingResponse()
    twiml.message(bot_response_text)

    # 4. Return the TwiML response
    return Response(content=str(twiml), media_type="application/xml")

# --- 9. HEALTH CHECK (Optional, but useful for deployment status) ---

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Simple health check endpoint."""
    return f"<h1>AI Reservation System is Running</h1><p>Timezone: {LOCAL_TZ.key}</p><p>Supabase Status: {'Connected' if supabase else 'Disconnected (Check ENV)'}</p>"
