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
# PACKAGE DETECTION (RULE-BASED)
# ---------------------------------------------------------
def detect_package(msg: str):
    msg = msg.lower().strip()

    # names
    if "cuidado esencial" in msg or "esencial" in msg or "kit escolar" in msg:
        return "Paquete Cuidado Esencial"
    if "salud activa" in msg or "activa" in msg:
        return "Paquete Salud Activa"
    if "bienestar total" in msg or "total" in msg or "completo" in msg:
        return "Paquete Bienestar Total"

    # price
    if "45" in msg or "45k" in msg or "45 mil" in msg:
        return "Paquete Cuidado Esencial"
    if "60" in msg or "60k" in msg or "60 mil" in msg:
        return "Paquete Salud Activa"
    if "75" in msg or "75k" in msg or "75 mil" in msg:
        return "Paquete Bienestar Total"

    # exam type
    if "odont" in msg:
        return "Paquete Bienestar Total"
    if "psico" in msg:
        return "Paquete Salud Activa"
    if "audio" in msg or "optometr" in msg or "medicina" in msg:
        return "Paquete Cuidado Esencial"

    # colors
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
    table = data.get("table_number") or assign_table(iso_to_store)
    if not table:
        return "❌ No hay mesas disponibles para ese horario."

    supabase.table("reservations").insert({
        "customer_name": data["customer_name"],
        "customer_email": "",
        "contact_phone": "",
        "datetime": iso_to_store,
        "party_size": int(data["party_size"]),
        "table_number": table,
        "notes": "",
        "status": "confirmado",
        "business_id": 2,
        "package": data.get("package", ""),
        "school_name": data.get("school_name", ""),
    }).execute()

    return "ok"


# ---------------------------------------------------------
# SMART AI BRAIN (GPT-4o)
# ---------------------------------------------------------
def smart_ai_brain(memory, user_msg):

    system_prompt = """
Eres un asistente de WhatsApp para un IPS que realiza exámenes escolares.

TU TRABAJO:
1. Extrae:
   - customer_name
   - school_name
   - datetime
   - package

2. Si falta algo → pide SOLO lo que falta.
3. Si está todo → responde EXACTAMENTE así:

Hola 😊
✅ ¡Reserva confirmada!
👤 {customer_name}
👥 1 estudiantes
📦 *{package}*
🏫 {school_name}
🗓 {datetime}

4. No inventes nada. Solo usa info del usuario.
5. Usa español colombiano natural.

Formato de retorno OBLIGATORIO:

{
 "fields": {
   "customer_name": "",
   "school_name": "",
   "datetime": "",
   "package": ""
 },
 "missing": [],
 "reply": ""
}
"""

    r = client.chat.completions.create(
        model="gpt-4o",
        temperature=0,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps({
                    "memory": memory,
                    "message": user_msg
                })
            }
        ]
    )

    try:
        return json.loads(r.choices[0].message.content)
    except:
        return {
            "fields": {},
            "missing": ["unknown"],
            "reply": "No entendí bien 🧐 ¿me lo repites porfa?"
        }


