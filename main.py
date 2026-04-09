print(">>> STARTING BARBERSHOP BOT v1.0.0 ✅")

from dotenv import load_dotenv
load_dotenv()

import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI

# =====================================================================
# BUSINESS CONFIGS — add new businesses here
# =====================================================================

BUSINESS_CONFIGS = {
    "+14155238886": {
        "business_id": 1,
        "name": "Barbería El Paisa",
        "services": ["Corte", "Corte + Barba", "Afeitado", "Corte de Niño"],
        "hours": "Lunes a Sábado de 9:00 a.m. a 7:00 p.m.",
        "timezone": "America/Bogota",
    }
}

# =====================================================================
# SETUP
# =====================================================================

try:
    LOCAL_TZ = ZoneInfo("America/Bogota")
except ZoneInfoNotFoundError:
    LOCAL_TZ = ZoneInfo("UTC")

app = FastAPI(title="AI Reservation Bot", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# OpenAI client
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Supabase
try:
    from supabase import create_client
    supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE"))
    print("✅ Supabase connected")
except Exception as e:
    supabase = None
    print(f"ERROR: Supabase connection failed: {e}")

# Twilio
try:
    from twilio.twiml.messaging_response import MessagingResponse
    print("✅ Twilio loaded")
except ImportError:
    print("WARNING: Twilio not available")

# =====================================================================
# SESSION MANAGEMENT
# =====================================================================

MEMORY_SESSIONS = {}

def get_session(phone):
    if supabase:
        try:
            result = supabase.table("sessions").select("data").eq("phone", phone).maybe_single().execute()
            if result and result.data and result.data.get("data"):
                return result.data["data"]
        except Exception as e:
            print(f"Session load error: {e}")
    return MEMORY_SESSIONS.get(phone, {"history": [], "booked": False})

def save_session(phone, session):
    MEMORY_SESSIONS[phone] = session
    if supabase:
        try:
            supabase.table("sessions").upsert({
                "phone": phone,
                "data": session,
                "last_updated": datetime.now(LOCAL_TZ).isoformat()
            }).execute()
        except Exception as e:
            print(f"Session save error: {e}")

# =====================================================================
# SAVE RESERVATION TO SUPABASE
# =====================================================================

def save_reservation(phone, business_id, extracted):
    if not supabase:
        print("No Supabase — reservation not saved")
        return
    try:
        supabase.table("reservations").insert({
            "contact_phone": phone,
            "business_id": business_id,
            "client_name": extracted.get("name"),
            "service": extracted.get("service"),
            "datetime": extracted.get("datetime"),
            "status": "confirmed"
        }).execute()
        print(f"✅ Reservation saved for {phone}")
    except Exception as e:
        print(f"ERROR saving reservation: {e}")

# =====================================================================
# OPENAI — BRAIN OF THE BOT
# =====================================================================

def build_system_prompt(config):
    services_list = ", ".join(config["services"])
    return f"""Eres un asistente de reservas para {config["name"]} en Medellín, Colombia.

Tu único trabajo es ayudar a los clientes a reservar una cita. No respondas preguntas que no tengan que ver con reservas o el negocio.

Servicios disponibles: {services_list}
Horario: {config["hours"]}

FLUJO DE RESERVA:
1. Saluda al cliente la primera vez.
2. Cuando el cliente quiera reservar, pídele en UN solo mensaje: nombre completo, servicio, fecha y hora.
3. Si el cliente responde con información incompleta, solo pregunta por lo que falta.
4. Cuando tengas toda la información, confírmala con el cliente en un resumen claro.
5. Cuando el cliente confirme, responde EXACTAMENTE con este JSON y nada más:
RESERVA_CONFIRMADA:{{"name":"<nombre>","service":"<servicio>","datetime":"<YYYY-MM-DD HH:MM>"}}

REGLAS:
- Responde siempre en español colombiano, tono amigable y casual.
- Si el cliente pregunta algo que no tiene que ver con reservas, redirígelo amablemente.
- No inventes horarios ni servicios que no estén en la lista.
- Si el cliente da una fecha u hora fuera del horario, díselo y pide otra.
- El formato del datetime SIEMPRE debe ser: YYYY-MM-DD HH:MM
- El año actual es 2026. Siempre usa 2026 cuando el cliente no especifique el año."""

def ask_openai(config, history, new_message):
    system_prompt = build_system_prompt(config)
    messages = [{"role": "system", "content": system_prompt}]
    messages += history
    messages.append({"role": "user", "content": new_message})

    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        max_tokens=500,
        temperature=0.7
    )
    return response.choices[0].message.content.strip()

