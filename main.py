print(">>> STARTING BARBERSHOP BOT v1.0.0 ✅")

from dotenv import load_dotenv
load_dotenv()

import os
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, Response, JSONResponse
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
        "hours_open": 9,
        "hours_close": 19,
        "timezone": "America/Bogota",
        "location": "Calle 10 #43-20, El Poblado, Medellín",
        "parking": "Sí hay parqueadero disponible cerca del local.",
        "service_duration": "Aproximadamente 30 minutos por servicio.",
        "mobile": False,
        "reviews": "Puedes ver nuestras reseñas en Google buscando 'Barbería El Paisa Medellín'.",
        "licensed": "Sí, todos nuestros barberos están certificados y capacitados.",
        "payment_methods": "Efectivo, Nequi, Daviplata y transferencia bancaria.",
        "prices": {
            "Corte": "$35.000 COP",
            "Corte + Barba": "$55.000 COP",
            "Afeitado": "$30.000 COP",
            "Corte de Niño": "$25.000 COP"
        },
        "service_details": {
            "Corte": "Incluye lavado, corte personalizado y peinado final.",
            "Corte + Barba": "Incluye lavado, corte, perfilado y arreglo completo de barba con navaja.",
            "Afeitado": "Afeitado clásico con navaja, toalla caliente y crema hidratante.",
            "Corte de Niño": "Corte especializado y tranquilo para niños hasta 12 años."
        }
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

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

try:
    from supabase import create_client
    supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE"))
    print("✅ Supabase connected")
except Exception as e:
    supabase = None
    print(f"ERROR: Supabase connection failed: {e}")

try:
    from twilio.twiml.messaging_response import MessagingResponse
    print("✅ Twilio loaded")
except ImportError:
    print("WARNING: Twilio not available")

# =====================================================================
# DATE RESOLVER
# =====================================================================

WEEKDAY_MAP = {
    "lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2,
    "jueves": 3, "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6
}

