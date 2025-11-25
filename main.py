# =========================================================
# main.py — Version con STATE MACHINE REAL (Estable)
# =========================================================

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
# USER MEMORY + STATE MACHINE
# ---------------------------------------------------------

"""
Possible states:

START
AFTER_PRICE_INFO
ASK_NAME
ASK_SCHOOL
ASK_DATETIME
ASK_PACKAGE
READY_TO_CONFIRM
FINISHED
"""

session_state = {}

def init_memory():
    return {
        "state": "START",
        "customer_name": None,
        "school_name": None,
        "datetime": None,
        "package": None,
        "party_size": "1",
    }

# ---------------------------------------------------------
# PACKAGE DETECTION
# ---------------------------------------------------------
def detect_package(msg: str):
    msg = msg.lower().strip()

    # direct names
    if "cuidado esencial" in msg or "esencial" in msg or "kit escolar" in msg:
        return "Paquete Cuidado Esencial"
    if "salud activa" in msg or "activa" in msg:
        return "Paquete Salud Activa"
    if "bienestar total" in msg or "total" in msg or "completo" in msg:
        return "Paquete Bienestar Total"

    # price-based
    if "45" in msg or "45k" in msg or "45 mil" in msg or "45mil" in msg:
        return "Paquete Cuidado Esencial"
    if "60" in msg or "60k" in msg or "60 mil" in msg or "60mil" in msg:
        return "Paquete Salud Activa"
    if "75" in msg or "75k" in msg or "75 mil" in msg or "75mil" in msg:
        return "Paquete Bienestar Total"

    # exam-based
    if "odont" in msg:
        return "Paquete Bienestar Total"
    if "psico" in msg:
        return "Paquete Salud Activa"
    if "audio" in msg or "optometr" in msg or "medicina" in msg:
        return "Paquete Cuidado Esencial"

    # colors (text-only)
    if "verde" in msg:
        return "Paquete Cuidado Esencial"
    if "azul" in msg:
        return "Paquete Salud Activa"
    if "amarillo" in msg:
        return "Paquete Bienestar Total"

    return None


# ---------------------------------------------------------
# SAVE RESERVATION
# ---------------------------------------------------------
def save_reservation(data: dict):
    try:
        raw = datetime.fromisoformat(data["datetime"])
        dt_local = raw if raw.tzinfo else raw.replace(tzinfo=LOCAL_TZ)
        iso_to_store = dt_local.isoformat()
    except:
        return "❌ Error procesando la fecha."

    supabase.table("reservations").insert({
        "customer_name": data["customer_name"],
        "customer_email": "",
        "contact_phone": "",
        "datetime": iso_to_store,
        "party_size": int(data["party_size"]),
        "table_number": "AUTO",
        "notes": "",
        "status": "confirmado",
        "business_id": 2,
        "package": data.get("package", ""),
        "school_name": data.get("school_name", "")
    }).execute()

    return (
        "✅ *¡Reserva confirmada!*\n"
        f"👤 {data['customer_name']}\n"
        f"🏫 {data['school_name']}\n"
        f"📦 {data['package']}\n"
        f"🗓 {dt_local.strftime('%Y-%m-%d %H:%M')}"
    )


