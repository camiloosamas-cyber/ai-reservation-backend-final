print(">>> STARTING ORIENTAL IPS BOT v3.4.0 ✅")

import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

# Optional imports with fallbacks
try:
    import dateparser
    DATEPARSER_AVAILABLE = True
except ImportError:
    DATEPARSER_AVAILABLE = False
    print("WARNING: dateparser not available")

try:
    from supabase import create_client, Client, PostgrestAPIError
    SUPABASE_AVAILABLE = True
except ImportError:
    SUPABASE_AVAILABLE = False
    print("WARNING: Supabase not available")

try:
    from twilio.twiml.messaging_response import MessagingResponse
    TWILIO_AVAILABLE = True
except ImportError:
    TWILIO_AVAILABLE = False
    print("WARNING: Twilio not available")

# =============================================================================
# CONFIGURATION
# =============================================================================

os.chdir(os.path.dirname(os.path.abspath(__file__)))

TEST_MODE = os.getenv("TEST_MODE") == "1"

app = FastAPI(title="Oriental IPS WhatsApp Bot", version="3.4.0")

print("🚀 Oriental IPS Bot v3.4.0 - Production Ready")

# Timezone
try:
    LOCAL_TZ = ZoneInfo("America/Bogota")
except ZoneInfoNotFoundError:
    LOCAL_TZ = ZoneInfo("UTC")
    print("WARNING: Using UTC timezone")

# Static files and templates
try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
    templates = Jinja2Templates(directory="templates")
    print("✅ Static files and templates loaded")
except Exception as e:
    print(f"WARNING: Could not load static files: {e}")
    templates = None

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# =============================================================================
# EXTERNAL SERVICES
# =============================================================================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE = os.getenv("SUPABASE_SERVICE_ROLE")

supabase = None

if SUPABASE_AVAILABLE and SUPABASE_URL and SUPABASE_SERVICE_ROLE:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE)
        print("✅ Supabase connected")
    except Exception as e:
        print(f"ERROR: Supabase connection failed: {e}")
else:
    print("WARNING: Supabase credentials missing")

# Business configuration
RESERVATION_TABLE = "reservations"
SESSION_TABLE = "sessions"
BUSINESS_ID = 2
TABLE_LIMIT = 10

# =============================================================================
# IN-MEMORY SESSION STORE (Fallback)
# =============================================================================

MEMORY_SESSIONS = {}

# =============================================================================
# SESSION MANAGEMENT
# =============================================================================

def create_new_session(phone):
    """Create a fresh session"""
    return {
        "phone": phone,
        "student_name": None,
        "school": None,
        "package": None,
        "date": None,
        "time": None,
        "age": None,
        "cedula": None,
        "booking_started": False,
        "booking_intro_shown": False,
        "greeted": False,
        "awaiting_confirmation": False
    }

def get_session(phone):
    """Retrieve or create session for phone number"""
    
    # Try database first
    if supabase:
        try:
            result = supabase.table(SESSION_TABLE).select("data").eq("phone", phone).maybe_single().execute()
            
            if result and result.data and result.data.get("data"):
                session = result.data["data"]
                session["phone"] = phone
                return session
        except Exception as e:
            print(f"Error loading session from DB: {e}")
    
    # Fallback to memory
    if phone not in MEMORY_SESSIONS:
        MEMORY_SESSIONS[phone] = create_new_session(phone)
    
    return MEMORY_SESSIONS[phone]

def save_session(session):
    """Save session to database and memory"""
    phone = session.get("phone")
    if not phone:
        return
    
    # Always save to memory
    MEMORY_SESSIONS[phone] = session
    
    # Try to save to database
    if supabase:
        try:
            data = {k: v for k, v in session.items() if k != "phone"}
            supabase.table(SESSION_TABLE).upsert({
                "phone": phone,
                "data": data,
                "last_updated": datetime.now(LOCAL_TZ).isoformat()
            }).execute()
        except Exception as e:
            print(f"Error saving session to DB: {e}")

# =============================================================================
# PACKAGE DATA
# =============================================================================

PACKAGES = {
    "esencial": {
        "name": "Paquete Cuidado Esencial",
        "price": "45.000",
        "description": "Medicina General, Optometria y Audiometria"
    },
    "activa": {
        "name": "Paquete Salud Activa",
        "price": "60.000",
        "description": "Medicina General, Optometria, Audiometria y Psicologia"
    },
    "bienestar": {
        "name": "Paquete Bienestar Total",
        "price": "75.000",
        "description": "Medicina General, Optometria, Audiometria, Psicologia y Odontologia"
    }
}