def resolve_dates(text: str) -> str:
    today = datetime.now(LOCAL_TZ).date()
    result = text

    if re.search(r"\bpasado\s+ma[ñn]ana\b", result, re.IGNORECASE):
        target = today + timedelta(days=2)
        result = re.sub(r"\bpasado\s+ma[ñn]ana\b", target.strftime("%Y-%m-%d"), result, flags=re.IGNORECASE)

    if re.search(r"\bma[ñn]ana\b", result, re.IGNORECASE):
        target = today + timedelta(days=1)
        result = re.sub(r"\bma[ñn]ana\b", target.strftime("%Y-%m-%d"), result, flags=re.IGNORECASE)

    if re.search(r"\bhoy\b", result, re.IGNORECASE):
        result = re.sub(r"\bhoy\b", today.strftime("%Y-%m-%d"), result, flags=re.IGNORECASE)

    for day_es, day_num in WEEKDAY_MAP.items():
        pattern = rf"\b(?:este\s+|el\s+(?:pr[oó]ximo\s+)?|pr[oó]ximo\s+)?{day_es}\b"
        match = re.search(pattern, result, re.IGNORECASE)
        if match:
            days_ahead = (day_num - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            if re.search(r"pr[oó]ximo", match.group(), re.IGNORECASE):
                days_ahead += 7
            target = today + timedelta(days=days_ahead)
            result = re.sub(pattern, target.strftime("%Y-%m-%d"), result, flags=re.IGNORECASE)

    return result

# =====================================================================
# TIME VALIDATOR — Python handles hour validation, not GPT
# =====================================================================

def extract_and_validate_time(text: str, config: dict) -> tuple[str | None, bool]:
    """
    Extract time from message and validate against business hours.
    Returns (time_str_24h, is_valid)
    """
    open_h = config.get("hours_open", 9)
    close_h = config.get("hours_close", 19)

    # Match "6 pm", "6:30 pm", "18:00", "18", "a las 6", etc.
    # Only match times with explicit am/pm OR preceded by "las"
    match = re.search(
        r"(?:a\s+las\s+|las\s+)(\d{1,2})(?::(\d{2}))?\s*(a\.m\.|p\.m\.|am|pm)?|(\d{1,2})(?::(\d{2}))?\s*(a\.m\.|p\.m\.|am|pm)",
        text, re.IGNORECASE
    )
    if not match:
        return None, True

    if match.group(1):
        hour = int(match.group(1))
        minute = int(match.group(2)) if match.group(2) else 0
        period = match.group(3).lower().replace(".", "") if match.group(3) else None
    else:
        hour = int(match.group(4))
        minute = int(match.group(5)) if match.group(5) else 0
        period = match.group(6).lower().replace(".", "") if match.group(6) else None

    if period is None:
        return None, True  # ambiguous, let GPT handle
        
    # Convert to 24h
    if period in ("pm", "p.m."):
        if hour != 12:
            hour += 12
    elif period in ("am", "a.m."):
        if hour == 12:
            hour = 0

    is_valid = open_h <= hour < close_h
    time_str = f"{hour:02d}:{minute:02d}"
    return time_str, is_valid

# =====================================================================
# CONFIRMATION FORMAT ENFORCER
# =====================================================================

def extract_confirmation_data(text: str) -> dict | None:
    lower = text.lower()
    if not any(phrase in lower for phrase in ["confirmas", "te parece bien", "está bien", "correcto", "confirma la cita"]):
        return None
    has_name = bool(re.search(r"nombre", lower))
    has_service = bool(re.search(r"servicio", lower))
    if not (has_name and has_service):
        return None

    name_match = re.search(r"nombre[:\*\s]+([A-Za-záéíóúñÁÉÍÓÚÑ\s]+?)(?:\n|\*|✂|📅|🕒|servicio|$)", text, re.IGNORECASE)
    name = name_match.group(1).strip().strip("*").strip() if name_match else None

    service_match = re.search(r"servicio[:\*\s]+([A-Za-záéíóúñÁÉÍÓÚÑ\s\+]+?)(?:\n|\*|📅|🕒|fecha|$)", text, re.IGNORECASE)
    service = service_match.group(1).strip().strip("*").strip() if service_match else None

    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    date = date_match.group(1) if date_match else None

    time_match = re.search(r"(\d{1,2}:\d{2})", text)
    if not time_match:
        time_match = re.search(r"(\d{1,2}(?::\d{2})?\s*(?:a\.m\.|p\.m\.|am|pm))", text, re.IGNORECASE)
    time = time_match.group(1).strip() if time_match else None

    if name and service and date and time:
        return {"name": name, "service": service, "date": date, "time": time}
    return None

def format_confirmation(data: dict) -> str:
    return (
        f"Aquí está el resumen de tu cita:\n\n"
        f"👤 Nombre: {data['name']}\n"
        f"✂️ Servicio: {data['service']}\n"
        f"📅 Fecha: {data['date']}\n"
        f"🕒 Hora: {data['time']}\n\n"
        f"¿Confirmas esta información? 😊"
    )

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
# SAVE RESERVATION
# =====================================================================

def save_reservation(phone, business_id, extracted):
    if not supabase:
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
# OPENAI
# =====================================================================

def build_system_prompt(config):
    services_list = ", ".join(config["services"])
    prices_text = "\n".join([f"  - {s}: {p}" for s, p in config.get("prices", {}).items()])
    details_text = "\n".join([f"  - {s}: {d}" for s, d in config.get("service_details", {}).items()])

    return f"""Eres el asistente virtual oficial de {config["name"]} en Medellín, Colombia.

Cuando saludes al cliente por primera vez, SIEMPRE menciona el nombre del negocio. Ejemplo: "¡Hola! Bienvenido a {config["name"]}. ¿En qué te puedo ayudar?"

Tu trabajo es ayudar a los clientes a reservar una cita Y responder preguntas frecuentes sobre el negocio.

Servicios disponibles: {services_list}
Horario: {config["hours"]}
Ubicación: {config.get("location", "Consultar con el negocio")}
Parqueadero: {config.get("parking", "No disponible")}
Duración de cada servicio: {config.get("service_duration", "Aproximadamente 30 minutos")}
¿Servicio a domicilio?: {"Sí" if config.get("mobile") else "No, el cliente debe venir al local"}
Reseñas: {config.get("reviews", "Buscar en Google")}
Barberos certificados: {config.get("licensed", "Sí")}
Métodos de pago: {config.get("payment_methods", "Efectivo y transferencia")}

Precios:
{prices_text}

Detalles de cada servicio:
{details_text}

FLUJO DE RESERVA:
1. Saluda al cliente mencionando el nombre del negocio.
2. Cuando el cliente quiera reservar, pídele su nombre completo, el servicio, la fecha y la hora. Recoge la información como el cliente la vaya dando.
3. Si el cliente responde con información incompleta, solo pregunta por lo que falta. NUNCA hagas preguntas de confirmación como "¿es correcto el nombre?" o "¿es correcta la hora?" — si tienes toda la info, muestra el resumen directamente.
4. Cuando tengas nombre, servicio, fecha Y hora, muestra INMEDIATAMENTE el resumen sin hacer más preguntas.
5. Cuando el cliente confirme, responde EXACTAMENTE con este JSON y nada más:
RESERVA_CONFIRMADA:{{"name":"<nombre>","service":"<servicio>","datetime":"<YYYY-MM-DD HH:MM>"}}

REGLAS:
- Responde siempre en español colombiano, tono amigable y casual.
- Si el cliente pregunta algo que no tiene que ver con el negocio o las reservas, redirígelo amablemente.
- Horario válido: 9:00 AM a 7:00 PM (09:00 a 19:00). Las 6:00 PM = 18:00, que ES válido. Solo rechaza horas antes de las 9:00 AM o después de las 7:00 PM (19:00).
- El formato de fecha SIEMPRE debe ser: YYYY-MM-DD
- El formato de hora SIEMPRE debe ser: HH:MM
- El año actual es 2026. Siempre usa 2026 cuando el cliente no especifique el año.
- Eres el asistente virtual oficial de {config["name"]}. Si alguien pregunta si este es el número correcto o quién eres, confirma que sí.
- Las fechas en los mensajes ya vienen resueltas como YYYY-MM-DD. SIEMPRE usa exactamente esa fecha en el resumen y en el JSON. NUNCA calcules ni inventes fechas."""

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

# =====================================================================
# AVAILABILITY + CANCELLATION + RESCHEDULE
# =====================================================================

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

def reschedule_reservation(phone: str, business_id: int, new_datetime: str) -> dict:
    if not supabase:
        return {"success": False}
    try:
        result = supabase.table("reservations").select("*").eq("contact_phone", phone).eq("business_id", business_id).eq("status", "confirmed").order("datetime", desc=True).limit(1).execute()
        if not result.data:
            return {"success": False, "reason": "no_booking"}
        booking = result.data[0]
        if not is_slot_available(new_datetime, business_id):
            return {"success": False, "reason": "slot_full"}
        supabase.table("reservations").update({"datetime": new_datetime}).eq("reservation_id", booking["reservation_id"]).execute()
        booking["datetime"] = new_datetime
        return {"success": True, "booking": booking}
    except Exception as e:
        print(f"Reschedule error: {e}")
        return {"success": False}

def transcribe_audio(media_url: str) -> str | None:
    """Download audio from Twilio and transcribe with OpenAI Whisper."""
    try:
        import httpx
        account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        response = httpx.get(media_url, auth=(account_sid, auth_token), timeout=30)
        if response.status_code != 200:
            print(f"Failed to download audio: {response.status_code}")
            return None
        audio_bytes = response.content
        import io
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = "audio.ogg"
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="es"
        )
        print(f"🎤 Transcribed: {transcript.text}")
        return transcript.text
    except Exception as e:
        print(f"Transcription error: {e}")
        return None

