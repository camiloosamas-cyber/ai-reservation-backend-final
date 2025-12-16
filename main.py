import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from twilio.twiml.messaging_response import MessagingResponse

import dateparser
from supabase import create_client, Client as SupabaseClient

os.chdir(os.path.dirname(os.path.abspath(__file__)))
TEST_MODE = os.getenv("TEST_MODE") == "1"

try:
    LOCAL_TZ = ZoneInfo("America/Bogota")
except ZoneInfoNotFoundError:
    LOCAL_TZ = ZoneInfo("UTC")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE")

supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(title="Oriental IPS Bot", version="3.0.2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

SESSIONS = {}

REQUIRED_FIELDS = ["student_name", "school", "age", "cedula", "package", "date", "time"]

PACKAGE_DATA = {
    "esencial": {"price": "45.000", "label": "Cuidado Esencial"},
    "activa": {"price": "60.000", "label": "Salud Activa"},
    "bienestar": {"price": "75.000", "label": "Bienestar Total"},
}

FAQ_RESPONSES = {
    "ubicados": "Estamos ubicados en Calle 31 #29-61, Yopal.",
    "pago": "Aceptamos Nequi y efectivo.",
    "duracion": "El examen dura entre 30 y 45 minutos.",
    "llevar": "Debes traer el documento de identidad del estudiante.",
    "domingo": "Si, atendemos todos los dias de 6am a 8pm.",
}

RELEVANT_KEYWORDS = [
    "cita", "reserv", "paquete", "precio", "colegio", "estudiante", 
    "examenes", "fecha", "hora", "ubicados", "pago", "nequi",
    "esencial", "activa", "bienestar", "agendar", "confirmo",
]

def twiml(message):
    if TEST_MODE:
        return Response(content=message, media_type="text/plain")
    r = MessagingResponse()
    r.message(message)
    return Response(content=str(r), media_type="application/xml")

def get_session(phone):
    if phone not in SESSIONS:
        SESSIONS[phone] = {
            "booking_started": False,
            "student_name": None,
            "school": None,
            "age": None,
            "cedula": None,
            "package": None,
            "date": None,
            "time": None,
            "awaiting_confirmation": False,
        }
    return SESSIONS[phone]

def reset_session(phone):
    SESSIONS[phone] = {
        "booking_started": False,
        "student_name": None,
        "school": None,
        "age": None,
        "cedula": None,
        "package": None,
        "date": None,
        "time": None,
        "awaiting_confirmation": False,
    }

def is_relevant(msg):
    m = msg.lower()
    return any(k in m for k in RELEVANT_KEYWORDS)

def extract_package(msg):
    m = msg.lower()
    if any(k in m for k in ["esencial", "45000", "45.000"]):
        return "esencial"
    if any(k in m for k in ["activa", "60000", "60.000"]):
        return "activa"
    if any(k in m for k in ["bienestar", "total", "75000", "75.000"]):
        return "bienestar"
    return None

def extract_school(msg):
    t = msg.strip()
    l = t.lower()
    patterns = [
        r"del colegio\s+([a-zA-Z\s]+)",
        r"colegio\s+([a-zA-Z\s]+)",
        r"gimnasio\s+([a-zA-Z\s]+)",
        r"instituto\s+([a-zA-Z\s]+)",
    ]
    for pat in patterns:
        m = re.search(pat, l)
        if m:
            return m.group(1).strip().title()
    if any(k in l for k in ["gimnasio", "instituto", "comfacasanare"]):
        return t.strip().title()
    return None

def extract_student_name(msg):
    t = msg.strip()
    l = t.lower()
    if l in ["hola", "buenos dias", "buenas"]:
        return None
    patterns = [
        r"se llama\s+([a-zA-Z\s]+)",
        r"nombre es\s+([a-zA-Z\s]+)",
    ]
    for pat in patterns:
        m = re.search(pat, l)
        if m:
            return m.group(1).strip().title()
    words = t.split()
    if len(words) >= 2:
        valid = [w for w in words if len(w) >= 2 and re.match(r'^[a-zA-Z]+$', w)]
        if len(valid) >= 2:
            combined = " ".join(valid).lower()
            if not any(k in combined for k in ["buenos", "confirmo", "paquete"]):
                return " ".join(valid).title()
    return None

def extract_age(msg):
    t = msg.lower()
    m = re.search(r'(\d{1,2})\s*anos?', t)
    if m:
        age = int(m.group(1))
        if 3 <= age <= 18:
            return str(age)
    if t.strip().isdigit():
        age = int(t.strip())
        if 3 <= age <= 18:
            return str(age)
    return None

def extract_cedula(msg):
    m = re.search(r'\b(\d{7,12})\b', msg)
    if m:
        return m.group(1)
    return None

def extract_date(msg):
    d = dateparser.parse(msg, settings={
        "TIMEZONE": "America/Bogota",
        "PREFER_DATES_FROM": "future",
    })
    if not d:
        return None
    d = d.astimezone(LOCAL_TZ)
    today = datetime.now(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    if d < today:
        return None
    return d.strftime("%Y-%m-%d")

def extract_time(msg):
    t = msg.lower()
    m = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)', t)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        ampm = m.group(3)
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        if 6 <= hour <= 20:
            return f"{hour:02d}:{minute:02d}"
    return None

def check_faq(msg):
    t = msg.lower()
    if "ubicad" in t or "donde" in t:
        return FAQ_RESPONSES["ubicados"]
    if "pago" in t or "nequi" in t:
        return FAQ_RESPONSES["pago"]
    if "dur" in t:
        return FAQ_RESPONSES["duracion"]
    if "llevar" in t or "traer" in t:
        return FAQ_RESPONSES["llevar"]
    if "domingo" in t or "horario" in t:
        return FAQ_RESPONSES["domingo"]
    return None

def detect_booking_intent(msg):
    t = msg.lower()
    return any(k in t for k in ["agendar", "reservar", "cita"])

def update_session_with_extracted_data(msg, session):
    updated = []
    extractors = [
        ("package", extract_package),
        ("student_name", extract_student_name),
        ("school", extract_school),
        ("age", extract_age),
        ("cedula", extract_cedula),
        ("date", extract_date),
        ("time", extract_time),
    ]
    for field, fn in extractors:
        if not session[field]:
            val = fn(msg)
            if val:
                session[field] = val
                session["booking_started"] = True
                updated.append(field)
    return updated

def missing_fields(session):
    return [f for f in REQUIRED_FIELDS if not session.get(f)]

def get_field_prompt(field):
    prompts = {
        "student_name": "Cual es el nombre completo del estudiante?",
        "school": "De que colegio es?",
        "age": "Que edad tiene?",
        "cedula": "Cual es el numero de cedula?",
        "package": "Tenemos 3 paquetes:\nEsencial $45.000\nActiva $60.000\nBienestar $75.000\n\nCual deseas?",
        "date": "Para que fecha? (ejemplo: 15 de enero)",
        "time": "A que hora? (ejemplo: 10am)",
    }
    return prompts.get(field, "")

def acknowledge_field(field, value):
    responses = {
        "student_name": f"Perfecto, {value}.",
        "school": f"Entendido, {value}.",
        "age": f"Ok, {value} anos.",
        "cedula": f"Cedula registrada.",
        "package": "Paquete seleccionado.",
        "date": "Fecha anotada.",
        "time": "Hora confirmada.",
    }
    return responses.get(field, "")

def build_summary(session):
    pkg = session["package"]
    label = PACKAGE_DATA[pkg]["label"]
    price = PACKAGE_DATA[pkg]["price"]
    return (
        f"Perfecto, resumen:\n\n"
        f"Estudiante: {session['student_name']}\n"
        f"Colegio: {session['school']}\n"
        f"Edad: {session['age']}\n"
        f"Cedula: {session['cedula']}\n"
        f"Paquete: {label} ${price}\n"
        f"Fecha: {session['date']}\n"
        f"Hora: {session['time']}\n\n"
        f"Responde Confirmo para agendar."
    )

def assign_table():
    if not supabase:
        return "T1"
    try:
        res = supabase.table("reservations").select("table_number").eq("business_id", 2).execute()
        return f"T{len(res.data) + 1}"
    except:
        return "T1"

def insert_reservation(phone, session):
    if not supabase:
        return True, "T1"
    table = assign_table()
    dt = datetime.strptime(f"{session['date']} {session['time']}", "%Y-%m-%d %H:%M")
    dt_iso = dt.astimezone(LOCAL_TZ).isoformat()
    try:
        supabase.table("reservations").insert({
            "student_name": session["student_name"],
            "phone": phone,
            "datetime": dt_iso,
            "school": session["school"],
            "package": session["package"],
            "age": int(session["age"]),
            "cedula": session["cedula"],
            "business_id": 2,
            "table_number": table,
            "status": "confirmado"
        }).execute()
        return True, table
    except Exception as e:
        print(f"Error: {e}")
        return False, str(e)

def handle_message(phone, msg):
    session = get_session(phone)
    t = msg.strip()
    l = t.lower()
    is_greeting = any(l.startswith(g) for g in ["hola", "buenos", "buenas"])
    
    if not session["booking_started"] and not is_relevant(t) and not is_greeting:
        return ""
    
    if not session["booking_started"] and is_greeting and not is_relevant(t):
        return "Buenos dias, estas comunicado con Oriental IPS. En que te podemos ayudar?"
    
    if not session["booking_started"]:
        ans = check_faq(t)
        if ans:
            return ans
    
    pkg = extract_package(t)
    if pkg and not session["booking_started"]:
        if detect_booking_intent(t):
            session["package"] = pkg
            session["booking_started"] = True
            missing = missing_fields(session)
            if missing:
                return get_field_prompt(missing,[object Object],)
        return f"Paquete {pkg} cuesta ${PACKAGE_DATA[pkg]['price']}. Deseas agendar?"
    
    if detect_booking_intent(t) and not session["booking_started"]:
        session["booking_started"] = True
        missing = missing_fields(session)
        if missing:
            return "Perfecto. " + get_field_prompt(missing,[object Object],)
    
    updated = update_session_with_extracted_data(t, session)
    
    if "confirmo" in l:
        if session.get("awaiting_confirmation"):
            missing = missing_fields(session)
            if not missing:
                ok, table = insert_reservation(phone, session)
                if ok:
                    name = session["student_name"]
                    reset_session(phone)
                    return f"Cita confirmada para {name}! Mesa {table}. Te esperamos!"
                return "Error registrando cita. Intenta nuevamente."
    
    if updated:
        ack = acknowledge_field(updated,[object Object],, session[updated,[object Object],])
        missing = missing_fields(session)
        if not missing:
            session["awaiting_confirmation"] = True
            return f"{ack}\n\n{build_summary(session)}"
        else:
            next_prompt = get_field_prompt(missing,[object Object],)
            return f"{ack} {next_prompt}"
    
    if session["booking_started"]:
        missing = missing_fields(session)
        if missing:
            return get_field_prompt(missing,[object Object],)
    
    return ""

@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    form = await request.form()
    text = form.get("Body", "")
    phone = form.get("From", "").replace("whatsapp:", "")
    reply = handle_message(phone, text)
    if reply == "":
        if TEST_MODE:
            return Response(content="", media_type="text/plain")
        return Response(content=str(MessagingResponse()), media_type="application/xml")
    return twiml(reply)

@app.get("/")
def root():
    return {"status": "running", "version": "3.0.2"}

@app.get("/health")
def health():
    return {"status": "healthy", "sessions": len(SESSIONS)}
