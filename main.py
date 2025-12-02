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

app = FastAPI(title="AI Reservation System", version="1.0.5")

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

# --- 2. DATABASE & SESSION MANAGEMENT ---

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
    "awaiting_confirmation": False, # New state flag
}

def get_session(phone: str) -> dict:
    """Retrieves session state from Supabase or initializes a new one."""
    if not supabase: 
        new_session = DEFAULT_SESSION.copy()
        new_session['phone'] = phone
        return new_session

    try:
        response = supabase.table(SESSION_TABLE).select("data").eq("phone", phone).maybe_single().execute()

        if not response or not hasattr(response, "data") or response.data is None:
            new_session = DEFAULT_SESSION.copy()
            new_session['phone'] = phone
            return new_session

        if response.data.get("data") is None:
            new_session = DEFAULT_SESSION.copy()
            new_session['phone'] = phone
            return new_session

        session_data = response.data["data"]
        session_data["phone"] = phone
        return {**DEFAULT_SESSION, **session_data}

    except Exception as e:
        print(f"Error retrieving session for {phone}: {e}")
        new_session = DEFAULT_SESSION.copy()
        new_session["phone"] = phone
        return new_session

def save_session(session: dict):
    """Saves the current session state back to Supabase."""
    if not supabase: return
    
    phone = session.get("phone")
    if not phone: return

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
            "✅ *¡Reservación confirmada!*\n"
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

def detect_package(msg: str) -> str | None:
    """
    VERY ROBUST package detector.
    Matches color, keyword, price, or the phrase "paquete X".
    """
    msg = msg.lower().strip()

    # DIRECT PHRASE MATCH
    if "paquete esencial" in msg or "cuidado esencial" in msg or "esencial" in msg or "verde" in msg:
        return "Paquete Cuidado Esencial"

    if "paquete activa" in msg or "salud activa" in msg or "activa" in msg or "psico" in msg or "azul" in msg:
        return "Paquete Salud Activa"

    if "paquete total" in msg or "bienestar total" in msg or "total" in msg or "amarillo" in msg or "completo" in msg:
        return "Paquete Bienestar Total"

    # PRICE-BASED MATCH
    if any(x in msg for x in ["45k", "45 mil", "45.000", "45000"]):
        return "Paquete Cuidado Esencial"

    if any(x in msg for x in ["60k", "60 mil", "60.000", "60000"]):
        return "Paquete Salud Activa"

    if any(x in msg for x in ["75k", "75 mil", "75.000", "75000"]):
        return "Paquete Bienestar Total"

    return None

