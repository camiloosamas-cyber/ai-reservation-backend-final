# ======================= BEGIN MAIN FILE =======================

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json, os, re

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
# MEMORY PER USER
# ---------------------------------------------------------
session_state = {}

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
# PACKAGE DETECTION
# ---------------------------------------------------------
def detect_package(msg: str):
    msg = msg.lower().strip()

    if "esencial" in msg or "45" in msg or "verde" in msg or "45mil" in msg:
        return "Paquete Cuidado Esencial"

    if "activa" in msg or "60" in msg or "azul" in msg or "psico" in msg:
        return "Paquete Salud Activa"

    if "total" in msg or "75" in msg or "amarillo" in msg or "odont" in msg:
        return "Paquete Bienestar Total"

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
    if not table:
        return "❌ No hay cupos disponibles para ese horario."

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
    }).execute()

    return (
        "✅ *¡Cita confirmada!*\n"
        f"👤 {data['customer_name']}\n"
        f"🏫 {data['school_name']}\n"
        f"📦 {data['package']}\n"
        f"🗓 {dt_local.strftime('%Y-%m-%d %H:%M')}"
    )


# ---------------------------------------------------------
# AI EXTRACTION
# ---------------------------------------------------------
def ai_extract(user_msg: str):
    import dateparser
    text = user_msg.lower().strip()

    # PACKAGE
    pkg = detect_package(text)

    # SCHOOL
    school = ""
    school_patterns = [
        r"(colegio [a-zA-Záéíóúñ ]+)",
        r"(gimnasio [a-zA-Záéíóúñ ]+)",
        r"(liceo [a-zA-Záéíóúñ ]+)",
        r"(instituto [a-zA-Záéíóúñ ]+)",
    ]
    for p in school_patterns:
        m = re.search(p, text)
        if m:
            school = m.group(1).strip()
            break

    # NAME
    name = ""
    if "se llama" in text:
        name = text.split("se llama",1)[1].strip().split()[0]
    elif re.fullmatch(r"[a-zA-Záéíóúñ ]{2,20}", text) and "colegio" not in text:
        name = text.strip()

    # DATE / TIME
    prompt = f"""
Extrae SOLO la fecha y hora:

{{
"datetime": "texto"
}}

Mensaje:
\"\"\"{user_msg}\"\"\""""

    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[{"role": "system","content":prompt}]
        )
        dt_raw = json.loads(r.choices[0].message.content)["datetime"]
    except:
        dt_raw = ""

    dt_parsed = dateparser.parse(dt_raw, settings={
        "PREFER_DATES_FROM": "future",
        "TIMEZONE": "America/Bogota",
        "RETURN_AS_TIMEZONE_AWARE": True
    })

    dt_iso = dt_parsed.isoformat() if dt_parsed else ""

    return {
        "customer_name": name,
        "school_name": school,
        "package": pkg,
        "datetime": dt_iso
    }


# ---------------------------------------------------------
# WHATSAPP HANDLER
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp(Body: str = Form(...)):
    resp = MessagingResponse()
    msg_raw = Body.strip()
    msg = msg_raw.lower()
    user_id = "default"

    # RESET
    if msg in ["reset","nuevo","reiniciar"]:
        session_state[user_id] = {
            "customer_name": None,
            "school_name": None,
            "package": None,
            "datetime": None,
            "started": False
        }
        resp.message("🔄 Memoria reiniciada.")
        return Response(str(resp), media_type="application/xml")

    # INIT MEMORY
    if user_id not in session_state:
        session_state[user_id] = {
            "customer_name": None,
            "school_name": None,
            "package": None,
            "datetime": None,
            "started": False
        }

    memory = session_state[user_id]

    # FIRST MESSAGE
    if not memory["started"]:
        memory["started"] = True

        # greeting
        if any(g in msg for g in ["hola","ola","buenas","buen dia"]):
            resp.message("Hola 😊 ¿En qué puedo ayudarte?")
            return Response(str(resp), media_type="application/xml")

        # price / info
        if any(w in msg for w in ["cuánto","precio","vale","incluye","trae"]):
            pkg = detect_package(msg)
            if pkg:
                resp.message(
                    f"Hola 😊\nEse corresponde al *{pkg}*.\n\n"
                    "Precios:\n"
                    "• Cuidado Esencial – $45.000\n"
                    "• Salud Activa – $60.000\n"
                    "• Bienestar Total – $75.000\n\n"
                    "¿Quieres agendar?"
                )
                return Response(str(resp), media_type="application/xml")

            resp.message(
                "Hola 😊\nAquí tienes los paquetes:\n\n"
                "• Cuidado Esencial – $45.000\n"
                "• Salud Activa – $60.000\n"
                "• Bienestar Total – $75.000\n\n"
                "¿Cuál deseas?"
            )
            return Response(str(resp), media_type="application/xml")

        # package detected
        pkg = detect_package(msg)
        if pkg:
            memory["package"] = pkg
            resp.message(f"Hola 😊 Ese es *{pkg}*. ¿Deseas agendar?")
            return Response(str(resp), media_type="application/xml")

        resp.message("Hola 😊 ¿En qué puedo ayudarte?")
        return Response(str(resp), media_type="application/xml")

    # NEXT MESSAGES
    extracted = ai_extract(msg)

    if extracted["customer_name"]:
        memory["customer_name"] = extracted["customer_name"]

    if extracted["school_name"]:
        memory["school_name"] = extracted["school_name"]

    if extracted["package"]:
        memory["package"] = extracted["package"]

    if extracted["datetime"]:
        memory["datetime"] = extracted["datetime"]

    # ASK FOR MISSING DATA
    if not memory["customer_name"]:
        resp.message("¿Cuál es el nombre del estudiante?")
        return Response(str(resp), media_type="application/xml")

    if not memory["school_name"]:
        resp.message("¿De qué colegio viene?")
        return Response(str(resp), media_type="application/xml")

    if not memory["package"]:
        resp.message(
            "¿Qué paquete deseas reservar?\n"
            "• Cuidado Esencial – $45.000\n"
            "• Salud Activa – $60.000\n"
            "• Bienestar Total – $75.000"
        )
        return Response(str(resp), media_type="application/xml")

    if not memory["datetime"]:
        resp.message("¿Para qué fecha y hora deseas la cita?")
        return Response(str(resp), media_type="application/xml")

    # SAVE
    confirm = save_reservation(memory)
    resp.message("Hola 😊\n" + confirm)

    # RESET AFTER SAVE
    session_state[user_id] = {
        "customer_name": None,
        "school_name": None,
        "package": None,
        "datetime": None,
        "started": False
    }

    return Response(str(resp), media_type="application/xml")

# ======================= END MAIN FILE =======================
