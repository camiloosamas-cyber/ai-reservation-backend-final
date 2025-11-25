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
# PACKAGE DETECTION (UPDATED EXACTLY AS REQUESTED)
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

    # Color-based (TEXT ONLY, NO IMAGE DETECTION)
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

    # Try structured patterns first
    for p in name_patterns:
        m = re.search(p, text)
        if m:
            candidate = m.group(1).strip()
            customer_name = " ".join(candidate.split()[:3])
            break

    # --------------------------
    # FALLBACK: message is ONLY a name
    # --------------------------
    if not customer_name:
        package_words = [
            "esencial", "activa", "total", "bienestar", "cuidado", "salud",
            "paquete", "kit", "45", "60", "75"
        ]

        is_just_text = re.fullmatch(r"[a-zA-Záéíóúñ ]{2,30}", text)
        is_short = len(text.split()) <= 3
        contains_package_word = any(w in text for w in package_words)

        if is_just_text and is_short and not contains_package_word:
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

# ---------------------------------------------------------
# WHATSAPP HANDLER (CLEAN + UPDATED)
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp(Body: str = Form(...)):
    resp = MessagingResponse()
    msg_raw = Body.strip()
    msg = msg_raw.lower()
    user_id = "default"

    # -----------------------------------------------------
    # 0. RESET MEMORY
    # -----------------------------------------------------
    if msg in ["reset", "reiniciar", "nuevo", "borrar"]:
        session_state[user_id] = {
            "customer_name": None,
            "school_name": None,
            "package": None,
            "datetime": None,
            "party_size": "1",
            "started": False,
            "awaiting_info": False,
            "waiting_for_confirmation": False
        }
        resp.message("🔄 Memoria reiniciada.\n\nPuedes empezar una conversación nueva 😊")
        return Response(str(resp), media_type="application/xml")

    # -----------------------------------------------------
    # 1. IF NEW USER → INIT MEMORY
    # -----------------------------------------------------
    if user_id not in session_state:
        session_state[user_id] = {
            "customer_name": None,
            "school_name": None,
            "package": None,
            "datetime": None,
            "party_size": "1",
            "started": False,
            "awaiting_info": False,
            "waiting_for_confirmation": False
        }

    memory = session_state[user_id]

    # -----------------------------------------------------
    # 2. FIRST MESSAGE HANDLER (FIXED)
    # -----------------------------------------------------
    if not memory["started"]:
        memory["started"] = True

        # 🔍 detect if user is asking for PRICE / INFO
        info_words = ["cuánto", "cuanto", "precio", "vale", "incluye", "trae"]
        if any(w in msg for w in info_words):
            pkg = detect_package(msg)

            if pkg:
                resp.message(
                    f"Hola 😊\nEl paquete que mencionas es *{pkg}*.\n\n"
                    "Precios:\n"
                    "• *Cuidado Esencial* – $45.000\n"
                    "• *Salud Activa* – $60.000\n"
                    "• *Bienestar Total* – $75.000\n\n"
                    "¿Te gustaría agendar una cita?"
                )
                return Response(str(resp), media_type="application/xml")

            resp.message(
                "Hola 😊\nAquí tienes la información de los paquetes:\n\n"
                "• *Cuidado Esencial* – $45.000\n"
                "• *Salud Activa* – $60.000\n"
                "• *Bienestar Total* – $75.000\n\n"
                "¿Cuál te interesa?"
            )
            return Response(str(resp), media_type="application/xml")

        # 🔍 detect package INSTANTLY (ONLY if not asking price)
        pkg = detect_package(msg)
        if pkg:
            memory["package"] = pkg
            memory["waiting_for_confirmation"] = True
            resp.message(
                f"Hola 😊 Claro, ese corresponde al *{pkg}*.\n"
                "¿Te gustaría agendar una cita?"
            )
            return Response(str(resp), media_type="application/xml")

        # 🔍 strong booking intent
        strong_booking = [
        "examen", "exmanes", "examenes", "exam", 
        "escolar", "escolares",
        "colegio", "cole",
        "matricula", "matrícula",
        "para mi hijo", "para mi hija",
        "urgente",
        "cupo", "hay cupo"
        ]

        if any(k in msg for k in strong_booking):
            memory["awaiting_info"] = True
            resp.message(
                "Hola 😊\nClaro, te ayudo con eso.\n"
                "Para agendar necesito estos datos:\n"
                "• Nombre del estudiante\n"
                "• Colegio\n"
                "• Fecha y hora\n"
                "• Paquete que deseas"
            )
            return Response(str(resp), media_type="application/xml")

        # 🔍 greetings
        if any(g in msg for g in ["hola", "ola", "buenas", "buen día"]):
            resp.message("Hola 👋 ¿En qué puedo ayudarte?")
            return Response(str(resp), media_type="application/xml")

        resp.message("Hola 👋 ¿En qué puedo ayudarte?")
        return Response(str(resp), media_type="application/xml")

    # -----------------------------------------------------
    # 3. USER CONFIRMS AFTER PACKAGE DETECTION
    # -----------------------------------------------------
    if memory["waiting_for_confirmation"]:
        yes_words = ["si", "sí", "claro", "dale", "ok", "listo", "quiero", "hagale", "hágale"]
        no_words = ["no", "nel", "ahora no", "más tarde", "mas tarde"]

        if any(w in msg for w in yes_words):
            memory["waiting_for_confirmation"] = False
            memory["awaiting_info"] = True
            resp.message(
                "Perfecto 😊\nPara agendar necesito:\n"
                "• Nombre del estudiante\n"
                "• Colegio\n"
                "• Fecha y hora deseada"
            )
            return Response(str(resp), media_type="application/xml")

        if any(w in msg for w in no_words):
            memory["waiting_for_confirmation"] = False
            resp.message("Perfecto 😊 Si deseas agendar luego, estaré aquí para ayudarte.")
            return Response(str(resp), media_type="application/xml")

        resp.message("¿Te gustaría agendar una cita?")
        return Response(str(resp), media_type="application/xml")

    # -----------------------------------------------------
    # 4. SECOND MESSAGE AND BEYOND → AI EXTRACTION
    # -----------------------------------------------------
    extracted = ai_extract(msg)

    if extracted.get("customer_name"):
        memory["customer_name"] = extracted["customer_name"]

    if extracted.get("school_name"):
        memory["school_name"] = extracted["school_name"]

    if extracted.get("datetime"):
        memory["datetime"] = extracted["datetime"]

    if extracted.get("package"):
        memory["package"] = extracted["package"]

    memory["party_size"] = "1"

    # 🔒 ALWAYS ask for the student name FIRST
    if not memory["customer_name"]:
       resp.message("¿Cuál es el nombre del estudiante?")
       return Response(str(resp), media_type="application/xml")

    # -----------------------------------------------------
    # 5. ASK FOR ANY MISSING FIELD
    # -----------------------------------------------------

    if not memory["school_name"]:
        resp.message("¿De qué colegio viene?")
        return Response(str(resp), media_type="application/xml")

    if not memory["datetime"]:
        resp.message("¿Para qué fecha y hora deseas la cita?")
        return Response(str(resp), media_type="application/xml")

    if not memory["package"]:
        resp.message(
            "¿Qué paquete deseas reservar?\n\n"
            "• *Cuidado Esencial* – $45.000\n"
            "• *Salud Activa* – $60.000\n"
            "• *Bienestar Total* – $75.000"
        )
        return Response(str(resp), media_type="application/xml")

    # -----------------------------------------------------
    # 6. SAVE RESERVATION
    # -----------------------------------------------------
    confirmation = save_reservation(memory)
    resp.message("Hola 😊\n" + confirmation)

    session_state[user_id] = {
        "customer_name": None,
        "school_name": None,
        "package": None,
        "datetime": None,
        "party_size": "1",
        "started": False,
        "awaiting_info": False,
        "waiting_for_confirmation": False
    }

    return Response(str(resp), media_type="application/xml")

  
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