def extract_datetime_info(msg: str) -> tuple[str, str]:
    """
    Uses manual regex and dateparser to robustly extract date and time from complex messages.
    """
    
    msg_lower = msg.lower()
    today = datetime.now(LOCAL_TZ).date()
    date_str = ""
    time_str = ""
    dt_local = None # Primary dateparser result for the full message

    # --- 1. MANUAL DATE DETECTION (HIGH PRIORITY) ---
    if "mañana" in msg_lower:
        date_str = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    elif "hoy" in msg_lower:
        date_str = today.strftime("%Y-%m-%d")
    elif "pasado mañana" in msg_lower:
        date_str = (today + timedelta(days=2)).strftime("%Y-%m-%d")
    
    # --- 2. DATEPARSER FULL FALLBACK (For "el viernes", "10/12/2024", etc.) ---
    if not date_str:
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
        if dt_local:
            dt_local = dt_local.astimezone(LOCAL_TZ)
            date_str = dt_local.strftime("%Y-%m-%d")
    
    # --- 3. PAST DATE VALIDATION ---
    if date_str:
        try:
            parsed_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            if parsed_date < today:
                return "", "" # Invalid past date
        except ValueError:
            pass 
    
    # Only proceed to time extraction if a valid date was found.
    if date_str:
        
        # --- 4. ROBUST TIME EXTRACTION VIA REGEX & DATEPARSER ---
        explicit_time_match = re.search(
            r"(\b\d{1,2}\s*(?:am|pm|a\.m\.|p\.m\.)\b)"      # 8am, 8 am, 8a.m.
            r"|(\b\d{1,2}:\d{2}\s*(?:am|pm|a\.m\.|p\.m\.)?\b)"  # 8:00, 8:00am
            r"|(\ba\s+las\s+\d{1,2}(?::\d{2})?\b)"           # a las 8, a las 8:00
            r"|(\b\d{1,2}\s*(mañana|tarde|noche)\b)",        # 8 mañana, 8 de la tarde
            msg.lower()
        )

        if explicit_time_match:
            raw_time = explicit_time_match.group(0)

            # Normalize: remove "a las" and "a la"
            raw_time = raw_time.replace("a las ", "").replace("a la ", "").strip()

            # Add :00 if needed (e.g., "8am" -> "8:00am")
            if ":" not in raw_time and any(x in raw_time.lower() for x in ["am","pm","a.m","p.m"]):
                try:
                    hour = re.findall(r"\d{1,2}", raw_time)[0]
                    ampm_match = re.search(r"(a\.?m\.?|p\.?m\.?)", raw_time.lower())
                    ampm = ampm_match.group(0) if ampm_match else ""
                    raw_time = f"{hour}:00{ampm}"
                except:
                    pass 

            # Convert to 24h
            parsed_time = dateparser.parse(
                f"{date_str} {raw_time}", 
                settings={
                    "TIMEZONE": LOCAL_TZ.key,
                    "TO_TIMEZONE": LOCAL_TZ.key,
                    "RETURN_AS_TIMEZONE_AWARE": True
                }
            )
            
            if parsed_time:
                time_str = parsed_time.strftime("%H:%M")
                
                # CRITICAL FIX: If we have BOTH date and time -> RETURN NOW
                return date_str, time_str 

        # --- 5. FALLBACK TIME: Use dateparser's time if no explicit time was found ---
        elif dt_local:
             time_str = dt_local.strftime("%H:%M")
             
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

            # Split only by explicit punctuation or very clear separators (like date/time markers)
            name = re.split(r"[,.!?\n]| a las | a la | mañana | hoy | pasado mañana", name)[0]
            if name and len(name.split()) > 1:
                return name.title().strip()
    return None

def extract_age_cedula(msg: str, session: dict):
    """Extracts age and cedula if they are reasonable numbers."""
    
    # AGE DETECTION (1-2 digits, 1-20 range)
    if not session.get("age"):
        age_match = re.search(r"\b(\d{1,2})\s*(años|anos|año|ano)?\b", msg.lower())
        if age_match:
            age_num = int(age_match.group(1))
            if 1 <= age_num <= 20:
                session["age"] = age_num

    # CEDULA DETECTION (5-12 digits)
    if not session.get("cedula"):
        ced_match = re.search(r"(?<!:)(\b\d{5,12}\b)(?!:)", msg)
        if ced_match:
            session["cedula"] = ced_match.group(1)

def extract_student_name(msg: str) -> str | None:
    """
    Extracts student name from natural Spanish messages.
    """
    text = msg.lower()

    # 1. Capture pattern – VERY flexible
    pattern = r"(?:mi\s+(?:hijo|hija)\s+(?:se\s+llama\s+)?|para\s+mi\s+(?:hijo|hija)\s+|es\s+para\s+(?:mi\s+)?(?:hijo|hija)\s+)([a-záéíóúñ ]+)"
    m = re.search(pattern, text)
    if m:
        raw = m.group(1).strip()

        # 2. Cut trailing context (school, date, time, "del colegio...", "mañana", etc)
        raw = re.split(
            r"(del\s+colegio|colegio|gimnasio|liceo|instituto|escuela|a\s+las|a\s+la|mañana|hoy|pasado\s+mañana|\d{1,2}\s*(am|pm)|\d{1,2}:\d{2})",
            raw
        )[0].strip()

        # FIX: AVOID FALSE POSITIVES ("De", "La", "El", etc.)
        if raw in ["de", "del", "la", "el", "al", "en", "con"]:
            return None
        
        # 3. Clean extra spaces and leave only 1–3 name words
        words = raw.split()
        if 1 <= len(words) <= 3:
            return " ".join(w.capitalize() for w in words)

    # 4. Fallback: Detect if the user sent only capitalized words (e.g., "Juan Perez")
    
    # FIX: ENHANCED NOISE WORDS LIST 
    noise_words = [
        "quiero","cita","reservar","agendar","necesito","la","el","una","un",
        "hora","fecha","dia","día","por","favor","gracias","me","referia",
        "refería","perdon","perdón","mejor","si","sí","ok","dale","listo",
        "perfecto","super","claro","de","del","al","mi","es","se","llama",
        "hijo","hija","bueno","mañana","tarde","noche","am","pm","para",
        "del", "en", "con", "a", "los", "las", "y", "o"
    ]
    
    words = [w for w in msg.split() if w.lower() not in noise_words]
    
    if 2 <= len(words) <= 3 and all(w[0].isupper() for w in words):
        return " ".join(words).title()

    return None

