from fastapi import FastAPI, Request, WebSocket, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import json, os

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


def assign_table(iso_utc: str):
    booked = supabase.table("reservations").select("table_number").eq("datetime", iso_utc).execute()
    taken = {r["table_number"] for r in (booked.data or [])}
    for i in range(1, TABLE_LIMIT + 1):
        t = f"T{i}"
        if t not in taken:
            return t
    return None


def save_reservation(data: dict):
    try:
        dt_local = datetime.fromisoformat(data["datetime"])
        dt_utc = dt_local.astimezone(timezone.utc)
    except:
        return "❌ No pude procesar fecha/hora."

    iso_utc = dt_utc.isoformat().replace("+00:00", "Z")
    table = assign_table(iso_utc)
    if not table:
        return "❌ No hay mesas disponibles para ese horario."

    supabase.table("reservations").insert({
        "customer_name": data["customer_name"],
        "customer_email": "",
        "contact_phone": "",
        "datetime": iso_utc,
        "party_size": int(data["party_size"]),
        "table_number": table,
        "notes": "",
        "status": "confirmado",
    }).execute()

    return (
        "✅ *¡Reserva confirmada!*\n"
        f"👤 {data['customer_name']}\n"
        f"👥 {data['party_size']} personas\n"
        f"🗓 {dt_local.strftime('%A %d %B, %I:%M %p')}\n"
        f"🍽 Mesa: {table}"
    )
# ---------------------------------------------------------
# SUPER AI EXTRACTION (REAL PYTHON DATE CALCULATION)
# ---------------------------------------------------------
def ai_extract(user_msg: str):
    today = datetime.now(LOCAL_TZ)

    # Step 1: Get raw AI extraction (name, party size, intent, text for datetime)
    superprompt = f"""
Eres un extractor de datos. No conviertas fechas.
Solamente identifica si el usuario mencionó:

- intent: "reserve", "info" o "other"
- customer_name
- party_size
- datetime_text: exactamente el texto que el usuario dijo sobre fecha/hora (sin convertirlo)

Ejemplo de salida:
{{
 "intent": "reserve",
 "customer_name": "Marcos",
 "party_size": "4",
 "datetime_text": "viernes a las 5am"
}}

NUNCA conviertas fechas. SOLO devuelve el texto.
Mensaje:
\"\"\"{user_msg}\"\"\"
"""

    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[{"role": "system", "content": superprompt}]
        )
        extracted = json.loads(r.choices[0].message.content)
    except:
        return {"intent": "", "customer_name": "", "datetime": "", "party_size": ""}

    # ---------------------------
    # Step 2: REAL DATE CONVERSION IN PYTHON
    # ---------------------------
    datetime_text = extracted.get("datetime_text", "").lower().strip()
    final_iso = ""

    if datetime_text:
        # Python natural language parsing (SAFE)
        import dateparser

        dt_local = dateparser.parse(
            datetime_text,
            settings={
                "PREFER_DATES_FROM": "future",
                "TIMEZONE": "America/Bogota",
                "RETURN_AS_TIMEZONE_AWARE": True
            }
        )

        if dt_local:
            final_iso = dt_local.isoformat()

    return {
        "intent": extracted.get("intent", ""),
        "customer_name": extracted.get("customer_name", ""),
        "party_size": extracted.get("party_size", ""),
        "datetime": final_iso
    }
