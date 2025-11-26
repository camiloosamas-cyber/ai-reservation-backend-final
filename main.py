from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware


from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json, os, re
import dateparser

# ---------- Supabase ----------
from supabase import create_client, Client

# ---------- OpenAI ----------
from openai import OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ---------- Twilio ----------
from twilio.twiml.messaging_response import MessagingResponse

# ---------------------------------------------------------
# INIT APP
# ---------------------------------------------------------
app = FastAPI()
os.chdir(os.path.dirname(os.path.abspath(__file__)))

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

LOCAL_TZ = ZoneInfo("America/Bogota")

# ---------------------------------------------------------
# SUPABASE
# ---------------------------------------------------------
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE")
)

TABLE_LIMIT = 10

def assign_table(iso_local: str):
    booked = supabase.table("reservations").select("table_number").eq("datetime", iso_local).execute()
    taken = {r["table_number"] for r in (booked.data or [])}

    for i in range(1, TABLE_LIMIT + 1):
        t = f"T{i}"
        if t not in taken:
            return t
    return None
    
# ---------------------------------------------------------
# SAVE RESERVATION
# ---------------------------------------------------------
def save_reservation(data: dict):
    try:
        raw_dt = datetime.fromisoformat(data["datetime"])
        if raw_dt.tzinfo is None:
            dt_local = raw_dt.replace(tzinfo=LOCAL_TZ)
        else:
            dt_local = raw_dt.astimezone(LOCAL_TZ)

        iso_to_store = dt_local.isoformat()

    except:
        return "❌ Error procesando la fecha."

    # table
    if data.get("table_number"):
        table = data["table_number"]
    else:
        table = assign_table(iso_to_store)

    if not table:
        return "❌ No hay mesas disponibles para ese horario."

    # Insert
    supabase.table("reservations").insert({
        "customer_name": data["customer_name"],
        "customer_email": "",
        "contact_phone": "",
        "datetime": iso_to_store,
        "party_size": int(data["party_size"]),
        "table_number": table,
        "notes": "",
        "status": "confirmado",
        "business_id": 2,  # ALWAYS IPS ID
        "package": data.get("package", ""),
        "school_name": data.get("school_name", ""),
    }).execute()

    return (
        "✅ *¡Reserva confirmada!*\n"
        f"👤 {data['customer_name']}\n"
        f"👥 {data['party_size']} estudiantes\n"
        f"📦 {data.get('package','')}\n"
        f"🏫 {data.get('school_name','')}\n"
        f"🗓 {dt_local.strftime('%Y-%m-%d %H:%M')}"
    )