def is_slot_available(datetime_str: str, business_id: int) -> bool:
    if not supabase:
        return True
    try:
        result = supabase.table("reservations").select("reservation_id", count="exact").eq("business_id", business_id).eq("datetime", datetime_str).eq("status", "confirmed").execute()
        count = result.count or 0
        return count < 3
    except Exception as e:
        print(f"Availability check error: {e}")
        return True

def cancel_reservation(phone: str, business_id: int) -> dict:
    if not supabase:
        return {"success": False}
    try:
        result = supabase.table("reservations").select("*").eq("contact_phone", phone).eq("business_id", business_id).eq("status", "confirmed").order("datetime", desc=True).limit(1).execute()
        if not result.data:
            return {"success": False, "reason": "no_booking"}
        booking = result.data[0]
        supabase.table("reservations").update({"status": "cancelled"}).eq("reservation_id", booking["reservation_id"]).execute()
        return {"success": True, "booking": booking}
    except Exception as e:
        print(f"Cancel error: {e}")
        return {"success": False}

# =====================================================================
# WEBHOOK — entry point for all WhatsApp messages
# =====================================================================

@app.post("/webhook")
async def webhook(request: Request):
    form = await request.form()
    incoming_msg = form.get("Body", "").strip()
    from_number = form.get("From", "").replace("whatsapp:", "")
    to_number = form.get("To", "").replace("whatsapp:", "")

    print(f"📩 Message from {from_number} to {to_number}: {incoming_msg}")

    # Get business config based on the receiving number
    config = BUSINESS_CONFIGS.get(to_number)
    if not config:
        print(f"No config found for number: {to_number}")
        resp = MessagingResponse()
        resp.message("Este número no está configurado.")
        return Response(content=str(resp), media_type="application/xml")

    # Load session
    session = get_session(from_number)
    history = session.get("history", [])

    # Handle cancellation before asking OpenAI
    cancel_keywords = ["cancelar", "cancela", "cancel", "quiero cancelar", "cancelar cita"]
    if any(kw in incoming_msg.lower() for kw in cancel_keywords):
        result = cancel_reservation(from_number, config["business_id"])
        if result["success"]:
            booking = result["booking"]
            reply = (
                f"✅ Tu cita ha sido cancelada.\n\n"
                f"👤 {booking.get('client_name')}\n"
                f"✂️ {booking.get('service')}\n"
                f"📅 {booking.get('datetime', '')[:16]}\n\n"
                f"Si quieres reservar de nuevo, con gusto te ayudo 😊"
            )
        elif result.get("reason") == "no_booking":
            reply = "No encontré ninguna cita activa para cancelar. ¿Quieres reservar una nueva?"
        else:
            reply = "Hubo un problema cancelando tu cita. Intenta de nuevo."
    else:
        # Ask OpenAI
        try:
            reply = ask_openai(config, history, incoming_msg)
        except Exception as e:
            print(f"OpenAI error: {e}")
            reply = "Hubo un error procesando tu mensaje. Intenta de nuevo."
            
    # Check if booking was confirmed
    if "RESERVA_CONFIRMADA:" in reply:
        try:
            json_str = reply.split("RESERVA_CONFIRMADA:")[1].strip()
            extracted = json.loads(json_str)
            if not is_slot_available(extracted.get("datetime"), config["business_id"]):
                reply = "Lo siento, ese horario ya está lleno 😅 ¿Puedes elegir otra hora?"
            else:
                save_reservation(from_number, config["business_id"], extracted)
                reply = (
                    f"✅ ¡Listo! Tu cita en {config['name']} está confirmada.\n\n"
                    f"👤 Nombre: {extracted.get('name')}\n"
                    f"✂️ Servicio: {extracted.get('service')}\n"
                    f"📅 Fecha y hora: {extracted.get('datetime')}\n\n"
                    f"¡Te esperamos! 💈"
                )
                session["booked"] = True
        except Exception as e:
            print(f"Error parsing booking: {e}")
            reply = "Hubo un problema al confirmar tu reserva. Intenta de nuevo."

    # Update conversation history
    history.append({"role": "user", "content": incoming_msg})
    history.append({"role": "assistant", "content": reply})

    # Keep history to last 20 messages to avoid token bloat
    session["history"] = history[-20:]
    save_session(from_number, session)

    # Send reply via Twilio
    resp = MessagingResponse()
    resp.message(reply)
    return Response(content=str(resp), media_type="application/xml")