# Known schools in Yopal (case-insensitive, used for extraction)
KNOWN_SCHOOLS = [
    # Public / Official Institutions
    "institución educativa 1 de mayo",
    "institución educativa antonio nariño",
    "institución educativa braulio gonzález",
    "institución educativa camilo torres",
    "institución educativa camilo torres restrepo",
    "institución educativa carlos lleras restrepo",
    "institución educativa centro social la presentación",
    "institución educativa luis hernández vargas",
    "institución educativa megacolegio yopal",
    "institución educativa policarpa salavarrieta",
    "institución educativa picón arenal",
    "institución educativa santa teresa",
    "institución educativa santa teresita",
    "escuela santa bárbara",
    "escuela santa inés",
    "escuela el paraíso",
    
    # Private / Non-Official Schools
    "academia militar josé antonio páez",
    "colegio alianza pedagógica",
    "colegio antonio nariño",
    "colegio bethel yopal",
    "colegio braulio gonzález – sede campestre",
    "colegio campestre hispano inglés del casanare",
    "colegio campestre liceo literario del casanare",
    "colegio lucila piragauta",
    "colegio rafael pombo",
    "colegio san mateo apóstol",
    "gimnasio comfacasanare",
    "gimnasio de los llanos",
    "gimnasio loris malaguzzi",
    "instituto pilosophia",
    "liceo escolar los ángeles",
    "liceo gustavo matamoros león",
    "liceo moderno celestín freinet casanare",
    "liceo pedagógico colombo inglés",
    "liceo pedagógico san martín",
    
    # Technical / Vocational
    "instituto técnico empresarial de yopal",
    "instituto técnico ambiental san mateo",
    
    # Early Childhood
    "jardín infantil castillo de la alegría",
    "jardín infantil carrusel mágico",
    
    # COMMON SHORT FORMS & TYPING VARIANTS (no accents, partial names)
    "gimnasio los llanos",
    "los llanos",
    "comfacasanare",
    "san jose",
    "santa teresita",
    "santa teresa",
    "rafael pombo",
    "lucila piragauta",
    "campestre hispano",
    "liceo literario",
    "megacolegio",
    "itey",
    "instituto tecnico",
    "jardin carrusel",
    "castillo alegria",
    "picon arenal",
    "braulio gonzalez",
    "camilo torres",
    "antonio narino",
]

# =============================================================================
# FAQ RESPONSES
# =============================================================================

FAQ = {
    "ubicacion": "Estamos ubicados en Calle 31 #29-61, Yopal.",
    "pago": "Aceptamos Nequi y efectivo.",
    "duracion": "El examen dura entre 30 y 45 minutos.",
    "llevar": "Debes traer el documento de identidad del estudiante.",
    "horario": "Atendemos de lunes a domingo de 7am a 5pm."
}

# =============================================================================
# EXTRACTION FUNCTIONS
# =============================================================================

def extract_package(msg):
    """Extract package from message"""
    text = msg.lower()
    
    # Esencial
    if any(k in text for k in ["esencial", "verde", "45k", "45000", "45.000", "45 mil"]):
        return "Paquete Cuidado Esencial"
    
    # Activa
    if any(k in text for k in ["activa", "salud activa", "azul", "psico", "psicologia", "60k", "60000", "60.000", "60 mil"]):
        return "Paquete Salud Activa"
    
    # Bienestar
    if any(k in text for k in ["bienestar", "total", "amarillo", "completo", "odonto", "75k", "75000", "75.000", "75 mil"]):
        return "Paquete Bienestar Total"
    
    return None

