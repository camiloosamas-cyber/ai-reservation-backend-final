print(">>> RUNNING ORIENTAL IPS BACKEND v3.2.0 ✅")

import os
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
from twilio.twiml.messaging_response import MessagingResponse

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

os.chdir(os.path.dirname(os.path.abspath(__file__)))

TEST_MODE = os.getenv("TEST_MODE") == "1"

app = FastAPI(title="Oriental IPS Bot", version="3.2.0")
print("🚀 Oriental IPS WhatsApp Bot v3.2.0 - Production Ready")

# Timezone
try:
    LOCAL_TZ = ZoneInfo("America/Bogota")
except ZoneInfoNotFoundError:
    LOCAL_TZ = ZoneInfo("UTC")

# Static files and templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------
# EXTERNAL SERVICES
# ---------------------------------------------------------

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE = os.getenv("SUPABASE_SERVICE_ROLE")

supabase = None

try:
    if SUPABASE_URL and SUPABASE_SERVICE_ROLE:
        supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE)
    else:
        print("WARNING: Missing Supabase credentials")
except Exception as e:
    print(f"ERROR loading Supabase: {e}")

# Business config
RESERVATION_TABLE = "reservations"
SESSION_TABLE = "sessions"
BUSINESS_ID = 2
TABLE_LIMIT = 10

# ---------------------------------------------------------
# SESSION MANAGEMENT
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
    "greeted": False,
    "awaiting_confirmation": False
}

def get_session(phone):
    """Retrieve or create session"""
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

        if not response or not response.data or not response.data.get("data"):
            s = DEFAULT_SESSION.copy()
            s["phone"] = phone
            return s

        stored = response.data["data"]
        stored["phone"] = phone
        return {**DEFAULT_SESSION, **stored}

    except Exception as e:
        print(f"Error getting session: {e}")
        s = DEFAULT_SESSION.copy()
        s["phone"] = phone
        return s

def save_session(session):
    """Save session to database"""
    if not supabase:
        return

    phone = session.get("phone")
    if not phone:
        return

    data = {k: v for k, v in session.items() if k != "phone"}

    try:
        supabase.table(SESSION_TABLE).upsert({
            "phone": phone,
            "data": data,
            "last_updated": datetime.now(LOCAL_TZ).isoformat()
        }).execute()
    except Exception as e:
        print(f"Error saving session: {e}")

# ---------------------------------------------------------
# PACKAGE DATA
# ---------------------------------------------------------

PACKAGES = {
    "esencial": {
        "name": "Paquete Cuidado Esencial",
        "price": "45.000",
        "description": "Medicina General, Optometria y Audiometria"
    },
    "activa": {
        "name": "Paquete Salud Activa",
        "price": "60.000",
        "description": "Esencial + Psicologia"
    },
    "bienestar": {
        "name": "Paquete Bienestar Total",
        "price": "75.000",
        "description": "Activa + Odontologia"
    }
}

# ---------------------------------------------------------
# EXTRACTION FUNCTIONS
# ---------------------------------------------------------

def extract_package(msg):
    """Extract package from message"""
    text = msg.lower()
    
    if any(k in text for k in ["esencial", "verde", "45k", "45000", "45.000"]):
        return "Paquete Cuidado Esencial"
    if any(k in text for k in ["activa", "salud activa", "azul", "psico", "60k", "60000", "60.000"]):
        return "Paquete Salud Activa"
    if any(k in text for k in ["bienestar", "total", "amarillo", "completo", "75k", "75000", "75.000"]):
        return "Paquete Bienestar Total"
    
    return None