def update_session_with_info(msg: str, session: dict):
    """
    Master function to extract and update all session parameters.
    Overwriting is always enabled if a new value is found. 
    """
    
    # 1. Data extraction
    new_name = extract_student_name(msg)
    new_school = extract_school_name(msg)
    new_package = detect_package(msg)
    new_date, new_time = extract_datetime_info(msg)
    extract_age_cedula(msg, session) # Updates session in place

    # 2. Apply extracted data: ALWAYS overwrite if a valid value is extracted
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

INTENTS["package_info"]["patterns"] = [
    "cuanto vale","cuánto vale","cuanto cuesta","precio","valor","paquete",
    "kit escolar",
    "psicologia","psicología","psicologo","psicólogo",   
    "odontologia","odontología",
    "el verde","el azul","el amarillo",
    "45k","60k","75k",
    "esencial","salud activa","bienestar total"
]
INTENTS["greeting"]["patterns"] = ["hola","buenas","buenos dias","buen dia","buenas tardes","buenas noches","disculpa","una pregunta","consulta","informacion","quisiera saber"]
INTENTS["booking_request"]["patterns"] = ["quiero reservar","quiero una cita","quiero agendar","necesito una cita","quiero el examen","me pueden reservar","agendar cita","reservar examen","separar cita"]
INTENTS["modify"]["patterns"] = ["cambiar cita","cambiar la cita","quiero cambiar","cambiar hora","cambiar fecha","mover cita","reagendar"]

# ✅ FIX #1 — Confirmación más natural
INTENTS["confirmation"]["patterns"] = [
    "confirmo",
    "sí confirmo",
    "si confirmo",
    "confirmar",
    "si",
    "sí",
    "ok",
    "vale",
    "dale",
    "listo",
    "correcto",
    "está bien",
    "esta bien",
    "confirmado"
]

INTENTS["cancel"]["patterns"] = ["cancelar","cancelar cita","anular","quitar la cita","ya no quiero la cita"]

def detect_explicit_intent(msg: str, session: dict) -> str | None:
    """
    Detects explicit intent based on keywords, using strict matching for confirmation.
    """
    msg_lower = msg.lower()
    msg_stripped = msg_lower.strip()
    
    # Prioritize disruptive or high-value intents
    priority = ["cancel", "modify", "confirmation", "booking_request", "package_info", "greeting"]
    for intent in priority:
        for p in INTENTS[intent]["patterns"]:
            
            if intent == "confirmation":
                # Only accept if the message is a PURE match against the natural confirmation list
                if msg_stripped == p:
                    # Permitir confirmaciones naturales (sólo si se está esperando confirmación)
                    if session.get("awaiting_confirmation"):
                        return "confirmation"
                    continue 

            else:
                # For all other intents, use the standard substring match
                if p in msg_lower:
                    return intent
    return None

def build_missing_fields_message(session: dict) -> str | None:
    """Generates a friendly message listing only the required missing fields."""
    missing = []
    if not session.get("student_name"): missing.append("el *nombre* del estudiante")
    if not session.get("school"): missing.append("el *colegio*")
    if not session.get("package"): missing.append("el *paquete* (ej: Esencial, Activa, Total)")
    if not session.get("date") or not session.get("time"): missing.append("la *fecha* y *hora* de la cita")
    if not session.get("age"): missing.append("la *edad* del estudiante")
    if not session.get("cedula"): missing.append("la *cédula* del estudiante")

    if not missing:
        return None

    if len(missing) == 1:
        return f"Listo 😊, solo me falta {missing[0]}. ¿Me lo compartes porfa? 🙏"
    
    joined = ", ".join(missing[:-1]) + " y " + missing[-1]
    return f"¡Perfecto! Para continuar, necesito estos datos: {joined}. ¿Me los colaboras? 🙏"