def extract_student_name(msg, current_name):
    """Extract student name from message"""
    text = msg.strip()
    lower = text.lower()
    
    # Skip if name exists and user not changing it
    if current_name and not any(k in lower for k in ["cambiar", "otro nombre", "se llama"]):
        return None
        
    # Skip greetings
    if lower in ["hola", "buenos dias", "buenas tardes", "buenas noches", "buenas"]:
        return None

    # Pattern: "se llama X"
    if "se llama" in lower:
        # Use flexible split that handles commas and extra spaces
        parts = lower.split("se llama", 1)
        if len(parts) > 1:
            candidate = parts[1].strip()
            # Remove everything after stop words
            for stop in ["del", "de", "tiene", "edad", "colegio", "cedula", "su", "para", "quiero", "el", "la", ",", "."]:
                if f" {stop}" in candidate:
                    candidate = candidate.split(f" {stop}")[0]
                elif candidate.startswith(stop):
                    candidate = ""
                    break
            candidate = candidate.strip().rstrip(".,!?")
            if candidate and len(candidate.split()) >= 2:
                return candidate.title()

    # Pattern: "mi hijo X"
    m = re.search(r"mi\s+(?:hijo|hiijo|niña|niño|hija)\s+([a-záéíóúñ\s]+?)(?=\s*(?:,|\.|del|de|tiene|edad|colegio|$))", lower)
    if m:
        name = m.group(1).strip()
        name = re.split(r"\s+(?:del|de|tiene|edad|colegio)", name)[0].strip()
        if name and len(name.split()) >= 2:
            return name.title()

    # Pattern: "nombre es X"
    if "nombre" in lower:
        m = re.search(r"nombre\s*:?\s*es\s+([a-záéíóúñ\s]+)", lower)
        if m:
            name = m.group(1).strip()
            name = re.split(r"\s+(?:de|del|tiene|anos?|colegio)", name)[0].strip()
            if name and len(name.split()) >= 2:
                return name.title()

    # Multi-line: first line as name (even if lowercase)
    lines = [line.strip() for line in msg.split("\n") if line.strip()]
    if len(lines) >= 2:
        first_line = lines[0]
        words = first_line.split()
        if 2 <= len(words) <= 3:
            clean_words = []
            for w in words:
                if re.match(r"^[a-záéíóúñ]+$", w.lower()) and len(w) >= 2:
                    clean_words.append(w.capitalize())
                else:
                    clean_words = []
                    break
            if len(clean_words) == len(words):
                return " ".join(clean_words)

    # Fallback: standalone name in single-line message
    words = lower.split()
    if 2 <= len(words) <= 4:
        valid_words = [w for w in words if len(w) >= 2 and re.match(r"^[a-záéíóúñ]+$", w)]
        if len(valid_words) >= 2:
            combined = " ".join(valid_words)
            avoid = ["buenos", "confirmo", "paquete", "colegio", "cita", "hora", "fecha", "mañana", "manana", "para", "tiene"]
            if not any(k in combined for k in avoid):
                return " ".join(valid_words).title()
    
    return None

def extract_school(msg):
    """Extract school name from message using known schools list"""
    text = msg.lower().strip()
    
    # Normalize common accent variations for matching
    normalized_text = (
        text.replace("á", "a")
             .replace("é", "e")
             .replace("í", "i")
             .replace("ó", "o")
             .replace("ú", "u")
             .replace("ñ", "n")
             .replace("ü", "u")
    )

    # First: try patterns with explicit keywords (original logic)
    patterns = [
        r"(?:colegio|gimnasio|liceo|instituto|escuela|jardín|jardin)\s+([a-z0-9\s]+)",
        r"(?:del\s+(?:colegio|gimnasio|liceo|instituto|escuela)|de\s+la\s+(?:escuela|institución))\s+([a-z0-9\s]+)",
    ]
    
    for pattern in patterns:
        m = re.search(pattern, normalized_text)
        if m:
            school_name = m.group(1).strip()
            # Clean up trailing context
            school_name = re.split(r"[.,!?]|\s+(?:tiene|anos?|edad|cédula|cedula)", school_name)[0].strip()
            if len(school_name) > 2:
                return school_name.title()

    # Second: match against known schools (even without keywords)
    for school in KNOWN_SCHOOLS:
        # Normalize school name too
        norm_school = (
            school.lower()
                 .replace("á", "a")
                 .replace("é", "e")
                 .replace("í", "i")
                 .replace("ó", "o")
                 .replace("ú", "u")
                 .replace("ñ", "n")
        )
        
        if norm_school in normalized_text:
            # Extract full match and clean
            start = normalized_text.find(norm_school)
            end = start + len(norm_school)
            
            # Try to capture up to 3 extra words (for full names)
            remaining = normalized_text[end:].split()[:3]
            full_match = norm_school + " " + " ".join(remaining)
            full_match = re.split(r"[.,!?]", full_match)[0].strip()
            
            if len(full_match) > 3:
                # Restore original capitalization from KNOWN_SCHOOLS
                return school.title()

    # Fallback: if any school-related keyword exists, take next words
    if any(kw in normalized_text for kw in ["gimnasio", "colegio", "instituto", "liceo", "comfacasanare"]):
        words = msg.strip().split()
        school_words = [w for w in words if len(w) >= 3]
        if school_words:
            return " ".join(school_words[:4]).title()
    
    return None