# =====================================================================
# DASHBOARD — business owner view
# =====================================================================

@app.get("/dashboard/{business_id}", response_class=HTMLResponse)
async def dashboard(request: Request, business_id: int):
    reservations = []
    if supabase:
        try:
            result = supabase.table("reservations").select("*").eq("business_id", business_id).order("datetime").execute()
            reservations = result.data or []
        except Exception as e:
            print(f"Dashboard error: {e}")

    # Find business name
    business_name = "Negocio"
    for config in BUSINESS_CONFIGS.values():
        if config["business_id"] == business_id:
            business_name = config["name"]
            break

    rows = ""
    for r in reservations:
        rows += f"""
        <tr>
            <td>{r.get('datetime', '-')[:16].replace('T', ' ') if r.get('datetime') else '-'}</td>
            <td>{r.get('client_name', '-')}</td>
            <td>{r.get('service', '-')}</td>
            <td>{r.get('contact_phone', '-')}</td>
            <td><span class="status">{r.get('status', '-')}</span></td>
        </tr>
        """

    html = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>{business_name} — Reservas</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{ font-family: 'Segoe UI', sans-serif; background: #f4f4f4; color: #222; }}
            .header {{ background: #1a1a1a; color: white; padding: 20px 32px; display: flex; align-items: center; justify-content: space-between; }}
            .header h1 {{ font-size: 1.4rem; font-weight: 600; }}
            .header span {{ font-size: 0.9rem; opacity: 0.6; }}
            .container {{ max-width: 1000px; margin: 32px auto; padding: 0 16px; }}
            .card {{ background: white; border-radius: 12px; padding: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}
            .total {{ font-size: 1rem; color: #555; margin-bottom: 24px; }}
            .total strong {{ color: #1a1a1a; font-size: 1.2rem; }}
            table {{ width: 100%; border-collapse: collapse; }}
            th {{ background: #1a1a1a; color: white; padding: 12px 16px; text-align: left; font-weight: 500; font-size: 0.9rem; }}
            td {{ padding: 12px 16px; border-bottom: 1px solid #f0f0f0; font-size: 0.9rem; }}
            tr:last-child td {{ border-bottom: none; }}
            tr:hover td {{ background: #fafafa; }}
            .status {{ background: #e6f4ea; color: #2e7d32; padding: 3px 10px; border-radius: 20px; font-size: 0.8rem; font-weight: 500; }}
            .empty {{ text-align: center; color: #999; padding: 40px; }}
            .refresh {{ background: #1a1a1a; color: white; border: none; padding: 8px 16px; border-radius: 8px; cursor: pointer; font-size: 0.85rem; }}
            .refresh:hover {{ background: #333; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>💈 {business_name} — Reservas</h1>
            <button class="refresh" onclick="location.reload()">↻ Refrescar</button>
        </div>
        <div class="container">
            <div class="card">
                <p class="total">Total citas: <strong>{len(reservations)}</strong></p>
                <table>
                    <thead>
                        <tr>
                            <th>Fecha & Hora</th>
                            <th>Cliente</th>
                            <th>Servicio</th>
                            <th>Teléfono</th>
                            <th>Estado</th>
                        </tr>
                    </thead>
                    <tbody>
                        {'<tr><td colspan="5" class="empty">No hay reservas aún.</td></tr>' if not reservations else rows}
                    </tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)

# =====================================================================
# HEALTH CHECK
# =====================================================================

@app.get("/")
async def root():
    return {"status": "running", "bot": "AI Reservation Bot v1.0.0"}