# ---------------------------------------------------------
# AI EXTRACTION  (PROMPT UPDATED EXACTLY AS REQUESTED)
# ---------------------------------------------------------
def ai_extract(user_msg: str):
    import dateparser

    text = user_msg.lower().strip()

    # -------------------------
    # PACKAGE
    # -------------------------
    detected_package = detect_package(text)

    # -------------------------
    # SCHOOL DETECTION
    # -------------------------
    school_name = ""
    school_patterns = [
        r"(colegio [a-zA-Záéíóúñ ]+)",
        r"(gimnasio [a-zA-Záéíóúñ ]+)",
        r"(liceo [a-zA-Záéíóúñ ]+)",
        r"(instituto [a-zA-Záéíóúñ ]+)",
    ]
    for p in school_patterns:
        m = re.search(p, text)
        if m:
            school_name = m.group(1).strip()
            break

    # -------------------------
    # NAME DETECTION (FIXED)
    # -------------------------
    customer_name = ""

    name_patterns = [
        r"se llama ([a-zA-Záéíóúñ ]+)",
        r"mi hijo ([a-zA-Záéíóúñ ]+)",
        r"nombre es ([a-zA-Záéíóúñ ]+)",
    ]

    # 1) Structured name detection
    for p in name_patterns:
        m = re.search(p, text)
        if m:
            candidate = m.group(1).strip()
            customer_name = " ".join(candidate.split()[:3])
            break

    # 2) FALLBACK NAME DETECTION (DO NOT CONFUSE WITH SCHOOL NAMES)
    if not customer_name:
        package_words = [
            "esencial", "activa", "total", "bienestar", "cuidado", "salud",
            "paquete", "kit", "45", "60", "75"
        ]

        school_words = ["colegio", "gimnasio", "liceo", "instituto", "school"]

        is_just_text = re.fullmatch(r"[a-zA-Záéíóúñ ]{2,30}", text)
        is_short = len(text.split()) <= 3
        contains_package_word = any(w in text for w in package_words)
        contains_school_word = any(w in text for w in school_words)

        if is_just_text and is_short and not contains_package_word and not contains_school_word:
            ignored = ["hola", "ola", "buenas", "buenos dias", "buen día"]
            if text not in ignored:
                customer_name = " ".join(text.split()[:3])

    # -------------------------
    # PARTY SIZE
    # -------------------------
    party_size = ""
    m = re.search(r"(\d+)\s*(estudiantes|alumnos|niños|personas)", text)
    if m:
        party_size = m.group(1)

    # -------------------------
    # DATE/TIME — LLM extraction
    # -------------------------
    prompt = f"""
Extrae SOLO la fecha y hora del siguiente mensaje.
Devuélvelo exactamente así:

{{
"datetime": "texto exacto de fecha y hora"
}}

No inventes nada.

Mensaje:
\"\"\"{user_msg}\"\"\"
"""

    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[{"role": "system", "content": prompt}]
        )
        result = json.loads(r.choices[0].message.content)
        dt_text = result.get("datetime", "").strip()
    except:
        dt_text = ""

    dt_local = dateparser.parse(
        dt_text,
        settings={
            "PREFER_DATES_FROM": "future",
            "TIMEZONE": "America/Bogota",
            "RETURN_AS_TIMEZONE_AWARE": True
        }
    )

    final_iso = dt_local.isoformat() if dt_local else ""

    # -------------------------
    # INTENT
    # -------------------------
    reserve_keywords = ["agendar", "reservar", "cita", "examen"]
    info_keywords = ["cuánto", "precio", "vale", "incluye"]

    if any(k in text for k in reserve_keywords):
        intent = "reserve"
    elif any(k in text for k in info_keywords):
        intent = "info"
    else:
        intent = "other"

    # -------------------------
    # RETURN
    # -------------------------
    return {
        "intent": intent,
        "customer_name": customer_name,
        "school_name": school_name,
        "datetime": final_iso,
        "party_size": party_size,
        "package": detected_package,
    }

# ======================================================================
#                      CHATBOT ENGINE (SKELETON)
# ======================================================================

# Session storage (in-memory, per phone number)
session_state = {}

def get_session(phone):
    if phone not in session_state:
        session_state[phone] = {
           "phone": phone,
           "student_name": None,
           "school": None,
           "package": None,
           "date": None,
           "time": None,
           "booking_started": False,
           "info_mode": False,          # true = user is only asking questions
           "first_booking_message": False,
           "greeted": False,
        }

    return session_state[phone]

# ----------------------------------------------------------------------
# INTENT MAP (Will be filled in PART 4)
# ----------------------------------------------------------------------
INTENTS = {
    "greeting": {
        "patterns": [],  # will fill later
        "handler": "handle_greeting"
    },
    "package_info": {
        "patterns": [],
        "handler": "handle_package_info"
    },
    "booking_request": {
        "patterns": [],
        "handler": "handle_booking_request"
    },
    "modify": {
        "patterns": [],
        "handler": "handle_modify"
    },
    "cancel": {
        "patterns": [],
        "handler": "handle_cancel"
    },
    "confirmation": {
        "patterns": [],
        "handler": "handle_confirmation"
    }
}

# ----------------------------------------------------------------------
# Intent Detection
# ----------------------------------------------------------------------
INTENT_PRIORITY = [
    "booking_request",
    "modify",
    "cancel",
    "package_info",
    "confirmation",
    "greeting",
]

