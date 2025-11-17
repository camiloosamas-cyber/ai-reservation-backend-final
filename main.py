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
# GLOBAL SESSION MEMORY
# ---------------------------------------------------------
session_state = {}
LOCAL_TZ = ZoneInfo("America/Bogota")


# ---------------------------------------------------------
# SUPABASE INIT
# ---------------------------------------------------------
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE")
)

TABLE_LIMIT = 10


# ---------------------------------------------------------
# ASSIGN TABLE
# ---------------------------------------------------------
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

    try:
        # datetime already comes as ISO Bogotá → convert to UTC
        dt_local = datetime.fromisoformat(data["datetime"])
        dt_utc = dt_local.astimezone(timezone.utc)
    except:
        return "❌ No pude procesar la fecha/hora."

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
        f"🗓 {dt_local.strftime('%A %d %B, %I:%M %p')}\n"
        f"🍽 Mesa: {table}"
    )


# ---------------------------------------------------------
# UNIVERSAL AI EXTRACTION (NAME + DATE + TIME + PARTY)
# ---------------------------------------------------------
def extract_with_ai(user_msg: str):
    prompt = f"""
Eres un agente de reservas para un restaurante en Colombia.

Tu tarea:
- Detectar el *nombre*
- Detectar la *fecha y hora exactas* en formato ISO Bogotá (America/Bogota)
- Detectar el *número de personas*

Responde SOLO JSON así:

{{
  "customer_name": "",
  "datetime": "",
  "party_size": ""
}}

Reglas:
- Si no encuentras un campo, déjalo vacío.
- datetime DEBE ser ISO, ejemplo:
  "2025-01-26T19:00:00-05:00"

Mensaje del usuario:
"{user_msg}"
"""

    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[{"role": "system", "content": prompt}]
        )
        return json.loads(r.choices[0].message.content)
    except:
        return {"customer_name": "", "datetime": "", "party_size": ""}


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
            "party_size": None
        }

    memory = session_state[user_id]

    # GREETINGS
    if msg.lower() in ["hola", "hello", "buenas", "hey", "holaa", "ola"]:
        resp.message("¡Hola! 😊 ¿En qué puedo ayudarte hoy?\n¿Quieres *información* o deseas *hacer una reserva*?")
        return Response(str(resp), media_type="application/xml")

    # DETECT INTENT
    if "reserv" in msg.lower() and not any(memory.values()):
        resp.message("Perfecto 😊 Para continuar, necesito:\n👉 Fecha y hora\n👉 Nombre\n👉 Número de personas")
        return Response(str(resp), media_type="application/xml")


    # AI EXTRACTION (1 CALL)
    extracted = extract_with_ai(msg)

    # UPDATE MEMORY
    if extracted.get("customer_name"):
        memory["customer_name"] = extracted["customer_name"]

    if extracted.get("datetime"):
        memory["datetime"] = extracted["datetime"]

    if extracted.get("party_size"):
        memory["party_size"] = extracted["party_size"]


    # ASK FOR WHAT'S MISSING
    if not memory["customer_name"]:
        resp.message("¿A nombre de quién sería la reserva?")
        return Response(str(resp), media_type="application/xml")

    if not memory["datetime"]:
        resp.message("¿Para qué fecha y hora deseas la reserva?")
        return Response(str(resp), media_type="application/xml")

    if not memory["party_size"]:
        resp.message("¿Para cuántas personas sería la reserva?")
        return Response(str(resp), media_type="application/xml")


    # ALL INFO PRESENT → SAVE
    confirmation = save_reservation(memory)
    resp.message(confirmation)

    # RESET AFTER BOOKING
    session_state[user_id] = {
        "customer_name": None,
        "datetime": None,
        "party_size": None
    }

    return Response(str(resp), media_type="application/xml")


# ---------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    res = supabase.table("reservations").select("*").order("datetime", desc=True).execute()
    rows = res.data or []

    return templates.TemplateResponse("dashboard.html", {"request": request, "reservations": rows})


# ---------------------------------------------------------
# WEBSOCKET (optional)
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


# ---------------------------------------------------------
# RENDER STARTUP
# ---------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