# =====================================================================
# WEBHOOK
# =====================================================================

@app.post("/webhook")
async def webhook(request: Request):
    form = await request.form()
    incoming_msg = form.get("Body", "").strip()
    media_url = form.get("MediaUrl0", "")
    media_type = form.get("MediaContentType0", "")

    # Handle voice messages
    if media_url and "audio" in media_type:
        transcribed = transcribe_audio(media_url)
        if transcribed:
            incoming_msg = transcribed
        else:
            resp = MessagingResponse()
            resp.message("No pude escuchar tu mensaje de voz. ¿Puedes escribirlo?")
            return Response(content=str(resp), media_type="application/xml")
    from_number = form.get("From", "").replace("whatsapp:", "")
    to_number = form.get("To", "").replace("whatsapp:", "")

    print(f"📩 Message from {from_number} to {to_number}: {incoming_msg}")

    config = BUSINESS_CONFIGS.get(to_number)
    if not config:
        resp = MessagingResponse()
        resp.message("Este número no está configurado.")
        return Response(content=str(resp), media_type="application/xml")

    session = get_session(from_number)
    history = session.get("history", [])

    # Resolve relative dates
    resolved_msg = resolve_dates(incoming_msg)
    if resolved_msg != incoming_msg:
        print(f"📅 Date resolved: '{incoming_msg}' → '{resolved_msg}'")
        # Add explicit note so GPT cannot miss the resolved date
        resolved_msg = resolved_msg + f" [FECHA RESUELTA POR SISTEMA: usa exactamente esta fecha en el resumen]"

    cancel_keywords = ["cancelar", "cancela", "cancel", "quiero cancelar", "cancelar cita"]
    reschedule_keywords = ["cambiar", "reschedule", "reprogramar", "cambiar cita", "mover cita", "otra fecha", "otro horario"]

    if any(kw in incoming_msg.lower() for kw in cancel_keywords):
        result = cancel_reservation(from_number, config["business_id"])
        if result["success"]:
            booking = result["booking"]
            reply = (
                f"✅ Tu cita ha sido cancelada.\n\n"
                f"👤 {booking.get('client_name')}\n"
                f"✂️ {booking.get('service')}\n"
                f"📅 {booking.get('datetime', '')[:16].replace('T', ' ')}\n\n"
                f"Si quieres reservar de nuevo, con gusto te ayudo 😊"
            )
        elif result.get("reason") == "no_booking":
            reply = "No encontré ninguna cita activa para cancelar. ¿Quieres reservar una nueva?"
        else:
            reply = "Hubo un problema cancelando tu cita. Intenta de nuevo."

    elif any(kw in incoming_msg.lower() for kw in reschedule_keywords):
        try:
            resolved_reschedule = resolve_dates(incoming_msg)
            temp_reply = ask_openai(config, history, f"El cliente quiere cambiar su cita. Extrae SOLO la nueva fecha y hora de este mensaje y responde ÚNICAMENTE con el formato YYYY-MM-DD HH:MM, nada más. Si no hay fecha clara responde NO_DATE. Mensaje: {resolved_reschedule}")
            if temp_reply.strip() != "NO_DATE" and len(temp_reply.strip()) == 16:
                new_datetime = temp_reply.strip()
                result = reschedule_reservation(from_number, config["business_id"], new_datetime)
                if result["success"]:
                    booking = result["booking"]
                    reply = (
                        f"✅ ¡Cita reprogramada!\n\n"
                        f"👤 {booking.get('client_name')}\n"
                        f"✂️ {booking.get('service')}\n"
                        f"📅 Nueva fecha: {new_datetime}\n\n"
                        f"¡Te esperamos! 💈"
                    )
                elif result.get("reason") == "slot_full":
                    reply = "Ese horario ya está lleno 😅 ¿Puedes elegir otra fecha u hora?"
                elif result.get("reason") == "no_booking":
                    reply = "No encontré ninguna cita activa para cambiar. ¿Quieres reservar una nueva?"
                else:
                    reply = "Hubo un problema reprogramando tu cita. Intenta de nuevo."
            else:
                reply = "Claro, ¿para qué fecha y hora quieres cambiar tu cita? 📅"
        except Exception as e:
            print(f"Reschedule OpenAI error: {e}")
            reply = "Claro, ¿para qué fecha y hora quieres cambiar tu cita? 📅"

    else:
        try:
            reply = ask_openai(config, history, resolved_msg)
        except Exception as e:
            print(f"OpenAI error: {e}")
            reply = "Hubo un error procesando tu mensaje. Intenta de nuevo."

    # Enforce confirmation format
    original_reply = reply
    if "RESERVA_CONFIRMADA:" not in reply:
        confirmation_data = extract_confirmation_data(reply)
        if confirmation_data:
            reply = format_confirmation(confirmation_data)
            print(f"✅ Confirmation reformatted for {from_number}")

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
                    f"📍 Te esperamos en {config.get('location', 'nuestro local')} 💈"
                )
                session["booked"] = True
        except Exception as e:
            print(f"Error parsing booking: {e}")
            reply = "Hubo un problema al confirmar tu reserva. Intenta de nuevo."

    history.append({"role": "user", "content": incoming_msg})
    history.append({"role": "assistant", "content": original_reply})
    session["history"] = history[-20:]
    save_session(from_number, session)

    resp = MessagingResponse()
    resp.message(reply)
    return Response(content=str(resp), media_type="application/xml")