# ---------------------------------------------------------
# WHATSAPP ROUTE
# ---------------------------------------------------------
# ---------------------------------------------------------
# WHATSAPP ROUTE (FIXED NAME MEMORY + FLOW)
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp(Body: str = Form(...)):
    resp = MessagingResponse()
    msg = Body.strip()

    user_id = "default_user"

    if user_id not in session_state:
        session_state[user_id] = {
            "customer_name": None,
            "datetime": None,
            "party_size": None,
            "awaiting_info": False
        }

    memory = session_state[user_id]

    # GREETING
    if msg.lower() in ["hola", "hello", "holaa", "buenas", "hey", "ola"]:
        resp.message("¡Hola! 😊 ¿En qué puedo ayudarte hoy?\n¿Quieres *información* o deseas *hacer una reserva*?")
        return Response(str(resp), media_type="application/xml")

    # AI INTERPRETATION
    extracted = ai_extract(msg)

    # START RESERVATION PROCESS
    if extracted["intent"] == "reserve" and not memory["awaiting_info"]:
        memory["awaiting_info"] = True
        resp.message("Perfecto 😊 Para continuar necesito:\n👉 Fecha y hora\n👉 Nombre\n👉 Número de personas")
        return Response(str(resp), media_type="application/xml")

    # UPDATE MEMORY SAFELY
    if extracted.get("customer_name"):
        memory["customer_name"] = extracted["customer_name"]

    if extracted.get("datetime"):
        memory["datetime"] = extracted["datetime"]

    if extracted.get("party_size"):
        memory["party_size"] = extracted["party_size"]

    # ASK MISSING FIELDS
    if not memory["customer_name"]:
        resp.message("¿A nombre de quién sería la reserva?")
        return Response(str(resp), media_type="application/xml")

    if not memory["datetime"]:
        resp.message("¿Para qué fecha y hora deseas la reserva?")
        return Response(str(resp), media_type="application/xml")

    if not memory["party_size"]:
        resp.message("¿Para cuántas personas sería la reserva?")
        return Response(str(resp), media_type="application/xml")

    # ALL INFO READY → SAVE
    confirmation = save_reservation(memory)
    resp.message(confirmation)

    # RESET MEMORY FOR NEXT RESERVATION
    session_state[user_id] = {
        "customer_name": None,
        "datetime": None,
        "party_size": None,
        "awaiting_info": False
    }

    return Response(str(resp), media_type="application/xml")
# ---------------------------------------------------------
# DASHBOARD (FIXED TIMEZONE + BLANK DATE BUG)
# ---------------------------------------------------------
from dateutil import parser

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    res = supabase.table("reservations").select("*").order("datetime", desc=True).execute()
    rows = res.data or []

    fixed_rows = []

    for r in rows:
        row = r.copy()
        dt_value = r.get("datetime")

        try:
            # Skip invalid values
            if not dt_value or dt_value in ["EMPTY", "None", "-", ""]:
                row["datetime"] = ""
            else:
                # Parse UTC datetime
                dt_utc = parser.isoparse(dt_value)

                if dt_utc.tzinfo is None:
                    dt_utc = dt_utc.replace(tzinfo=timezone.utc)

                # Convert to Colombia time
                dt_local = dt_utc.astimezone(LOCAL_TZ)

                # Format clean for dashboard
                row["datetime"] = dt_local.strftime("%Y-%m-%d %H:%M")

        except Exception as e:
            # If anything fails, show empty
            row["datetime"] = ""

        fixed_rows.append(row)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "reservations": fixed_rows
    })
# ---------------------------------------------------------
# API FOR DASHBOARD ACTIONS
# ---------------------------------------------------------

@app.post("/createReservation")
async def create_reservation(payload: dict):
    msg = save_reservation(payload)
    return {"success": True, "message": msg}


@app.post("/updateReservation")
async def update_reservation(update: dict):
    supabase.table("reservations").update({
        "customer_name": update.get("customer_name"),
        "party_size": update.get("party_size"),
        "datetime": update.get("datetime"),
        "notes": update.get("notes"),
        "table_number": update.get("table_number"),
        "status": update.get("status")
    }).eq("reservation_id", update["reservation_id"]).execute()

    return {"success": True}


@app.post("/cancelReservation")
async def cancel_reservation(update: dict):
    supabase.table("reservations").update({"status": "cancelado"}).eq("reservation_id", update["reservation_id"]).execute()
    return {"success": True}


@app.post("/markArrived")
async def mark_arrived(update: dict):
    supabase.table("reservations").update({"status": "llegó"}).eq("reservation_id", update["reservation_id"]).execute()
    return {"success": True}


@app.post("/markNoShow")
async def mark_no_show(update: dict):
    supabase.table("reservations").update({"status": "no llegó"}).eq("reservation_id", update["reservation_id"]).execute()
    return {"success": True}


@app.post("/archiveReservation")
async def archive_reservation(update: dict):
    supabase.table("reservations").update({"status": "archivado"}).eq("reservation_id", update["reservation_id"]).execute()
    return {"success": True}


# ---------------------------------------------------------
# RUN
# ---------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
