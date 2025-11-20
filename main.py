from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

from datetime import datetime, timezone, timedelta
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

LOCAL_TZ = ZoneInfo("America/Bogota")   # ALWAYS BOGOTA


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
# ---------------------------------------------------------
# SAVE RESERVATION — STORE DIRECTLY IN BOGOTÁ TIME
# ---------------------------------------------------------
def save_reservation(data: dict):
    try:
        raw_dt = datetime.fromisoformat(data["datetime"])

        # If no timezone, assume Bogotá
        if raw_dt.tzinfo is None:
            dt_local = raw_dt.replace(tzinfo=LOCAL_TZ)
        else:
            dt_local = raw_dt.astimezone(LOCAL_TZ)

        dt_store = dt_local  # local Bogotá time
        iso_to_store = dt_store.isoformat()

    except Exception as e:
        print("ERROR in save_reservation:", e)
        return "❌ Error procesando la fecha."

    # ---------------------------------------------------------
    # CORRECT FIX: respect dashboard table_number if provided
    # ---------------------------------------------------------
    if data.get("table_number"):
        table = data["table_number"]  # Dashboard
    else:
        table = assign_table(iso_to_store)  # WhatsApp auto assign

    if not table:
        return "❌ No hay mesas disponibles para ese horario."

    # INSERT CORRECTLY
    supabase.table("reservations").insert({
        "customer_name": data["customer_name"],
        "customer_email": "",
        "contact_phone": "",
        "datetime": iso_to_store,
        "party_size": int(data["party_size"]),
        "table_number": table,
        "notes": "",
        "status": "confirmado",
    }).execute()

    return (
        "✅ *¡Reserva confirmada!*\n"
        f"👤 {data['customer_name']}\n"
        f"👥 {data['party_size']} personas\n"
        f"🗓 {dt_store.strftime('%Y-%m-%d %H:%M')}\n"
        f"🍽 Mesa: {table}"
    )
    
