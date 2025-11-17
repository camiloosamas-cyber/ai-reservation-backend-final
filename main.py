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
# SESSION MEMORY PER USER
# ---------------------------------------------------------
session_state = {}  # stores conversation + reservation progress

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
# WHATSAPP ROUTE — FULL LOGIC
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp(Body: str = Form(...)):

    resp = MessagingResponse()
    msg = Body.strip().lower()

    user_id = "default_user"  # later assign real user via phone #

    # ---------------------------
    # INIT SESSION
    # ---------------------------
    if user_id not in session_state:
        session_state[user_id] = {
            "step": "none",
            "data": {
                "customer_name": None,
                "datetime": None,
                "party_size": None,
                "notes": None,
            },
            "expires": None
        }

    state = session_state[user_id]
    step = state["step"]
    data = state["data"]


    # ---------------------------
    # RESET IF RESERVATION EXPIRED
    # ---------------------------
    if state["expires"]:
        if datetime.now(LOCAL_TZ) > state["expires"]:
            session_state[user_id] = {
                "step": "none",
                "data": {
                    "customer_name": None,
                    "datetime": None,
                    "party_size": None,
                    "notes": None,
                },
                "expires": None
            }
            state = session_state[user_id]
            data = state["data"]


    # ---------------------------
    # GREETING FLOW EXACTLY LIKE BEFORE
    # ---------------------------
    if step == "none":
        if any(w in msg for w in ["hola", "buenas", "hey"]):
            resp.message("¡Hola! 😊 ¿En qué puedo ayudarte hoy? ¿Quieres información o deseas hacer una reserva?")
            state["step"] = "awaiting_intent"
            return Response(str(resp), media_type="application/xml")

    if step == "awaiting_intent":
        if "reserv" in msg:
            resp.message("Perfecto 😊 empecemos con tu reserva. ¿Cuál es tu nombre?")
            state["step"] = "need_name"
            return Response(str(resp), media_type="application/xml")

        resp.message("¿Deseas hacer una reserva? 😊")
        return Response(str(resp), media_type="application/xml")


    # ---------------------------------------------------------
    # STEP 1: NAME
    # ---------------------------------------------------------
    if step == "need_name":

        # extract name with AI
        ai_prompt = f"""
Extrae el nombre del usuario del siguiente mensaje. 
Responde SOLO JSON así:
{{"customer_name": "Luis"}}

Mensaje: "{Body}"
        """

        try:
            r = client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0,
                messages=[{"role": "system", "content": ai_prompt}]
            )
            j = json.loads(r.choices[0].message.content)
            if j.get("customer_name"):
                data["customer_name"] = j["customer_name"]
        except:
            pass

        if not data["customer_name"]:
            resp.message("¿Cuál es tu nombre para la reserva?")
            return Response(str(resp), media_type="application/xml")

        # go to next question
        resp.message("¿Para qué fecha y hora es la reserva?")
        state["step"] = "need_datetime"
        return Response(str(resp), media_type="application/xml")


    # ---------------------------------------------------------
    # STEP 2: DATE & TIME
    # ---------------------------------------------------------
    if step == "need_datetime":

        # Extract datetime from message
        dt = parse_to_utc(Body)
        if dt:
            data["datetime"] = Body
        else:
            resp.message("¿Para qué día y a qué hora deseas la reserva?")
            return Response(str(resp), media_type="application/xml")

        # next question
        resp.message("¿Para cuántas personas es la reserva?")
        state["step"] = "need_party"
        return Response(str(resp), media_type="application/xml")


    # ---------------------------------------------------------
    # STEP 3: PARTY SIZE
    # ---------------------------------------------------------
    if step == "need_party":

        # AI extract number
        ai_prompt = f"""
Extrae SOLO el número de personas.
Responde formato JSON:
{{"party_size": "4"}}

Mensaje: "{Body}"
        """

        try:
            r = client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0,
                messages=[{"role": "system", "content": ai_prompt}]
            )
            j = json.loads(r.choices[0].message.content)
            if j.get("party_size"):
                data["party_size"] = j["party_size"]
        except:
            pass

        if not data["party_size"]:
            resp.message("¿Para cuántas personas sería?")
            return Response(str(resp), media_type="application/xml")

        # -------------------
        # ALL DATA READY → BOOK
        # -------------------
        confirmation = save_reservation(data)
        resp.message(confirmation)

        # set expiration silently
        dt_utc = parse_to_utc(data["datetime"])
        if dt_utc:
            state["expires"] = utc_to_local(dt_utc) + timedelta(hours=1)

        state["step"] = "none"
        return Response(str(resp), media_type="application/xml")



    # ---------------------------------------------------------
    # DEFAULT FALLBACK (rarely used)
    # ---------------------------------------------------------
    resp.message("¿Podrías repetirlo por favor?")
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
# WEBSOCKET LIVE REFRESH
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
# RENDER STARTUP (REQUIRED)
# ---------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
