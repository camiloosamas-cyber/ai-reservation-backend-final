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

    except Exception:
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
# PACKAGE DETECTOR (from backup, returns clean names)
# ---------------------------------------------------------
def detect_package(msg: str):
    msg = msg.lower().strip()

    # Direct names
    if "cuidado esencial" in msg or "esencial" in msg or "kit escolar" in msg:
        return "Paquete Cuidado Esencial"

    if "salud activa" in msg or "activa" in msg:
        return "Paquete Salud Activa"

    if "bienestar total" in msg or "total" in msg or "completo" in msg:
        return "Paquete Bienestar Total"

    # Price-based
    if "45" in msg or "45k" in msg or "45 mil" in msg or "45mil" in msg:
        return "Paquete Cuidado Esencial"

    if "60" in msg or "60k" in msg or "60 mil" in msg or "60mil" in msg:
        return "Paquete Salud Activa"

    if "75" in msg or "75k" in msg or "75 mil" in msg or "75mil" in msg:
        return "Paquete Bienestar Total"

    # Exam-based
    if "odont" in msg:
        return "Paquete Bienestar Total"

    if "psico" in msg:
        return "Paquete Salud Activa"

    if "audio" in msg or "optometr" in msg or "medicina" in msg:
        return "Paquete Cuidado Esencial"

    # Color-based
    if "verde" in msg:
        return "Paquete Cuidado Esencial"

    if "azul" in msg:
        return "Paquete Salud Activa"

    if "amarillo" in msg:
        return "Paquete Bienestar Total"

    return None


# ---------------------------------------------------------
# AI EXTRACTION (from backup, with dateparser)
# ---------------------------------------------------------
def ai_extract(user_msg: str):
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
    # NAME DETECTION
    # -------------------------
    customer_name = ""

    name_patterns = [
        r"el estudiante se llama ([a-zA-Záéíóúñ ]+)",
        r"el estudiante es ([a-zA-Záéíóúñ ]+)",
        r"es para ([a-zA-Záéíóúñ ]+)",
        r"mi hijo ([a-zA-Záéíóúñ ]+)",
        r"mi hija ([a-zA-Záéíóúñ ]+)",
        r"nombre es ([a-zA-Záéíóúñ ]+)",
        r"se llama ([a-zA-Záéíóúñ ]+)",
    ]

    def clean_candidate(raw: str):
        cand = raw.strip()

        # Stop at school keywords
        cand = re.split(r"\b(colegio|gimnasio|liceo|instituto)\b", cand)[0].strip()

        # Remove date/time contamination
        cand = re.sub(
            r"\b(mañana|hoy|tarde|noche|pasado mañana|a las|a la)\b.*",
            "",
            cand
        ).strip()

        # Remove prepositions
        cand = re.sub(r"\b(de|del|la|el|los|las)$", "", cand).strip()
        cand = re.sub(r"\b(a|al|para)$", "", cand).strip()

        # Remove digits
        if any(ch.isdigit() for ch in cand):
            return None

        if not cand:
            return None

        # Max 3 words
        words = cand.split()
        if len(words) > 3:
            cand = " ".join(words[:3])

        return cand.title()

    # Pattern-based
    for p in name_patterns:
        m = re.search(p, text)
        if m:
            candidate = clean_candidate(m.group(1))
            if candidate:
                customer_name = candidate
                break

    # Fallback (short messages that are just a name)
    if not customer_name:
        if re.fullmatch(r"[a-zA-Záéíóúñ ]{2,30}", text):
            if len(text.split()) <= 3:
                ignored = ["hola", "ola", "buenas", "buenos dias", "buen día"]
                if text not in ignored:
                    customer_name = text.title()

    # -------------------------
    # PARTY SIZE
    # -------------------------
    party_size = ""
    m = re.search(r"(\d+)\s*(estudiantes|alumnos|niños|personas)", text)
    if m:
        party_size = m.group(1)

    # -------------------------
    # DATE/TIME — GPT extraction
    # -------------------------
    prompt = f"""
Extrae SOLO la fecha y hora del siguiente mensaje.
Devuélvelo exactamente así:

{{
"datetime": "texto exacto de fecha y hora"
}}

No inventes nada.

Mensaje:
\"\"\"{user_msg}\"\"\""""

    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[{"role": "system", "content": prompt}]
        )
        result = json.loads(r.choices[0].message.content)
        dt_text = result.get("datetime", "").strip()
    except Exception:
        dt_text = ""

    # -------------------------
    # PARSE DATE/TIME CLEANLY
    # -------------------------
    dt_local = None
    if dt_text:
        dt_local = dateparser.parse(
            dt_text,
            settings={
                "PREFER_DATES_FROM": "future",
                "TIMEZONE": "America/Bogota",
                "RETURN_AS_TIMEZONE_AWARE": True
            }
        )

    # -------------------------
    # FIX MINUTES bug
    # -------------------------
    if dt_local:
        # user DID specify hour?
        has_hour = re.search(r"\b\d{1,2}\b", dt_text)
        # did they specify minutes?
        has_minutes = re.search(r":\d{2}", dt_text)

        # If hour but NOT minutes → force :00
        if has_hour and not has_minutes:
            dt_local = dt_local.replace(minute=0)

        # If no hour at all → we DO NOT assign time (important!)
        if not has_hour:
            # leave dt_local with date only, but without time
            pass

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

    return {
        "intent": intent,
        "customer_name": customer_name,
        "school_name": school_name,
        "datetime": final_iso,
        "party_size": party_size,
        "package": detected_package,
    }


