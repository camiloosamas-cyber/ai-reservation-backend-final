from fastapi import FastAPI, Request, Form
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


# ---------------------------------------------------------
# SPANISH DATE FORMAT
# ---------------------------------------------------------
spanish_weekdays = {
    "Monday": "lunes",
    "Tuesday": "martes",
    "Wednesday": "miércoles",
    "Thursday": "jueves",
    "Friday": "viernes",
    "Saturday": "sábado",
    "Sunday": "domingo",
}
spanish_months = {
    "January": "enero",
    "February": "febrero",
    "March": "marzo",
    "April": "abril",
    "May": "mayo",
    "June": "junio",
    "July": "julio",
    "August": "agosto",
    "September": "septiembre",
    "October": "octubre",
    "November": "noviembre",
    "December": "diciembre"
}

def spanish_date(dt: datetime):
    wd = spanish_weekdays[dt.strftime("%A")]
    month = spanish_months[dt.strftime("%B")]
    return f"{wd} {dt.day} de {month}, {dt.strftime('%I:%M %p')}"


# ---------------------------------------------------------
# SAVE RESERVATION
# ---------------------------------------------------------
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
        f"🗓 {spanish_date(dt_local)}\n"
        f"🍽 Mesa: {table}"
    )


# ---------------------------------------------------------
# AI EXTRACTION — CORRECT DATE LOGIC
# ---------------------------------------------------------
def ai_extract(user_msg: str):
    from dateutil.relativedelta import relativedelta, MO, TU, WE, TH, FR, SA, SU
    import dateparser

    today = datetime.now(LOCAL_TZ)

    weekday_map = {
        "lunes": MO,
        "martes": TU,
        "miércoles": WE,
        "miercoles": WE,
        "jueves": TH,
        "viernes": FR,
        "sábado": SA,
        "sabado": SA,
        "domingo": SU
    }

    superprompt = f"""
Eres un extractor de datos. NO conviertas fechas.
Devuelve:
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
            messages=[{"role": "system", "content": superprompt}]
        )
        extracted = json.loads(r.choices[0].message.content)
    except:
        return {"intent": "", "customer_name": "", "party_size": "", "datetime": ""}

    text = extracted.get("datetime_text", "").lower()
    final_iso = ""

    detected_weekday = None
    for name, rr in weekday_map.items():
        if name in text:
            detected_weekday = rr
            break

    import dateparser

    if detected_weekday:
        target_date = today + relativedelta(weekday=detected_weekday(+1))

        tparsed = dateparser.parse(
            text,
            settings={"RETURN_AS_TIMEZONE_AWARE": True, "TIMEZONE": "America/Bogota"}
        )

        if tparsed:
            target_date = target_date.replace(hour=tparsed.hour, minute=tparsed.minute)

        final_iso = target_date.isoformat()

    else:
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
        resp.message("¡Hola! 😊 ¿En qué puedo ayudarte hoy?\n¿Quieres *información* o deseas *hacer una reserva*?")
        return Response(str(resp), media_type="application/xml")

    extracted = ai_extract(msg)

    if extracted["intent"] == "reserve" and not memory["awaiting_info"]:
        memory["awaiting_info"] = True
        resp.message("Perfecto 😊 Para continuar necesito:\n👉 Fecha y hora\n👉 Nombre\n👉 Número de personas")
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
        resp.message("¿Para qué fecha y hora deseas la reserva?")
        return Response(str(resp), media_type="application/xml")

    if not memory["party_size"]:
        resp.message("¿Para cuántas personas sería la reserva?")
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
# DASHBOARD — DATE/TIME FIX
# ---------------------------------------------------------
from dateutil import parser

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    res = supabase.table("reservations").select("*").order("datetime", desc=True).execute()
    rows = res.data or []

    fixed = []

    for r in rows:
        row = r.copy()
        dt_value = r.get("datetime")

        try:
            if not dt_value:
                row["date"] = "-"
                row["time"] = "-"
            else:
                dt_utc = parser.isoparse(dt_value)
                if dt_utc.tzinfo is None:
                    dt_utc = dt_utc.replace(tzinfo=timezone.utc)

                dt_local = dt_utc.astimezone(LOCAL_TZ)

                row["date"] = dt_local.strftime("%Y-%m-%d")
                row["time"] = dt_local.strftime("%H:%M")

        except:
            row["date"] = "-"
            row["time"] = "-"

        fixed.append(row)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "reservations": fixed
    })


# ---------------------------------------------------------
# SAFE DASHBOARD ACTION BUTTONS — FIXED
# ---------------------------------------------------------

def safe_update(reservation_id: int, fields: dict):
    clean = {k: v for k, v in fields.items() if v not in [None, "null", "", "-", "None"]}
    if clean:
        supabase.table("reservations").update(clean).eq("reservation_id", reservation_id).execute()


@app.post("/cancelReservation")
async def cancel_reservation(update: dict):
    safe_update(update["reservation_id"], {"status": "cancelado"})
    return {"success": True}


@app.post("/markArrived")
async def mark_arrived(update: dict):
    safe_update(update["reservation_id"], {"status": "llegó"})
    return {"success": True}


@app.post("/markNoShow")
async def mark_no_show(update: dict):
    safe_update(update["reservation_id"], {"status": "no llegó"})
    return {"success": True}


@app.post("/archiveReservation")
async def archive_reservation(update: dict):
    safe_update(update["reservation_id"], {"status": "archivado"})
    return {"success": True}


@app.post("/updateReservation")
async def update_reservation(update: dict):
    reservation_id = update["reservation_id"]

    safe_update(reservation_id, {
        "customer_name": update.get("customer_name"),
        "party_size": update.get("party_size"),
        "datetime": update.get("datetime"),
        "notes": update.get("notes"),
        "table_number": update.get("table_number"),
        "status": update.get("status")
    })

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