# ---------------------------------------------------------
# WHATSAPP HANDLER (AI POWERED + PRICE INFO)
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp(Body: str = Form(...)):
    resp = MessagingResponse()
    msg_raw = Body.strip()
    msg_lower = msg_raw.lower()
    user_id = "default"

    # -----------------------------------------------------
    # 0. RESET MEMORY
    # -----------------------------------------------------
    if msg_lower in ["reset", "reiniciar", "nuevo", "borrar"]:
        session_state[user_id] = {
            "customer_name": None,
            "school_name": None,
            "package": None,
            "datetime": None,
            "party_size": "1",
            "started": False
        }
        resp.message("🔄 Memoria reiniciada.\n\nPuedes empezar de nuevo 😊")
        return Response(str(resp), media_type="application/xml")

    # -----------------------------------------------------
    # 1. INIT MEMORY
    # -----------------------------------------------------
    if user_id not in session_state:
        session_state[user_id] = {
            "customer_name": None,
            "school_name": None,
            "package": None,
            "datetime": None,
            "party_size": "1",
            "started": False
        }

    memory = session_state[user_id]

    # -----------------------------------------------------
    # 2. FIRST MESSAGE ALWAYS GREETS
    # -----------------------------------------------------
    if not memory["started"]:
        memory["started"] = True
        resp.message("Hola 👋 ¿En qué puedo ayudarte?")
        return Response(str(resp), media_type="application/xml")

    # -----------------------------------------------------
    # 3. PRICE QUESTIONS (natural handling)
    # -----------------------------------------------------
    price_words = ["cuánto", "cuanto", "precio", "vale", "cuesta", "coste", "valor"]

    if any(w in msg_lower for w in price_words):
        pkg = detect_package(msg_lower)

        # If user mentioned a package → return ONLY that price
        if pkg:
            price_map = {
                "Paquete Cuidado Esencial": "$45.000",
                "Paquete Salud Activa": "$60.000",
                "Paquete Bienestar Total": "$75.000"
            }

            resp.message(
                f"Claro 😊\nEl *{pkg}* tiene un valor de **{price_map[pkg]}**.\n\n"
                "¿Te gustaría agendar una cita?"
            )
            return Response(str(resp), media_type="application/xml")

        # If no package detected → send price list
        resp.message(
            "Claro 😊 Aquí tienes los precios:\n\n"
            "• *Cuidado Esencial* – $45.000\n"
            "• *Salud Activa* – $60.000\n"
            "• *Bienestar Total* – $75.000\n\n"
            "¿Cuál te interesa?"
        )
        return Response(str(resp), media_type="application/xml")

    # -----------------------------------------------------
    # 4. AI MAGIC (smart brain)
    # -----------------------------------------------------
    ai_result = smart_ai_brain(memory, msg_raw)

    fields = ai_result.get("fields", {})
    missing = ai_result.get("missing", [])
    reply = ai_result.get("reply", "")

    # Update memory
    if fields.get("customer_name"):
        memory["customer_name"] = fields["customer_name"]
    if fields.get("school_name"):
        memory["school_name"] = fields["school_name"]
    if fields.get("datetime"):
        memory["datetime"] = fields["datetime"]
    if fields.get("package"):
        memory["package"] = fields["package"]

    memory["party_size"] = "1"

    # -----------------------------------------------------
    # 5. IF SOMETHING IS MISSING → ASK FOR THAT
    # -----------------------------------------------------
    if missing:
        resp.message(reply)
        return Response(str(resp), media_type="application/xml")

    # -----------------------------------------------------
    # 6. IF COMPLETE → SAVE RESERVATION
    # -----------------------------------------------------
    if memory["customer_name"] and memory["school_name"] and memory["datetime"] and memory["package"]:
        dt_display = memory["datetime"].replace("T", " ")[:16]

        confirm_msg = f"""
Hola 😊
✅ ¡Reserva confirmada!
👤 {memory['customer_name']}
👥 1 estudiantes
📦 *{memory['package']}*
🏫 {memory['school_name']}
🗓 {dt_display}
"""

        save_reservation(memory)
        resp.message(confirm_msg)

        session_state[user_id] = {
            "customer_name": None,
            "school_name": None,
            "package": None,
            "datetime": None,
            "party_size": "1",
            "started": False
        }

        return Response(str(resp), media_type="application/xml")

    # -----------------------------------------------------
    # 7. SAFETY FALLBACK
    # -----------------------------------------------------
    resp.message("No entendí bien, ¿me confirmas porfa?")
    return Response(str(resp), media_type="application/xml")



# ---------------------------------------------------------
# DASHBOARD
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
# UPDATE & ACTIONS
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
        "table_number": None
    })
    return {"success": True}


# ---------------------------------------------------------
# RUN
# ---------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
