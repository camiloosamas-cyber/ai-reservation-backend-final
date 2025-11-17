from fastapi import FastAPI, Request, WebSocket, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import json, os, asyncio, time
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
# MEMORY PER USER
# ---------------------------------------------------------
session_state = {}


# ---------------------------------------------------------
# TIMEZONE
# ---------------------------------------------------------
LOCAL_TZ = ZoneInfo("America/Bogota")


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
    try:
        parsed = dateparser.parse(
            dt_str,
            settings={
                "RETURN_AS_TIMEZONE_AWARE": True,
                "TIMEZONE": "America/Bogota",
                "TO_TIMEZONE": "UTC"
            }
        )
        if not parsed:
            return None
        return parsed.isoformat().replace("+00:00", "Z")
    except:
        return None


def _readable(dt_utc):
    dtu = _safe_fromiso(dt_utc)
    if not dtu:
        return "Fecha inválida"
    return dtu.astimezone(LOCAL_TZ).strftime("%A %d %B, %I:%M %p")


# ---------------------------------------------------------
# SUPABASE
# ---------------------------------------------------------
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE")
)

TABLE_LIMIT = 10


def assign_table(dt):
    booked = supabase.table("reservations").select("table_number").eq("datetime", dt).execute()
    taken = {r["table_number"] for r in (booked.data or [])}
    for i in range(1, TABLE_LIMIT + 1):
        t = f"T{i}"
        if t not in taken:
            return t
    return None


def save_reservation(data):
    dt_utc = _to_utc_iso(data["datetime"])
    if not dt_utc:
        return "❌ Fecha u hora inválida."

    table = assign_table(dt_utc)
    if not table:
        return "❌ No hay mesas disponibles en ese horario."

    supabase.table("reservations").insert({
        "customer_name": data["customer_name"],
        "datetime": dt_utc,
        "party_size": int(data["party_size"]),
        "table_number": table,
        "notes": data.get("notes", ""),
        "status": "confirmed"
    }).execute()

    return (
        "✅ *¡Reserva confirmada!*\n"
        f"👤 {data['customer_name']}\n"
        f"👥 {data['party_size']} personas\n"
        f"🗓 {_readable(dt_utc)}\n"
        f"🍽 Mesa: {table}"
    )


# ---------------------------------------------------------
# WHATSAPP BOT
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp(Body: str = Form(...)):
    resp = MessagingResponse()
    text = Body.lower().strip()
    user_id = "default"

    # Create session
    if user_id not in session_state:
        session_state[user_id] = {
            "mode": "none",
            "data": {
                "customer_name": None,
                "date": None,
                "time": None,
                "datetime": None,
                "party_size": None,
                "notes": None
            }
        }

    state = session_state[user_id]
    data = state["data"]
    mode = state["mode"]

    # ---------------- GREETING ----------------
    if any(g in text for g in ["hola", "buenas", "buenos días", "buenas tardes"]) and mode == "none":
        resp.message("¡Hola! 😊 ¿Quieres hacer una reserva?")
        return Response(str(resp), media_type="application/xml")

    # ---------------- ENTER RESERVATION ----------------
    if "reserv" in text and mode != "reservation":
        state["mode"] = "reservation"
        resp.message("Perfecto 😊 ¿Cuál es tu nombre?")
        return Response(str(resp), media_type="application/xml")

    if state["mode"] != "reservation":
        resp.message("¿Te gustaría hacer una reserva? 😊")
        return Response(str(resp), media_type="application/xml")

    # ---------------------------------------------------------
    # AI EXTRACTION LOGIC — STRICT (Option B)
    # ---------------------------------------------------------
    ai_prompt = f"""
Extrae información de una reserva SIN asumir nada.

ESTADO ACTUAL:
{json.dumps(data, indent=2, ensure_ascii=False)}

MENSAJE NUEVO:
"{Body}"

REGLAS:
- SOLO extrae:
  customer_name, date, time, party_size, notes
- NO asumas que un número = personas.
- SOLO es party_size si el mensaje menciona "personas", "somos", "para", "ppl".
- Si el usuario dice solo fecha → NO completar hora.
- Si el usuario dice solo hora → NO completar fecha.
- Cuando tengamos date y time → formar:
    datetime = "<date> <time>"
- Si falta nombre → ask: "¿Cuál es tu nombre?"
- Si falta fecha → ask: "¿Para qué fecha sería?"
- Si falta hora → ask: "¿A qué hora sería?"
- Si falta party_size → ask: "¿Para cuántas personas sería?"

Cuando todo esté completo (date, time, customer_name, party_size), responde:
{"complete": true}

Responde únicamente JSON.
"""

    try:
        ai_response = client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0,
            messages=[{"role": "system", "content": ai_prompt}]
        )
        extracted = json.loads(ai_response.choices[0].message.content)
    except:
        resp.message("❌ No entendí eso, ¿podrías repetirlo?")
        return Response(str(resp), media_type="application/xml")

    # Update memory
    for key in ["customer_name", "date", "time", "party_size", "notes"]:
        if key in extracted and extracted[key]:
            data[key] = extracted[key]

    # Build datetime if ready
    if data["date"] and data["time"]:
        data["datetime"] = f"{data['date']} {data['time']}"

    # Ask for missing info
    if "ask" in extracted:
        resp.message(extracted["ask"])
        return Response(str(resp), media_type="application/xml")

    # Complete
    if extracted.get("complete") and data["datetime"]:
        message = save_reservation(data)
        resp.message(message)

        # Reset
        session_state[user_id] = {
            "mode": "none",
            "data": {
                "customer_name": None,
                "date": None,
                "time": None,
                "datetime": None,
                "party_size": None,
                "notes": None
            }
        }

        return Response(str(resp), media_type="application/xml")

    resp.message("¿Podrías repetirlo?")
    return Response(str(resp), media_type="application/xml")
