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

    # ---------------------------------------------------
    # 1. PACKAGE DETECTION (rule-based)
    # ---------------------------------------------------
    detected_package = detect_package(text)

    # ---------------------------------------------------
    # 2. SCHOOL NAME DETECTION (dataset-based)
    # ---------------------------------------------------
    school_name = ""
    school_keywords = [
        "colegio", "gimnasio", "gimnacio", "liceo", "instituto",
        "campestre", "la salle", "sagrado", "andres", "boston",
        "mayor", "presentación", "monseñor", "arces", "villegas",
        "sabiduría", "san josé", "la presentación", "los andes",
        "germán", "arciniegas"
    ]

    for kw in school_keywords:
        if kw in text:
            # Extract everything after the keyword
            part = text.split(kw, 1)[1].strip()
            # Keep the keyword + next 5 words
            school_name = kw + " " + " ".join(part.split()[:5])
            school_name = school_name.strip()
            break

    # ---------------------------------------------------
    # 3. PARTY SIZE DETECTION (rule-based)
    # ---------------------------------------------------
    party_size = ""
    number_patterns = [
        r"(\d+)\s*niñ", r"(\d+)\s*hij", r"(\d+)\s*person",
        r"(\d+)\s*estudiante", r"(\d+)\s*alumno", r"\bpara (\d+)"
    ]

    for pattern in number_patterns:
        m = re.search(pattern, text)
        if m:
            party_size = m.group(1)
            break

    # ---------------------------------------------------
    # 4. NAME DETECTION (very simplified)
    # ---------------------------------------------------
    customer_name = ""
    name_patterns = [
        r"mi hijo ([a-zA-Záéíóúñ ]+)",
        r"para ([a-zA-Záéíóúñ ]+)",
        r"nombre es ([a-zA-Záéíóúñ ]+)"
    ]

    for p in name_patterns:
        m = re.search(p, text)
        if m:
            candidate = m.group(1).strip()
            # keep 1–3 words only
            customer_name = " ".join(candidate.split()[:3])
            break

    # ---------------------------------------------------
    # 5. INTENT DETECTION
    # ---------------------------------------------------
    reserve_keywords = [
        "agendar", "reservar", "cita", "sita", "agenda",
        "sacar cita", "quiero cita", "necesito cita",
        "quiero agendar", "hacer exámenes", "exam", "examen"
    ]
    info_keywords = ["?", "cuánto", "vale", "incluye", "nequi", "precio"]

    if any(k in text for k in reserve_keywords):
        intent = "reserve"
    elif any(k in text for k in info_keywords):
        intent = "info"
    else:
        intent = "other"

    # ---------------------------------------------------
    # 6. DATE/TIME EXTRACTION — via LLM
    # ---------------------------------------------------
    prompt = f"""
Extrae SOLO fecha y hora del siguiente texto. 
No inventes nada. No corrijas nada.

Mensaje:
\"\"\"{user_msg}\"\"\"

Devuelve JSON:
{{
 "datetime_text": ""
}}
"""

    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[{"role": "system", "content": prompt}]
        )
        datetime_text = json.loads(r.choices[0].message.content).get("datetime_text", "")
    except:
        datetime_text = ""

    # Try parsing the datetime
    dt_local = dateparser.parse(
        datetime_text,
        settings={
            "PREFER_DATES_FROM": "future",
            "TIMEZONE": "America/Bogota",
            "RETURN_AS_TIMEZONE_AWARE": True
        }
    )
    final_iso = dt_local.isoformat() if dt_local else ""

    # ---------------------------------------------------
    # RETURN STRUCTURED DATA
    # ---------------------------------------------------
    return {
        "intent": intent,
        "customer_name": customer_name,
        "party_size": party_size,
        "datetime": final_iso,
        "package": detected_package,
        "school_name": school_name
    }

