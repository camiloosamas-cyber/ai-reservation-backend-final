from fastapi import FastAPI, Request, WebSocket, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import json, os, asyncio, time, dateparser

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
# SESSION MEMORY
# ---------------------------------------------------------
session_state = {}


# ---------------------------------------------------------
# TIMEZONE
# ---------------------------------------------------------
LOCAL_TZ_NAME = os.getenv("LOCAL_TZ", "America/Bogota")
LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)


def _safe_fromiso(s):
    try:
        if not s:
            return None
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except:
        return None


def _to_utc_iso(dt_str):
    if not dt_str:
        return None

    parsed = dateparser.parse(
        dt_str,
        settings={
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TIMEZONE": LOCAL_TZ_NAME,
            "TO_TIMEZONE": "UTC",
        }
    )

    if not parsed:
        return None

    return parsed.isoformat().replace("+00:00", "Z")


def _utc_iso_to_local_iso(iso_utc):
    dtu = _safe_fromiso(iso_utc or "")
    if not dtu:
        return None
    return dtu.astimezone(LOCAL_TZ).isoformat()


def _readable_local(iso_utc):
    dtu = _safe_fromiso(iso_utc)
    if not dtu:
        return "Horario inválido"
    return dtu.astimezone(LOCAL_TZ).strftime("%A %d %B, %I:%M %p")


def _norm_name(name):
    return (name or "").strip().casefold()


# ---------------------------------------------------------
# SUPABASE
# ---------------------------------------------------------
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE")
)

TABLE_LIMIT = 10


# ---------------------------------------------------------
# IDEMPOTENCY CACHE
# ---------------------------------------------------------
_recent_keys = {}
IDEMPOTENCY_TTL = 60


def _cache_check_and_add(key):
    now = time.time()
    if key in _recent_keys and _recent_keys[key] > now:
        return True
    _recent_keys[key] = now + IDEMPOTENCY_TTL
    return False


# ---------------------------------------------------------
# TABLE ASSIGNMENT + SAVE
# ---------------------------------------------------------
def assign_table(iso_utc):
    booked = supabase.table("reservations").select("table_number").eq("datetime", iso_utc).execute()
    taken = {r["table_number"] for r in (booked.data or [])}

    for i in range(1, TABLE_LIMIT + 1):
        t = f"T{i}"
        if t not in taken:
            return t

    return None


def _find_existing(utc_iso, name):
    rows = supabase.table("reservations").select("*").eq("datetime", utc_iso).execute().data or []
    for r in rows:
        if _norm_name(r.get("customer_name")) == _norm_name(name):
            return r
    return None


def save_reservation(data):
    dt = _to_utc_iso(data.get("datetime"))
    if not dt:
        return "❌ Fecha u hora inválida."

    name = data.get("customer_name", "")
    key = f"{name}|{dt}"

    if _cache_check_and_add(key):
        return "ℹ️ Esta reserva ya estaba registrada."

    table = assign_table(dt)
    if not table:
        return "❌ No hay mesas disponibles para ese horario."

    supabase.table("reservations").insert({
        "customer_name": name,
        "customer_email": "",
        "contact_phone": "",
        "datetime": dt,
        "party_size": int(data.get("party_size", 1)),
        "table_number": table,
        "notes": "",
        "status": "confirmed",
    }).execute()

    readable = _readable_local(dt)

    return (
        "✅ *¡Reserva confirmada!*\n"
        f"👤 {name}\n"
        f"👥 {data.get('party_size')} personas\n"
        f"🗓 {readable}\n"
        f"🍽 Mesa: {table}"
    )


# ---------------------------------------------------------
# WHATSAPP ROUTE — FINAL
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp(Body: str = Form(...)):
    print("📩 Incoming WhatsApp:", Body)
    resp = MessagingResponse()

    user_id = "default"
    text = Body.lower().strip()

    # Initialize session
    if user_id not in session_state:
        session_state[user_id] = {
            "mode": "none",
            "data": {
                "customer_name": None,
                "datetime": None,
                "party_size": None,
            }
        }

    state = session_state[user_id]
    mode = state["mode"]
    data = state["data"]

    # ----------------------------------
    # GREETING
    # ----------------------------------
    if any(g in text for g in ["hola", "buenas", "buenos días", "buenas tardes"]) and mode == "none":
        resp.message("¡Hola! 😊 ¿En qué puedo ayudarte hoy? ¿Quieres información o deseas hacer una reserva?")
        return Response(str(resp), media_type="application/xml")

    # ----------------------------------
    # ENTER RESERVATION MODE
    # ----------------------------------
    if "reserv" in text and mode != "reservation":
        state["mode"] = "reservation"
        resp.message("Perfecto 😊 empecemos con tu reserva. ¿Cuál es tu nombre?")
        return Response(str(resp), media_type="application/xml")

    # If not reservation mode yet
    if state["mode"] != "reservation":
        resp.message("¿Te gustaría hacer una reserva? 😊")
        return Response(str(resp), media_type="application/xml")

    # ----------------------------------
    # AI EXTRACTION WITH MEMORY
    # ----------------------------------
    ai_prompt = f"""
Eres un asistente que extrae información de reservas en español.

INFORMACIÓN YA CONOCIDA:
{json.dumps(data, indent=2, ensure_ascii=False)}

NUEVO MENSAJE:
"{Body}"

CAMPOS NECESARIOS:
- customer_name
- datetime
- party_size

REGLAS:
- No elimines datos ya conocidos.
- Extrae SOLO lo nuevo.
- Si falta algo, responde:
  {{"ask": "pregunta"}}
- Si todo está completo, responde:
  {{"complete": true}}

Responde SOLO JSON.
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0,
            messages=[{"role": "system", "content": ai_prompt}]
        )
        extracted = json.loads(response.choices[0].message.content.strip())
    except:
        resp.message("❌ No pude entender. ¿Podrías repetirlo?")
        return Response(str(resp), media_type="application/xml")

    # Update data
    for key in data.keys():
        if key in extracted and extracted[key]:
            data[key] = extracted[key]

    # Ask for missing info
    if "ask" in extracted:
        resp.message(extracted["ask"])
        return Response(str(resp), media_type="application/xml")

    # Completed → book
    if extracted.get("complete"):
        confirmation = save_reservation(data)
        resp.message(confirmation)

        # Reset session
        session_state[user_id] = {
            "mode": "none",
            "data": {
                "customer_name": None,
                "datetime": None,
                "party_size": None,
            }
        }

        asyncio.create_task(notify_refresh())
        return Response(str(resp), media_type="application/xml")

    resp.message("¿Podrías repetir eso?")
    return Response(str(resp), media_type="application/xml")


# ---------------------------------------------------------
# DASHBOARD + WS
# ---------------------------------------------------------
@app.get("/")
def home():
    return "<h2>Backend activo</h2>"


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    res = supabase.table("reservations").select("*").order("datetime", desc=True).execute()
    rows = res.data or []

    for r in rows:
        r["datetime"] = _utc_iso_to_local_iso(r["datetime"])

    return templates.TemplateResponse("dashboard.html", {"request": request, "reservations": rows})


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
# SERVER STARTUP (REQUIRED FOR RENDER)
# ---------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
