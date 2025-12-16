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
    
    # Skip package/price queries
    if any(k in lower for k in ["paquete", "precio", "cuesta", "cuanto"]):
        return None
    
    # Pattern: "se llama X"
    if "se llama" in lower:
        parts = lower.split("se llama", 1)
        if len(parts) > 1:
            name = parts[1].strip()
            # Clean up (remove age, school mentions)
            name = re.split(r"\s+(?:de|del|tiene|anos?|colegio)", name)[0].strip()
            if name:
                return name.title()
    
    # Pattern: "nombre es X" or "nombre: X"
    if "nombre" in lower:
        m = re.search(r"nombre\s*:?\s*es\s+([a-z\s]+)", lower)
        if m:
            name = m.group(1).strip()
            name = re.split(r"\s+(?:de|del|tiene|anos?|colegio)", name),strip()
            if name:
                return name.title()
    
    # Capitalized name (Juan Perez)
    m = re.search(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b", text)
    if m:
        return m.group(1).strip()
    
    # Standalone name (2-4 words, all lowercase letters)
    words = lower.split()
    if 2 <= len(words) <= 4:
        valid_words = [w for w in words if len(w) >= 2 and re.match(r"^[a-z]+$", w)]
        if len(valid_words) >= 2:
            combined = " ".join(valid_words)
            # Avoid common non-name phrases
            avoid = ["buenos", "confirmo", "paquete", "colegio", "cita", "hora", "fecha"]
            if not any(k in combined for k in avoid):
                return " ".join(valid_words).title()
    
    return None

def extract_school(msg):
    """Extract school name from message"""
    text = msg.lower()
    
    # Patterns with school keywords
    patterns = [
        r"(?:colegio|gimnasio|liceo|instituto|escuela)\s+([a-z0-9\s]+)",
        r"(?:del\s+colegio|del\s+gimnasio|de\s+la\s+escuela)\s+([a-z0-9\s]+)",
    ]
    
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            school_name = m.group(1).strip()
            # Clean up (stop at punctuation or age mentions)
            school_name = re.split(r"[.,!?]|\s+(?:tiene|anos?|edad)", school_name)[0].strip()
            if len(school_name) > 1:
                return school_name.title()
    
    # Standalone school keywords
    if any(k in text for k in ["gimnasio", "instituto", "comfacasanare"]):
        words = msg.strip().split()
        school_words = [w for w in words if len(w) >= 3]
        if school_words:
            return " ".join(school_words[:4]).title()
    
    return None

def extract_age(msg):
    """Extract age from message"""
    text = msg.lower()
    
    # Pattern: "12 años" or "12 anos"
    m = re.search(r"(\d{1,2})\s*anos?", text)
    if m:
        age = int(m.group(1))
        if 5 <= age <= 25:
            return age
    
    # Pattern: "edad 12" or "tiene 12"
    m = re.search(r"(?:edad|tiene)\s+(\d{1,2})", text)
    if m:
        age = int(m.group(1))
        if 5 <= age <= 25:
            return age
    
    # Standalone number
    if text.strip().isdigit():
        age = int(text.strip())
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
    
    # Explicit keywords
    if "manana" in text or "mañana" in text:
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    
    if "hoy" in text:
        return today.strftime("%Y-%m-%d")
    
    if "pasado manana" in text or "pasado mañana" in text:
        return (today + timedelta(days=2)).strftime("%Y-%m-%d")
    
    # Use dateparser if available
    if DATEPARSER_AVAILABLE:
        try:
            dt = dateparser.parse(
                msg,
                languages=["es"],
                settings={
                    "TIMEZONE": str(LOCAL_TZ),
                    "PREFER_DATES_FROM": "future"
                }
            )
            
            if dt:
                parsed_date = dt.date()
                if parsed_date >= today:
                    return parsed_date.strftime("%Y-%m-%d")
        except Exception as e:
            print(f"Dateparser error: {e}")
    
    # Keep existing date if no new one found
    return session.get("date")

def extract_time(msg, session):
    """Extract time from message"""
    text = msg.lower()
    
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
    
    # Extract each field
    pkg = extract_package(msg)
    name = extract_student_name(msg, session.get("student_name"))
    school = extract_school(msg)
    age = extract_age(msg)
    cedula = extract_cedula(msg)
    date = extract_date(msg, session)
    time = extract_time(msg, session)
    
    # Track what was updated
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
        "date": "Para que fecha deseas la cita? (ejemplo: manana, 15 de enero)",
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
        f"Edad: {session['age']} anos\n"
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
        print(f"Error inserting reservation: {e}")
        return False, str(e)

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
    """Main conversation logic"""
    text = msg.strip()
    lower = text.lower()
    
    # 1. Extract data from message
    update_result = update_session_with_message(text, session)
    
    if update_result == "INVALID_TIME":
        return "Lo siento, solo atendemos de 7am a 5pm. Por favor elige otra hora dentro de ese horario."
    
    # 2. FAQ (before booking)
    if not session.get("booking_started"):
        faq_answer = check_faq(text)
        if faq_answer:
            return faq_answer
    
    # 3. Greeting
    is_greeting = any(lower.startswith(g) for g in ["hola", "buenos", "buenas", "buen dia"])
    if is_greeting and not session.get("booking_started") and not session.get("greeted"):
        session["greeted"] = True
        save_session(session)
        return "Buenos dias, estas comunicado con Oriental IPS. En que te puedo ayudar?"
    
    # 4. Package info (before booking)
    pkg = extract_package(text)
    if pkg and not session.get("booking_started"):
        for key, data in PACKAGES.items():
            if data["name"] == pkg:
                return (
                    f"Perfecto, {data['name']} cuesta ${data['price']} COP.\n"
                    f"Incluye: {data['description']}\n\n"
                    "Deseas agendar una cita con este paquete?"
                )
    
    # 5. Start booking automatically if something was extracted
    if any([
        session.get("student_name"),
        session.get("school"),
        session.get("package"),
        session.get("date"),
        session.get("time"),
        session.get("age"),
        session.get("cedula"),
    ]):
        session["booking_started"] = True
        save_session(session)
    
    # 6. Explicit booking intent
    if any(k in lower for k in ["agendar", "cita", "reservar", "reserva"]):
        session["booking_started"] = True
        save_session(session)
    
    # 7. Still not booking → help message
    if not session.get("booking_started"):
        return "Soy Oriental IPS. Puedo ayudarte a agendar una cita o responder preguntas. Que necesitas?"
    
    # 8. Handle confirmation
    if session.get("awaiting_confirmation") and any(k in lower for k in ["confirmo", "confirmar"]):
        ok, table = insert_reservation(session["phone"], session)
        
        if ok:
            name = session["student_name"]
            pkg = session["package"]
            date = session["date"]
            time = session["time"]
            
            phone = session["phone"]
            session.clear()
            session.update(create_new_session(phone))
            save_session(session)
            
            return (
                f"Cita confirmada!\n\n"
                f"El estudiante {name} tiene su cita para {pkg}.\n"
                f"Fecha: {date} a las {time}\n"
                f"Mesa: {table}\n\n"
                f"Te esperamos en Oriental IPS! {FAQ['ubicacion']}"
            )
        
        return "No pudimos completar la reserva."
        
        # 9. Allow questions EVEN during booking (price / FAQ)
        if any(k in lower for k in ["cuanto", "precio", "cuesta"]):
            pkg = extract_package(text)
            if pkg:
                for key, data in PACKAGES.items():
                    if data["name"] == pkg:
                        return (
                            f"El {data['name']} cuesta ${data['price']} COP.\n"
                            f"Incluye: {data['description']}"
                        )
            return "Tenemos 3 paquetes: Esencial ($45.000), Activa ($60.000), Bienestar ($75.000)."
            
        faq_answer = check_faq(text)
        if faq_answer:
            return faq_answer
            
       # 10. Ask for missing fields (ONLY after questions are handled)
       missing = get_missing_fields(session)
       if missing:
           return get_field_prompt(missing[0])
           
       # 11. Show summary once everything is complete
       if not session.get("awaiting_confirmation"):
           return build_summary(session)
           
        # 12. Silent fallback (optional — or return nothing)
        return "No entendi bien. Puedes repetir o decirme que necesitas?"

# =============================================================================
# TWILIO WEBHOOK
# =============================================================================

@app.post("/whatsapp")
async def whatsapp_webhook(request: Request, WaId: str = Form(...), Body: str = Form(...)):
    """Handle incoming WhatsApp messages"""
    
    phone = WaId.split(":")[-1].strip()
    user_msg = Body.strip()
    
    # Get session and process message
    session = get_session(phone)
    response_text = process_message(user_msg, session)
    
    # Test mode - return plain text
    if TEST_MODE:
        return Response(content=response_text, media_type="text/plain")
    
    # Production mode - return Twilio XML
    if TWILIO_AVAILABLE:
        twiml = MessagingResponse()
        twiml.message(response_text)
        return Response(content=str(twiml), media_type="application/xml")
    
    # Fallback - plain text
    return Response(content=response_text, media_type="text/plain")

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