def extract_age(msg):
    """Extract age from message ONLY if explicitly stated"""
    text = msg.lower()

    # Normalize accents
    text = (
        text.replace("á", "a")
            .replace("é", "e")
            .replace("í", "i")
            .replace("ó", "o")
            .replace("ú", "u")
            .replace("ñ", "n")
    )

    # Pattern: "13 años", "13 anos"
    m = re.search(r"\b(\d{1,2})\s*(?:ano|anos)\b", text)
    if m:
        age = int(m.group(1))
        if 5 <= age <= 25:
            return age

    # Pattern: "edad 13", "tiene 13"
    m = re.search(r"\b(?:edad|tiene)\s+(\d{1,2})\b", text)
    if m:
        age = int(m.group(1))
        if 5 <= age <= 25:
            return age

    return None

def extract_cedula(msg):
    """Extract cedula (ID number) from message"""
    # Colombian ID: 7-12 digits
    m = re.search(r"\b(\d{7,12})\b", msg)
    if m:
        return m.group(0)
    return None

def extract_date(msg, session):
    """Extract date from message"""
    text = msg.lower()
    today = datetime.now(LOCAL_TZ).date()

    # Quick natural dates
    if "mañana" in text or "manana" in text:
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    if "hoy" in text:
        return today.strftime("%Y-%m-%d")
    if "pasado mañana" in text or "pasado manana" in text:
        return (today + timedelta(days=2)).strftime("%Y-%m-%d")

    # Handle "13 de febrero" directly with regex
    # Pattern: "13 de febrero", "13 feb", "13/02", etc.
    date_patterns = [
        r"(\d{1,2})\s+de\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)",
        r"(\d{1,2})\s+(ene|feb|mar|abr|may|jun|jul|ago|sep|oct|nov|dic)",
        r"(\d{1,2})[/\-](\d{1,2})",
        r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})"
    ]

    for pattern in date_patterns:
        m = re.search(pattern, text)
        if m:
            day = int(m.group(1))
            month_str = m.group(2) if len(m.groups()) > 1 else None
            
            # Map month names to numbers
            month_map = {
                "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
                "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
                "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
                "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12
            }

            if month_str:
                month = month_map.get(month_str.lower())
                if month:
                    year = today.year
                    # If date is in past, assume next year
                    try:
                        parsed_date = datetime(year, month, day).date()
                        if parsed_date < today:
                            parsed_date = parsed_date.replace(year=year + 1)
                        return parsed_date.strftime("%Y-%m-%d")
                    except ValueError:
                        pass  # Invalid date (e.g., Feb 30)

            # For MM/DD or DD/MM formats
            if len(m.groups()) == 2:
                # Assume DD/MM if first number <= 12
                if day <= 12:
                    month = int(m.group(2))
                    year = today.year
                    try:
                        parsed_date = datetime(year, month, day).date()
                        if parsed_date < today:
                            parsed_date = parsed_date.replace(year=year + 1)
                        return parsed_date.strftime("%Y-%m-%d")
                    except ValueError:
                        pass

    # Fallback to dateparser if no direct match
    if DATEPARSER_AVAILABLE:
        try:
            dt = dateparser.parse(
                msg,  # ← Use original msg, not cleaned!
                languages=["es"],
                settings={
                    "TIMEZONE": "America/Bogota",
                    "RETURN_AS_TIMEZONE_AWARE": True,
                    "PREFER_DATES_FROM": "future",
                    "DATE_ORDER": "DMY"
                }
            )

            if dt:
                local_dt = dt.astimezone(LOCAL_TZ)
                parsed_date = local_dt.date()

                # If date is still before today, assume user means next year
                if parsed_date < today:
                    parsed_date = parsed_date.replace(year=today.year + 1)

                return parsed_date.strftime("%Y-%m-%d")

        except Exception as e:
            print("Dateparser error:", e)

    return session.get("date")

