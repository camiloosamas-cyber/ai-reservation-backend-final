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

# ✅ VERSION 1.0.15 - Stable (Fixes finales de regex)
app = FastAPI(title="AI Reservation System", version="1.0.15")
print("🚀 AI Reservation System Loaded — Version 1.0.15 (Startup Confirmed)")

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
            f"📦 Paquete: {data.get('package', 'N/A')}\n"
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
            languages=["es"],
            settings={
                "TIMEZONE": LOCAL_TZ.key,
                "TO_TIMEZONE": LOCAL_TZ.key,
                "RETURN_AS_TIMEZONE_AWARE": True,
                "PREFER_DATES_FROM": "future",
                "STRICT_PARSING": False,
                "RELATIVE_BASE": datetime.now(LOCAL_TZ) 
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
        
        # ✅ FIX #1 — Correct TIME REGEX (USER PROVIDED in previous step)
        explicit_time_match = re.search(
            r"(?:(?:a\s+las\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)\b)"   # 3 pm, 3:00 pm, a las 3 pm
            r"|(?:(?:a\s+las\s+)?(\d{1,2})(?::(\d{2}))?\s*(mañana|tarde|noche)\b)"   # 3 tarde, a las 3 noche
        , msg_lower)


        if explicit_time_match:
            # We must reconstruct the raw_time string based on which group matched.
            
            # Group structure:
            # Group 1 (Hour AM/PM), Group 2 (Minute AM/PM), Group 3 (AM/PM marker)
            # Group 4 (Hour Mañana/Tarde), Group 5 (Minute Mañana/Tarde), Group 6 (Mañana/Tarde marker)
            
            if explicit_time_match.group(1): # AM/PM Match
                hour = explicit_time_match.group(1)
                minute = explicit_time_match.group(2) or "00"
                ampm = explicit_time_match.group(3)
                raw_time = f"{hour}:{minute}{ampm}"
            elif explicit_time_match.group(4): # Mañana/Tarde Match
                hour = explicit_time_match.group(4)
                minute = explicit_time_match.group(5) or "00"
                period = explicit_time_match.group(6)
                raw_time = f"{hour}:{minute} {period}" # dateparser needs space for period
            else:
                 return date_str, time_str # Should not happen with the new regex, but safety fallback


            # Convert to 24h
            parsed_time = dateparser.parse(
                f"{date_str} {raw_time}", 
                languages=["es"], # Critical for Spanish time parsing
                settings={
                    "TIMEZONE": LOCAL_TZ.key,
                    "TO_TIMEZONE": LOCAL_TZ.key,
                    "RETURN_AS_TIMEZONE_AWARE": True,
                    "RELATIVE_BASE": datetime.now(LOCAL_TZ),
                    "STRICT_PARSING": False # <-- CRITICAL FOR ROBUST TIME PARSING
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
    """
    Robustly extracts school name using multiple patterns and cutoffs.
    """
    msg_clean = msg.lower()

    # New robust patterns
    patterns = [
        # ✅ FIX #4 — Fixed the regex grouping for end markers
        r"(?:colegio|gimnasio|liceo|instituto|escuela)\s+([a-záéíóúñ0-9\s]+?)(?=$|\.|,|\d|(mañana)|(tarde)|(noche)|(a\s+las)|(\s+para)|(\s+el\s+))",
        # Catches 'del colegio X'
        r"(?:del\s+|de\s+)(?:colegio|gimnasio|liceo|instituto|escuela)\s+([a-záéíóúñ0-9\s]+)"
    ]

    for p in patterns:
        m = re.search(p, msg_clean)
        if m:
            name = m.group(1).strip()
            # Final clean cut at punctuation
            name = re.split(r"[.,!?\n]", name)[0].strip()
            # Ensure it captured at least one word
            if len(name.split()) >= 1:
                return name.title()
    return None

def extract_age_cedula(msg: str, session: dict):
    """Extracts age and cedula if they are reasonable numbers."""
    
    # ✅ FIX #2 — Correct AGE DETECTION (USER PROVIDED in previous step)
    # AGE DETECTION (must include the word "años" or "edad")
    if not session.get("age"):
        age_match = re.search(r"(edad\s+(\d{1,2}))|(\b(\d{1,2})\s*(años|anos|año|ano)\b)", msg.lower())
        if age_match:
            # Group 2 is for 'edad X', Group 4 is for 'X años'
            session["age"] = int(age_match.group(2) or age_match.group(4))

    # CEDULA DETECTION (5-12 digits)
    if not session.get("cedula"):
        ced_match = re.search(r"(?<!:)(\b\d{5,12}\b)(?!:)", msg)
        if ced_match:
            session["cedula"] = ced_match.group(1)

# ✅ FIX #3 — Replaced with improved student name regex
def extract_student_name(msg: str) -> str | None:
    text = msg.lower()

    # Capture names in ALL common Spanish variations
    # This pattern is robust to capture names after common phrases like "mi hija", "para mi hijo", etc.
    pattern = r"(?:mi\s+(?:hijo|hija)\s+(?:se\s+llama\s+)?|para\s+mi\s+(?:hijo|hija)\s+|es\s+para\s+(?:mi\s+)?(?:hijo|hija)\s+)([a-záéíóúñ ]+)"

    m = re.search(pattern, text)
    if not m:
        return None

    raw = m.group(1).strip()

    # Cut at words that imply the end of the name
    raw = re.split(
        r"(del\s+colegio|colegio|gimnasio|liceo|instituto|escuela|a\s+las|a\s+la|mañana|tarde|noche|\d{1,2}\s*(am|pm)|\d{1,2}:\d{2})",
        raw
    )[0].strip()

    # Limit to 1–3 name parts
    words = raw.split()
    if 1 <= len(words) <= 3:
        return " ".join(w.capitalize() for w in words)

    return None

def update_session_with_info(msg: str, session: dict):
    new_name = extract_student_name(msg)
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
    if new_time:
        session["time"] = new_time

    save_session(session)
    return new_date, new_time

# --- 5. INTENT & CONTEXTUAL HANDLING ---

INTENTS = {
    "greeting": {"patterns": ["hola","buenas","buenos dias","buen dia","buenas tardes","buenas noches","disculpa","una pregunta","consulta","informacion","quisiera saber"], "handler": "handle_greeting"},
    "package_info": {"patterns": ["cuanto vale","cuánto vale","cuanto cuesta","precio","valor","paquete","kit escolar","psicologia","psicología","psicologo","psicólogo","odontologia","odontología","el verde","el azul","el amarillo","45k","60k","75k","esencial","salud activa","bienestar total"], "handler": "handle_package_info"},
    "booking_request": {"patterns": ["quiero reservar","quiero una cita","quiero agendar","necesito una cita","quiero el examen","me pueden reservar","agendar cita","reservar examen","separar cita"], "handler": "handle_booking_request"},
    "modify": {"patterns": ["cambiar cita","cambiar la cita","quiero cambiar","cambiar hora","cambiar fecha","mover cita","reagendar"], "handler": "handle_modify"},
    "cancel": {"patterns": ["cancelar","cancelar cita","anular","quitar la cita","ya no quiero la cita"], "handler": "handle_cancel"},
    "confirmation": {"patterns": ["confirmo","sí confirmo","si confirmo","confirmar"], "handler": "handle_confirmation"}
}

def detect_explicit_intent(msg: str, session: dict) -> str | None:
    msg_lower = msg.lower()
    msg_stripped = msg_lower.strip()

    priority = ["cancel", "modify", "confirmation", "booking_request", "package_info", "greeting"]

    for intent in priority:
        for p in INTENTS[intent]["patterns"]:

            if intent == "confirmation":
                if msg_stripped == p and session.get("awaiting_confirmation"):
                    return intent
                continue

            if p in msg_lower:
                return intent

    return None

def build_missing_fields_message(session: dict) -> str | None:
    missing = []
    if not session["student_name"]: missing.append("el *nombre* del estudiante")
    if not session["school"]: missing.append("el *colegio*")
    if not session["package"]: missing.append("el *paquete* (Esencial, Activa, Total)")
    if not session["date"] or not session["time"]: missing.append("la *fecha y hora*")
    if not session["age"]: missing.append("la *edad*")
    if not session["cedula"]: missing.append("la *cédula*")

    if not missing: return None

    if len(missing) == 1:
        return f"Listo, solo me falta {missing[0]}. ¿Me lo compartes porfa? 🙏"

    joined = ", ".join(missing[:-1]) + " y " + missing[-1]
    return f"Perfecto 😊, solo necesito: {joined}"

def finish_booking_summary(session: dict) -> str:
    session["awaiting_confirmation"] = True
    save_session(session)

    return (
        f"Ya tengo toda la información:\n\n"
        f"👤 Estudiante: {session['student_name']}\n"
        f"🎒 Colegio: {session['school']}\n"
        f"📦 Paquete: {session['package']}\n"
        f"📅 Fecha: {session['date']}\n"
        f"⏰ Hora: {session['time']}\n"
        f"🧒 Edad: {session['age']}\n"
        f"🪪 Cédula: {session['cedula']}\n\n"
        f"¿Deseas confirmar esta cita? (Responde *Confirmo*)"
    )

# --- 6. HANDLERS (State Machine Steps) ---

def handle_greeting(msg, session):
    if not session["greeted"]:
        session["greeted"] = True
        return "¡Hola! Claro que sí, ¿en qué te puedo ayudar hoy? 😊"
    return "¿En qué te puedo ayudar? 😊"

def handle_package_info(msg, session):
    session["awaiting_confirmation"] = False
    pkg = detect_package(msg) or session.get("package")

    prices = {
        "Paquete Cuidado Esencial": "45.000",
        "Paquete Salud Activa": "60.000",
        "Paquete Bienestar Total": "75.000",
    }

    details = {
        "Paquete Cuidado Esencial": "Medicina General, Optometría, Audiometría",
        "Paquete Salud Activa": "Esencial + Psicología",
        "Paquete Bienestar Total": "Activa + Odontología",
    }

    if pkg and pkg in prices:
        session["package"] = pkg
        save_session(session)
        return f"{pkg} cuesta {prices[pkg]} COP.\nIncluye: {details[pkg]}\n\n¿Deseas agendar?"

    return (
        "Ofrecemos tres paquetes:\n"
        "• Esencial — 45.000\n"
        "• Salud Activa — 60.000\n"
        "• Bienestar Total — 75.000\n\n"
        "¿Cuál te interesa?"
    )

def handle_booking_request(msg, session):
    session["booking_started"] = True
    session["awaiting_confirmation"] = False

    missing = build_missing_fields_message(session)
    if missing:
        return missing
    return finish_booking_summary(session)

def handle_modify(msg, session):
    session["awaiting_confirmation"] = False
    session["booking_started"] = True
    missing = build_missing_fields_message(session)

    if missing:
        return "Perfecto 😊, indícame la NUEVA fecha y hora."
    return finish_booking_summary(session)

def handle_cancel(msg, session):
    session["awaiting_confirmation"] = False
    return "¿Confirmas que deseas *cancelar* la cita? (Responde *Confirmo*)"

def handle_confirmation(msg, session):
    response = save_reservation(session)
    reset = DEFAULT_SESSION.copy()
    reset["phone"] = session["phone"]
    reset["greeted"] = session["greeted"]
    save_session(reset)
    return response

def handle_contextual(msg: str, session: dict) -> str | None:
    text = msg.lower()

    if any(x in text for x in ["horario","abren","cerrar"]):
        return "Atendemos de lunes a viernes de 7am a 5pm y sábados de 7am a 1pm 😊"

    if any(x in text for x in ["donde","ubicados","dirección","direccion"]):
        return "Estamos en Bogotá, calle 75 #20-36. 📍"

    if any(x in text for x in ["duración","cuánto dura","que incluye","como funciona"]):
        return "El examen dura 30–45 minutos e incluye todas las pruebas del paquete. 😊"

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

    # Only attempt extraction if it's NOT a pure confirmation
    if not is_pure_confirmation:
        
        # 3. Perform Extraction 
        
        # --- Capture Old State for Modification Check ---
        old_date = session.get("date")
        old_time = session.get("time")

        # Perform the actual data update (updates session dict in place and saves to DB)
        # CRITICAL: This call updates and SAVES the session with all extracted data.
        new_date, new_time = update_session_with_info(msg, session) 

        # 🔥 SUPER PRIORIDAD: Si ya tenemos todos los datos, ignorar intents
        required_fields = ["student_name", "school", "package", "date", "time", "age", "cedula"]
        if all(session.get(f) for f in required_fields):
            session["booking_started"] = True
            session["awaiting_confirmation"] = True # Set flag before showing summary
            # Session is already saved in update_session_with_info, but saving again here is harmless.
            save_session(session) 
            return natural_tone("Listo 😊, ya tengo toda la información:\n\n" + finish_booking_summary(session)) # SALTO DIRECTO
        
        # Force booking_started=True if ANY relevant data was captured.
        if session.get("student_name") or session.get("school") or session.get("package") or session.get("date") or session.get("time"):
            session["booking_started"] = True
            # NOTE: session is already saved in update_session_with_info.

        # --- AUTO MODIFY DETECTION (FIXED) ---
        previous_date = old_date if old_date else None
        previous_time = old_time if old_time else None

        # Auto-modify ONLY if there was an existing date/time previously 
        # FIX: Also allow auto-modify if we are waiting for confirmation and user changes date/time
        auto_modify_allowed = (previous_date is not None or previous_time is not None) or session.get("awaiting_confirmation") 
        
        if (
            session.get("booking_started")
            and auto_modify_allowed                     
            and (new_date or new_time)
        ):
            # If they modify, we leave awaiting_confirmation as TRUE so the summary is shown right away
            session["awaiting_confirmation"] = True 
            save_session(session)  # ensure updated timestamp and values

            return natural_tone(
                "Perfecto 😊, ya actualicé la fecha y/o la hora.\n\n" +
                finish_booking_summary(session)
            )

    # 4. CRITICAL FIX: Calculate missing fields AFTER extraction and auto-modify logic.
    # This ensures it reads the session with all new data (date, time, etc.)
    missing_message = build_missing_fields_message(session)
    missing_fields_exist = missing_message is not None

    # 5. Contextual responses SHOULD NOT interrupt booking or modifications
    # If we are waiting for confirmation and they ask a question, we unset the flag and answer.
    if session.get("awaiting_confirmation") and intent not in ["confirmation", "modify", "booking_request"]:
        # Allow a contextual answer to reset the confirmation flag
        contextual_response = handle_contextual(msg, session)
        if contextual_response:
             session["awaiting_confirmation"] = False
             save_session(session)
             return natural_tone(contextual_response)

    # Allow contextual answers only when NOT waiting for confirmation, NOT modifying, and NO fields are missing.
    if (
        not session.get("awaiting_confirmation")
        and not missing_fields_exist          
        and intent not in ["modify", "booking_request"]
    ):
        contextual_response = handle_contextual(msg, session)
        if contextual_response:
            return natural_tone(contextual_response)

    # 6. Handle High-Priority Intents 
    if intent and intent in INTENTS:
        handler = globals()[INTENTS[intent]["handler"]]
        
        # A. Handle Confirmation (Only if state flag is set AND it was a pure confirmation)
        if intent == "confirmation" and session.get("awaiting_confirmation"):
            return natural_tone(handler(msg, session))
        
        # B. Handle other high-level intents.
        if intent in ["cancel", "modify", "booking_request", "package_info", "greeting"]:
            return natural_tone(handler(msg, session))
        
    # 7. Handle Booking Continuation Flow
    if session["booking_started"]:
        
        # A. ALL FIELDS COMPLETE -> Show Final Summary (Fallback check)
        if not missing_fields_exist: # Use the pre-calculated flag
            session["awaiting_confirmation"] = True
            save_session(session)
            return natural_tone("Listo 😊, ya tengo toda la información:\n\n" + finish_booking_summary(session))
        
        # B. FIELDS MISSING -> Ask for the next missing piece
        return natural_tone(missing_message) # Use the pre-calculated message

    # 8. Default/Fallback
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
    return f"<h1>AI Reservation System is Running (v1.0.15)</h1><p>Timezone: {LOCAL_TZ.key}</p><p>Supabase Status: {'Connected' if supabase else 'Disconnected (Check ENV)'}</p>"