def detect_intent(msg):
    msg = msg.lower()
    for intent in INTENT_PRIORITY:
        data = INTENTS[intent]
        for p in data["patterns"]:
            if p in msg:
                return intent
    return None  # Silence fallback


# ----------------------------------------------------------------------
# Handler: Greeting (INFO MODE)
# ----------------------------------------------------------------------
def handle_greeting(msg, session):

    if not session["greeted"]:
        session["greeted"] = True
        return "Hola, claro que sí. ¿En qué te puedo ayudar?"

    # Already greeted → respond softly without "Hola"
    return "Claro que sí, ¿en qué te puedo ayudar?"

# ----------------------------------------------------------------------
# Handler: Package Info (INFO MODE)
# ----------------------------------------------------------------------
def handle_package_info(msg, session):

    session["info_mode"] = True  # Still in info mode

    pkg = detect_package(msg)

    # If they clearly mention a specific package → give price of that one
    if pkg == "Cuidado Esencial":
        price = "45.000"
    elif pkg == "Salud Activa":
        price = "60.000"
    elif pkg == "Bienestar Total":
        price = "75.000"
    else:
        # General info question → list all packages with bullets
        return (
            "Claro. Ofrecemos tres paquetes:\n\n"
            "• Cuidado Esencial — $45.000\n"
            "• Salud Activa — $60.000\n"
            "• Bienestar Total — $75.000\n\n"
            "¿Cuál te interesa?"
        )

    # If they asked about a specific one
    return (
        f"Claro. El paquete {pkg} cuesta ${price}. "
        "Si deseas, puedo ayudarte a agendar una cita. ¿Te gustaría hacerlo?"
    )
    
# ----------------------------------------------------------------------
# Handler: Booking Request (BEGIN BOOKING MODE)
# ----------------------------------------------------------------------
def handle_booking_request(msg, session):

    # Switch to booking mode
    session["booking_started"] = True
    session["info_mode"] = False

    # Extract info BEFORE checking missing fields
    update_session_with_info(msg, session)

    # If they already gave everything in one sentence, go straight to summary
    if session["student_name"] and session["school"] and session["package"] and session["date"] and session["time"]:
        return finish_booking(session)

    # Otherwise, ask for the fields (without repeating "Hola")
    return (
        "Por supuesto. Para agendar la cita necesito los siguientes datos:\n\n"
        "– Nombre del estudiante\n"
        "– Colegio\n"
        "– Paquete\n"
        "– Fecha en que deseas la cita\n"
        "– Hora\n\n"
        "¿Me los puedes compartir?"
    )

# ----------------------------------------------------------------------
# Handler: Modify (one-line)
# ----------------------------------------------------------------------
def handle_modify(msg, session):
    return "Claro, ¿cuál sería la nueva fecha y hora que deseas?"

# ----------------------------------------------------------------------
# Handler: Cancel
# ----------------------------------------------------------------------
def handle_cancel(msg, session):
    return "Perfecto, ¿confirmas que deseas cancelar la cita?"

# ----------------------------------------------------------------------
# Handler: Confirmation (final)
# ----------------------------------------------------------------------
def handle_confirmation(msg, session):

    required = [
        session["student_name"],
        session["school"],
        session["package"],
        session["date"],
        session["time"]
    ]

    # Not all data collected yet
    if not all(required):
        return "Perfecto, ¿me confirmas algo más?"

    # Build datetime to ISO for saving
    try:
        dt_text = f"{session['date']} {session['time']}"
        dt = dateparser.parse(dt_text, languages=["es"], settings={"TIMEZONE": "America/Bogota"})
        iso = dt.isoformat()
    except:
        return "Hubo un error procesando la fecha/hora."

    # SAVE INTO SUPABASE
    save_reservation({
        "customer_name": session["student_name"],
        "package": session["package"],
        "school_name": session["school"],
        "datetime": iso,
        "party_size": 1,
        "table_number": None
    })

    # Clear session
    phone = session["phone"]
    session_state.pop(phone, None)

    return (
        f"¡Perfecto! La cita de {session['student_name']} quedó confirmada "
        f"en el {session['school']}, paquete {session['package']}, "
        f"el día {session['date']} a las {session['time']}."
    )
    