# =====================================================================
# DASHBOARD API ROUTES
# =====================================================================

@app.post("/api/reservation/{reservation_id}/cancel")
async def api_cancel_reservation(reservation_id: int):
    if not supabase:
        return JSONResponse({"success": False}, status_code=500)
    try:
        supabase.table("reservations").update({"status": "cancelled"}).eq("reservation_id", reservation_id).execute()
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False}, status_code=500)

@app.post("/api/reservation/{reservation_id}/complete")
async def api_complete_reservation(reservation_id: int):
    if not supabase:
        return JSONResponse({"success": False}, status_code=500)
    try:
        supabase.table("reservations").update({"status": "completed"}).eq("reservation_id", reservation_id).execute()
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False}, status_code=500)

@app.post("/api/reservation/{reservation_id}/edit")
async def api_edit_reservation(reservation_id: int, request: Request):
    if not supabase:
        return JSONResponse({"success": False}, status_code=500)
    try:
        body = await request.json()
        update_data = {}
        if body.get("client_name"):
            update_data["client_name"] = body["client_name"]
        if body.get("service"):
            update_data["service"] = body["service"]
        if body.get("datetime"):
            update_data["datetime"] = body["datetime"]
        if body.get("status"):
            update_data["status"] = body["status"]
        supabase.table("reservations").update(update_data).eq("reservation_id", reservation_id).execute()
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False}, status_code=500)