# ---------------------------------------------------------
# WHATSAPP LOGIC
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp(Body: str = Form(...)):
    msg_raw = Body.strip()
    msg = msg_raw.lower()
    resp = MessagingResponse()

    user_id = "default"
    if user_id not in session_state:
        session_state[user_id] = init_memory()

    memory = session_state[user_id]

    # RESET COMMAND
    if msg in ["reset", "reiniciar", "nuevo"]:
        session_state[user_id] = init_memory()
        resp.message("🔄 Memoria reiniciada. ¿En qué puedo ayudarte?")
        return Response(str(resp), media_type="application/xml")

    # ---------------------------------------------------------
    # STATE: START (First message)
    # ---------------------------------------------------------
    if memory["state"] == "START":

        # If user asks price
        if any(k in msg for k in ["cuánto", "cuanto", "precio", "vale", "incluye", "trae"]):
            pkg = detect_package(msg)
            if pkg:
                memory["state"] = "AFTER_PRICE_INFO"
                memory["package"] = pkg
                resp.message(
                    f"Hola 😊\nEl paquete que mencionas es *{pkg}*.\n\n"
                    "Precios:\n"
                    "• Cuidado Esencial – $45.000\n"
                    "• Salud Activa – $60.000\n"
                    "• Bienestar Total – $75.000\n\n"
                    "¿Te gustaría agendar una cita?"
                )
                return Response(str(resp), media_type="application/xml")

            # If no specific package detected
            memory["state"] = "AFTER_PRICE_INFO"
            resp.message(
                "Hola 😊\nAquí tienes la información de los paquetes:\n\n"
                "• Cuidado Esencial – $45.000\n"
                "• Salud Activa – $60.000\n"
                "• Bienestar Total – $75.000\n\n"
                "¿Te gustaría agendar una cita?"
            )
            return Response(str(resp), media_type="application/xml")

        # Any other first message → ask name
        memory["state"] = "ASK_NAME"
        resp.message("Claro 😊 ¿Cuál es el nombre del estudiante?")
        return Response(str(resp), media_type="application/xml")

    # ---------------------------------------------------------
    # STATE: AFTER_PRICE_INFO
    # ---------------------------------------------------------
    if memory["state"] == "AFTER_PRICE_INFO":

        if msg in ["si", "sí", "claro", "ok", "dale", "quiero", "si por favor"]:
            memory["state"] = "ASK_NAME"
            resp.message(
                "Perfecto 😊\nPara agendar necesito:\n"
                "• Nombre del estudiante\n"
                "• Colegio\n"
                "• Fecha y hora deseada\n"
                "• Paquete"
            )
            return Response(str(resp), media_type="application/xml")

        resp.message("¿Te gustaría agendar una cita?")
        return Response(str(resp), media_type="application/xml")

    # ---------------------------------------------------------
    # STATE: ASK_NAME
    # ---------------------------------------------------------
    if memory["state"] == "ASK_NAME":
        memory["customer_name"] = msg_raw
        memory["state"] = "ASK_SCHOOL"
        resp.message("Perfecto 😊 ¿De qué colegio viene?")
        return Response(str(resp), media_type="application/xml")

    # ---------------------------------------------------------
    # STATE: ASK_SCHOOL
    # ---------------------------------------------------------
    if memory["state"] == "ASK_SCHOOL":
        memory["school_name"] = msg_raw
        memory["state"] = "ASK_DATETIME"
        resp.message("¿Para qué fecha y hora deseas la cita?")
        return Response(str(resp), media_type="application/xml")

    # ---------------------------------------------------------
    # STATE: ASK_DATETIME
    # ---------------------------------------------------------
    if memory["state"] == "ASK_DATETIME":
        import dateparser
        dt = dateparser.parse(
            msg_raw,
            settings={
                "PREFER_DATES_FROM": "future",
                "TIMEZONE": "America/Bogota",
                "RETURN_AS_TIMEZONE_AWARE": True
            }
        )
        if not dt:
            resp.message("No entendí la fecha 😅 ¿Puedes repetirla?")
            return Response(str(resp), media_type="application/xml")

        memory["datetime"] = dt.isoformat()
        memory["state"] = "ASK_PACKAGE"

        resp.message(
            "Perfecto 😊 ¿Qué paquete deseas reservar?\n\n"
            "• Cuidado Esencial – $45.000\n"
            "• Salud Activa – $60.000\n"
            "• Bienestar Total – $75.000"
        )
        return Response(str(resp), media_type="application/xml")

    # ---------------------------------------------------------
    # STATE: ASK_PACKAGE
    # ---------------------------------------------------------
    if memory["state"] == "ASK_PACKAGE":
        pkg = detect_package(msg)
        if not pkg:
            resp.message("No entendí el paquete 😅 ¿Cuál deseas?")
            return Response(str(resp), media_type="application/xml")

        memory["package"] = pkg
        memory["state"] = "READY_TO_CONFIRM"

        confirmation = save_reservation(memory)
        resp.message("Hola 😊\n" + confirmation)

        memory["state"] = "FINISHED"
        return Response(str(resp), media_type="application/xml")

    # ---------------------------------------------------------
    # FINISHED: restart
    # ---------------------------------------------------------
    resp.message("¿Te gustaría agendar otra cita?")
    return Response(str(resp), media_type="application/xml")