def finish_booking_summary(session: dict) -> str:
    """
    Generates the confirmation summary message and sets the 'awaiting_confirmation' flag.
    """
    session["awaiting_confirmation"] = True
    save_session(session) # Save state before returning response

    return (
        f"Listo 😊, ya tengo toda la información:\n\n"
        f"👤 Estudiante: {session.get('student_name', 'N/A')}\n"
        f"🎒 Colegio: {session.get('school', 'N/A')}\n"
        f"📦 Paquete: {session.get('package', 'N/A')}\n"
        f"📅 Fecha: {session.get('date', 'N/A')}\n"
        f"⏰ Hora: {session.get('time', 'N/A')}\n"
        f"🧒 Edad: {session.get('age', 'N/A')}\n"
        f"🪪 Cédula: {session.get('cedula', 'N/A')}\n\n"
        f"¿*Deseas confirmar la cita* con estos datos? (Responde *Confirmo*)"
    )

# --- 6. HANDLERS (State Machine Steps) ---

def handle_greeting(msg, session):
    if not session["greeted"]:
        session["greeted"] = True
        return "¡Hola! Soy tu asistente de agendamiento 🤖. Claro que sí, ¿en qué te puedo ayudar hoy? 😊"
    return "Claro que sí, ¿en qué te puedo ayudar?"

def handle_package_info(msg, session):
    session["awaiting_confirmation"] = False

    pkg = detect_package(msg) or session.get("package")

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

    if pkg and pkg in prices:
        session["package"] = pkg
        
        if session.get("student_name") or session.get("school") or session.get("date") or session.get("time"):
            session["booking_started"] = True
            
        session["info_mode"] = False
        save_session(session) 

        return (
            f"Claro 😊\n"
            f"*{pkg}* cuesta *${prices[pkg]}*.\n\n"
            f"📋 *Incluye:*\n{details[pkg]}\n\n"
            "¿Te gustaría agendar una cita?"
        )

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
    session["awaiting_confirmation"] = False 
    
    missing_message = build_missing_fields_message(session)
    if not missing_message:
        return finish_booking_summary(session)

    return missing_message


def handle_confirmation(msg, session):
    required = [session.get(f) for f in ["student_name", "school", "package", "date", "time", "age", "cedula"]]
    if not all(required):
        session["awaiting_confirmation"] = False 
        save_session(session)
        return "Disculpa, parece que falta información clave. ¿Me la puedes completar?"

    response_msg = save_reservation(session)

    reset_session = {k: v for k, v in DEFAULT_SESSION.items() if k != "phone"}
    reset_session["phone"] = session["phone"]
    reset_session["greeted"] = session["greeted"]
    save_session(reset_session)

    return response_msg

def handle_modify(msg, session):
    session["awaiting_confirmation"] = False
    if not session["booking_started"]:
        session["booking_started"] = True
    
    missing = build_missing_fields_message(session)
    
    if missing:
        if "fecha" in missing:
            return "Entendido. ¿Me indicas la *nueva fecha y hora* que deseas para la cita? Por ejemplo: _'el martes a las 10 am'_"
        return missing

    return finish_booking_summary(session)


def handle_cancel(msg, session):
    session["awaiting_confirmation"] = False
    return "Perfecto, ¿confirmas que deseas *cancelar* completamente la cita agendada? (Responde *Sí Confirmo* si estás seguro)"

def handle_contextual(msg: str, session: dict) -> str | None:
    """Handles non-booking questions (hours, location, process)."""
    text = msg.lower().strip()
    
    if text not in INTENTS["confirmation"]["patterns"] and session.get("awaiting_confirmation"):
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
        if session.get("booking_started"):
            missing = build_missing_fields_message(session)
            if missing:
                return missing
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

# --- 7. MAIN MESSAGE PROCESSING FLOW (FINAL, SAFE, HIGH-CAPTURE) ---