# ----------------------------------------------------------------------
# MAIN CHATBOT ENGINE
# ----------------------------------------------------------------------
def process_message(msg, session):

    # Always update student/school/package/date/time
    update_session_with_info(msg, session)

    # If already in booking mode → skip intent detection
    if session["booking_started"]:
        return continue_booking_process(msg, session)

    # Not booking yet → detect intent
    intent = detect_intent(msg)

    # If user said "sí", "claro", "dale", etc after price info → switch to booking
    if intent == "confirmation" and session["info_mode"] and not session["booking_started"]:
        intent = "booking_request"

    # If user says a confirmation word AND we already sent the summary → confirm

    if session["booking_started"] and is_confirmation_message(msg):
        return handle_confirmation(msg, session)

    # Silence fallback
    if intent is None:
        return ""

    handler_name = INTENTS[intent]["handler"]
    handler = globals()[handler_name]

    # Mark that next message is no longer first-booking-message
    session["first_booking_message"] = False

    return handler(msg, session)


# ======================================================================
#                        EXTRACTORS (PART 2)
# ======================================================================

import re
import dateparser

# --------------------------------------------------------------
# STUDENT NAME EXTRACTOR
# --------------------------------------------------------------
def extract_student_name(msg):
    msg = msg.strip()

    banned = ["si", "sí", "ok", "dale", "claro", "perfecto", "bueno", "listo"]
    if msg.lower() in banned:
        return None

    patterns = [
        r"es para ([a-zA-Záéíóúñ ]+)",
        r"para ([a-zA-Záéíóúñ ]+)",
        r"mi hijo ([a-zA-Záéíóúñ ]+)",
        r"mi hija ([a-zA-Záéíóúñ ]+)",
        r"nombre es ([a-zA-Záéíóúñ ]+)",
    ]

    for p in patterns:
        m = re.search(p, msg, re.IGNORECASE)
        if m:
            return m.group(1).strip().title()

    # Only accept standalone names (1–4 words)
    if len(msg.split()) <= 4 and all(c.isalpha() or c.isspace() for c in msg):
        return msg.title()

    return None

# --------------------------------------------------------------
# SCHOOL EXTRACTOR
# --------------------------------------------------------------
def extract_school(msg):
    msg_clean = msg.lower()

    patterns = [
        r"del colegio ([a-zA-Záéíóúñ ]+)",
        r"de colegio ([a-zA-Záéíóúñ ]+)",
        r"del col ([a-zA-Záéíóúñ ]+)",
        r"colegio ([a-zA-Záéíóúñ ]+)",
        r"gimnasio ([a-zA-Záéíóúñ ]+)",
        r"liceo ([a-zA-Záéíóúñ ]+)",
        r"instituto ([a-zA-Záéíóúñ ]+)",
    ]

    for p in patterns:
        m = re.search(p, msg_clean)
        if m:
            return m.group(1).strip().title()

    return None

# --------------------------------------------------------------
# PACKAGE DETECTOR
# --------------------------------------------------------------
def detect_package(msg):
    msg = msg.lower()

    # Package rules
    esencial_words = ["esencial", "verde", "45k", "45 mil", "kit escolar"]
    activa_words   = ["activa", "psico", "psicologia", "60k", "60 mil", "azul"]
    bienestar_words = ["bienestar", "total", "completo", "odont", "75k", "75 mil", "amarillo"]

    if any(w in msg for w in esencial_words):
        return "Cuidado Esencial"

    if any(w in msg for w in activa_words):
        return "Salud Activa"

    if any(w in msg for w in bienestar_words):
        return "Bienestar Total"

    return None


