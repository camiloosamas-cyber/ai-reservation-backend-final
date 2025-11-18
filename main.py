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
# SUPER AI EXTRACTION
# ---------------------------------------------------------
def ai_extract(user_msg: str):
    superprompt = f"""
Eres un asistente de reservas para un restaurante colombiano vía WhatsApp.

Tu tarea es interpretar cualquier mensaje del usuario y extraer:

- "intent": "reserve" | "info" | "other"
- "customer_name": nombre del cliente (si está presente)
- "datetime": fecha + hora exactas en ISO America/Bogota (ej: "2025-01-26T19:00:00-05:00")
- "party_size": número de personas (string)

RESPONDE SIEMPRE SOLO EN JSON.

REGLAS IMPORTANTES:

1. "quiero reservar", "quiero hacer una reserva", "me gustaría reservar", "reservar mesa" → intent = "reserve"

2. Si hay fecha/hora como:
   - "lunes a las 7"
   - "mañana 9am"
   - "el viernes en la noche"
   - "pasado mañana a las 3"
   - "este sábado tipo 7"
   → conviértelo a ISO Bogotá SI y SOLO SI la hora es exacta.

3. Si la hora NO es exacta:
   - "en la noche"
   - "en la tarde"
   - "tipo 7"
   → datetime = "".

4. Si hay solo fecha sin hora → datetime = "".

5. "yo", "para mí", "a mi nombre" → customer_name = "".

6. "somos varios", "unos cuantos", "varios" → party_size = "".

7. Si hay número:
   - "somos 4"
   - "para 3 personas"
   - "seríamos 2"
   → party_size = número.

8. Si el usuario da TODO JUNTO:
   "Quiero reservar para Luis el lunes a las 7pm somos 4"
   → llena todos los campos.

9. NO inventes datos.  
   Si NO lo encuentras → déjalo vacío.

FORMATO DE RESPUESTA:

{{
  "intent": "",
  "customer_name": "",
  "datetime": "",
  "party_size": ""
}}

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

    # GREETING
    if msg.lower() in ["hola", "hello", "holaa", "buenas", "hey", "ola"]:
        resp.message("¡Hola! 😊 ¿En qué puedo ayudarte hoy?\n¿Quieres *información* o deseas *hacer una reserva*?")
        return Response(str(resp), media_type="application/xml")

    # AI INTERPRETATION
    extracted = ai_extract(msg)

    # INTENT MANAGEMENT
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

    # ASK FOR MISSING PARTS
    if not memory["customer_name"]:
        resp.message("¿A nombre de quién sería la reserva?")
        return Response(str(resp), media_type="application/xml")

    if not memory["datetime"]:
        resp.message("¿Para qué fecha y hora deseas la reserva?")
        return Response(str(resp), media_type="application/xml")

    if not memory["party_size"]:
        resp.message("¿Para cuántas personas sería la reserva?")
        return Response(str(resp), media_type="application/xml")

    # EVERYTHING READY → SAVE
    confirmation = save_reservation(memory)
    resp.message(confirmation)

    # RESET
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
