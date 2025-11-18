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
# SESSION MEMORY
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
        return "❌ No pude procesar la fecha/hora."

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
# SUPER AI EXTRACTION (NOW WITH CURRENT DATE FIX)
# ---------------------------------------------------------
def ai_extract(user_msg: str):
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")

    superprompt = f"""
Eres un asistente de reservas para un restaurante colombiano vía WhatsApp.

HOY es {today} en zona America/Bogota.
Debes interpretar cualquier fecha relativa usando esta fecha actual.

Tu tarea es extraer:
- "intent": "reserve" | "info" | "other"
- "customer_name"
- "datetime": fecha + hora exactas en ISO America/Bogota
- "party_size"

SIEMPRE responde SOLO JSON.

-----------------------------------------------
REGLAS PARA INTERPRETAR FECHAS:
-----------------------------------------------

1. Palabras relativas:
   - "hoy" → {today}
   - "mañana"
   - "pasado mañana"
   - "el martes"
   - "este sábado"
   → Calcula SIEMPRE una fecha futura o del mismo día, NUNCA del pasado.

2. Si el usuario solo dice "martes", "viernes", etc.:
   - Usa el día más cercano hacia el futuro (o hoy mismo si coincide).

3. Si la hora NO es exacta:
   - "en la noche", "en la tarde", "tipo 7"
   → datetime = "" (vacío)

4. Si no hay hora exacta:
   → datetime = "".

-----------------------------------------------
REGLAS GENERALES:
-----------------------------------------------

- "yo", "para mí", "a mi nombre" → NO asumas nombre.
- "somos varios", "unos cuantos" → NO asumas número.
- Si viene todo junto ("martes 7pm Luis 4 personas") → llena todo.
- NO inventes nada. Si no está claro → deja ""

-----------------------------------------------
FORMATO OBLIGATORIO:
{{
  "intent": "",
  "customer_name": "",
  "datetime": "",
  "party_size": ""
}}
-----------------------------------------------

Mensaje del usuario:
"{user_msg}"
"""

    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[{"role": "system", "content": superprompt}]
        )
        return json.loads(r.choices[0].message.content)

    except:
        return {"intent": "", "customer_name": "", "datetime": "", "party_size": ""}



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

    # GREETINGS
    if msg.lower() in ["hola", "hello", "holaa", "buenas", "hey", "ola"]:
        resp.message("¡Hola! 😊 ¿En qué puedo ayudarte hoy?\n¿Quieres *información* o deseas *hacer una reserva*?")
       return Response(str(resp), media_type="application/xml")

    # AI INTERPRETATION
    extracted = ai_extract(msg)

    # INTENT
    if extracted["intent"] == "reserve" and not memory["awaiting_info"]:
        memory["awaiting_info"] = True
        resp.message("Perfecto 😊 Para continuar necesito:\n👉 Fecha y hora\n👉 Nombre\n👉 Número de personas")
        return Response(str(resp), media_type="application/xml")

    # UPDATE MEMORY
    if extracted["customer_name"]:
        memory["customer_name"] = extracted["customer_name"]

    if extracted["datetime"]:
        memory["datetime"] = extracted["datetime"]

    if extracted["party_size"]:
        memory["party_size"] = extracted["party_size"]

    # ASK FOR MISSING
    if not memory["customer_name"]:
        resp.message("¿A nombre de quién sería la reserva?")
        return Response(str(resp), media_type="application/xml")

    if not memory["datetime"]:
        resp.message("¿Para qué fecha y hora deseas la reserva?")
        return Response(str(resp), media_type="application/xml")

    if not memory["party_size"]:
        resp.message("¿Para cuántas personas sería la reserva?")
        return Response(str(resp), media_type="application/xml")

    # READY → SAVE
    confirmation = save_reservation(memory)
    resp.message(confirmation)

    # RESET AFTER BOOKING
    session_state[user_id] = {
        "customer_name": None,
        "datetime": None,
        "party_size": None,
        "awaiting_info": False
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
# RUN
# ---------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