def extract_student_name(msg, current_name):
    """Extract student name"""
    text = msg.lower().strip()
    
    # Skip if name exists and user not trying to change it
    if current_name and not any(k in text for k in ["cambiar", "otro nombre", "se llama"]):
        return None
    
    # Skip greetings
    if text in ["hola", "buenos dias", "buenas tardes", "buenas"]:
        return None
    
    # Skip package queries
    if any(k in text for k in ["paquete", "precio", "cuesta"]):
        return None
    
    # Pattern: "se llama X"
    if "se llama" in text:
        name = text.split("se llama", 1),[object Object],strip()
        if name:
            return name.title()
    
    # Pattern: "el nombre es X"
    if "nombre es" in text:
        name = text.split("nombre es", 1),[object Object],strip()
        if name:
            return name.title()
    
    # Pattern: "nombre X"
    m = re.search(r"nombre\s+([a-z\s]+)", text)
    if m:
        name = m.group(1).strip()
        if 2 <= len(name.split()) <= 4:
            return name.title()
    
    # Capitalized name detection
    m = re.search(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b", msg)
    if m:
        return m.group(1).strip()
    
    # Standalone name (2-4 words, all letters)
    words = text.split()
    if 2 <= len(words) <= 4:
        valid = [w for w in words if len(w) >= 2 and re.match(r'^[a-z]+$', w)]
        if len(valid) >= 2:
            combined = " ".join(valid)
            avoid = ["buenos", "confirmo", "paquete", "colegio", "cita"]
            if not any(k in combined for k in avoid):
                return " ".join(valid).title()
    
    return None

def extract_school(msg):
    """Extract school name"""
    text = msg.lower()
    
    patterns = [
        r"(?:colegio|gimnasio|liceo|instituto|escuela)\s+([a-z0-9\s]+)",
        r"(?:del\s+colegio|del\s+gimnasio)\s+([a-z0-9\s]+)",
    ]
    
    for p in patterns:
        m = re.search(p, text)
        if m:
            name = m.group(1).strip()
            name = re.split(r"[.,!?]", name),[object Object],strip()
            if len(name) > 1:
                return name.title()
    
    # Standalone school keywords
    if any(k in text for k in ["gimnasio", "instituto", "comfacasanare"]):
        words = msg.strip().split()
        school_words = [w for w in words if len(w) >= 3]
        if school_words:
            return " ".join(school_words).title()
    
    return None

def extract_age(msg):
    """Extract age from message"""
    text = msg.lower()
    
    # Pattern: "12 años"
    m = re.search(r"(\d{1,2})\s*anos?", text)
    if m:
        age = int(m.group(1))
        if 5 <= age <= 25:
            return age
    
    # Pattern: "edad 12"
    m = re.search(r"edad\s+(\d{1,2})", text)
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
    """Extract cedula (ID number)"""
    m = re.search(r"\b(\d{5,12})\b", msg)
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
    
    # Use dateparser
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
    
    return session.get("date")

def extract_time(msg, session):
    """Extract time from message"""
    text = msg.lower()
    
    # Pattern: 10am, 3pm, 10:30am
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        ampm = m.group(3)
        
        if ampm == "pm" and hour != 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        
        # Validate business hours (7am - 5pm)
        if 7 <= hour <= 17:
            return f"{hour:02d}:{minute:02d}"
        else:
            return "INVALID"
    
    # Pattern: "las 11"
    m = re.search(r"(?:las|a las)\s+(\d{1,2})", text)
    if m:
        hour = int(m.group(1))
        if 7 <= hour <= 17:
            return f"{hour:02d}:00"
    
    # Vague times
    if "tarde" in text:
        return "15:00"
    if "manana" in text or "mañana" in text:
        return "09:00"
    
    return session.get("time")

# ---------------------------------------------------------
# SESSION UPDATE
# ---------------------------------------------------------

def update_session(msg, session):
    """Extract all data from message and update session"""
    
    # Extract each field
    new_package = extract_package(msg)
    new_name = extract_student_name(msg, session.get("student_name"))
    new_school = extract_school(msg)
    new_age = extract_age(msg)
    new_cedula = extract_cedula(msg)
    new_date = extract_date(msg, session)
    new_time = extract_time(msg, session)
    
    # Update session
    if new_package:
        session["package"] = new_package
    if new_name:
        session["student_name"] = new_name
    if new_school:
        session["school"] = new_school
    if new_age:
        session["age"] = new_age
    if new_cedula:
        session["cedula"] = new_cedula
    if new_date:
        session["date"] = new_date
    if new_time and new_time != "INVALID":
        session["time"] = new_time
    
    save_session(session)
    
    return new_time == "INVALID"

# ---------------------------------------------------------
# MISSING FIELDS
# ---------------------------------------------------------

def get_missing_fields(session):
    """Get list of missing required fields"""
    required = ["student_name", "school", "package", "date", "time", "age", "cedula"]
    return [f for f in required if not session.get(f)]

def get_field_prompt(field):
    """Get prompt for missing field"""
    prompts = {
        "student_name": "Cual es el nombre completo del estudiante?",
        "school": "De que colegio es?",
        "age": "Que edad tiene?",
        "cedula": "Cual es el numero de cedula?",
        "package": (
            "Tenemos 3 paquetes:\n"
            "- Cuidado Esencial: $45.000\n"
            "- Salud Activa: $60.000\n"
            "- Bienestar Total: $75.000\n\n"
            "Cual deseas?"
        ),
        "date": "Para que fecha deseas la cita? (ejemplo: 15 de enero)",
        "time": "A que hora prefieres? Atendemos de 7am a 5pm (ejemplo: 10am)",
    }
    return prompts.get(field, "")

# ---------------------------------------------------------
# SUMMARY & CONFIRMATION
# ---------------------------------------------------------

def build_summary(session):
    """Build booking summary"""
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
        f"Edad: {session['age']}\n"
        f"Cedula: {session['cedula']}\n\n"
        "Deseas confirmar esta cita? (Responde Confirmo)"
    )
    
    session["awaiting_confirmation"] = True
    save_session(session)
    
    return summary

