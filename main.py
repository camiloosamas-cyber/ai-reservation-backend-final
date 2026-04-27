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
        "slot_duration": 30,
        "timezone": "America/Bogota",
        "location": "Calle 10 #43-20, El Poblado, Medellín",
        "parking": "Sí hay parqueadero disponible cerca del local.",
        "service_duration": "Aproximadamente 30 minutos por servicio.",
        "mobile": False,
        "reviews": "Puedes ver nuestras reseñas en Google buscando 'Barbería El Paisa Medellín'.",
        "licensed": "Sí, todos nuestros barberos están certificados y capacitados.",
        "payment_methods": "Efectivo, Nequi, Daviplata y transferencia bancaria.",
        "avg_price": 35000,
        "prices": {
            "Corte": "$35.000 COP",
            "Corte + Barba": "$55.000 COP",
            "Afeitado": "$30.000 COP",
            "Corte de Niño": "$25.000 COP"
        },
        "service_prices": {
            "Corte": 35000,
            "Corte + Barba": 55000,
            "Afeitado": 30000,
            "Corte de Niño": 25000
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
# TIME VALIDATOR
# =====================================================================

def extract_and_validate_time(text: str, config: dict) -> tuple[str | None, bool]:
    open_h = config.get("hours_open", 9)
    close_h = config.get("hours_close", 19)

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
        return None, True

    if period in ("pm",):
        if hour != 12:
            hour += 12
    elif period in ("am",):
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
3. Si el cliente responde con información incompleta, solo pregunta por lo que falta. NUNCA hagas preguntas de confirmación como "¿es correcto el nombre?" — si tienes toda la info, muestra el resumen directamente.
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
- Las fechas en los mensajes ya vienen resueltas como YYYY-MM-DD. SIEMPRE usa exactamente esa fecha en el resumen y en el JSON. NUNCA calcules ni inventes fechas.
- Si el cliente pregunta por disponibilidad, horarios disponibles, o cuándo pueden atenderlo, responde SOLO con: CONSULTA_DISPONIBILIDAD"""

def ask_openai(config, history, new_message):
    system_prompt = build_system_prompt(config)
    messages = [{"role": "system", "content": system_prompt}]
    messages += history
    messages.append({"role": "user", "content": new_message})
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        max_tokens=500,
        temperature=0.3
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

def get_available_slots(business_id: int, config: dict, days_ahead: int = 7) -> list:
    today = datetime.now(LOCAL_TZ).date()
    open_h = config.get("hours_open", 9)
    close_h = config.get("hours_close", 19)
    slot_duration = config.get("slot_duration", 30)
    available = []

    for i in range(1, days_ahead + 1):
        check_date = today + timedelta(days=i)
        if check_date.weekday() == 6:
            continue
        slots_for_day = []
        current_hour = open_h
        current_min = 0
        while True:
            slot_end_min = current_min + slot_duration
            end_hour = current_hour + slot_end_min // 60
            if end_hour > close_h:
                break
            dt_str = f"{check_date.strftime('%Y-%m-%d')} {current_hour:02d}:{current_min:02d}"
            if is_slot_available(dt_str, business_id):
                slots_for_day.append(f"{current_hour:02d}:{current_min:02d}")
            current_min += slot_duration
            if current_min >= 60:
                current_hour += 1
                current_min = current_min % 60
        if slots_for_day:
            available.append({"date": check_date, "slots": slots_for_day})

    return available

def transcribe_audio(media_url: str) -> str | None:
    try:
        import httpx
        account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        response = httpx.get(media_url, auth=(account_sid, auth_token), timeout=30, follow_redirects=True)
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

    resolved_msg = resolve_dates(incoming_msg)
    if resolved_msg != incoming_msg:
        print(f"📅 Date resolved: '{incoming_msg}' → '{resolved_msg}'")
        resolved_msg = resolved_msg + f" [FECHA RESUELTA POR SISTEMA: usa exactamente esta fecha en el resumen]"

    cancel_keywords = ["cancelar", "cancela", "cancel", "quiero cancelar", "cancelar cita"]
    reschedule_keywords = ["cambiar", "reschedule", "reprogramar", "cambiar cita", "mover cita", "otra fecha", "otro horario"]
    availability_keywords = ["disponibilidad", "cuando tienen", "cuándo tienen", "qué días", "que dias", "horarios disponibles", "cuando puedo", "cuándo puedo"]

    def fmt_slot(s):
        h, m = map(int, s.split(":"))
        period = "AM" if h < 12 else "PM"
        h12 = h if h <= 12 else h - 12
        if h12 == 0: h12 = 12
        return f"{h12}:{str(m).zfill(2)} {period}"

    if any(kw in incoming_msg.lower() for kw in availability_keywords):
        slots = get_available_slots(config["business_id"], config)
        if not slots:
            reply = "Lo siento, no hay disponibilidad en los próximos 7 días. Contáctanos directamente."
        else:
            lines = ["Tenemos disponibilidad en los siguientes horarios:\n"]
            for day in slots[:3]:
                date_obj = day["date"]
                dia = DIAS_ES[date_obj.weekday()]
                mes = MESES_ES[date_obj.month - 1]
                preferred = [s for s in day["slots"] if s in ["09:00", "10:00", "11:00", "13:00", "14:00", "15:00", "16:00", "17:00"]]
                slot_list = " · ".join(fmt_slot(s) for s in (preferred if preferred else day["slots"][:6]))
                lines.append(f"{dia} {date_obj.day} {mes} → {slot_list}")
            lines.append("\n¿Cuál te queda mejor? 😊")
            reply = "\n".join(lines)

    elif any(kw in incoming_msg.lower() for kw in cancel_keywords):
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

    original_reply = reply
    if "RESERVA_CONFIRMADA:" not in reply:
        confirmation_data = extract_confirmation_data(reply)
        if confirmation_data:
            reply = format_confirmation(confirmation_data)
            print(f"✅ Confirmation reformatted for {from_number}")

    if "RESERVA_CONFIRMADA:" in reply:
        try:
            json_str = reply.split("RESERVA_CONFIRMADA:")[1].strip()
            json_end = json_str.index("}") + 1
            extracted = json.loads(json_str[:json_end])
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
    history.append({"role": "assistant", "content": reply})
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
DIAS_SHORT = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
MESES_ES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]

def format_datetime_display(dt_str: str) -> tuple[str, str]:
    try:
        dt_str_clean = dt_str[:16].replace("T", " ")
        dt = datetime.strptime(dt_str_clean, "%Y-%m-%d %H:%M")
        dia = DIAS_SHORT[dt.weekday()]
        mes = MESES_ES[dt.month - 1]
        hora = dt.strftime("%I:%M %p").lstrip("0")
        date_part = f"{dia} {dt.day} {mes}"
        return date_part, hora
    except:
        raw = dt_str[:16].replace("T", " ")
        return raw, ""

def format_price(service: str, config: dict) -> str:
    prices = config.get("service_prices", {})
    price = prices.get(service, config.get("avg_price", 35000))
    return f"${price:,}".replace(",", ".")

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
    business_config = {}
    for config in BUSINESS_CONFIGS.values():
        if config["business_id"] == business_id:
            business_name = config["name"]
            business_services = config.get("services", [])
            business_config = config
            break

    today_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    now = datetime.now(LOCAL_TZ)

    today_reservations = [r for r in reservations if r.get("datetime", "")[:10] == today_str]
    future_reservations = [r for r in reservations if r.get("datetime", "")[:10] > today_str]
    past_reservations = [r for r in reservations if r.get("datetime", "")[:10] < today_str]

    # Stats
    current_month = now.strftime("%Y-%m")
    month_reservations = [r for r in reservations if r.get("datetime", "")[:7] == current_month and r.get("status") == "confirmed"]
    month_completed = [r for r in reservations if r.get("datetime", "")[:7] == current_month and r.get("status") == "completed"]
    month_all = month_reservations + month_completed
    month_count = len(month_all)

    service_prices = business_config.get("service_prices", {})
    avg_price = business_config.get("avg_price", 35000)
    month_revenue = sum(service_prices.get(r.get("service", ""), avg_price) for r in month_all)

    month_cancelled = len([r for r in reservations if r.get("datetime", "")[:7] == current_month and r.get("status") == "cancelled"])
    today_count = len(today_reservations)
    upcoming_count = len(future_reservations)

    def fmt_currency(amount):
        if amount >= 1000000:
            return f"${amount/1000000:.1f}M"
        elif amount >= 1000:
            return f"${amount/1000:.0f}K"
        return f"${amount:,}"

    services_options = "".join([f'<option value="{s}">{s}</option>' for s in business_services])
    hours_options = "".join([f'<option value="{h:02d}:00">{h:02d}:00</option>' for h in range(9, 20)])

    def build_today_cards(res_list):
        if not res_list:
            return '<div class="empty-state">Sin citas programadas para hoy</div>'
        cards = ""
        for r in res_list:
            rid = r.get("reservation_id")
            status = r.get("status", "-")
            dt = r.get("datetime", "")
            date_part, time_part = format_datetime_display(dt)
            is_presencial = r.get("contact_phone") == "presencial"
            phone_display = "Presencial" if is_presencial else r.get("contact_phone", "-")
            name_safe = r.get("client_name", "").replace("'", "\\'")
            service_safe = r.get("service", "").replace("'", "\\'")
            dt_edit = dt[:16].replace("T", " ") if dt else ""
            price = format_price(r.get("service", ""), business_config)

            if status == "confirmed":
                status_html = '<span class="badge badge-green">Confirmada</span>'
                actions = (
                    f'<button class="btn-done" onclick="completeReservation({rid})">✔ Listo</button>'
                    f'<div class="dots-wrap">'
                    f'<button class="btn-dots-sm" onclick="toggleDropdown(this)">⋯</button>'
                    f'<div class="drop-menu">'
                    f'<div class="drop-item" onclick="openEdit({rid},\'{name_safe}\',\'{service_safe}\',\'{dt_edit}\',\'{status}\')">✏️ Editar</div>'
                    f'<div class="drop-item danger" onclick="cancelReservation({rid})">✖ Cancelar</div>'
                    f'</div>'
                    f'</div>'
                )
            elif status == "completed":
                status_html = '<span class="badge badge-blue">Completada</span>'
                actions = f'<button class="btn-edit-sm" onclick="openEdit({rid},\'{name_safe}\',\'{service_safe}\',\'{dt_edit}\',\'{status}\')">✏️</button>'
            else:
                status_html = '<span class="badge badge-red">Cancelada</span>'
                actions = f'<button class="btn-edit-sm" onclick="openEdit({rid},\'{name_safe}\',\'{service_safe}\',\'{dt_edit}\',\'{status}\')">✏️</button>'

            cards += f"""
            <div class="appt-card">
                <div class="appt-time">{time_part}</div>
                <div class="appt-sep"></div>
                <div class="appt-info">
                    <div class="appt-name">{r.get("client_name", "-")}</div>
                    <div class="appt-meta">{r.get("service", "-")} · {price} · {phone_display}</div>
                </div>
                {status_html}
                <div class="appt-actions">{actions}</div>
            </div>"""
        return cards

    def build_table_rows(res_list):
        if not res_list:
            return '<tr><td colspan="6" class="empty-state">Sin citas</td></tr>'
        rows = ""
        for r in res_list:
            rid = r.get("reservation_id")
            status = r.get("status", "-")
            dt = r.get("datetime", "")
            date_part, time_part = format_datetime_display(dt)
            is_presencial = r.get("contact_phone") == "presencial"
            phone_display = "🚶 Presencial" if is_presencial else r.get("contact_phone", "-")
            name_safe = r.get("client_name", "").replace("'", "\\'")
            service_safe = r.get("service", "").replace("'", "\\'")
            dt_edit = dt[:16].replace("T", " ") if dt else ""

            if status == "confirmed":
                status_html = '<span class="badge badge-green">Confirmada</span>'
                actions = (
                    f'<button class="btn-done" onclick="completeReservation({rid})">✔ Listo</button>'
                    f'<div class="dots-wrap">'
                    f'<button class="btn-dots-sm" onclick="toggleDropdown(this)">⋯</button>'
                    f'<div class="drop-menu">'
                    f'<div class="drop-item" onclick="openEdit({rid},\'{name_safe}\',\'{service_safe}\',\'{dt_edit}\',\'{status}\')">✏️ Editar</div>'
                    f'<div class="drop-item danger" onclick="cancelReservation({rid})">✖ Cancelar</div>'
                    f'</div>'
                    f'</div>'
                )
            elif status == "completed":
                status_html = '<span class="badge badge-blue">Completada</span>'
                actions = f'<button class="btn-edit-sm" onclick="openEdit({rid},\'{name_safe}\',\'{service_safe}\',\'{dt_edit}\',\'{status}\')">✏️</button>'
            else:
                status_html = '<span class="badge badge-red">Cancelada</span>'
                actions = f'<button class="btn-edit-sm" onclick="openEdit({rid},\'{name_safe}\',\'{service_safe}\',\'{dt_edit}\',\'{status}\')">✏️</button>'

            rows += f"""
            <tr>
                <td><span class="td-date">{date_part}</span><span class="td-time">{time_part}</span></td>
                <td class="td-name">{r.get("client_name", "-")}</td>
                <td>{r.get("service", "-")}</td>
                <td class="td-phone">{phone_display}</td>
                <td>{status_html}</td>
                <td class="td-actions">{actions}</td>
            </tr>"""
        return rows

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{business_name} — Panel</title>
    <link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg: #0f0f0f;
            --surface: #1a1a1a;
            --surface2: #222;
            --border: #2a2a2a;
            --text: #f0f0f0;
            --muted: #666;
            --muted2: #444;
            --green: #22c55e;
            --green-dim: rgba(34,197,94,0.12);
            --green-border: rgba(34,197,94,0.25);
            --red: #ef4444;
            --red-dim: rgba(239,68,68,0.1);
            --blue: #3b82f6;
            --blue-dim: rgba(59,130,246,0.1);
            --amber: #f59e0b;
        }}
        * {{ margin:0; padding:0; box-sizing:border-box; }}
        body {{ font-family:'DM Sans',sans-serif; background:var(--bg); color:var(--text); min-height:100vh; }}

        /* HEADER */
        .header {{ background:var(--surface); border-bottom:1px solid var(--border); padding:14px 24px; display:flex; align-items:center; justify-content:space-between; position:sticky; top:0; z-index:100; }}
        .brand {{ display:flex; align-items:center; gap:10px; }}
        .brand-icon {{ width:34px; height:34px; background:var(--green-dim); border:1px solid var(--green-border); border-radius:9px; display:flex; align-items:center; justify-content:center; font-size:17px; }}
        .brand-name {{ font-size:0.9rem; font-weight:600; }}
        .brand-sub {{ font-size:0.68rem; color:var(--muted); margin-top:1px; }}
        .header-right {{ display:flex; align-items:center; gap:8px; }}
        .live-badge {{ display:flex; align-items:center; gap:5px; background:var(--green-dim); border:1px solid var(--green-border); padding:4px 9px; border-radius:20px; font-size:0.7rem; color:var(--green); font-weight:500; }}
        .live-dot {{ width:5px; height:5px; background:var(--green); border-radius:50%; animation:pulse 2s infinite; }}
        @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:0.3}} }}
        .btn-presencial {{ background:var(--green); color:#000; border:none; padding:7px 13px; border-radius:8px; font-size:0.78rem; font-weight:700; cursor:pointer; font-family:inherit; }}
        .btn-refresh {{ background:var(--surface2); color:var(--muted); border:1px solid var(--border); padding:7px 11px; border-radius:8px; font-size:0.8rem; cursor:pointer; font-family:inherit; }}

        /* TABS */
        .tabs {{ background:var(--surface); border-bottom:1px solid var(--border); padding:0 24px; display:flex; }}
        .tab {{ padding:11px 16px; font-size:0.8rem; color:var(--muted); cursor:pointer; border-bottom:2px solid transparent; font-weight:500; transition:all 0.15s; display:flex; align-items:center; gap:5px; user-select:none; }}
        .tab.active {{ color:var(--text); border-bottom-color:var(--green); }}
        .tab-count {{ background:var(--surface2); color:var(--muted2); font-size:0.67rem; padding:1px 6px; border-radius:10px; font-family:'DM Mono',monospace; }}
        .tab.active .tab-count {{ background:var(--green-dim); color:var(--green); }}
        .tab-content {{ display:none; }}
        .tab-content.active {{ display:block; }}

        /* CONTAINER */
        .container {{ max-width:1020px; margin:0 auto; padding:22px 20px; }}

        /* STATS */
        .stats-row {{ display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin-bottom:22px; }}
        .stat-card {{ background:var(--surface); border:1px solid var(--border); border-radius:11px; padding:14px 16px; }}
        .stat-label {{ font-size:0.68rem; color:var(--muted); text-transform:uppercase; letter-spacing:0.06em; margin-bottom:7px; }}
        .stat-value {{ font-size:1.55rem; font-weight:700; font-family:'DM Mono',monospace; line-height:1; }}
        .stat-sub {{ font-size:0.68rem; color:var(--muted); margin-top:4px; }}
        .stat-green .stat-value {{ color:var(--green); }}
        .stat-blue .stat-value {{ color:var(--blue); }}

        /* SECTION */
        .section-header {{ display:flex; align-items:center; justify-content:space-between; margin-bottom:12px; }}
        .section-title {{ font-size:0.72rem; font-weight:600; color:var(--muted); text-transform:uppercase; letter-spacing:0.07em; }}
        .section-right {{ font-size:0.72rem; color:var(--muted); font-family:'DM Mono',monospace; }}

        /* APPT CARDS */
        .appt-list {{ display:flex; flex-direction:column; gap:7px; margin-bottom:26px; }}
        .appt-card {{ background:var(--surface); border:1px solid var(--border); border-radius:11px; padding:12px 14px; display:flex; align-items:center; gap:12px; transition:border-color 0.15s; }}
        .appt-card:hover {{ border-color:#333; }}
        .appt-time {{ font-family:'DM Mono',monospace; font-size:0.82rem; font-weight:500; min-width:62px; color:var(--text); }}
        .appt-sep {{ width:1px; height:28px; background:var(--border); flex-shrink:0; }}
        .appt-info {{ flex:1; min-width:0; }}
        .appt-name {{ font-size:0.85rem; font-weight:600; }}
        .appt-meta {{ font-size:0.72rem; color:var(--muted); margin-top:2px; }}
        .appt-actions {{ display:flex; gap:5px; align-items:center; flex-shrink:0; }}

        /* BADGES */
        .badge {{ padding:3px 8px; border-radius:6px; font-size:0.68rem; font-weight:600; white-space:nowrap; }}
        .badge-green {{ background:var(--green-dim); color:var(--green); }}
        .badge-blue {{ background:var(--blue-dim); color:var(--blue); }}
        .badge-red {{ background:var(--red-dim); color:var(--red); }}

        /* BUTTONS */
        .btn-done {{ background:var(--green-dim); color:var(--green); border:1px solid var(--green-border); padding:5px 11px; border-radius:7px; font-size:0.73rem; font-weight:600; cursor:pointer; font-family:inherit; white-space:nowrap; }}
        .btn-done:hover {{ background:rgba(34,197,94,0.2); }}
        .btn-dots-sm {{ background:var(--surface2); color:var(--muted); border:1px solid var(--border); padding:5px 9px; border-radius:7px; font-size:0.85rem; cursor:pointer; font-family:inherit; position:relative; }}
        .btn-edit-sm {{ background:var(--surface2); color:var(--muted); border:1px solid var(--border); padding:5px 9px; border-radius:7px; font-size:0.78rem; cursor:pointer; font-family:inherit; }}
        .drop-menu {{ display:none; position:absolute; right:0; top:110%; background:var(--surface); border:1px solid var(--border); border-radius:9px; box-shadow:0 8px 24px rgba(0,0,0,0.4); z-index:200; min-width:130px; overflow:hidden; text-align:left; }}
        .drop-menu.open {{ display:block; }}
        .drop-item {{ padding:9px 13px; font-size:0.8rem; cursor:pointer; color:var(--text); white-space:nowrap; }}
        .drop-item:hover {{ background:var(--surface2); }}
        .drop-item.danger {{ color:var(--red); }}
        .drop-item.danger:hover {{ background:var(--red-dim); }}

        /* TABLE */
        .table-card {{ background:var(--surface); border:1px solid var(--border); border-radius:11px; overflow:visible; }}
        .table-header {{ padding:13px 16px; border-bottom:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap; }}
        .search-row {{ display:flex; gap:7px; }}
        .search-input {{ background:var(--surface2); border:1px solid var(--border); color:var(--text); padding:6px 11px; border-radius:7px; font-size:0.77rem; font-family:inherit; outline:none; width:160px; }}
        .search-input::placeholder {{ color:var(--muted); }}
        table {{ width:100%; border-collapse:collapse; }}
        th {{ padding:9px 14px; text-align:left; font-size:0.67rem; font-weight:600; color:var(--muted); text-transform:uppercase; letter-spacing:0.05em; border-bottom:1px solid var(--border); }}
        td {{ padding:11px 14px; font-size:0.8rem; vertical-align:middle; }}
        tbody tr {{ border-bottom:1px solid var(--border); }}
        tbody tr:last-child {{ border-bottom:none; }}
        tr:hover td {{ background:rgba(255,255,255,0.015); }}
        .td-date {{ display:block; font-weight:500; }}
        .td-time {{ display:block; font-size:0.7rem; color:var(--muted); font-family:'DM Mono',monospace; margin-top:1px; }}
        .td-name {{ font-weight:500; }}
        .td-phone {{ font-size:0.75rem; color:var(--muted); font-family:'DM Mono',monospace; }}
        table {{ table-layout: fixed; width: 100%; }}
        table th:nth-last-child(2), table td:nth-last-child(2) {{ width:140px; text-align:left; padding-right:8px; box-sizing:border-box; overflow:hidden; }}
        table th:last-child, table td:last-child {{ width:150px; padding-right:16px; box-sizing:border-box; overflow:visible; border-left:none; background:transparent; }}
        .dots-wrap {{ position:relative; display:inline-block; }}


        .empty-state {{ text-align:center; padding:36px; color:var(--muted); font-size:0.82rem; }}

        /* MODAL */
        .modal-overlay {{ display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.7); z-index:300; justify-content:center; align-items:center; }}
        .modal-overlay.active {{ display:flex; }}
        .modal {{ background:var(--surface); border:1px solid var(--border); border-radius:14px; padding:26px; width:400px; max-width:95%; }}
        .modal h2 {{ font-size:1rem; font-weight:600; margin-bottom:18px; }}
        .modal label {{ display:block; font-size:0.75rem; color:var(--muted); margin-bottom:4px; margin-top:13px; }}
        .modal input, .modal select {{ width:100%; padding:8px 11px; background:var(--surface2); border:1px solid var(--border); border-radius:8px; font-size:0.85rem; color:var(--text); font-family:inherit; outline:none; }}
        .modal-actions {{ display:flex; gap:8px; margin-top:18px; justify-content:flex-end; }}
        .btn-save {{ background:var(--green); color:#000; border:none; padding:8px 16px; border-radius:8px; cursor:pointer; font-size:0.82rem; font-weight:700; font-family:inherit; }}
        .btn-cancel-modal {{ background:var(--surface2); color:var(--muted); border:1px solid var(--border); padding:8px 16px; border-radius:8px; cursor:pointer; font-size:0.82rem; font-family:inherit; }}
        .error-msg {{ color:var(--red); font-size:0.78rem; margin-top:7px; display:none; }}

        @media(max-width:640px) {{
            .stats-row {{ grid-template-columns:repeat(2,1fr); }}
            .header {{ padding:12px 16px; }}
            .container {{ padding:16px; }}
            .td-phone {{ display:none; }}
            .appt-meta {{ font-size:0.68rem; }}
        }}
    </style>
</head>
<body>

<div class="header">
    <div class="brand">
        <div class="brand-icon">💈</div>
        <div>
            <div class="brand-name">{business_name}</div>
            <div class="brand-sub">Panel de reservas</div>
        </div>
    </div>
    <div class="header-right">
        <div class="live-badge"><div class="live-dot"></div>Bot activo</div>
        <button class="btn-presencial" onclick="openWalkin()">+ Presencial</button>
        <button class="btn-refresh" onclick="location.reload()">↻</button>
    </div>
</div>

<div class="tabs">
    <div class="tab active" onclick="switchTab('hoy',this)">📅 Hoy <span class="tab-count">{today_count}</span></div>
    <div class="tab" onclick="switchTab('proximas',this)">🗓 Próximas <span class="tab-count">{upcoming_count}</span></div>
    <div class="tab" onclick="switchTab('historial',this)">🕐 Historial <span class="tab-count">{len(past_reservations)}</span></div>
</div>

<div class="container">

    <!-- STATS -->
    <div class="stats-row">
        <div class="stat-card">
            <div class="stat-label">Hoy</div>
            <div class="stat-value">{today_count}</div>
            <div class="stat-sub">{len([r for r in today_reservations if r.get('status')=='confirmed'])} confirmadas</div>
        </div>
        <div class="stat-card stat-green">
            <div class="stat-label">Este mes</div>
            <div class="stat-value">{month_count}</div>
            <div class="stat-sub">citas agendadas</div>
        </div>
        <div class="stat-card stat-blue">
            <div class="stat-label">Ingresos est.</div>
            <div class="stat-value">{fmt_currency(month_revenue)}</div>
            <div class="stat-sub">COP este mes</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Canceladas</div>
            <div class="stat-value">{month_cancelled}</div>
            <div class="stat-sub">este mes</div>
        </div>
    </div>

    <!-- HOY TAB -->
    <div id="tab-hoy" class="tab-content active">
        <div class="section-header">
            <div class="section-title">Citas de hoy</div>
            <div class="section-right">{DIAS_ES[now.weekday()]} {now.day} {MESES_ES[now.month-1]} {now.year}</div>
        </div>
        <div class="appt-list">
            {build_today_cards(today_reservations)}
        </div>
    </div>

    <!-- PROXIMAS TAB -->
    <div id="tab-proximas" class="tab-content">
        <div class="table-card">
            <div class="table-header">
                <div class="section-title">Próximas citas</div>
                <div class="search-row">
                    <input class="search-input" id="searchName" placeholder="Nombre..." oninput="filterTable()">
                    <input class="search-input" id="searchPhone" placeholder="Teléfono..." oninput="filterTable()" style="width:130px">
                    <input class="search-input" type="date" id="searchDate" onchange="filterTable()" style="width:140px">
                </div>
            </div>
            <table>
                <thead><tr><th>Fecha & Hora</th><th>Cliente</th><th>Servicio</th><th>Teléfono</th><th>Estado</th><th>Acciones</th></tr></thead>
                <tbody id="futureBody">{build_table_rows(future_reservations)}</tbody>
            </table>
        </div>
    </div>

    <!-- HISTORIAL TAB -->
    <div id="tab-historial" class="tab-content">
        <div class="table-card">
            <div class="table-header">
                <div class="section-title">Historial</div>
            </div>
            <table>
                <thead><tr><th>Fecha & Hora</th><th>Cliente</th><th>Servicio</th><th>Teléfono</th><th>Estado</th><th>Acciones</th></tr></thead>
                <tbody>{build_table_rows(past_reservations)}</tbody>
            </table>
        </div>
    </div>

</div>

<!-- EDIT MODAL -->
<div class="modal-overlay" id="editModal">
    <div class="modal">
        <h2>✏️ Editar Reserva</h2>
        <input type="hidden" id="editId">
        <label>Nombre</label>
        <input type="text" id="editName">
        <label>Servicio</label>
        <input type="text" id="editService">
        <label>Fecha y hora (YYYY-MM-DD HH:MM)</label>
        <input type="text" id="editDatetime" placeholder="2026-04-25 15:00">
        <label>Estado</label>
        <select id="editStatus">
            <option value="confirmed">Confirmada</option>
            <option value="completed">Completada</option>
            <option value="cancelled">Cancelada</option>
        </select>
        <div class="modal-actions">
            <button class="btn-cancel-modal" onclick="closeModal('editModal')">Cancelar</button>
            <button class="btn-save" onclick="saveEdit()">Guardar</button>
        </div>
    </div>
</div>

<!-- WALKIN MODAL -->
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
            <button class="btn-cancel-modal" onclick="closeModal('walkinModal')">Cancelar</button>
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

    document.addEventListener('click', function(e) {{
        if (!e.target.closest('.btn-dots-sm')) {{
            document.querySelectorAll('.drop-menu').forEach(m => m.classList.remove('open'));
        }}
    }});

    function toggleDropdown(btn) {{
        const menu = btn.parentElement.querySelector('.drop-menu');
        document.querySelectorAll('.drop-menu').forEach(m => {{ if(m !== menu) m.classList.remove('open'); }});
        menu.classList.toggle('open');
        event.stopPropagation();
    }}

    function filterTable() {{
        const name = document.getElementById('searchName').value.toLowerCase();
        const phone = document.getElementById('searchPhone').value.toLowerCase();
        const date = document.getElementById('searchDate').value;
        document.querySelectorAll('#futureBody tr').forEach(row => {{
            const cells = row.querySelectorAll('td');
            if (cells.length < 4) return;
            const matchName = cells[1].textContent.toLowerCase().includes(name);
            const matchPhone = cells[3].textContent.toLowerCase().includes(phone);
            const matchDate = !date || cells[0].textContent.includes(date);
            row.style.display = (matchName && matchPhone && matchDate) ? '' : 'none';
        }});
    }}

    function openEdit(id, name, service, datetime, status) {{
        document.getElementById('editId').value = id;
        document.getElementById('editName').value = name;
        document.getElementById('editService').value = service;
        document.getElementById('editDatetime').value = datetime.replace('T',' ');
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

    function closeModal(id) {{ document.getElementById(id).classList.remove('active'); }}

    async function saveEdit() {{
        const id = document.getElementById('editId').value;
        const data = {{
            client_name: document.getElementById('editName').value,
            service: document.getElementById('editService').value,
            datetime: document.getElementById('editDatetime').value,
            status: document.getElementById('editStatus').value
        }};
        const res = await fetch(`/api/reservation/${{id}}/edit`, {{
            method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(data)
        }});
        const result = await res.json();
        if (result.success) {{ closeModal('editModal'); location.reload(); }}
        else {{ alert('Error al guardar.'); }}
    }}

    async function saveWalkin() {{
        document.getElementById('walkinSaveBtn').disabled = true;
        const date = document.getElementById('walkinDate').value;
        const hour = document.getElementById('walkinHour').value;
        const data = {{
            business_id: {business_id},
            client_name: document.getElementById('walkinName').value,
            service: document.getElementById('walkinService').value,
            datetime: date + ' ' + hour
        }};
        const res = await fetch('/api/reservation/walkin', {{
            method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(data)
        }});
        const result = await res.json();
        if (result.success) {{ closeModal('walkinModal'); location.reload(); }}
        else if (result.reason === 'slot_full') {{
            document.getElementById('walkinError').style.display = 'block';
            document.getElementById('walkinSaveBtn').disabled = false;
        }}
        else {{ alert('Error al guardar.'); document.getElementById('walkinSaveBtn').disabled = false; }}
    }}

    async function completeReservation(id) {{
        if (!confirm('¿Marcar como completada?')) return;
        const res = await fetch(`/api/reservation/${{id}}/complete`, {{method:'POST'}});
        const result = await res.json();
        if (result.success) location.reload();
        else alert('Error.');
    }}

    async function cancelReservation(id) {{
        if (!confirm('¿Cancelar esta reserva?')) return;
        const res = await fetch(`/api/reservation/${{id}}/cancel`, {{method:'POST'}});
        const result = await res.json();
        if (result.success) location.reload();
        else alert('Error al cancelar.');
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