# --------------------------------------------------------------
# DATE EXTRACTOR (USING dateparser)
# --------------------------------------------------------------
def extract_date(msg):
    msg = msg.lower()

    patterns = [
        # “este viernes”, “para este viernes”, “sería este viernes”
        r"((?:para|el|es|seria|sería|este|para este|para el)?\s*este\s+(lunes|martes|miercoles|miércoles|jueves|viernes|sabado|sábado|domingo))",

        # single weekday: “viernes”, “jueves”
        r"((lunes|martes|miercoles|miércoles|jueves|viernes|sabado|sábado|domingo))",

        # mañana / pasado mañana
        r"(mañana)",
        r"(pasado mañana)",

        # explicit date
        r"(el\s+\d{1,2}\s+de\s+[a-záéíóú]+)",
        r"(\d{1,2}/\d{1,2}/\d{2,4})",
        r"(\d{4}-\d{2}-\d{2})"
    ]

    for p in patterns:
        m = re.search(p, msg)
        if m:
            phrase = m.group(1)
            dt = dateparser.parse(
                phrase,
                languages=["es"],
                settings={
                    "PREFER_DATES_FROM": "future",
                    "TIMEZONE": "America/Bogota"
                }
            )
            if dt:
                return dt.strftime("%Y-%m-%d")

    # fallback
    dt = dateparser.parse(
        msg,
        languages=["es"],
        settings={
            "PREFER_DATES_FROM": "future",
            "TIMEZONE": "America/Bogota"
        }
    )
    if dt:
        return dt.strftime("%Y-%m-%d")

    return None

    
# --------------------------------------------------------------
# TIME EXTRACTOR
# --------------------------------------------------------------
def extract_time(msg):
    msg = msg.lower()

    # Capture "3 pm", "3:00 pm", "15:00", "3pm", etc
    patterns = [
        r"(\d{1,2}\s*pm)",
        r"(\d{1,2}\s*am)",
        r"(\d{1,2}:\d{2}\s*pm)",
        r"(\d{1,2}:\d{2}\s*am)",
        r"(\d{1,2}:\d{2})"
    ]

    for p in patterns:
        m = re.search(p, msg)
        if m:
            phrase = m.group(1)
            dt = dateparser.parse(phrase, languages=["es"], settings={
                "PREFER_DATES_FROM": "future",
                "TIMEZONE": "America/Bogota"
            })
            if dt:
                return dt.strftime("%H:%M")

    # fallback
    dt = dateparser.parse(msg, languages=["es"], settings={
        "PREFER_DATES_FROM": "future",
        "TIMEZONE": "America/Bogota"
    })
    if dt:
        return dt.strftime("%H:%M")

    return None
    
# ======================================================================
#                    BOOKING LOGIC (PART 3)
# ======================================================================

def update_session_with_info(msg, session):
    """
    Update session with ANY information the user provided.
    This runs EVERY TIME the user sends a message.
    """

    # Extract student name
    if session["student_name"] is None:
        name = extract_student_name(msg)
        if name:
            session["student_name"] = name

    # Extract school
    if session["school"] is None:
        school = extract_school(msg)
        if school:
            session["school"] = school

    # Detect package
    if session["package"] is None:
        pkg = detect_package(msg)
        if pkg:
            session["package"] = pkg

    # Extract date
    if session["date"] is None:
        date = extract_date(msg)
        if date:
            session["date"] = date

    # Extract time
    if session["time"] is None:
        time = extract_time(msg)
        if time:
            session["time"] = time


def build_missing_fields_message(session):
    """
    For the FOLLOW-UP messages — clean, one-line, natural.
    """
    missing = []

    if not session["student_name"]:
        missing.append("el nombre del estudiante")
    if not session["school"]:
        missing.append("el colegio")
    if not session["package"]:
        missing.append("el paquete")
    if not session["date"]:
        missing.append("la fecha en que deseas la cita")
    if not session["time"]:
        missing.append("la hora")

    if len(missing) == 0:
        return None

    # Build human one-line message based on missing count
    if len(missing) == 1:
        return f"Listo, solo me falta {missing[0]}. ¿Me lo compartes?"

    if len(missing) == 2:
        return f"Perfecto, me falta {missing[0]} y {missing[1]}. ¿Me los compartes?"

    # 3-5 missing
    joined = ", ".join(missing[:-1]) + " y " + missing[-1]
    return f"Perfecto, me falta {joined}. ¿Me los compartes?"