# ---------------------------------------------------------
# DATABASE OPERATIONS
# ---------------------------------------------------------

def assign_table(dt_iso):
    """Assign next available table"""
    if not supabase:
        return "T1"
    
    try:
        booked = (
            supabase.table(RESERVATION_TABLE)
            .select("table_number")
            .eq("datetime", dt_iso)
            .execute()
        )
        taken = {r["table_number"] for r in (booked.data or [])}
        
        for i in range(1, TABLE_LIMIT + 1):
            table = f"T{i}"
            if table not in taken:
                return table
        
        return None  # No tables available
        
    except Exception as e:
        print(f"Error assigning table: {e}")
        return "T1"

def insert_reservation(phone, session):
    """Insert reservation into database"""
    if not supabase:
        return True, "T1"
    
    # Build datetime
    dt = datetime.strptime(
        f"{session['date']} {session['time']}",
        "%Y-%m-%d %H:%M"
    )
    dt_local = dt.replace(tzinfo=LOCAL_TZ)
    dt_iso = dt_local.isoformat()
    
    # Assign table
    table = assign_table(dt_iso)
    if not table:
        return False, "No hay cupos disponibles"
    
    try:
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
            "cedula": session["cedula"],
        }).execute()
        
        return True, table
        
    except PostgrestAPIError as e:
        print(f"Error inserting reservation: {e}")
        return False, str(e)

# ---------------------------------------------------------
# MAIN MESSAGE HANDLER
# ---------------------------------------------------------