# ---------------------------------------------------------
# AI EXTRACTION
# ---------------------------------------------------------
def ai_extract(user_msg: str):
    import dateparser
    from dateutil.relativedelta import relativedelta, MO, TU, WE, TH, FR, SA, SU

    today = datetime.now(LOCAL_TZ)

    weekday_map = {
        "lunes": MO, "martes": TU, "miércoles": WE, "miercoles": WE,
        "jueves": TH, "viernes": FR, "sábado": SA, "sabado": SA, "domingo": SU
    }

    prompt = f"""
Eres un extractor. NO conviertas fechas. NO cambies horas.
Devuelve estrictamente JSON:
{{
 "intent": "",
 "customer_name": "",
 "party_size": "",
 "datetime_text": ""
}}
Mensaje:
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
        return {"intent": "", "customer_name": "", "party_size": "", "datetime": ""}

    text = extracted.get("datetime_text", "").lower()
    final_iso = ""

    # Parse as Bogotá time
    dt_local = dateparser.parse(
        text,
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

    if msg.lower() in ["hola", "hello", "holaa", "buenas", "hey", "ola"]:
        resp.message("¡Hola! ¿Quieres información o deseas hacer una reserva?")
        return Response(str(resp), media_type="application/xml")

    extracted = ai_extract(msg)

    if extracted["intent"] == "reserve" and not memory["awaiting_info"]:
        memory["awaiting_info"] = True
        resp.message("Perfecto 😊 Dame fecha, nombre y número de personas.")
        return Response(str(resp), media_type="application/xml")

    if extracted.get("customer_name"):
        memory["customer_name"] = extracted["customer_name"]

    if extracted.get("datetime"):
        memory["datetime"] = extracted["datetime"]

    if extracted.get("party_size"):
        memory["party_size"] = extracted["party_size"]

    if not memory["customer_name"]:
        resp.message("¿A nombre de quién sería la reserva?")
        return Response(str(resp), media_type="application/xml")

    if not memory["datetime"]:
        resp.message("¿Para qué fecha y hora?")
        return Response(str(resp), media_type="application/xml")

    if not memory["party_size"]:
        resp.message("¿Para cuántas personas?")
        return Response(str(resp), media_type="application/xml")

    confirmation = save_reservation(memory)
    resp.message(confirmation)

    session_state[user_id] = {
        "customer_name": None,
        "datetime": None,
        "party_size": None,
        "awaiting_info": False
    }

    return Response(str(resp), media_type="application/xml")


# ---------------------------------------------------------
# DASHBOARD — ALWAYS SHOW BOGOTÁ TIME
# ---------------------------------------------------------
from dateutil import parser
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    res = supabase.table("reservations").select("*").order("datetime", desc=True).execute()
    rows = res.data or []

    fixed = []
    weekly_count = 0

    now_local = datetime.now(LOCAL_TZ)
    week_ago = now_local - timedelta(days=7)

    for r in rows:
        row = r.copy()
        iso = r.get("datetime")

        if iso:
            dt_utc = parser.isoparse(iso)
            dt_local = dt_utc.astimezone(LOCAL_TZ)

            row["date"] = dt_local.strftime("%Y-%m-%d")
            row["time"] = dt_local.strftime("%H:%M")

            # Count reservations in the last 7 days
            if dt_local >= week_ago:
                weekly_count += 1
        else:
            row["date"] = "-"
            row["time"] = "-"

        fixed.append(row)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "reservations": fixed,
        "weekly_count": weekly_count  # <<---- THIS FIXES “ESTA SEMANA”
    })

# ---------------------------------------------------------
# SAFE UPDATE
# ---------------------------------------------------------
def safe_update(reservation_id: int, fields: dict):
    clean = {k: v for k, v in fields.items() if v not in [None, "", "null", "-", "None"]}
    if clean:
        supabase.table("reservations").update(clean).eq("reservation_id", reservation_id).execute()
        
# ---------------------------------------------------------
# ACTION BUTTON ROUTES — EXACTLY LIKE BACKUP (WORKING)
# ---------------------------------------------------------

@app.post("/cancelReservation")
async def cancel_reservation(update: dict):
    supabase.table("reservations") \
        .update({"status": "cancelled"}) \
        .eq("reservation_id", update["reservation_id"]) \
        .execute()
    return {"success": True}

@app.post("/markArrived")
async def mark_arrived(update: dict):
    supabase.table("reservations") \
        .update({"status": "arrived"}) \
        .eq("reservation_id", update["reservation_id"]) \
        .execute()
    return {"success": True}

@app.post("/markNoShow")
async def mark_no_show(update: dict):
    supabase.table("reservations") \
        .update({"status": "no_show"}) \
        .eq("reservation_id", update["reservation_id"]) \
        .execute()
    return {"success": True}

@app.post("/archiveReservation")
async def archive_reservation(update: dict):
    supabase.table("reservations") \
        .update({"status": "archived"}) \
        .eq("reservation_id", update["reservation_id"]) \
        .execute()
    return {"success": True}

# ---------------------------------------------------------
# UPDATE RESERVATION — EXACT WORKING VERSION
@app.post("/updateReservation")
async def update_reservation(update: dict):

    reservation_id = update.get("reservation_id")
    if not reservation_id:
        return {"success": False, "error": "Missing reservation_id"}

    fields = {}

    if update.get("datetime"):
        fields["datetime"] = update["datetime"]

    if update.get("party_size"):
        fields["party_size"] = update["party_size"]

    if update.get("table_number"):
        fields["table_number"] = update["table_number"]

    if update.get("notes") is not None:
        fields["notes"] = update["notes"]

    # ALWAYS update status (actions like Cancelar, Llegó)
    if update.get("status"):
        fields["status"] = update["status"]

    if fields:
        supabase.table("reservations") \
            .update(fields) \
            .eq("reservation_id", reservation_id) \
            .execute()

    return {"success": True}
    
@app.post("/createReservation")
async def create_reservation(payload: dict):
    msg = save_reservation(payload)
    return {"success": True, "message": msg}


# ---------------------------------------------------------
# RUN
# ---------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