def finish_booking(session):
    """
    Build the final confirmation message once we have everything.
    """

    name = session["student_name"]
    school = session["school"]
    pkg = session["package"]
    date = session["date"]
    time = session["time"]

    return (
        f"Listo, tu cita quedó agendada para {name} en el {school}, "
        f"paquete {pkg}, el día {date} a las {time}. ¿Deseas confirmar?"
    )

def is_confirmation_message(msg: str) -> bool:
    """
    Check if the user message looks like a confirmation (sí, dale, deseo confirmar, etc.)
    using the same patterns defined in INTENTS["confirmation"]["patterns"].
    """
    text = msg.lower().strip()
    for p in INTENTS["confirmation"]["patterns"]:
        if p in text:
            return True
    return False

def continue_booking_process(msg, session):
    """
    This is called whenever booking has ALREADY started
    and it's NOT the first booking message.
    """

    # STEP 1 — extract any info from user's message
    update_session_with_info(msg, session)

    # STEP 2 — if something is still missing, ask only for that
    missing_message = build_missing_fields_message(session)
    if missing_message:
        return missing_message

    # STEP 3 — we already have ALL fields (name, school, package, date, time)

    # If the user is now confirming ("sí", "deseo confirmar", etc.)
    if is_confirmation_message(msg):
        return handle_confirmation(msg, session)

    # If they haven't explicitly confirmed yet, show the summary and ask
    return finish_booking(session)


# ======================================================================
#                       PART 4A — INTENT PATTERNS
# ======================================================================

INTENTS["greeting"]["patterns"] = [
    # Basic greetings
    "hola", "holaa", "holaaa", "buenas", "buenos dias", "buenos días",
    "buen dia", "buen día", "buenas tardes", "buenas noches",

    # Informal / slang
    "ola", "holi", "holis", "hello", "alo", "aló", "alo?", "que mas",
    "qué más", "q mas", "que tal", "que hubo", "qué hubo", "k hubo",

    # Polite openings
    "disculpa", "una pregunta", "consulta", "hola una pregunta",
    "buen dia una pregunta", "buenos dias una consulta", "quisiera saber",

    # Soft info openers
    "vi un poster", "vi un anuncio", "vi su aviso", "vi la publicidad",
    "tienen info", "informacion", "información", "quisiera informacion",
    "quisiera información"
]


INTENTS["package_info"]["patterns"] = [
    # Asking for price
    "cuanto vale", "cuánto vale", "cuanto cuesta", "cuánto cuesta",
    "precio", "precio del paquete", "valor del paquete",
    "cuanto es el de", "cuánto es el de",

    # Direct package references
    "paquete", "kit escolar", "el de psicologia", "el de psicología",
    "el de psico", "trae psicologia", "trae psicología",
    "incluye psicologia", "incluye psicología",

    # Colors mapped to packages
    "el verde", "verde", "de color verde",
    "el azul", "azul", "de color azul",
    "el amarillo", "amarillo", "de color amarillo",

    # Prices as references
    "45k", "45 mil", "45mil",
    "60k", "60 mil", "60mil",
    "75k", "75 mil", "75mil",

    # Keywords for each package
    "esencial", "salud activa", "activa",
    "bienestar total", "bienestar", "total", "completo",

    # General info questions
    "que paquetes tienen", "qué paquetes tienen",
    "ofrecen paquetes", "tienen paquetes",
    "como funcionan los paquetes", "examenes escolares",
    "exámenes escolares", "paquetes escolares",
]
# ======================================================================
#                  PART 4B — BOOKING / MODIFY / CANCEL
# ======================================================================

