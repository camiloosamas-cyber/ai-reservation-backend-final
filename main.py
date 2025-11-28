from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json, os, re
import dateparser

from supabase import create_client, Client
from openai import OpenAI
from twilio.twiml.messaging_response import MessagingResponse
from dateutil import parser

# ---------------------------------------------------------
# INIT
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
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE")
)

TABLE_LIMIT = 10

# ---------------------------------------------------------
# TABLE ASSIGNMENT
# ---------------------------------------------------------
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

    table = assign_table(iso_to_store)

    supabase.table("reservations").insert({
        "customer_name": data["customer_name"],
        "customer_email": "",
        "contact_phone": "",
        "datetime": iso_to_store,
        "party_size": 1,
        "table_number": table,
        "notes": "",
        "status": "confirmado",
        "business_id": 2,
        "package": data.get("package", ""),
        "school_name": data.get("school_name", ""),
        "cedula": data.get("cedula", ""),
        "edad": data.get("edad", "")
    }).execute()

    return (
        "✅ *¡Reserva confirmada!*\n"
        f"👤 {data['customer_name']}\n"
        f"📦 {data.get('package','')}\n"
        f"🏫 {data.get('school_name','')}\n"
        f"🪪 Cédula: {data.get('cedula','')}\n"
        f"🎂 Edad: {data.get('edad','')}\n"
        f"🗓 {dt_local.strftime('%Y-%m-%d %H:%M')}"
    )

# ---------------------------------------------------------
# PACKAGE DETECTOR
# ---------------------------------------------------------
def detect_package(msg: str):
    msg = msg.lower()
    if any(w in msg for w in ["esencial", "45"]):
        return "Paquete Cuidado Esencial"
    if any(w in msg for w in ["activa", "60", "psico"]):
        return "Paquete Salud Activa"
    if any(w in msg for w in ["total", "75", "odont"]):
        return "Paquete Bienestar Total"
    return None

# ---------------------------------------------------------
# AI EXTRACT
# ---------------------------------------------------------
def ai_extract(user_msg: str):

    # extract datetime via GPT
    prompt = f"""
Extrae SOLO la fecha y hora exacta del mensaje.
Devuelve JSON así:
{{
"dt": "texto exacto de fecha y hora"
}}
Si no hay fecha u hora, deja "".

Mensaje:
\"\"\"{user_msg}\"\"\"
"""

    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[{"role": "system", "content": prompt}]
        )
        dt_text = json.loads(r.choices[0].message.content).get("dt", "")
    except:
        dt_text = ""

    dt_local = None
    if dt_text:
        try:
            dt_local = dateparser.parse(
                dt_text,
                settings={
                    "RETURN_AS_TIMEZONE_AWARE": True,
                    "TIMEZONE": "America/Bogota",
                    "PREFER_DATES_FROM": "future"
                }
            )
        except:
            dt_local = None

    iso_dt = dt_local.isoformat() if dt_local else ""

    # school
    school = ""
    ptns = [
        r"colegio\s+[a-záéíóúñ0-9 ]+",
        r"gimnasio\s+[a-záéíóúñ0-9 ]+",
        r"liceo\s+[a-záéíóúñ0-9 ]+",
        r"instituto\s+[a-záéíóúñ0-9 ]+"
    ]
    low = user_msg.lower()
    for p in ptns:
        m = re.search(p, low)
        if m:
            raw = m.group(0)
            school = re.split(r"[,.!\n]", raw)[0].title()
            break

    # student name
    name = ""
    ptns_name = [
        r"mi hijo ([a-záéíóúñ ]+)",
        r"mi hija ([a-záéíóúñ ]+)",
        r"es para ([a-záéíóúñ ]+)",
        r"se llama ([a-záéíóúñ ]+)"
    ]
    for p in ptns_name:
        m = re.search(p, low)
        if m:
            n = m.group(1).strip().title()
            if 2 <= len(n) <= 30:
                name = n
            break

    # cedula
    cedula = ""
    m = re.search(r"\b(\d{6,12})\b", user_msg)
    if m:
        cedula = m.group(1)

    # edad
    edad = ""
    m = re.search(r"\b([1-9]|1[0-8])\b", user_msg)
    if m:
        edad = m.group(1)

    pkg = detect_package(user_msg)

    return {
        "name": name,
        "school": school,
        "datetime": iso_dt,
        "package": pkg,
        "cedula": cedula,
        "edad": edad
    }

# ---------------------------------------------------------
# SESSION MANAGEMENT
# ---------------------------------------------------------
session_state = {}

def get_session(phone):
    if phone not in session_state:
        session_state[phone] = {
            "phone": phone,
            "name": None,
            "school": None,
            "package": None,
            "date": None,
            "time": None,
            "cedula": None,
            "edad": None,
            "booking": False,
            "info_mode": False
        }
    return session_state[phone]

# ---------------------------------------------------------
# UPDATE SESSION
# ---------------------------------------------------------
def update_session(msg, session):
    extracted = ai_extract(msg)

    if extracted["name"]:
        session["name"] = extracted["name"]

    if extracted["school"]:
        session["school"] = extracted["school"]

    if extracted["package"]:
        session["package"] = extracted["package"]

    if extracted["cedula"]:
        session["cedula"] = extracted["cedula"]

    if extracted["edad"]:
        session["edad"] = extracted["edad"]

    if extracted["datetime"]:
        try:
            dt = datetime.fromisoformat(extracted["datetime"]).astimezone(LOCAL_TZ)
            session["date"] = dt.strftime("%Y-%m-%d")
            session["time"] = dt.strftime("%H:%M")
        except:
            pass