@app.post("/api/reservation/walkin")
async def api_walkin_booking(request: Request):
    if not supabase:
        return JSONResponse({"success": False}, status_code=500)
    try:
        body = await request.json()
        business_id = body.get("business_id")
        datetime_str = body.get("datetime")
        if not is_slot_available(datetime_str, business_id):
            return JSONResponse({"success": False, "reason": "slot_full"})
        supabase.table("reservations").insert({
            "contact_phone": "presencial",
            "business_id": business_id,
            "client_name": body.get("client_name"),
            "service": body.get("service"),
            "datetime": datetime_str,
            "status": "confirmed"
        }).execute()
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"success": False}, status_code=500)

# =====================================================================
# DASHBOARD
# =====================================================================

DIAS_ES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
MESES_ES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]

def format_datetime_display(dt_str: str) -> str:
    try:
        dt_str_clean = dt_str[:16].replace("T", " ")
        dt = datetime.strptime(dt_str_clean, "%Y-%m-%d %H:%M")
        dia = DIAS_ES[dt.weekday()]
        mes = MESES_ES[dt.month - 1]
        hora = dt.strftime("%I:%M %p").lstrip("0")
        return f"{dia} {dt.day} {mes} · {hora}"
    except:
        return dt_str[:16].replace("T", " ")