def extract_time(msg, session):
    """Extract time from message"""
    text = msg.lower()
    
    # Handle "9m" as "9am"
    text = re.sub(r"\b(\d{1,2})\s*m\b", r"\1am", text)
    
    # Pattern: 10am, 3pm, 10:30am, 10.30am
    m = re.search(r"(\d{1,2})(?:[:\.](\d{2}))?\s*(am|pm)", text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        ampm = m.group(3)
        
        # Convert to 24-hour format
        if ampm == "pm" and hour != 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        
        # Validate business hours (7am - 5pm = 7:00 - 17:00)
        if 7 <= hour <= 17:
            return f"{hour:02d}:{minute:02d}"
        else:
            return "INVALID_TIME"
    
    # Pattern: "las 11" or "a las 11"
    m = re.search(r"(?:las|a las)\s+(\d{1,2})", text)
    if m:
        hour = int(m.group(1))
        if 7 <= hour <= 17:
            return f"{hour:02d}:00"
        else:
            return "INVALID_TIME"
    
    # Vague times
    if "tarde" in text:
        return "15:00"
    if "manana" in text and "en la" in text:
        return "09:00"
    
    # Keep existing time if no new one found
    return session.get("time")

# =============================================================================
# SESSION UPDATE
# =============================================================================

def update_session_with_message(msg, session):
    """Extract all possible data from message and update session"""

    # ---------- NORMALIZATION ----------
    raw_msg = msg.lower()

    normalized_msg = (
        raw_msg
        .replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
    )

    FILLER_PHRASES = [
        "para el ",
        "para la ",
        "el dia ",
        "el día ",
        "dia ",
        "día ",
    ]

    COMMON_FIXES = {
        "febero": "febrero",        # Fix your normalization damage
        "febreo": "febrero",
        "febro": "febrero",
    
        "enero": "enero",
        "marzo": "marzo",
        "abril": "abril",
        "mayo": "mayo",
        "junio": "junio",
        "julio": "julio",
        "agosto": "agosto",
        "septiembre": "septiembre",
        "setiembre": "septiembre",  # common misspelling
        "octubre": "octubre",
        "noviembre": "noviembre",
        "diciembre": "diciembre",
        "diembre": "diciembre",     # your existing fix
    }

    for wrong, correct in COMMON_FIXES.items():
        normalized_msg = normalized_msg.replace(wrong, correct)

    for filler in FILLER_PHRASES:
        normalized_msg = normalized_msg.replace(filler, "")

    # If all fields are set, don't re-extract unless user says "cambiar"
    required_fields = ["student_name", "school", "package", "date", "time", "age", "cedula"]
    if all(session.get(f) for f in required_fields):
        if not any(kw in msg.lower() for kw in ["cambiar", "otro nombre", "se llama", "nombre es"]):
            return None  # Don't overwrite

    # ---------- EXTRACTION (USE RAW MSG FOR NAME, NORMALIZED FOR OTHERS) ----------
    pkg = extract_package(normalized_msg)
    name = extract_student_name(msg, session.get("student_name"))  # ← Use raw msg
    school = extract_school(normalized_msg)
    age = extract_age(normalized_msg)
    cedula = extract_cedula(normalized_msg)
    date = extract_date(msg, session)   # ← Raw msg for dateparser
    time = extract_time(normalized_msg, session)

    # ---------- UPDATE SESSION ----------
    updated = []

    if pkg:
        session["package"] = pkg
        updated.append("package")

    if name:
        session["student_name"] = name
        updated.append("name")

    if school:
        session["school"] = school
        updated.append("school")

    if age:
        session["age"] = age
        updated.append("age")

    if cedula:
        session["cedula"] = cedula
        updated.append("cedula")

    if date == "PAST_DATE":
        return "PAST_DATE"

    if date:
        session["date"] = date
        updated.append("date")

    if time:
        if time == "INVALID_TIME":
            return "INVALID_TIME"
        session["time"] = time
        updated.append("time")

    save_session(session)
    return updated

# =============================================================================
# MISSING FIELDS & PROMPTS
# =============================================================================

def get_missing_fields(session):
    """Get list of missing required fields"""
    required = ["student_name", "school", "package", "date", "time", "age", "cedula"]
    return [f for f in required if not session.get(f)]

def get_field_prompt(field):
    """Get prompt for specific missing field"""
    prompts = {
        "student_name": "Cual es el nombre completo del estudiante?",
        "school": "De que colegio es el estudiante?",
        "age": "Que edad tiene el estudiante?",
        "cedula": "Cual es el numero de cedula del estudiante?",
        "package": (
            "Tenemos 3 paquetes:\n\n"
            "1. Cuidado Esencial - $45.000\n"
            "   (Medicina General, Optometria, Audiometria)\n\n"
            "2. Salud Activa - $60.000\n"
            "   (Esencial + Psicologia)\n\n"
            "3. Bienestar Total - $75.000\n"
            "   (Activa + Odontologia)\n\n"
            "Cual paquete deseas?"
        ),
        "date": "Para que fecha deseas la cita? (ejemplo: mañana, 15 de enero)",
        "time": "A que hora prefieres? Atendemos de 7am a 5pm (ejemplo: 10am o 3pm)",
    }
    return prompts.get(field, "")

# =============================================================================
# SUMMARY & CONFIRMATION
# =============================================================================

def build_summary(session):
    """Build booking summary for confirmation"""
    
    # Get package details
    pkg_key = None
    for key, data in PACKAGES.items():
        if data["name"] == session["package"]:
            pkg_key = key
            break
    
    if not pkg_key:
        pkg_key = "esencial"
    
    pkg_data = PACKAGES[pkg_key]
    
    summary = (
        "Ya tengo toda la informacion:\n\n"
        f"Estudiante: {session['student_name']}\n"
        f"Colegio: {session['school']}\n"
        f"Paquete: {pkg_data['name']} (${pkg_data['price']})\n"
        f"Fecha: {session['date']}\n"
        f"Hora: {session['time']}\n"
        f"Edad: {session['age']} años\n"
        f"Cedula: {session['cedula']}\n\n"
        "Deseas confirmar esta cita? Responde CONFIRMO para agendar."
    )
    
    session["awaiting_confirmation"] = True
    save_session(session)
    
    return summary

# =============================================================================
# DATABASE OPERATIONS
# =============================================================================

def assign_table_number(dt_iso):
    """Assign next available table for given datetime"""
    if not supabase:
        return "T1"
    
    try:
        # Get all reservations for this datetime
        result = supabase.table(RESERVATION_TABLE).select("table_number").eq("datetime", dt_iso).execute()
        
        taken_tables = {r["table_number"] for r in (result.data or [])}
        
        # Find first available table
        for i in range(1, TABLE_LIMIT + 1):
            table = f"T{i}"
            if table not in taken_tables:
                return table
        
        return None  # No tables available
        
    except Exception as e:
        print(f"Error assigning table: {e}")
        return "T1"

def insert_reservation(phone, session):
    """Insert confirmed reservation into database"""
    if not supabase:
        return True, "T1"
    
    try:
        # Build datetime
        dt = datetime.strptime(
            f"{session['date']} {session['time']}",
            "%Y-%m-%d %H:%M"
        )
        dt_local = dt.replace(tzinfo=LOCAL_TZ)
        dt_iso = dt_local.isoformat()
        
        # Assign table
        table = assign_table_number(dt_iso)
        if not table:
            return False, "No hay cupos disponibles para ese horario"
        
        # Insert reservation
        supabase.table(RESERVATION_TABLE).insert({
            "customer_name": session["student_name"],
            "contact_phone": phone,
            "datetime": dt_iso,
            "table_number": table,
            "status": "confirmado",
            "business_id": BUSINESS_ID,
            "package": session["package"],
            "school_name": session["school"],
            "age": session["age"],
            "cedula": session["cedula"]
        }).execute()
        
        return True, table
        
    except Exception as e:
        print("❌ INSERT RESERVATION FAILED")
        print("ERROR:", repr(e))
        print("SESSION DATA:", session)
        return False, repr(e)

# =============================================================================
# FAQ HANDLER
# =============================================================================

def check_faq(msg):
    """Check if message is asking an FAQ question"""
    text = msg.lower()
    
    if any(k in text for k in ["ubicad", "direcc", "donde", "dónde"]):
        return FAQ["ubicacion"]
    
    if any(k in text for k in ["pago", "nequi", "efectivo", "como pag"]):
        return FAQ["pago"]
    
    if any(k in text for k in ["dur", "demora", "cuanto tiempo"]):
        return FAQ["duracion"]
    
    if any(k in text for k in ["llevar", "traer", "documento", "necesito"]):
        return FAQ["llevar"]
    
    if any(k in text for k in ["horario", "atienden", "abren", "cierran"]):
        return FAQ["horario"]
    
    return None

# =============================================================================
# MAIN MESSAGE HANDLER
# =============================================================================

def process_message(msg, session):
    """Main conversation logic (STRICT FLOW VERSION)"""
    
    print("PROCESS MESSAGE:", msg)
    print("SESSION STATE:", session)
    
    text = msg.strip()
    lower = text.lower()
    normalized = (
        lower.replace("á", "a")
             .replace("é", "e")
             .replace("í", "i")
             .replace("ó", "o")
             .replace("ú", "u")
    )

    # --------------------------------------------------
    # Detect intent
    # --------------------------------------------------
    ACTION_VERBS = [
        "agendar", "reservar", "reserva", "cita",
        "apartar", "adquirir", "tomar", "hacer",
        "realizar", "sacar",
        "quiero", "gustaria", "gustaría"
    ]
    has_action = any(w in normalized for w in ACTION_VERBS)

    GREETING_PATTERNS = [
        "hola",
        "buenas",
        "buenos dias",
        "buenos días",
        "buen dia",
        "buen día",
        "buenas tardes",
        "buenas noches",
        "hi",
        "hello"
    ]
    cleaned = normalized.replace(",", "").replace(".", "").replace("!", "").strip()
    is_greeting = any(cleaned == g or cleaned.startswith(g) for g in GREETING_PATTERNS)

    INFO_TRIGGERS = [
        "que contiene", "que incluye", "informacion",
        "información", "paquetes", "paquete", "examenes", "exámenes"
    ]
    is_info = any(p in normalized for p in INFO_TRIGGERS)

    SCHOOL_CONTEXT = [
        "examen", "examenes", "medico", "medicos",
        "colegio", "escolar", "ingreso"
    ]
    has_context = any(w in normalized for w in SCHOOL_CONTEXT)

    # Treat package mentions as booking intent
    PACKAGE_KEYWORDS = ["paquete", "45 mil", "60 mil", "75 mil", "esencial", "activa", "bienestar", "45k", "60k", "75k"]
    has_package_intent = any(kw in normalized for kw in PACKAGE_KEYWORDS)

    # --------------------------------------------------
    # ALWAYS extract data unless it's a pure greeting or info query
    # --------------------------------------------------

    # If user mentions school/exam context, treat as booking intent
    force_booking_intent = has_action or has_context or has_package_intent
    
    if force_booking_intent or not (is_greeting and not session.get("booking_started")):
        update_result = update_session_with_message(text, session)
        if update_result == "PAST_DATE":
            return "La fecha que indicaste ya pasó este año. ¿Te refieres a otro día?"
        if update_result == "INVALID_TIME":
            return "Lo siento, solo atendemos de 7am a 5pm. Por favor elige otra hora."

    # --------------------------------------------------
    # 1. GREETING (ONLY IF MESSAGE IS JUST A GREETING)
    # --------------------------------------------------
    if is_greeting and not session.get("booking_started"):
        session["greeted"] = True
        save_session(session)
        return "Buenos días, estás comunicado con Oriental IPS. ¿En qué te puedo ayudar?"

    # --------------------------------------------------
    # 2. INFO QUESTIONS (ALLOWED ANYTIME, BUT NO ACTION)
    # --------------------------------------------------
    if is_info and not has_action:
        pkg = extract_package(text)
        if pkg:
            for data in PACKAGES.values():
                if data["name"] == pkg:
                    return (
                        f"El *{data['name']}* incluye:\n{data['description']}\n\n"
                        f"Precio: ${data['price']} COP"
                    )

        return (
            "Claro 😊 Estos son nuestros *paquetes de exámenes médicos escolares*:\n\n"
            "🔹 *Cuidado Esencial* – $45.000 COP\n"
            "Medicina General, Optometría y Audiometría.\n\n"
            "🔹 *Salud Activa* – $60.000 COP\n"
            "Incluye el Esencial + Psicología.\n\n"
            "🔹 *Bienestar Total* – $75.000 COP\n"
            "Incluye el Activa + Odontología."
        )

    # --------------------------------------------------
    # 3. FAQ (ONLY BEFORE BOOKING STARTS)
    # --------------------------------------------------
    if not session.get("booking_started"):
        faq_answer = check_faq(text)
        if faq_answer:
            return faq_answer

    # --------------------------------------------------
    # 4. START BOOKING (ONCE)
    # --------------------------------------------------
    SCHOOL_CONTEXT = [
        "examen", "examenes", "medico", "medicos",
        "colegio", "escolar", "ingreso"
    ]
    has_context = any(w in normalized for w in SCHOOL_CONTEXT)

    if not session.get("booking_started") and (has_action or has_context or has_package_intent):
        session["booking_started"] = True
        session["booking_intro_shown"] = False
        save_session(session)
        
    # --------------------------------------------------
    # 5. SHOW INTRO OR CONTINUE
    # --------------------------------------------------
    if session.get("booking_started") and not session.get("booking_intro_shown"):
        session["booking_intro_shown"] = True
        save_session(session)
        if any([
            session.get("student_name"),
            session.get("school"),
            session.get("package"),
            session.get("date"),
            session.get("time"),
            session.get("age"),
            session.get("cedula"),
        ]):
            missing = get_missing_fields(session)
            if missing:
                return get_field_prompt(missing[0])
            return build_summary(session)
        else:
            return (
                "Perfecto 😊 Para agendar la cita solo necesito la siguiente información:\n\n"
                "- Nombre completo del estudiante\n"
                "- Colegio\n"
                "- Paquete\n"
                "- Fecha y hora\n"
                "- Edad\n"
                "- Cédula\n\n"
                "Puedes enviarme los datos poco a poco o todos en un solo mensaje."
            )

    # --------------------------------------------------
    # 6. ASK NEXT MISSING FIELD
    # --------------------------------------------------
    if session.get("booking_started"):
        missing = get_missing_fields(session)
        if missing:
            return get_field_prompt(missing[0])

    # --------------------------------------------------
    # 7. SUMMARY + CONFIRMATION
    # --------------------------------------------------
    if session.get("booking_started") and not session.get("awaiting_confirmation"):
        if not get_missing_fields(session):
            return build_summary(session)

    if session.get("awaiting_confirmation") and "confirmo" in normalized:
        ok, table = insert_reservation(session["phone"], session)
        if ok:
            date = session.get("date")
            time = session.get("time")
            confirmation_message = (
                "✅ *Cita confirmada*\n\n"
                f"📅 Fecha: {date}\n"
                f"⏰ Hora: {time}\n\n"
                "📍 *Oriental IPS*\n"
                "Calle 31 #29-61, Yopal\n\n"
                "🪪 Recuerda traer el documento de identidad del estudiante.\n\n"
                "¡Te estaremos esperando!"
            )
            phone = session["phone"]
            session.clear()
            session.update(create_new_session(phone))
            save_session(session)
            return confirmation_message
        return "❌ No pudimos completar la reserva. Intenta nuevamente."

    # --------------------------------------------------
    # 8. SILENT FALLBACK (do not reply)
    # --------------------------------------------------
    return None  # Bot stays silent

# =============================================================================
# TWILIO WEBHOOK
# =============================================================================

@app.post("/whatsapp")
async def whatsapp_webhook(request: Request, WaId: str = Form(...), Body: str = Form(...)):
    phone = WaId.split(":")[-1].strip()
    user_msg = Body.strip()
    
    session = get_session(phone)
    response_text = process_message(user_msg, session)
    
    # Test mode - return plain text
    if TEST_MODE:
        return Response(content=response_text or "", media_type="text/plain")
    
    # Production mode - return Twilio XML
    if TWILIO_AVAILABLE:
        if response_text:  # Only reply if there's a message
            twiml = MessagingResponse()
            twiml.message(response_text)
            return Response(content=str(twiml), media_type="application/xml")
        else:
            # Return empty response → no WhatsApp reply
            return Response(content="", media_type="text/plain")
    
    # Fallback - plain text
    return Response(content=response_text or "", media_type="text/plain")

# =============================================================================
# WEB INTERFACE
# =============================================================================

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Home page / admin dashboard"""
    
    if templates:
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "version": app.version,
                "supabase_status": "Connected" if supabase else "Disconnected",
                "local_tz": str(LOCAL_TZ),
            },
        )
    
    # Fallback if no templates
    return HTMLResponse(content=f"<h1>Oriental IPS Bot v{app.version}</h1><p>Status: Running</p>")

# =============================================================================
# API ENDPOINTS
# =============================================================================

@app.get("/api/reservations")
async def get_reservations():
    """Get upcoming reservations"""
    
    if not supabase:
        return {"error": "Supabase not available"}
    
    try:
        now = datetime.now(LOCAL_TZ)
        seven_days = now + timedelta(days=7)
        
        result = (
            supabase.table(RESERVATION_TABLE)
            .select("*")
            .eq("business_id", BUSINESS_ID)
            .gte("datetime", now.isoformat())
            .lt("datetime", seven_days.isoformat())
            .order("datetime", desc=False)
            .execute()
        )
        
        return {"reservations": result.data}
        
    except Exception as e:
        return {"error": str(e)}

@app.get("/health")
def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "version": "3.4.0",
        "supabase": supabase is not None,
        "timezone": str(LOCAL_TZ),
        "active_sessions": len(MEMORY_SESSIONS)
    }

@app.get("/api/sessions")
def get_all_sessions():
    """Debug endpoint - view all active sessions"""
    return {"sessions": MEMORY_SESSIONS}

# =============================================================================
# STARTUP
# =============================================================================

print("✅ Oriental IPS Bot v3.4.0 ready!")
print(f"   - Supabase: {'Connected' if supabase else 'Not available'}")
print(f"   - Timezone: {LOCAL_TZ}")
print(f"   - Test mode: {TEST_MODE}")