# ---------------------------------------------------------
# MISSING FIELDS
# ---------------------------------------------------------
def missing_fields(session):
    missing = []
    if not session["name"]:
        missing.append("el nombre del estudiante")
    if not session["school"]:
        missing.append("el colegio")
    if not session["package"]:
        missing.append("el paquete")
    if not session["date"]:
        missing.append("la fecha")
    if not session["time"]:
        missing.append("la hora")
    if not session["cedula"]:
        missing.append("la cédula")
    if not session["edad"]:
        missing.append("la edad")

    if not missing:
        return None

    if len(missing) == 1:
        return f"Listo, solo me falta {missing[0]}. ¿Me lo compartes?"
    if len(missing) == 2:
        return f"Perfecto, me falta {missing[0]} y {missing[1]}. ¿Me los compartes?"

    return (
        "Perfecto, me falta " +
        ", ".join(missing[:-1]) +
        f" y {missing[-1]}. ¿Me los compartes?"
    )

# ---------------------------------------------------------
# FINISH BOOKING
# ---------------------------------------------------------
def finish_booking(session):
    return (
        f"Listo 😊 Tu cita quedó agendada para *{session['name']}*.\n"
        f"🏫 Colegio: {session['school']}\n"
        f"📦 Paquete: {session['package']}\n"
        f"🪪 Cédula: {session['cedula']}\n"
        f"🎂 Edad: {session['edad']}\n"
        f"🗓 {session['date']} a las {session['time']}\n\n"
        "¿Deseas confirmar?"
    )

# ---------------------------------------------------------
# INTENTS
# ---------------------------------------------------------
def detect_intent(msg):
    msg = msg.lower()

    if any(x in msg for x in ["hola","buenas","buenos días","que tal","hola una pregunta"]):
        return "greeting"

    if any(x in msg for x in ["cuánto","cuanto","precio","paquete","que incluye"]):
        return "package_info"

    if any(x in msg for x in ["quiero","necesito","reservar","agendar","cita"]):
        return "booking"

    if any(x in msg for x in ["sí","si","confirmo","ok","dale","listo","perfecto"]):
        return "confirm"

    return None

# ---------------------------------------------------------
# HANDLERS
# ---------------------------------------------------------
def handle_greeting(msg, session):
    return "Hola 😊 ¿En qué te puedo ayudar?"

def handle_package_info(msg, session):
    pkg = detect_package(msg)
    prices = {
        "Paquete Cuidado Esencial": "45.000",
        "Paquete Salud Activa": "60.000",
        "Paquete Bienestar Total": "75.000",
    }
    details = {
        "Paquete Cuidado Esencial": "Medicina General, Optometría, Audiometría",
        "Paquete Salud Activa": "Medicina General, Optometría, Audiometría, Psicología",
        "Paquete Bienestar Total": "Medicina General, Optometría, Audiometría, Psicología, Odontología",
    }

    if pkg:
        return (
            f"*{pkg}* cuesta *${prices[pkg]}*.\n"
            f"📋 Incluye: {details[pkg]}\n\n"
            "¿Deseas agendar una cita?"
        )
    else:
        return (
            "Ofrecemos 3 paquetes:\n\n"
            "• Cuidado Esencial — $45.000\n"
            "• Salud Activa — $60.000\n"
            "• Bienestar Total — $75.000\n\n"
            "¿Cuál te interesa?"
        )

def handle_booking(msg, session):
    session["booking"] = True
    update_session(msg, session)

    miss = missing_fields(session)
    if miss:
        return miss

    return finish_booking(session)

def handle_confirm(msg, session):
    miss = missing_fields(session)
    if miss:
        return miss

    dt_iso = f"{session['date']}T{session['time']}:00"
    save_reservation({
        "customer_name": session["name"],
        "school_name": session["school"],
        "package": session["package"],
        "datetime": dt_iso,
        "cedula": session["cedula"],
        "edad": session["edad"]
    })

    phone = session["phone"]
    session_state.pop(phone, None)

    return "¡Perfecto! Tu cita quedó confirmada 😊"

# ---------------------------------------------------------
# PROCESS MESSAGE
# ---------------------------------------------------------
def process_message(msg, session):
    update_session(msg, session)
    intent = detect_intent(msg)

    if intent == "greeting":
        return handle_greeting(msg, session)
    if intent == "package_info":
        return handle_package_info(msg, session)
    if intent == "booking":
        return handle_booking(msg, session)
    if intent == "confirm":
        return handle_confirm(msg, session)

    # if nothing matches
    miss = missing_fields(session)
    if miss:
        return miss

    return ""

# ---------------------------------------------------------
# WHATSAPP ENDPOINT
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp_reply(request: Request):
    form = await request.form()
    incoming = form.get("Body", "").strip()
    phone = form.get("From", "").replace("whatsapp:", "")

    session = get_session(phone)
    reply = process_message(incoming, session)

    tw = MessagingResponse()
    tw.message(reply if reply else "¿Me repites porfa?")
    return Response(content=str(tw), media_type="application/xml")

# ---------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    res = supabase.table("reservations").select("*").order("datetime", desc=True).execute()
    rows = res.data or []

    fixed = []
    now = datetime.now(LOCAL_TZ)
    week_ago = now - timedelta(days=7)
    weekly_count = 0

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
# RESERVATION ACTIONS
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

# ---------------------------------------------------------
# CREATE RESERVATION FROM DASHBOARD
# ---------------------------------------------------------
@app.post("/createReservation")
async def create_reservation(data: dict):
    save_reservation({
        "customer_name": data.get("customer_name",""),
        "datetime": data.get("datetime",""),
        "package": data.get("package",""),
        "school_name": data.get("school_name",""),
        "cedula": data.get("cedula",""),
        "edad": data.get("edad","")
    })
    return {"success": True}

# ---------------------------------------------------------
# RUN
# ---------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