# ======================================================================
#                      CHATBOT ENGINE (INTENTS)
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
# INTENT MAP
# ----------------------------------------------------------------------
INTENTS = {
    "greeting": {
        "patterns": [],
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

INTENT_PRIORITY = [
    "booking_request",
    "modify",
    "cancel",
    "package_info",
    "confirmation",
    "greeting",
]

def detect_intent(msg: str):
    msg = msg.lower()
    for intent in INTENT_PRIORITY:
        data = INTENTS[intent]
        for p in data["patterns"]:
            if p in msg:
                return intent
    return None  # Silence fallback

# ----------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------
def handle_greeting(msg, session):
    if not session["greeted"]:
        session["greeted"] = True
        return "Hola, claro que sí. ¿En qué te puedo ayudar?"
    return "Claro que sí, ¿en qué te puedo ayudar?"

def handle_package_info(msg, session):
    session["info_mode"] = True

    pkg = detect_package(msg)

    # Price mapping with NEW package names
    prices = {
        "Paquete Cuidado Esencial": "45.000",
        "Paquete Salud Activa": "60.000",
        "Paquete Bienestar Total": "75.000",
    }

    # Package descriptions (added as requested)
    details = {
        "Paquete Cuidado Esencial": "Medicina General, Optometría y Audiometría.",
        "Paquete Salud Activa": "Medicina General, Optometría, Audiometría y Psicología.",
        "Paquete Bienestar Total": "Medicina General, Optometría, Audiometría, Psicología y Odontología.",
    }

    # If a package was detected
    if pkg:
        price = prices.get(pkg)
        detail = details.get(pkg)

        return (
            f"Claro 😊\n"
            f"*{pkg}* cuesta *${price}*.\n\n"
            f"📋 *Incluye:*\n{detail}\n\n"
            "¿Te gustaría agendar una cita?"
        )

    # If no package detected → show menu with details
    return (
        "Claro. Ofrecemos tres paquetes:\n\n"
        "• *Cuidado Esencial* — $45.000\n"
        "  Medicina General, Optometría, Audiometría\n\n"
        "• *Salud Activa* — $60.000\n"
        "  Medicina General, Optometría, Audiometría, Psicología\n\n"
        "• *Bienestar Total* — $75.000\n"
        "  Medicina General, Optometría, Audiometría, Psicología, Odontología\n\n"
        "¿Cuál te interesa?"
    )

def handle_booking_request(msg, session):
    session["booking_started"] = True
    session["info_mode"] = False

    # Extract info BEFORE checking missing fields (for one-sentence bookings)
    update_session_with_info(msg, session)

    if (
        session["student_name"]
        and session["school"]
        and session["package"]
        and session["date"]
        and session["time"]
    ):
        return finish_booking(session)

    return (
        "Por supuesto. Para agendar la cita necesito los siguientes datos:\n\n"
        "– Nombre del estudiante\n"
        "– Colegio\n"
        "– Paquete\n"
        "– Fecha en que deseas la cita\n"
        "– Hora\n\n"
        "¿Me los puedes compartir?"
    )

def handle_modify(msg, session):
    return "Claro, ¿cuál sería la nueva fecha y hora que deseas?"

def handle_cancel(msg, session):
    return "Perfecto, ¿confirmas que deseas cancelar la cita?"

def handle_confirmation(msg, session):
    update_session_with_info(msg, session)

    required = [
        session["student_name"],
        session["school"],
        session["package"],
        session["date"],
        session["time"],
    ]

    if not all(required):
        return "Perfecto, ¿me confirmas algo más?"

    if not session["date"] or not session["time"]:
        return "Necesito la fecha y la hora exactas para confirmar."

    # Build datetime to ISO safely
    try:
        dt_text = f"{session['date']} {session['time']}"
        dt = datetime.strptime(dt_text, "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ)
        iso = dt.isoformat()
    except Exception as e:
        return f"Hubo un error procesando la fecha/hora. ({e})"

    # SAVE INTO SUPABASE
    save_reservation({
        "customer_name": session["student_name"],
        "package": session["package"],
        "school_name": session["school"],
        "datetime": iso,
        "party_size": 1,
        "table_number": None,
    })

    phone = session["phone"]
    session_state.pop(phone, None)

    return (
        f"¡Perfecto! La cita de {session['student_name']} quedó confirmada "
        f"en el {session['school']}, paquete {session['package']}, "
        f"el día {session['date']} a las {session['time']}."
    )

# ----------------------------------------------------------------------
# MAIN ENGINE
# ----------------------------------------------------------------------
def process_message(msg, session):
    # Always try to update data (name, school, package, date, time)
    update_session_with_info(msg, session)

    if session["booking_started"]:
        return continue_booking_process(msg, session)

    intent = detect_intent(msg)

    if intent == "confirmation" and session["info_mode"] and not session["booking_started"]:
        intent = "booking_request"

    if session["booking_started"] and is_confirmation_message(msg):
        return handle_confirmation(msg, session)

    if intent is None:
        return ""

    handler_name = INTENTS[intent]["handler"]
    handler = globals()[handler_name]

    session["first_booking_message"] = False

    return handler(msg, session)

# ======================================================================
#                        EXTRACTORS
# ======================================================================

# STUDENT NAME EXTRACTOR (kept for some flows, but ai_extract is main)
def extract_student_name(msg):
    msg = msg.strip().lower()

    blocked_phrases = [
        "si", "sí", "si por favor", "sí por favor", "por favor",
        "ok", "dale", "claro", "perfecto", "bueno", "listo",
        "de una", "super", "ok listo", "okk", "okay", "okey"
    ]
    if msg in blocked_phrases:
        return None

    # We prefer ai_extract, so this is only a backup when message is ONLY the name
    if 1 <= len(msg.split()) <= 3:
        if all(c.isalpha() or c.isspace() for c in msg):
            return msg.title()

    return None

# SCHOOL EXTRACTOR
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
            name = m.group(1).strip()
            # stop at punctuation, hours, dates, or extra text
            name = re.split(r"[,.!?\n]| a las | a la | mañana|hoy|pasado mañana", name)[0]
            return name.title().strip()

    return None


# ======================================================================
#                    BOOKING LOGIC
# ======================================================================
def update_session_with_info(msg, session):
    """
    Update session with ANY information the user provided.
    Uses ai_extract from backup, but ignores pure confirmations.
    """

    text = msg.lower().strip()

    # FULL BLOCK: if the entire message is a pure confirmation, don't extract anything
    confirmation_words = set(INTENTS["confirmation"]["patterns"])
    if text in confirmation_words:
        return

    # FIRST: use ai_extract (robust)
    extracted = ai_extract(msg)

    # -------------------------
    # STUDENT NAME
    # -------------------------
    if extracted.get("customer_name"):
        session["student_name"] = extracted["customer_name"]
    elif session["student_name"] is None:
        # fallback (simple, only when message is literally just the name)
        name = extract_student_name(msg)
        if name:
            session["student_name"] = name

    # -------------------------
    # SCHOOL (always detect every message)
    # -------------------------
    school = extract_school(msg)
    if school:
        session["school"] = school

    # -------------------------
    # PACKAGE – allow OVERRIDES
    # -------------------------
    pkg = extracted.get("package")
    if pkg:
        session["package"] = pkg

    # -------------------------
    # DATE/TIME from extracted ISO
    # -------------------------
    iso = extracted.get("datetime")
    if iso:
        try:
            dt = datetime.fromisoformat(iso)
            dt = dt.astimezone(LOCAL_TZ)
            session["date"] = dt.strftime("%Y-%m-%d")
            session["time"] = dt.strftime("%H:%M")
        except Exception:
            pass


def build_missing_fields_message(session):
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

    if len(missing) == 1:
        return f"Listo, solo me falta {missing[0]}. ¿Me lo compartes?"

    if len(missing) == 2:
        return f"Perfecto, me falta {missing[0]} y {missing[1]}. ¿Me los compartes?"

    joined = ", ".join(missing[:-1]) + " y " + missing[-1]
    return f"Perfecto, me falta {joined}. ¿Me los compartes?"

def finish_booking(session):
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
    text = msg.lower().strip()
    for p in INTENTS["confirmation"]["patterns"]:
        if p in text:
            return True
    return False

def continue_booking_process(msg, session):
    update_session_with_info(msg, session)

    # Ask for missing fields FIRST
    missing_message = build_missing_fields_message(session)
    if missing_message:
        return missing_message

    # Only allow confirmation if ALL fields exist
    if is_confirmation_message(msg):
        required = [
            session["student_name"],
            session["school"],
            session["package"],
            session["date"],
            session["time"]
        ]
        if all(required):
            return handle_confirmation(msg, session)
        else:
            return "Antes de confirmar necesito toda la información completa 😊"

    # Otherwise, just show the summary
    return finish_booking(session)


# ======================================================================
#                       INTENT PATTERNS
# ======================================================================

INTENTS["greeting"]["patterns"] = [
    "hola", "holaa", "holaaa", "buenas", "buenos dias", "buenos días",
    "buen dia", "buen día", "buenas tardes", "buenas noches",
    "ola", "holi", "holis", "hello", "alo", "aló", "alo?", "que mas",
    "qué más", "q mas", "que tal", "que hubo", "qué hubo", "k hubo",
    "disculpa", "una pregunta", "consulta", "hola una pregunta",
    "buen dia una pregunta", "buenos dias una consulta", "quisiera saber",
    "vi un poster", "vi un anuncio", "vi su aviso", "vi la publicidad",
    "tienen info", "informacion", "información", "quisiera informacion",
    "quisiera información"
]

INTENTS["package_info"]["patterns"] = [
    "cuanto vale", "cuánto vale", "cuanto cuesta", "cuánto cuesta",
    "precio", "precio del paquete", "valor del paquete",
    "cuanto es el de", "cuánto es el de",
    "paquete", "kit escolar", "el de psicologia", "el de psicología",
    "el de psico", "trae psicologia", "trae psicología",
    "incluye psicologia", "incluye psicología",
    "el verde", "verde", "de color verde",
    "el azul", "azul", "de color azul",
    "el amarillo", "amarillo", "de color amarillo",
    "45k", "45 mil", "45mil",
    "60k", "60 mil", "60mil",
    "75k", "75 mil", "75mil",
    "esencial", "salud activa", "activa",
    "bienestar total", "bienestar", "total", "completo",
    "que paquetes tienen", "qué paquetes tienen",
    "ofrecen paquetes", "tienen paquetes",
    "como funcionan los paquetes", "examenes escolares",
    "exámenes escolares", "paquetes escolares",
]

INTENTS["booking_request"]["patterns"] = [
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
    "quiero reservar el paquete",
    "quiero reservar el de",
    "quiero agendar una cita para",
    "quiero agendar cita para",
    "quiero reservar para mi hijo",
    "quiero reservar para mi hija",
    "quiero el examen",
    "quiero hacer el examen",
    "quiero hacer el examen escolar",
    "quiero hacer los exámenes",
    "quiero hacer el examen del colegio",
    "me pueden reservar",
    "me puedes reservar",
    "me pueden agendar",
    "me puedes agendar",
    "me ayudan a reservar",
    "me ayudas a reservar",
    "me ayudas a agendar",
    "agendar cita",
    "agendar una cita",
    "reservar examen",
    "reservar el examen",
    "reservar examen escolar",
    "reservar paquete",
    "agendar paquete",
    "quiero pedir la cita",
    "quiero pedir cita",
    "quiero separar cita",
    "quiero separar el examen",
    "quiero una cita para mi hijo",
    "quiero una cita para mi hija",
    "necesito cita para mi hijo",
    "necesito cita para mi hija",
]

INTENTS["modify"]["patterns"] = [
    "cambiar cita",
    "cambiar la cita",
    "quiero cambiar la cita",
    "necesito cambiar la cita",
    "puedo cambiar la cita",
    "cambiar hora",
    "cambiar la hora",
    "quiero otra hora",
    "me sirve otra hora",
    "puedo mover la hora",
    "cambiar fecha",
    "cambiar la fecha",
    "quiero otra fecha",
    "mover fecha",
    "puedo mover la fecha",
    "quiero mover la cita",
    "necesito mover la cita",
    "quiero reagendar",
    "quiero re agendar",
    "quiero re-agendar",
    "reagendar cita",
    "mover cita",
    "reagendarr", "reagenda", "re agendar cita",
    "otra hora", "otra fecha",
]

INTENTS["cancel"]["patterns"] = [
    "cancelar",
    "cancelar cita",
    "cancelar la cita",
    "quiero cancelar",
    "quiero cancelar la cita",
    "quiero cancelar la cita de mi hijo",
    "quiero cancelar la cita de mi hija",
    "anular", "anular cita", "anular la cita",
    "inhabilitar cita",
    "quitar la cita",
    "me gustaría cancelar",
    "necesito cancelar",
    "me ayudas a cancelar",
    "cancelarr", "cancelala", "cancela la", "cancela eso",
    "ya no quiero la cita",
]

INTENTS["confirmation"]["patterns"] = [
    "confirmo",
    "sí confirmo",
    "si confirmo",
    "confirmar",
    "confirmada",
    "confirmado",
    "si", "sí", "ok", "dale", "listo", "perfecto",
    "super", "claro", "de una", "por supuesto",
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
    "si es para mi hijo",
    "si es para mi hija",
    "si está correcto",
    "sí está correcto",
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

# ======================================================================
# WHATSAPP ROUTE
# ======================================================================
@app.post("/whatsapp")
async def whatsapp_reply(request: Request):
    form = await request.form()
    incoming_msg = form.get("Body", "").strip()
    phone = form.get("From", "").replace("whatsapp:", "").strip()

    session = get_session(phone)

    response_text = process_message(incoming_msg, session)

    if not response_text:
        return Response(content=str(MessagingResponse().message(
             "Disculpa, no entendí bien. ¿Me lo repites por favor?"
        )), media_type="application/xml")


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

    fields = {k: v for k, v in update.items()
              if k != "reservation_id" and v not in ["", None, "-", "null"]}

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
    return {"success": True}

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
    save_reservation({
        "customer_name": data.get("customer_name", ""),
        "customer_email": data.get("customer_email", ""),
        "contact_phone": data.get("contact_phone", ""),
        "datetime": data.get("datetime", ""),
        "party_size": data.get("party_size", 1),
        "school_name": data.get("school_name", ""),
        "package": data.get("package", ""),
        "table_number": None,
    })
    return {"success": True}

# ---------------------------------------------------------
# RUN (LOCAL)
# ---------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