def process_message(msg: str, session: dict) -> str:
    """
    The central state machine logic, with the final, robust extraction policy.
    """
    
    # 1. Detect Intent (NO SIDE EFFECTS) - Must be done early for handler logic
    intent = detect_explicit_intent(msg, session)
    
    # 2. HIGH-CAPTURE DATA EXTRACTION LOGIC
    msg_lower = msg.lower().strip()
    
    confirmation_words = INTENTS["confirmation"]["patterns"]
    is_pure_confirmation = msg_lower in confirmation_words

    if not is_pure_confirmation:
        # 3. Perform Extraction 
        
        # --- Capture Old State for Modification Check (NEW FIX) ---
        old_date = session.get("date")
        old_time = session.get("time")

        # Perform the actual data update (updates session dict in place and saves to DB)
        update_session_with_info(msg, session)

        # 🔥 SUPER PRIORIDAD: Si ya tenemos todos los datos, ignorar intents
        required_fields = ["student_name", "school", "package", "date", "time", "age", "cedula"]
        if all(session.get(f) for f in required_fields):
            session["booking_started"] = True
            save_session(session)
            return natural_tone(finish_booking_summary(session)) # SALTO DIRECTO
        
        # Force booking_started=True if ANY relevant data was captured.
        if session.get("student_name") or session.get("school") or session.get("package") or session.get("date") or session.get("time"):
            session["booking_started"] = True
            # NOTE: session is already saved in update_session_with_info.

        # --- AUTO MODIFY DETECTION (MEJORADA) ---
        # Check for modification only if a booking is already underway and we are not confirming.
        if session.get("booking_started") and not session.get("awaiting_confirmation"):

            modify = False

            # Compare the new values (now in session) against the old state captured earlier.
            if session.get("date") and session.get("date") != old_date:
                modify = True

            if session.get("time") and session.get("time") != old_time:
                modify = True

            # Si hubo cambio real → activar flujo de modificación
            if modify:
                # The session is already saved with the new data from update_session_with_info.
                # We return the modify message and skip the rest of the intent logic.
                return natural_tone("Perfecto 😊, actualicé la fecha y/o la hora. ¿Deseas confirmar la cita? (Responde *Confirmo*)")

    # 4. Handle Contextual/General Questions (High Priority Response)
    contextual_response = handle_contextual(msg, session)
    if contextual_response:
        return natural_tone(contextual_response)

    # 5. Handle High-Priority Intents 
    if intent and intent in INTENTS:
        handler = globals()[INTENTS[intent]["handler"]]
        
        # A. Handle Confirmation (Only if state flag is set AND it was a pure confirmation)
        if intent == "confirmation" and session.get("awaiting_confirmation"):
            return natural_tone(handler(msg, session))
        
        # B. Handle other high-level intents.
        if intent in ["cancel", "modify", "booking_request", "package_info", "greeting"]:
            return natural_tone(handler(msg, session))
        
    # 6. Handle Booking Continuation Flow
    if session["booking_started"]:
        missing_message = build_missing_fields_message(session)
        
        # A. ALL FIELDS COMPLETE -> Show Final Summary (Fallback check)
        if not missing_message:
            return natural_tone(finish_booking_summary(session))
        
        # B. FIELDS MISSING -> Ask for the next missing piece
        return natural_tone(missing_message)

    # 7. Default/Fallback
    # ✅ FIX #3 — Evitar que el bot envíe saludo en mitad de la reserva
    if not session["greeted"] and not session.get("booking_started"):
        return natural_tone(handle_greeting(msg, session))

    return "Disculpa, no entendí bien. ¿Me lo repites o me indicas si quieres *agendar una cita* o saber sobre los *paquetes*? 😊"


# --- 8. TWILIO WEBHOOK ENDPOINT ---

@app.post("/whatsapp", response_class=Response)
async def whatsapp_webhook(
    request: Request,
    WaId: str = Form(...), # Sender's WhatsApp ID (phone number)
    Body: str = Form(...), # Message content
):
    """
    Handles incoming POST requests from the Twilio WhatsApp webhook.
    """
    
    # Clean phone number for database key
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

# --- 9. HEALTH CHECK ---

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Simple health check endpoint."""
    return f"<h1>AI Reservation System is Running (v1.0.5)</h1><p>Timezone: {LOCAL_TZ.key}</p><p>Supabase Status: {'Connected' if supabase else 'Disconnected (Check ENV)'}</p>"
