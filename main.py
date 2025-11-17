from fastapi import FastAPI, Request, WebSocket, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import json, os, time, asyncio
import dateparser

# ---------- Supabase ----------
from supabase import create_client, Client

# ---------- OpenAI ----------
from openai import OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ---------- Twilio ----------
from twilio.twiml.messaging_response import MessagingResponse


# ---------------------------------------------------------
# APP INIT
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

# ---------------------------------------------------------
# MEMORY (per user via phone)
# ---------------------------------------------------------
session_state = {}  # stores current reservations + memory
LOCAL_TZ = ZoneInfo(os.getenv("LOCAL_TZ", "America/Bogota"))


# ---------------------------------------------------------
# DATE HELPERS
# ---------------------------------------------------------
def parse_to_utc(text: str):
    try:
        dt = dateparser.parse(
            text,
            settings={
                "TIMEZONE": "America/Bogota",
                "RETURN_AS_TIMEZONE_AWARE": True,
                "TO_TIMEZONE": "UTC"
            }
        )
        if not dt:
            return None
        return dt.astimezone(timezone.utc)
    except:
        return None


def utc_to_local(utc_dt: datetime):
    return utc_dt.astimezone(LOCAL_TZ)


def readable(dt_utc: datetime):
    dt_local = utc_to_local(dt_utc)
    return dt_local.strftime("%A %d %B, %I:%M %p")


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
# SAVE RESERVATION
# ---------------------------------------------------------
def save_reservation(data: dict):
    dt_utc = parse_to_utc(data["datetime"])
    if not dt_utc:
        return "❌ No pude entender la fecha/hora."

    iso = dt_utc.isoformat().replace("+00:00", "Z")
    table = assign_table(iso)
    if not table:
        return "❌ No hay mesas disponibles para ese horario."

    supabase.table("reservations").insert({
        "customer_name": data["customer_name"],
        "customer_email": "",
        "contact_phone": "",
        "datetime": iso,
        "party_size": int(data["party_size"]),
        "table_number": table,
        "notes": data.get("notes", ""),
        "status": "confirmed",
    }).execute()

    return (
        "✅ *¡Reserva confirmada!*\n"
        f"👤 {data['customer_name']}\n"
        f"👥 {data['party_size']} personas\n"
        f"🗓 {readable(dt_utc)}\n"
        f"🍽 Mesa: {table}"
    )


# ---------------------------------------------------------
# WHATSAPP ROUTE — NATURAL AI EXTRACTION + MEMORY
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp(Body: str = Form(...)):
    resp = MessagingResponse()
    user_id = "default_user"   # later: real phone number

    text = Body.strip().lower()

    # Init session
    if user_id not in session_state:
        session_state[user_id] = {
            "data": {
                "customer_name": None,
                "datetime": None,
                "party_size": None,
                "notes": None
            },
            "reservation_expires": None
        }

    state = session_state[user_id]
    data = state["data"]

    # ---------- RESET MEMORY IF RESERVATION TIME PASSED ----------
    if state["reservation_expires"]:
        if datetime.now(LOCAL_TZ) > state["reservation_expires"]:
            session_state[user_id] = {
                "data": {
                    "customer_name": None,
                    "datetime": None,
                    "party_size": None,
                    "notes": None
                },
                "reservation_expires": None
            }
            data = session_state[user_id]["data"]

    # ---------- GREETING ----------
    if any(g in text for g in ["hola", "buenas", "hey"]) and not data["customer_name"]:
        resp.message("¡Hola! 😊 ¿Quieres hacer una reserva?")
        return Response(str(resp), media_type="application/xml")

    # ---------- AI EXTRACTION ----------
    extraction_prompt = f"""
Extrae SOLO los datos de reserva.

Datos actuales:
{json.dumps(data, ensure_ascii=False)}

Mensaje:
"{Body}"

Reglas:
- NO elimines datos ya conocidos.
- Si detectas que el usuario cambia algo (hora, día, nombre, personas), ACTUALIZA ese campo.
- Si faltan datos: responde {{ "ask": "pregunta" }}
- Si ya tenemos nombre, fecha+hora y personas: responde {{ "complete": true }}

Responde SOLO JSON:
{{
 "customer_name": "",
 "datetime": "",
 "party_size": "",
 "notes": ""
}}
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[{"role": "system", "content": extraction_prompt}]
        )
        extracted = json.loads(response.choices[0].message.content.strip())
    except:
        resp.message("❌ No entendí eso. ¿Podrías decirlo de nuevo?")
        return Response(str(resp), media_type="application/xml")

    # ---------- UPDATE MEMORY ----------
    for key in data.keys():
        if key in extracted and extracted[key]:
            data[key] = extracted[key]

    # ---------- ASK FOR MISSING ----------
    if "ask" in extracted:
        resp.message(extracted["ask"])
        return Response(str(resp), media_type="application/xml")

    # ---------- CHECK IF ALL REQUIRED FIELDS EXIST ----------
    if data["customer_name"] and data["datetime"] and data["party_size"]:

        # parse date to check expiration time
        dt_utc = parse_to_utc(data["datetime"])
        if dt_utc:
            dt_local = utc_to_local(dt_utc)
            state["reservation_expires"] = dt_local + timedelta(hours=1)

        msg = save_reservation(data)
        resp.message(msg)

        return Response(str(resp), media_type="application/xml")

    # STILL MISSING SOME FIELD
    resp.message("Perfecto 😊 ¿Qué más te gustaría agregar?")
    return Response(str(resp), media_type="application/xml")


# ---------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    res = supabase.table("reservations").select("*").order("datetime", desc=True).execute()
    rows = res.data or []

    for r in rows:
        dt = dateparser.parse(r["datetime"])
        r["datetime"] = readable(dt)

    return templates.TemplateResponse("dashboard.html", {"request": request, "reservations": rows})


# ---------------------------------------------------------
# WEBSOCKET REFRESH
# ---------------------------------------------------------
clients = []


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except:
        clients.remove(websocket)


async def notify_refresh():
    for ws in clients:
        try:
            await ws.send_text("refresh")
        except:
            pass


# ---------------------------------------------------------
# RENDER STARTUP (IMPORTANT)
# ---------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