# ------------------------------------------------------------
# BOOKING REQUEST PATTERNS
# ------------------------------------------------------------
INTENTS["booking_request"]["patterns"] = [

    # Direct booking phrases
    "quiero reservar",
    "quiero una cita",
    "quiero agendar",
    "quiero sacar una cita",
    "quiero sacar cita",
    "quiero hacer una cita",
    "quiero hacer cita",
    "necesito una cita",
    "necesito agendar",
    "necesito reservar",
    "necesito sacar cita",

    # More explicit
    "quiero reservar el paquete",
    "quiero reservar el de",
    "quiero agendar una cita para",
    "quiero agendar cita para",
    "quiero reservar para mi hijo",
    "quiero reservar para mi hija",

    # When users say they want the exam
    "quiero el examen",
    "quiero hacer el examen",
    "quiero hacer el examen escolar",
    "quiero hacer los exámenes",
    "quiero hacer el examen del colegio",

    # Indirect booking intent
    "me pueden reservar",
    "me puedes reservar",
    "me pueden agendar",
    "me puedes agendar",
    "me ayudan a reservar",
    "me ayudas a reservar",
    "me ayudas a agendar",

    # Misspellings / shortcuts
    "agendar cita",
    "agendar una cita",
    "reservar examen",
    "reservar el examen",
    "reservar examen escolar",
    "reservar paquete",
    "agendar paquete",

    # Colombian slang
    "quiero pedir la cita",
    "quiero pedir cita",
    "quiero separar cita",
    "quiero separar el examen",

    # For parents
    "quiero una cita para mi hijo",
    "quiero una cita para mi hija",
    "necesito cita para mi hijo",
    "necesito cita para mi hija",
]


# ------------------------------------------------------------
# MODIFY PATTERNS
# ------------------------------------------------------------
INTENTS["modify"]["patterns"] = [

    # Direct change
    "cambiar cita",
    "cambiar la cita",
    "quiero cambiar la cita",
    "necesito cambiar la cita",
    "puedo cambiar la cita",

    # Time change
    "cambiar hora",
    "cambiar la hora",
    "quiero otra hora",
    "me sirve otra hora",
    "puedo mover la hora",

    # Date change
    "cambiar fecha",
    "cambiar la fecha",
    "quiero otra fecha",
    "mover fecha",
    "puedo mover la fecha",

    # Combined
    "quiero mover la cita",
    "necesito mover la cita",
    "quiero reagendar",
    "quiero re agendar",
    "quiero re-agendar",
    "reagendar cita",
    "mover cita",

    # Common errors and slang
    "reagendarr", "reagenda", "re agendar cita",
    "otra hora", "otra fecha",
]


# ------------------------------------------------------------
# CANCEL PATTERNS
# ------------------------------------------------------------
INTENTS["cancel"]["patterns"] = [

    # Direct cancellation
    "cancelar",
    "cancelar cita",
    "cancelar la cita",
    "quiero cancelar",
    "quiero cancelar la cita",

    # Parents cancel for kids
    "quiero cancelar la cita de mi hijo",
    "quiero cancelar la cita de mi hija",

    # Variations
    "anular", "anular cita", "anular la cita",
    "inhabilitar cita",
    "quitar la cita",

    # More formal
    "me gustaría cancelar",
    "necesito cancelar",
    "me ayudas a cancelar",

    # Typos and slang
    "cancelarr", "cancelala", "cancela la", "cancela eso",
    "ya no quiero la cita",
]
# ======================================================================
#                  PART 4C — CONFIRMATION PATTERNS
# ======================================================================