def process_message(msg, session):
    """Main conversation logic"""
    text = msg.strip()
    lower = text.lower()
    
    # 1. Extract data first
    invalid_time = update_session(text, session)
    
    # Handle invalid time
    if invalid_time:
        return "Lo siento, solo atendemos de 7am a 5pm. Por favor elige otra hora."
    
    # 2. Detect greeting (before booking)
    is_greeting = any(lower.startswith(g) for g in ["hola", "buenos", "buenas"])
    if is_greeting and not session.get("booking_started"):
        session["greeted"] = True
        save_session(session)
        return "Buenos dias, estas comunicado con Oriental IPS, en que te podemos ayudar?"
    
    # 3. Package info request (before booking)
    pkg = extract_package(text)
    if pkg and not session.get("booking_started"):
        pkg_key = None
        for key, data in PACKAGES.items():
            if data["name"] == pkg:
                pkg_key = key
                break
        
        if pkg_key:
            pkg_data = PACKAGES[pkg_key]
            return (
                f"Perfecto, {pkg_data['name']} cuesta ${pkg_data['price']} COP.\n"
                f"Incluye: {pkg_data['description']}\n\n"
                "Deseas agendar una cita?"
            )
    
    # 4. Start booking if any data extracted
    if any([
        session.get("student_name"),
        session.get("school"),
        session.get("package"),
        session.get("date"),
        session.get("time"),
        session.get("age"),
        session.get("cedula")
    ]):
        session["booking_started"] = True
        save_session(session)
    
    # 5. Explicit booking intent
    if any(k in lower for k in ["agendar", "cita", "reservar"]):
        session["booking_started"] = True
        save_session(session)
    
    # 6. If not booking, provide help
    if not session.get("booking_started"):
        return "Soy Oriental IPS. Puedo ayudarte a agendar una cita o consultar precios. Que necesitas?"
    
    # 7. Handle confirmation
    if any(k in lower for k in ["confirmo", "si", "confirmar"]) and session.get("awaiting_confirmation"):
        ok, table = insert_reservation(session["phone"], session)
        if ok:
            name = session["student_name"]
            pkg = session["package"]
            date = session["date"]
            time = session["time"]
            
            # Clear session
            phone = session["phone"]
            session.clear()
            session.update(DEFAULT_SESSION)
            session["phone"] = phone
            save_session(session)
            
            return (
                f"Cita confirmada!\n\n"
                f"El estudiante {name} tiene su cita para el paquete {pkg}.\n"
                f"Fecha: {date} a las {time}.\n"
                f"Te atenderemos en la mesa {table}.\n\n"
                "Te esperamos!"
            )
        else:
            return f"No pudimos completar la reserva: {table}"
    
    # 8. Check for missing fields
    missing = get_missing_fields(session)
    if missing:
        return get_field_prompt(missing,[object Object],)
    
    # 9. All fields complete - show summary
    if not session.get("awaiting_confirmation"):
        return build_summary(session)
    
    # 10. Fallback
    return "No entendi bien. Deseas agendar una cita o consultar precios?"

# ---------------------------------------------------------
# TWILIO WEBHOOK
# ---------------------------------------------------------

@app.post("/whatsapp", response_class=Response)
async def whatsapp_webhook(request: Request, WaId: str = Form(...), Body: str = Form(...)):
    phone = WaId.split(":")[-1].strip()
    user_msg = Body.strip()
    
    session = get_session(phone)
    response_text = process_message(user_msg, session)
    
    # Test mode - return plain text
    if TEST_MODE:
        return Response(content=response_text, media_type="text/plain")
    
    # Production - return Twilio XML
    twiml = MessagingResponse()
    twiml.message(response_text)
    return Response(content=str(twiml), media_type="application/xml")

# ---------------------------------------------------------
# WEB INTERFACE
# ---------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "version": app.version,
            "supabase_status": "Connected" if supabase else "Disconnected",
            "local_tz": str(LOCAL_TZ),
        },
    )

@app.get("/api/reservations")
async def get_reservations():
    if not supabase:
        return {"error": "Supabase not available"}
    
    now = datetime.now(LOCAL_TZ)
    seven_days = now + timedelta(days=7)
    
    try:
        response = (
            supabase.table(RESERVATION_TABLE)
            .select("*")
            .eq("business_id", BUSINESS_ID)
            .gte("datetime", now.isoformat())
            .lt("datetime", seven_days.isoformat())
            .order("datetime", desc=False)
            .execute()
        )
        return response.data
    except Exception as e:
        return {"error": str(e)}

@app.get("/health")
def health():
    return {
        "status": "healthy",
        "version": "3.2.0",
        "supabase": supabase is not None,
    }