# ---------------------------------------------------------
# WHATSAPP HANDLER (FINAL FIXED VERSION)
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp(Body: str = Form(...)):
    resp = MessagingResponse()
    msg_raw = Body.strip()
    msg = msg_raw.lower()
    user_id = "default"

    # -----------------------------------------------------
    # 1. RESET MEMORY
    # -----------------------------------------------------
    if msg in ["reset", "reiniciar", "borrar", "nuevo"]:
        session_state[user_id] = {
            "customer_name": None,
            "datetime": None,
            "party_size": None,
            "school_name": None,
            "package": None,
            "awaiting_info": False,
            "started": False,
            "waiting_for_confirmation": False,
        }
        resp.message("🔄 Memoria reiniciada.\n\nPuedes empezar una conversación nueva 😊")
        return Response(str(resp), media_type="application/xml")

    # -----------------------------------------------------
    # 2. INIT MEMORY
    # -----------------------------------------------------
    if user_id not in session_state:
        session_state[user_id] = {
            "customer_name": None,
            "datetime": None,
            "party_size": None,
            "school_name": None,
            "package": None,
            "awaiting_info": False,
            "started": False,
            "waiting_for_confirmation": False,
        }

    memory = session_state[user_id]

    # -----------------------------------------------------
    # 3. FIRST MESSAGE LOGIC
    # -----------------------------------------------------
    if not memory["started"]:
        memory["started"] = True

        strong_booking = [
            "examen", "exámenes", "examenes", "escolar",
            "colegio", "matrícula", "matricula",
            "para mi hijo", "para mi hija", "urgente",
            "antes del", "antes de", "cupo", "hay cupo"
        ]

        # If they are clearly booking → ask for info directly
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

        # They ask about prices/info
        info_triggers = ["cuánto", "precio", "vale", "incluye", "?"]

        if any(k in msg for k in info_triggers):
            resp.message(
                "Hola 😊\nAquí tienes la información de los paquetes:\n\n"
                "• *Cuidado Esencial* – $45.000\n"
                "• *Salud Activa* – $60.000\n"
                "• *Bienestar Total* – $75.000\n\n"
                "¿Te gustaría agendar una cita?"
            )
            return Response(str(resp), media_type="application/xml")

        # They mention a package directly → ask if they want to book
        pkg = detect_package(msg)
        if pkg:
            memory["package"] = pkg
            memory["waiting_for_confirmation"] = True
            resp.message(
                f"Hola 😊 Claro, ese corresponde al *{pkg}*.\n"
                "¿Te gustaría agendar una cita?"
            )
            return Response(str(resp), media_type="application/xml")

        # Generic greeting
        greetings = ["hola", "ola", "buenas", "buen día", "buenas tardes", "buenas noches"]

        if any(g in msg for g in greetings):
            resp.message("Hola 👋 ¿En qué puedo ayudarte?")
            return Response(str(resp), media_type="application/xml")

        # Default fallback
        resp.message("Hola 👋 ¿En qué puedo ayudarte?")
        return Response(str(resp), media_type="application/xml")

    # -----------------------------------------------------
    # 4. USER CONFIRMS BOOKING AFTER PACKAGE DETECTION
    # -----------------------------------------------------
    if memory.get("waiting_for_confirmation"):
        # YES
        if any(word in msg for word in ["si", "sí", "claro", "dale", "ok", "listo", "quiero", "hágale", "hagale"]):
            memory["waiting_for_confirmation"] = False
            memory["awaiting_info"] = True
            resp.message(
                "Perfecto 😊\nPara agendar necesito:\n"
                "• Nombre del estudiante\n"
                "• Colegio\n"
                "• Fecha y hora deseada"
            )
            return Response(str(resp), media_type="application/xml")

        # NO
        if any(word in msg for word in ["no", "nel", "nahi", "ahora no", "más tarde", "mas tarde"]):
            memory["waiting_for_confirmation"] = False
            resp.message("Perfecto 😊 Si deseas agendar luego, estaré aquí para ayudarte.")
            return Response(str(resp), media_type="application/xml")

        # If unclear → ask again
        resp.message("¿Te gustaría agendar una cita?")
        return Response(str(resp), media_type="application/xml")

    # -----------------------------------------------------
    # 5. SECOND MESSAGE AND BEYOND → EXTRACT INFO
    # -----------------------------------------------------
    extracted = ai_extract(msg)

    if extracted.get("customer_name"):
        memory["customer_name"] = extracted["customer_name"]

    if extracted.get("school_name"):
        memory["school_name"] = extracted["school_name"]

    if extracted.get("datetime"):
        memory["datetime"] = extracted["datetime"]

    if extracted.get("party_size"):
        memory["party_size"] = extracted["party_size"]

    pkg = detect_package(msg)
    if pkg:
        memory["package"] = pkg

    # -----------------------------------------------------
    # 6. ASK FOR MISSING REQUIRED INFO
    # -----------------------------------------------------
    if not memory["customer_name"]:
        resp.message("¿Cuál es el nombre del estudiante?")
        return Response(str(resp), media_type="application/xml")

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

    # party_size default = 1 (IPS always one kid)
    if not memory["party_size"]:
        memory["party_size"] = "1"

    # -----------------------------------------------------
    # 7. CONFIRM RESERVATION
    # -----------------------------------------------------
    confirmation = save_reservation(memory)
    resp.message("Hola 😊\n" + confirmation)

    # Reset memory
    session_state[user_id] = {
        "customer_name": None,
        "datetime": None,
        "party_size": None,
        "school_name": None,
        "package": None,
        "awaiting_info": False,
        "started": False,
        "waiting_for_confirmation": False,
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