@app.get("/dashboard/{business_id}", response_class=HTMLResponse)
async def dashboard(request: Request, business_id: int):
    reservations = []
    if supabase:
        try:
            result = supabase.table("reservations").select("*").eq("business_id", business_id).order("datetime").execute()
            reservations = result.data or []
        except Exception as e:
            print(f"Dashboard error: {e}")

    business_name = "Negocio"
    business_services = []
    for config in BUSINESS_CONFIGS.values():
        if config["business_id"] == business_id:
            business_name = config["name"]
            business_services = config.get("services", [])
            break

    today_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    today_reservations = [r for r in reservations if r.get("datetime", "")[:10] == today_str]
    future_reservations = [r for r in reservations if r.get("datetime", "")[:10] > today_str]
    past_reservations = [r for r in reservations if r.get("datetime", "")[:10] < today_str]

    def build_rows(res_list):
        rows = ""
        for r in res_list:
            rid = r.get("reservation_id")
            status = r.get("status", "-")
            if status == "confirmed":
                status_class = "status-confirmed"
                status_label = "Confirmada"
            elif status == "completed":
                status_class = "status-completed"
                status_label = "Completada"
            else:
                status_class = "status-cancelled"
                status_label = "Cancelada"
            dt = r.get("datetime", "")
            dt_display = format_datetime_display(dt) if dt else "-"
            is_presencial = r.get("contact_phone") == "presencial"
            phone_display = "🚶 Presencial" if is_presencial else r.get("contact_phone", "-")
            name_safe = r.get("client_name", "").replace("'", "\\'")
            service_safe = r.get("service", "").replace("'", "\\'")
            dt_edit = dt[:16].replace("T", " ") if dt else ""

            listo_btn = f'<button class="btn-complete" onclick="completeReservation({rid})">✔ Listo</button>' if status == "confirmed" else "—"

            dropdown = ""
            if status == "confirmed":
                dropdown = f"""
                <div class="dropdown">
                    <button class="btn-dots" onclick="toggleDropdown(this)">⋯</button>
                    <div class="dropdown-menu">
                        <div class="dropdown-item" onclick="openEdit({rid}, '{name_safe}', '{service_safe}', '{dt_edit}', '{status}'); closeAllDropdowns()">✏️ Editar</div>
                        <div class="dropdown-item danger" onclick="cancelReservation({rid}); closeAllDropdowns()">✖ Cancelar</div>
                    </div>
                </div>"""

            rows += f"""
            <tr>
                <td>{dt_display}</td>
                <td>{r.get("client_name", "-")}</td>
                <td>{r.get("service", "-")}</td>
                <td>{phone_display}</td>
                <td class="actions">
                    {listo_btn}
                    {dropdown}
                </td>
                <td><span class="status {status_class}">{status_label}</span></td>
            </tr>"""
        return rows

    services_options = "".join([f'<option value="{s}">{s}</option>' for s in business_services])
    hours_options = "".join([f'<option value="{h:02d}:00">{h:02d}:00</option>' for h in range(9, 20)])

    html = f"""<!DOCTYPE html>
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
        .header-btns {{ display: flex; gap: 10px; }}
        .tabs {{ background: white; border-bottom: 1px solid #e0e0e0; padding: 0 32px; display: flex; }}
        .tab {{ padding: 14px 24px; cursor: pointer; font-size: 0.9rem; color: #666; border-bottom: 3px solid transparent; font-weight: 500; transition: all 0.2s; user-select: none; }}
        .tab:hover {{ color: #1a1a1a; }}
        .tab.active {{ color: #1a1a1a; border-bottom-color: #1a1a1a; }}
        .tab-content {{ display: none; }}
        .tab-content.active {{ display: block; }}
        .container {{ max-width: 1100px; margin: 32px auto; padding: 0 16px; }}
        .card {{ background: white; border-radius: 12px; padding: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); margin-bottom: 24px; }}
        .section-title {{ font-size: 1rem; font-weight: 600; margin-bottom: 16px; color: #1a1a1a; display: flex; align-items: center; gap: 8px; }}
        .badge {{ background: #1a1a1a; color: white; font-size: 0.75rem; padding: 2px 8px; border-radius: 20px; }}
        .top-bar {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; flex-wrap: wrap; gap: 12px; }}
        .search-bar {{ display: flex; gap: 8px; flex-wrap: wrap; }}
        .search-bar input {{ padding: 8px 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 0.85rem; width: 150px; }}
        .search-bar button {{ background: #555; color: white; border: none; padding: 8px 12px; border-radius: 8px; cursor: pointer; font-size: 0.85rem; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th {{ background: #1a1a1a; color: white; padding: 12px 16px; text-align: left; font-weight: 500; font-size: 0.85rem; }}
        td {{ padding: 11px 16px; border-bottom: 1px solid #f0f0f0; font-size: 0.85rem; vertical-align: middle; }}
        tr:last-child td {{ border-bottom: none; }}
        tr:hover td {{ background: #fafafa; }}
        .status {{ padding: 3px 10px; border-radius: 20px; font-size: 0.78rem; font-weight: 500; }}
        .status-confirmed {{ background: #e6f4ea; color: #2e7d32; }}
        .status-cancelled {{ background: #fdecea; color: #c62828; }}
        .status-completed {{ background: #e8f0fe; color: #1a73e8; }}
        .actions {{ display: flex; align-items: center; gap: 6px; }}
        .btn-complete {{ background: #e8f0fe; color: #1a73e8; border: 1px solid #c5d8f8; padding: 5px 10px; border-radius: 6px; cursor: pointer; font-size: 0.78rem; font-weight: 500; }}
        .btn-complete:hover {{ background: #c5d8f8; }}
        .dropdown {{ position: relative; }}
        .btn-dots {{ background: #f0f0f0; border: none; padding: 5px 10px; border-radius: 6px; cursor: pointer; font-size: 1rem; color: #555; font-weight: 700; line-height: 1; }}
        .btn-dots:hover {{ background: #e0e0e0; }}
        .dropdown-menu {{ display: none; position: absolute; right: 0; top: 110%; background: white; border: 1px solid #e0e0e0; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); z-index: 50; min-width: 130px; overflow: hidden; }}
        .dropdown-menu.open {{ display: block; }}
        .dropdown-item {{ padding: 10px 14px; font-size: 0.85rem; cursor: pointer; color: #333; }}
        .dropdown-item:hover {{ background: #f5f5f5; }}
        .dropdown-item.danger {{ color: #c62828; }}
        .dropdown-item.danger:hover {{ background: #fdecea; }}
        .empty {{ text-align: center; color: #999; padding: 28px; }}
        .btn-refresh {{ background: #1a1a1a; color: white; border: none; padding: 8px 16px; border-radius: 8px; cursor: pointer; font-size: 0.85rem; }}
        .btn-walkin {{ background: #2e7d32; color: white; border: none; padding: 8px 16px; border-radius: 8px; cursor: pointer; font-size: 0.85rem; }}
        .modal-overlay {{ display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); z-index: 100; justify-content: center; align-items: center; }}
        .modal-overlay.active {{ display: flex; }}
        .modal {{ background: white; border-radius: 12px; padding: 28px; width: 420px; max-width: 95%; }}
        .modal h2 {{ font-size: 1.1rem; margin-bottom: 20px; }}
        .modal label {{ display: block; font-size: 0.85rem; color: #555; margin-bottom: 4px; margin-top: 14px; }}
        .modal input, .modal select {{ width: 100%; padding: 8px 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 0.9rem; }}
        .modal-actions {{ display: flex; gap: 10px; margin-top: 20px; justify-content: flex-end; }}
        .btn-save {{ background: #1a1a1a; color: white; border: none; padding: 8px 18px; border-radius: 8px; cursor: pointer; font-size: 0.85rem; }}
        .btn-close {{ background: #f0f0f0; color: #333; border: none; padding: 8px 18px; border-radius: 8px; cursor: pointer; font-size: 0.85rem; }}
        .error-msg {{ color: #c62828; font-size: 0.83rem; margin-top: 8px; display: none; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>💈 {business_name} — Reservas</h1>
        <div class="header-btns">
            <button class="btn-walkin" onclick="openWalkin()">+ Presencial</button>
            <button class="btn-refresh" onclick="location.reload()">↻ Refrescar</button>
        </div>
    </div>

    <div class="tabs">
        <div class="tab active" onclick="switchTab('hoy', this)">📅 Hoy <span class="badge" style="margin-left:6px">{len(today_reservations)}</span></div>
        <div class="tab" onclick="switchTab('proximas', this)">🗓 Próximas <span class="badge" style="margin-left:6px">{len(future_reservations)}</span></div>
        <div class="tab" onclick="switchTab('historial', this)">🕐 Historial <span class="badge" style="margin-left:6px">{len(past_reservations)}</span></div>
    </div>

    <div class="container">

        <div id="tab-hoy" class="tab-content active">
            <div class="card">
                <div class="section-title">Citas de hoy — {today_str}</div>
                <table>
                    <thead><tr><th>Fecha & Hora</th><th>Cliente</th><th>Servicio</th><th>Teléfono</th><th>Acciones</th><th>Estado</th></tr></thead>
                    <tbody>{"<tr><td colspan='6' class='empty'>Sin citas hoy.</td></tr>" if not today_reservations else build_rows(today_reservations)}</tbody>
                </table>
            </div>
        </div>

        <div id="tab-proximas" class="tab-content">
            <div class="card">
                <div class="top-bar">
                    <div class="section-title">Próximas citas</div>
                    <div class="search-bar">
                        <input type="text" id="searchName" placeholder="Nombre..." oninput="filterTable()">
                        <input type="text" id="searchPhone" placeholder="Teléfono..." oninput="filterTable()">
                        <input type="date" id="searchDate" onchange="filterTable()">
                        <button onclick="clearSearch()">Limpiar</button>
                    </div>
                </div>
                <table>
                    <thead><tr><th>Fecha & Hora</th><th>Cliente</th><th>Servicio</th><th>Teléfono</th><th>Acciones</th><th>Estado</th></tr></thead>
                    <tbody id="futureBody">{"<tr><td colspan='6' class='empty'>Sin citas próximas.</td></tr>" if not future_reservations else build_rows(future_reservations)}</tbody>
                </table>
            </div>
        </div>

        <div id="tab-historial" class="tab-content">
            <div class="card">
                <div class="section-title">Historial</div>
                <table>
                    <thead><tr><th>Fecha & Hora</th><th>Cliente</th><th>Servicio</th><th>Teléfono</th><th>Acciones</th><th>Estado</th></tr></thead>
                    <tbody>{"<tr><td colspan='6' class='empty'>Sin historial.</td></tr>" if not past_reservations else build_rows(past_reservations)}</tbody>
                </table>
            </div>
        </div>

    </div>

    <div class="modal-overlay" id="editModal">
        <div class="modal">
            <h2>✏️ Editar Reserva</h2>
            <input type="hidden" id="editId">
            <label>Nombre del cliente</label>
            <input type="text" id="editName">
            <label>Servicio</label>
            <input type="text" id="editService">
            <label>Fecha y hora (YYYY-MM-DD HH:MM)</label>
            <input type="text" id="editDatetime" placeholder="2026-04-10 15:00">
            <label>Estado</label>
            <select id="editStatus">
                <option value="confirmed">Confirmada</option>
                <option value="completed">Completada</option>
                <option value="cancelled">Cancelada</option>
            </select>
            <div class="modal-actions">
                <button class="btn-close" onclick="closeModal('editModal')">Cancelar</button>
                <button class="btn-save" onclick="saveEdit()">Guardar</button>
            </div>
        </div>
    </div>

    <div class="modal-overlay" id="walkinModal">
        <div class="modal">
            <h2>🚶 Reserva Presencial</h2>
            <label>Nombre del cliente</label>
            <input type="text" id="walkinName" placeholder="Nombre completo">
            <label>Servicio</label>
            <select id="walkinService">{services_options}</select>
            <label>Fecha</label>
            <input type="date" id="walkinDate">
            <label>Hora</label>
            <select id="walkinHour">{hours_options}</select>
            <p class="error-msg" id="walkinError">Ese horario ya está lleno. Elige otra hora.</p>
            <div class="modal-actions">
                <button class="btn-close" onclick="closeModal('walkinModal')">Cancelar</button>
                <button class="btn-save" id="walkinSaveBtn" onclick="saveWalkin()">Confirmar</button>
            </div>
        </div>
    </div>

    <script>
        function switchTab(name, el) {{
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
            document.getElementById('tab-' + name).classList.add('active');
            el.classList.add('active');
        }}
        function toggleDropdown(btn) {{
            closeAllDropdowns();
            const menu = btn.nextElementSibling;
            menu.classList.toggle('open');
            event.stopPropagation();
        }}
        function closeAllDropdowns() {{
            document.querySelectorAll('.dropdown-menu').forEach(m => m.classList.remove('open'));
        }}
        document.addEventListener('click', closeAllDropdowns);
        function filterTable() {{
            const name = document.getElementById('searchName').value.toLowerCase();
            const phone = document.getElementById('searchPhone').value.toLowerCase();
            const date = document.getElementById('searchDate').value;
            const rows = document.querySelectorAll('#futureBody tr');
            rows.forEach(row => {{
                const cells = row.querySelectorAll('td');
                if (cells.length < 4) return;
                const matchName = cells[1].textContent.toLowerCase().includes(name);
                const matchPhone = cells[3].textContent.toLowerCase().includes(phone);
                const matchDate = !date || cells[0].textContent.includes(date);
                row.style.display = (matchName && matchPhone && matchDate) ? '' : 'none';
            }});
        }}
        function clearSearch() {{
            document.getElementById('searchName').value = '';
            document.getElementById('searchPhone').value = '';
            document.getElementById('searchDate').value = '';
            filterTable();
        }}
        function openEdit(id, name, service, datetime, status) {{
            document.getElementById('editId').value = id;
            document.getElementById('editName').value = name;
            document.getElementById('editService').value = service;
            document.getElementById('editDatetime').value = datetime.replace('T', ' ');
            document.getElementById('editStatus').value = status;
            document.getElementById('editModal').classList.add('active');
        }}
        function openWalkin() {{
            document.getElementById('walkinError').style.display = 'none';
            document.getElementById('walkinName').value = '';
            document.getElementById('walkinDate').value = '{today_str}';
            document.getElementById('walkinSaveBtn').disabled = false;
            document.getElementById('walkinModal').classList.add('active');
        }}
        function closeModal(id) {{
            document.getElementById(id).classList.remove('active');
        }}
        async function saveEdit() {{
            const id = document.getElementById('editId').value;
            const data = {{
                client_name: document.getElementById('editName').value,
                service: document.getElementById('editService').value,
                datetime: document.getElementById('editDatetime').value,
                status: document.getElementById('editStatus').value
            }};
            const res = await fetch(`/api/reservation/${{id}}/edit`, {{
                method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(data)
            }});
            const result = await res.json();
            if (result.success) {{ closeModal('editModal'); location.reload(); }}
            else {{ alert('Error al guardar.'); }}
        }}
        async function saveWalkin() {{
            document.getElementById('walkinSaveBtn').disabled = true;
            const date = document.getElementById('walkinDate').value;
            const hour = document.getElementById('walkinHour').value;
            const datetime = date + ' ' + hour;
            const data = {{
                business_id: {business_id},
                client_name: document.getElementById('walkinName').value,
                service: document.getElementById('walkinService').value,
                datetime: datetime
            }};
            const res = await fetch('/api/reservation/walkin', {{
                method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(data)
            }});
            const result = await res.json();
            if (result.success) {{ closeModal('walkinModal'); location.reload(); }}
            else if (result.reason === 'slot_full') {{
                document.getElementById('walkinError').style.display = 'block';
                document.getElementById('walkinSaveBtn').disabled = false;
            }}
            else {{
                alert('Error al guardar.');
                document.getElementById('walkinSaveBtn').disabled = false;
            }}
        }}
        async function completeReservation(id) {{
            if (!confirm('¿Marcar esta cita como completada?')) return;
            const res = await fetch(`/api/reservation/${{id}}/complete`, {{method: 'POST'}});
            const result = await res.json();
            if (result.success) {{ location.reload(); }}
            else {{ alert('Error.'); }}
        }}
        async function cancelReservation(id) {{
            if (!confirm('¿Seguro que quieres cancelar esta reserva?')) return;
            const res = await fetch(`/api/reservation/${{id}}/cancel`, {{method: 'POST'}});
            const result = await res.json();
            if (result.success) {{ location.reload(); }}
            else {{ alert('Error al cancelar.'); }}
        }}
    </script>
</body>
</html>"""
    return HTMLResponse(content=html)

# =====================================================================
# HEALTH CHECK
# =====================================================================

@app.get("/")
async def root():
    return {"status": "running", "bot": "AI Reservation Bot v1.0.0"}
