print(">>> STARTING BARBERSHOP BOT v1.0.0 ✅")

from dotenv import load_dotenv
load_dotenv()

import os
import json
from datetime import datetime
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

# =====================================================================
# AVAILABILITY + CANCELLATION
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
        # Check if they already gave a new datetime in the same message
        try:
            temp_reply = ask_openai(config, history, f"El cliente quiere cambiar su cita. Extrae SOLO la nueva fecha y hora de este mensaje y responde ÚNICAMENTE con el formato YYYY-MM-DD HH:MM, nada más. Si no hay fecha clara responde NO_DATE. Mensaje: {incoming_msg}")
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
# DASHBOARD API ROUTES — for edit/cancel from dashboard
# =====================================================================

@app.post("/api/reservation/{reservation_id}/cancel")
async def api_cancel_reservation(reservation_id: int):
    if not supabase:
        return JSONResponse({"success": False}, status_code=500)
    try:
        supabase.table("reservations").update({"status": "cancelled"}).eq("reservation_id", reservation_id).execute()
        return JSONResponse({"success": True})
    except Exception as e:
        print(f"API cancel error: {e}")
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
        print(f"API edit error: {e}")
        return JSONResponse({"success": False}, status_code=500)

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
        rid = r.get('reservation_id')
        status = r.get('status', '-')
        status_class = "status-confirmed" if status == "confirmed" else "status-cancelled"
        status_label = "confirmada" if status == "confirmed" else "cancelada"
        dt = r.get('datetime', '')
        dt_display = dt[:16].replace('T', ' ') if dt else '-'

        rows += f"""
        <tr data-id="{rid}">
            <td>{dt_display}</td>
            <td>{r.get('client_name', '-')}</td>
            <td>{r.get('service', '-')}</td>
            <td>{r.get('contact_phone', '-')}</td>
            <td><span class="status {status_class}">{status_label}</span></td>
            <td class="actions">
                <button class="btn-edit" onclick="openEdit({rid}, '{r.get('client_name','').replace("'","\\'")}', '{r.get('service','').replace("'","\\'")}', '{dt[:16] if dt else ''}', '{status}')">✏️ Editar</button>
                <button class="btn-cancel" onclick="cancelReservation({rid})" {'disabled' if status == 'cancelled' else ''}>✖ Cancelar</button>
            </td>
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
            .container {{ max-width: 1100px; margin: 32px auto; padding: 0 16px; }}
            .card {{ background: white; border-radius: 12px; padding: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}
            .top-bar {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; flex-wrap: wrap; gap: 12px; }}
            .total {{ font-size: 1rem; color: #555; }}
            .total strong {{ color: #1a1a1a; font-size: 1.2rem; }}
            .search-bar {{ display: flex; gap: 8px; flex-wrap: wrap; }}
            .search-bar input {{ padding: 8px 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 0.85rem; width: 180px; }}
            .search-bar button {{ background: #1a1a1a; color: white; border: none; padding: 8px 14px; border-radius: 8px; cursor: pointer; font-size: 0.85rem; }}
            .search-bar button:hover {{ background: #333; }}
            table {{ width: 100%; border-collapse: collapse; }}
            th {{ background: #1a1a1a; color: white; padding: 12px 16px; text-align: left; font-weight: 500; font-size: 0.85rem; }}
            td {{ padding: 12px 16px; border-bottom: 1px solid #f0f0f0; font-size: 0.85rem; }}
            tr:last-child td {{ border-bottom: none; }}
            tr:hover td {{ background: #fafafa; }}
            .status {{ padding: 3px 10px; border-radius: 20px; font-size: 0.8rem; font-weight: 500; }}
            .status-confirmed {{ background: #e6f4ea; color: #2e7d32; }}
            .status-cancelled {{ background: #fdecea; color: #c62828; }}
            .actions {{ display: flex; gap: 6px; }}
            .btn-edit {{ background: #1a1a1a; color: white; border: none; padding: 5px 10px; border-radius: 6px; cursor: pointer; font-size: 0.78rem; }}
            .btn-edit:hover {{ background: #333; }}
            .btn-cancel {{ background: #fdecea; color: #c62828; border: 1px solid #f5c6c6; padding: 5px 10px; border-radius: 6px; cursor: pointer; font-size: 0.78rem; }}
            .btn-cancel:hover {{ background: #fbc8c8; }}
            .btn-cancel:disabled {{ opacity: 0.4; cursor: not-allowed; }}
            .empty {{ text-align: center; color: #999; padding: 40px; }}
            .refresh {{ background: #1a1a1a; color: white; border: none; padding: 8px 16px; border-radius: 8px; cursor: pointer; font-size: 0.85rem; }}
            .refresh:hover {{ background: #333; }}

            /* Modal */
            .modal-overlay {{ display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); z-index: 100; justify-content: center; align-items: center; }}
            .modal-overlay.active {{ display: flex; }}
            .modal {{ background: white; border-radius: 12px; padding: 28px; width: 420px; max-width: 95%; }}
            .modal h2 {{ font-size: 1.1rem; margin-bottom: 20px; }}
            .modal label {{ display: block; font-size: 0.85rem; color: #555; margin-bottom: 4px; margin-top: 14px; }}
            .modal input, .modal select {{ width: 100%; padding: 8px 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 0.9rem; }}
            .modal-actions {{ display: flex; gap: 10px; margin-top: 20px; justify-content: flex-end; }}
            .btn-save {{ background: #1a1a1a; color: white; border: none; padding: 8px 18px; border-radius: 8px; cursor: pointer; font-size: 0.85rem; }}
            .btn-save:hover {{ background: #333; }}
            .btn-close {{ background: #f0f0f0; color: #333; border: none; padding: 8px 18px; border-radius: 8px; cursor: pointer; font-size: 0.85rem; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>💈 {business_name} — Reservas</h1>
            <button class="refresh" onclick="location.reload()">↻ Refrescar</button>
        </div>
        <div class="container">
            <div class="card">
                <div class="top-bar">
                    <p class="total">Total citas: <strong>{len(reservations)}</strong></p>
                    <div class="search-bar">
                        <input type="text" id="searchName" placeholder="Buscar por nombre..." oninput="filterTable()">
                        <input type="text" id="searchPhone" placeholder="Teléfono..." oninput="filterTable()">
                        <input type="date" id="searchDate" onchange="filterTable()">
                        <button onclick="clearSearch()">Limpiar</button>
                    </div>
                </div>
                <table id="reservationTable">
                    <thead>
                        <tr>
                            <th>Fecha & Hora</th>
                            <th>Cliente</th>
                            <th>Servicio</th>
                            <th>Teléfono</th>
                            <th>Estado</th>
                            <th>Acciones</th>
                        </tr>
                    </thead>
                    <tbody id="tableBody">
                        {'<tr><td colspan="6" class="empty">No hay reservas aún.</td></tr>' if not reservations else rows}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Edit Modal -->
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
                    <option value="cancelled">Cancelada</option>
                </select>
                <div class="modal-actions">
                    <button class="btn-close" onclick="closeModal()">Cancelar</button>
                    <button class="btn-save" onclick="saveEdit()">Guardar</button>
                </div>
            </div>
        </div>

        <script>
            function filterTable() {{
                const name = document.getElementById('searchName').value.toLowerCase();
                const phone = document.getElementById('searchPhone').value.toLowerCase();
                const date = document.getElementById('searchDate').value;
                const rows = document.querySelectorAll('#tableBody tr');
                rows.forEach(row => {{
                    const cells = row.querySelectorAll('td');
                    if (cells.length < 4) return;
                    const rowDate = cells[0].textContent;
                    const rowName = cells[1].textContent.toLowerCase();
                    const rowPhone = cells[3].textContent.toLowerCase();
                    const matchName = rowName.includes(name);
                    const matchPhone = rowPhone.includes(phone);
                    const matchDate = !date || rowDate.startsWith(date);
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

            function closeModal() {{
                document.getElementById('editModal').classList.remove('active');
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
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify(data)
                }});
                const result = await res.json();
                if (result.success) {{
                    closeModal();
                    location.reload();
                }} else {{
                    alert('Error al guardar. Intenta de nuevo.');
                }}
            }}

            async function cancelReservation(id) {{
                if (!confirm('¿Seguro que quieres cancelar esta reserva?')) return;
                const res = await fetch(`/api/reservation/${{id}}/cancel`, {{method: 'POST'}});
                const result = await res.json();
                if (result.success) {{
                    location.reload();
                }} else {{
                    alert('Error al cancelar. Intenta de nuevo.');
                }}
            }}
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)

# =====================================================================
# HEALTH CHECK
# =====================================================================

@app.get("/")
async def root():
    return {{"status": "running", "bot": "AI Reservation Bot v1.0.0"}}