INTENTS["confirmation"]["patterns"] = [

    # Strong confirmations
    "confirmo",
    "sí confirmo",
    "si confirmo",
    "confirmar",
    "confirmada",
    "confirmado",

    # Simple yes
    "si", "sí", "ok", "dale", "listo", "perfecto",
    "super", "claro", "de una", "por supuesto",

    # Longer confirmations
    "si está bien",
    "sí está bien",
    "si esta bien",
    "sí esta bien",
    "está bien",
    "esta bien",
    "si claro",
    "sí claro",
    "si dale",
    "sí dale",
    "si por favor",
    "sí por favor",
    "si gracias",
    "sí gracias",

    # Parents confirming for kids
    "si es para mi hijo",
    "si es para mi hija",
    "si está correcto",
    "sí está correcto",

    # WhatsApp common quick replies
    "ok listo",
    "okay",
    "okk",
    "okey",
    "gracias si",
    "si gracias",
    "si señora",
    "si señor",
    "ah bueno",
    "perfecto gracias",
]

@app.post("/whatsapp")
async def whatsapp_reply(request: Request):
    form = await request.form()
    incoming_msg = form.get("Body", "").strip()
    phone = form.get("From", "").replace("whatsapp:", "").strip()

    # Get or create session for this phone
    session = get_session(phone)

    # MAIN LOGIC → pass message + session to chatbot engine
    response_text = process_message(incoming_msg, session)

    # If empty → silence (your rule)
    if not response_text:
        return Response(status_code=204)

    # Respond through Twilio
    twilio_resp = MessagingResponse()
    twilio_resp.message(response_text)

    return Response(content=str(twilio_resp), media_type="application/xml")

# ---------------------------------------------------------
# DASHBOARD (BOGOTÁ)
# ---------------------------------------------------------
from dateutil import parser

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):

    res = supabase.table("reservations").select("*").order("datetime", desc=True).execute()

    rows = res.data or []

    fixed = []
    weekly_count = 0

    now = datetime.now(LOCAL_TZ)
    week_ago = now - timedelta(days=7)

    for r in rows:
        iso = r.get("datetime")
        row = r.copy()

        if iso:
            dt = parser.isoparse(iso).astimezone(LOCAL_TZ)
            row["date"] = dt.strftime("%Y-%m-%d")
            row["time"] = dt.strftime("%H:%M")

            if dt >= week_ago:
                weekly_count += 1
        else:
            row["date"] = "-"
            row["time"] = "-"

        fixed.append(row)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "reservations": fixed,
        "weekly_count": weekly_count
    })


# ---------------------------------------------------------
# UPDATE / ACTIONS
# ---------------------------------------------------------
@app.post("/updateReservation")
async def update_reservation(update: dict):
    rid = update.get("reservation_id")
    if not rid:
        return {"success": False}

    fields = {k: v for k, v in update.items() if k != "reservation_id" and v not in ["", None, "-", "null"]}

    if fields:
        supabase.table("reservations").update(fields).eq("reservation_id", rid).execute()

    return {"success": True}


@app.post("/cancelReservation")
async def cancel_reservation(update: dict):
    supabase.table("reservations").update({"status": "cancelled"}).eq("reservation_id", update["reservation_id"]).execute()
    return {"success": True}

@app.post("/archiveReservation")
async def archive_reservation(update: dict):
    supabase.table("reservations").update({"status": "archived"}).eq("reservation_id", update["reservation_id"]).execute()
    return {"success":True}

@app.post("/markArrived")
async def mark_arrived(update: dict):
    supabase.table("reservations").update({"status": "arrived"}).eq("reservation_id", update["reservation_id"]).execute()
    return {"success": True}

@app.post("/markNoShow")
async def mark_no_show(update: dict):
    supabase.table("reservations").update({"status": "no_show"}).eq("reservation_id", update["reservation_id"]).execute()
    return {"success": True}

@app.post("/createReservation")
async def create_reservation(data: dict):
    result = save_reservation({
        "customer_name": data.get("customer_name",""),
        "customer_email": data.get("customer_email",""),
        "contact_phone": data.get("contact_phone",""),
        "datetime": data.get("datetime",""),
        "party_size": data.get("party_size",1),
        "school_name": data.get("school_name",""),
        "package": data.get("package",""),
        "table_number": None
    })
    return {"success": True}


# ---------------------------------------------------------
# RUN
# ---------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))


