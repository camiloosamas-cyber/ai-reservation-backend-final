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
def detect_package(user_msg: str):
    msg = user_msg.lower()

    if "esencial" in msg:
        return "Paquete Cuidado Esencial"
    if "activa" in msg:
        return "Paquete Salud Activa"
    if "total" in msg or "completo" in msg:
        return "Paquete Bienestar Total"

    # Detect by price
    if "45" in msg or "45." in msg or "45 mil" in msg:
        return "Paquete Cuidado Esencial"

    if "60" in msg or "60." in msg or "60 mil" in msg:
        return "Paquete Salud Activa"

    if "75" in msg or "75." in msg or "75 mil" in msg:
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
        f"👥 {data['party_size']} personas\n"
        f"📦 {data.get('package','')}\n"
        f"🏫 {data.get('school_name','')}\n"
        f"🗓 {dt_local.strftime('%Y-%m-%d %H:%M')}\n"
        f"🍽 Mesa: {table}"
    )


# ---------------------------------------------------------
# AI EXTRACTION  (PROMPT UPDATED EXACTLY AS REQUESTED)
# ---------------------------------------------------------
def ai_extract(user_msg: str):
    import dateparser

    PACKAGES = {
        "cuidado esencial": "Paquete Cuidado Esencial",
        "salud activa": "Paquete Salud Activa",
        "bienestar total": "Paquete Bienestar Total",
    }

    # Detect package
    detected_package = ""
    lower_msg = user_msg.lower()
    for key, full in PACKAGES.items():
        if key in lower_msg:
            detected_package = full
            break

    # Detect school name (simple extraction)
    school_name = ""
    if "colegio" in lower_msg:
        try:
            # everything after "colegio"
            school_name = user_msg.lower().split("colegio", 1)[1].strip()
        except:
            pass

    # NEW STRONG EXTRACTOR PROMPT
    prompt = f"""
Eres un extractor de intención para un sistema de reservas médicas escolares.

NO cambies fechas.
NO conviertas horas.
NO inventes datos.

Devuelve SIEMPRE este JSON:
{{
 "intent": "reserve" | "info" | "other",
 "customer_name": "",
 "party_size": "",
 "datetime_text": ""
}}

REGLAS:
- Si el usuario dice palabras como "agendar", "reservar", "quiero una cita", "quiero agenda", "quiero reservar", INTENT = "reserve".
- Si solo hace preguntas, INTENT = "info".
- En cualquier otro caso, INTENT = "other".
- "customer_name": nombre de la persona si aparece.
- "datetime_text": la parte del texto que representa fecha u hora.
- "party_size": número de personas si está claro, de lo contrario vacío.

EXTRACTA del mensaje:
\"\"\"{user_msg}\"\"\"
"""

    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[{"role": "system", "content": prompt}]
        )
        extracted = json.loads(r.choices[0].message.content)
    except:
        extracted = {"intent": "", "customer_name": "", "party_size": "", "datetime_text": ""}

    # Parse date
    text = extracted.get("datetime_text", "").lower()
    dt_local = dateparser.parse(
        text,
        settings={
            "PREFER_DATES_FROM": "future",
            "TIMEZONE": "America/Bogota",
            "RETURN_AS_TIMEZONE_AWARE": True
        }
    )

    final_iso = dt_local.isoformat() if dt_local else ""

    return {
        "intent": extracted.get("intent", ""),
        "customer_name": extracted.get("customer_name", ""),
        "party_size": extracted.get("party_size", ""),
        "datetime": final_iso,
        "package": detected_package,
        "school_name": school_name
    }


# ---------------------------------------------------------
# WHATSAPP HANDLER
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp(Body: str = Form(...)):
    resp = MessagingResponse()
    msg = Body.strip().lower()
    user_id = "default"

    if user_id not in session_state:
        session_state[user_id] = {
            "customer_name": None,
            "datetime": None,
            "party_size": None,
            "school_name": None,
            "package": None,
            "awaiting_info": False,
        }

    memory = session_state[user_id]
    extracted = ai_extract(msg)

    # Intent
    if extracted.get("intent") == "reserve" and not memory["awaiting_info"]:
        memory["awaiting_info"] = True
        resp.message(
            "Perfecto 😊\n\nPor favor envíame:\n• Nombre del estudiante\n• Colegio\n• Fecha y hora\n• Número de personas\n• Paquete deseado"
        )
        return Response(str(resp), media_type="application/xml")

    # Fill memory
    if extracted.get("customer_name"):
        memory["customer_name"] = extracted["customer_name"]

    if extracted.get("datetime"):
        memory["datetime"] = extracted["datetime"]

    if extracted.get("party_size"):
        memory["party_size"] = extracted["party_size"]

    if extracted.get("school_name"):
        memory["school_name"] = extracted["school_name"]

    # Package detection
    pkg = detect_package(msg)
    if pkg:
        memory["package"] = pkg

    # Ask missing fields
    if not memory["customer_name"]:
        resp.message("¿Cuál es el nombre del estudiante?")
        return Response(str(resp), media_type="application/xml")

    if not memory["school_name"]:
        resp.message("¿De qué colegio viene?")
        return Response(str(resp), media_type="application/xml")

    if not memory["datetime"]:
        resp.message("¿Para qué fecha y hora deseas la cita?")
        return Response(str(resp), media_type="application/xml")

    if not memory["party_size"]:
        resp.message("¿Para cuántas personas?")
        return Response(str(resp), media_type="application/xml")

    if not memory["package"]:
        resp.message(
            "¿Qué paquete deseas reservar?\n\n"
            "• *Cuidado Esencial* – $45.000\n"
            "• *Salud Activa* – $60.000\n"
            "• *Bienestar Total* – $75.000"
        )
        return Response(str(resp), media_type="application/xml")

    # Save reservation
    confirmation = save_reservation(memory)
    resp.message(confirmation)

    # Reset
    session_state[user_id] = {
        "customer_name": None,
        "datetime": None,
        "party_size": None,
        "school_name": None,
        "package": None,
        "awaiting_info": False,
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
# RUN
# ---------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
