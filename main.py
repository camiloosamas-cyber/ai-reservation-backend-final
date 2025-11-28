from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json, os, re
import dateparser

from supabase import create_client, Client
from openai import OpenAI
from twilio.twiml.messaging_response import MessagingResponse

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

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
LOCAL_TZ = ZoneInfo("America/Bogota")

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE")
)

TABLE_LIMIT = 10

def assign_table(iso_local: str):
    booked = supabase.table("reservations").select("table_number").eq("datetime", iso_local).execute()
    taken = {r["table_number"] for r in (booked.data or [])}
    for i in range(1, TABLE_LIMIT + 1):
        t = f"T{i}"
        if t not in taken:
            return t
    return None

def save_reservation(data: dict):
    try:
        raw_dt = datetime.fromisoformat(data["datetime"])
        if raw_dt.tzinfo is None:
            dt_local = raw_dt.replace(tzinfo=LOCAL_TZ)
        else:
            dt_local = raw_dt.astimezone(LOCAL_TZ)
        iso_to_store = dt_local.isoformat()
    except Exception:
        return "❌ Error procesando la fecha."
    table = data.get("table_number") or assign_table(iso_to_store)
    if not table:
        return "❌ No hay mesas disponibles para ese horario."
    supabase.table("reservations").insert({
        "customer_name": data["customer_name"],
        "customer_email": "",
        "contact_phone": "",
        "datetime": iso_to_store,
        "party_size": int(data["party_size"]),
        "table_number": table,
        "notes": "",
        "status": "confirmado",
        "business_id": 2,
        "package": data.get("package", ""),
        "school_name": data.get("school_name", ""),
    }).execute()
    return (
        "✅ *¡Reserva confirmada!*\n"
        f"👤 {data['customer_name']}\n"
        f"👥 {data['party_size']} estudiantes\n"
        f"📦 {data.get('package','')}\n"
        f"🏫 {data.get('school_name','')}\n"
        f"🗓 {dt_local.strftime('%Y-%m-%d %H:%M')}"
    )

def detect_package(msg: str):
    msg = msg.lower().strip()
    if any(w in msg for w in ["cuidado esencial", "esencial", "kit escolar"]):
        return "Paquete Cuidado Esencial"
    if any(w in msg for w in ["salud activa", "activa"]):
        return "Paquete Salud Activa"
    if any(w in msg for w in ["bienestar total", "total", "completo"]):
        return "Paquete Bienestar Total"
    if any(w in msg for w in ["45", "45k", "45 mil", "45mil"]):
        return "Paquete Cuidado Esencial"
    if any(w in msg for w in ["60", "60k", "60 mil", "60mil"]):
        return "Paquete Salud Activa"
    if any(w in msg for w in ["75", "75k", "75 mil", "75mil"]):
        return "Paquete Bienestar Total"
    if "odont" in msg:
        return "Paquete Bienestar Total"
    if "psico" in msg:
        return "Paquete Salud Activa"
    if any(w in msg for w in ["audio", "optometr", "medicina"]):
        return "Paquete Cuidado Esencial"
    if "verde" in msg:
        return "Paquete Cuidado Esencial"
    if "azul" in msg:
        return "Paquete Salud Activa"
    if "amarillo" in msg:
        return "Paquete Bienestar Total"
    return None

def ai_extract(user_msg: str):
    msg = user_msg.lower().strip()
    noise_words = [
        "no perdon", "no perdón", "perdon", "perdón",
        "quise decir", "me referia", "me refería",
        "quise poner", "quise mandar", "ok", "vale",
        "listo", "claro"
    ]
    for w in noise_words:
        msg = msg.replace(w, "")

    detected_package = detect_package(msg)
    if detected_package:
        for w in ["esencial", "activa", "total", "bienestar", "verde", "azul", "amarillo"]:
            msg = msg.replace(w, "")

    prompt = f"""
Extrae SOLO la fecha y hora exacta del mensaje.
Devuelve exactamente este JSON:
{{
"fecha_hora": "texto exacto que contiene fecha y hora"
}}
Si no encuentras fecha o hora, deja "".
Mensaje:
\"\"\"{user_msg}\"\"\"
"""
    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[{"role": "system", "content": prompt}]
        )
        result = json.loads(r.choices[0].message.content)
        dt_text = result.get("fecha_hora", "")
    except:
        dt_text = ""

    dt_local = None
    if dt_text:
        try:
            dt_local = dateparser.parse(
                dt_text,
                settings={
                    "RETURN_AS_TIMEZONE_AWARE": True,
                    "TIMEZONE": "America/Bogota",
                    "PREFER_DATES_FROM": "future"
                }
            )
        except:
            dt_local = None
    if dt_local:
        has_minutes = re.search(r":\d{2}", dt_text)
        if not has_minutes:
            dt_local = dt_local.replace(minute=0)
    final_iso = dt_local.isoformat() if dt_local else ""

    if dt_text:
        msg = msg.replace(dt_text.lower(), "")

    for w in ["mañana", "pasado mañana", "hoy", "tarde", "noche", "am", "pm", "a las", "a la", "este", "próximo"]:
        msg = msg.replace(w, "")

    school_name = ""
    school_patterns = [
        r"colegio\s+[a-záéíóúñ0-9 ]+",
        r"gimnasio\s+[a-záéíóúñ0-9 ]+",
        r"liceo\s+[a-záéíóúñ0-9 ]+",
        r"instituto\s+[a-záéíóúñ0-9 ]+"
    ]
    for p in school_patterns:
        m = re.search(p, msg)
        if m:
            raw = m.group(0)
            raw = re.split(r"[,.!\n]", raw)[0]
            school_name = raw.strip().title()
            msg = msg.replace(raw.lower(), "")
            break

    customer_name = ""
    name_stopwords = [
        "quiero","cita","reservar","agendar","necesito","la","el","una","un","hora","fecha",
        "dia","día","por","favor","gracias","me","referia","refería","perdon","perdón","mejor"
    ]
    name_patterns = [
        r"mi hijo ([a-záéíóúñ ]+)",
        r"mi hija ([a-záéíóúñ ]+)",
        r"es para ([a-záéíóúñ ]+)",
        r"se llama ([a-záéíóúñ ]+)"
    ]
    def clean_name(n):
        n = n.strip()
        n = re.split(r"[,.!\n]", n)[0]
        words = n.split()
        words = [w for w in words if w not in name_stopwords]
        if len(words) == 0:
            return ""
        if len(words) > 3:
            words = words[:3]
        return " ".join(words).title()

    for p in name_patterns:
        m = re.search(p, msg)
        if m:
            candidate = clean_name(m.group(1))
            if candidate:
                customer_name = candidate
                break

    if not customer_name:
        if re.fullmatch(r"[a-záéíóúñ ]{2,30}", msg.strip()):
            candidate = clean_name(msg.strip())
            if candidate:
                customer_name = candidate

    party_size = ""
    m = re.search(r"(\d+)\s*(estudiantes|alumnos|niños|personas)", user_msg.lower())
    if m:
        party_size = m.group(1)

    intent = "info"
    if "cita" in user_msg.lower() or "reserv" in user_msg.lower():
        intent = "reserve"

    return {
        "intent": intent,
        "customer_name": customer_name,
        "school_name": school_name,
        "datetime": final_iso,
        "party_size": party_size,
        "package": detected_package,
    }
session_state = {}

def get_session(phone):
    if phone not in session_state:
        session_state[phone] = {
            "phone": phone,
            "student_name": None,
            "school": None,
            "package": None,
            "date": None,
            "time": None,
            "party_size": None,
            "booking_started": False,
            "info_mode": False,
            "first_booking_message": False,
            "greeted": False,
        }
    return session_state[phone]

INTENTS = {
    "greeting": {"patterns": [], "handler": "handle_greeting"},
    "package_info": {"patterns": [], "handler": "handle_package_info"},
    "booking_request": {"patterns": [], "handler": "handle_booking_request"},
    "modify": {"patterns": [], "handler": "handle_modify"},
    "cancel": {"patterns": [], "handler": "handle_cancel"},
    "confirmation": {"patterns": [], "handler": "handle_confirmation"}
}

INTENT_PRIORITY = [
    "booking_request",
    "modify",
    "cancel",
    "package_info",
    "confirmation",
    "greeting",
]

def detect_intent(msg: str):
    msg = msg.lower()
    for intent in INTENT_PRIORITY:
        data = INTENTS[intent]
        for p in data["patterns"]:
            if p in msg:
                return intent
    return None

def handle_greeting(msg, session):
    if not session["greeted"]:
        session["greeted"] = True
        return "Hola, claro que sí. ¿En qué te puedo ayudar?"
    return "Claro que sí, ¿en qué te puedo ayudar?"

def handle_package_info(msg, session):
    session["info_mode"] = True
    pkg = detect_package(msg)
    prices = {
        "Paquete Cuidado Esencial": "45.000",
        "Paquete Salud Activa": "60.000",
        "Paquete Bienestar Total": "75.000",
    }
    details = {
        "Paquete Cuidado Esencial": "Medicina General, Optometría y Audiometría.",
        "Paquete Salud Activa": "Medicina General, Optometría, Audiometría y Psicología.",
        "Paquete Bienestar Total": "Medicina General, Optometría, Audiometría, Psicología y Odontología.",
    }
    if pkg:
        return (
            f"Claro 😊\n"
            f"*{pkg}* cuesta *${prices[pkg]}*.\n\n"
            f"📋 *Incluye:*\n{details[pkg]}\n\n"
            "¿Te gustaría agendar una cita?"
        )
    return (
        "Claro. Ofrecemos tres paquetes:\n\n"
        "• *Cuidado Esencial* — $45.000\n"
        "  Medicina General, Optometría, Audiometría\n\n"
        "• *Salud Activa* — $60.000\n"
        "  Medicina General, Optometría, Audiometría, Psicología\n\n"
        "• *Bienestar Total* — $75.000\n"
        "  Medicina General, Optometría, Audiometría, Psicología, Odontología\n\n"
        "¿Cuál te interesa?"
    )

def handle_booking_request(msg, session):
    session["booking_started"] = True
    session["info_mode"] = False
    update_session_with_info(msg, session)
    auto = auto_finalize_if_ready(session)
    if auto:
        return auto
    if (
        session["student_name"] and session["school"] and
        session["package"] and session["date"] and session["time"]
    ):
        return finish_booking(session)
    return (
        "Por supuesto. Para agendar la cita necesito los siguientes datos:\n\n"
        "– Nombre del estudiante\n"
        "– Colegio\n"
        "– Paquete\n"
        "– Fecha en que deseas la cita\n"
        "– Hora\n\n"
        "¿Me los puedes compartir?"
    )

def handle_modify(msg, session):
    return "Claro, ¿cuál sería la nueva fecha y hora que deseas?"

def handle_cancel(msg, session):
    return "Perfecto, ¿confirmas que deseas cancelar la cita?"

def handle_confirmation(msg, session):
    update_session_with_info(msg, session)
    required = [
        session["student_name"],
        session["school"],
        session["package"],
        session["date"],
        session["time"]
    ]
    if not all(required):
        return "Perfecto, ¿me confirmas algo más?"
    try:
        dt_text = f"{session['date']} {session['time']}"
        dt = datetime.strptime(dt_text, "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ)
        iso = dt.isoformat()
    except Exception as e:
        return f"Hubo un error procesando la fecha/hora. ({e})"
    save_reservation({
        "customer_name": session["student_name"],
        "package": session["package"],
        "school_name": session["school"],
        "datetime": iso,
        "party_size": 1,
        "table_number": None,
    })
    phone = session["phone"]
    session_state.pop(phone, None)
    return (
        f"¡Perfecto! La cita de {session['student_name']} quedó confirmada "
        f"en el {session['school']}, paquete {session['package']}, "
        f"el día {session['date']} a las {session['time']}."
    )

def contextual_intent(msg: str):
    text = msg.lower().strip()
    if any(x in text for x in ["atienden", "abren", "horario", "horarios", "sábados", "sabados"]):
        return "general_hours"
    if any(x in text for x in ["donde queda", "ubicados", "direccion", "dirección"]):
        return "general_location"
    if any(x in text for x in ["como funciona", "cómo funciona", "como es el proceso", "como es el examen"]):
        return "general_process"
    if any(x in text for x in ["puedes repetir", "puede repetir", "repiteme", "repite"]):
        return "repeat_summary"
    if any(x in text for x in ["que incluye", "qué incluye", "incluye", "que trae"]):
        return "package_detail_request"
    if any(x in text for x in ["espera", "un momento", "dame un segundo"]):
        return "delay"
    return None

def contextual_handler(intent: str, session):
    if intent == "general_hours":
        return "Nuestros horarios son de lunes a viernes de 7:00 AM a 5:00 PM y sábados de 7:00 AM a 1:00 PM 😊"
    if intent == "general_location":
        return "Estamos ubicados en Bogotá, en la calle 75 #20-36. Si necesitas compartir la ubicación te la envío."
    if intent == "general_process":
        return (
            "Claro 😊 El examen escolar se hace en aproximadamente 30–45 minutos e incluye:\n"
            "• Historia clínica y revisión general\n"
            "• Pruebas del paquete que elijas\n"
            "• Entrega del certificado escolar\n\n"
            "¿Te gustaría agendar una cita?"
        )
    if intent == "repeat_summary":
        missing = build_missing_fields_message(session)
        return missing or finish_booking(session)
    if intent == "package_detail_request":
        pkg = session.get("package")
        if not pkg:
            return "Claro 😊 ¿De cuál paquete deseas saber más?"
        return handle_package_info(pkg, session)
    if intent == "delay":
        return "Claro, te espero 😊"
    return None

def auto_finalize_if_ready(session):
    if (
        session["student_name"] and session["school"] and
        session["package"] and session["date"] and session["time"]
    ):
        return finish_booking(session)
    return None

def natural_tone(text: str):
    replacements = {
        "Perfecto,": "Perfecto 😊,",
        "Listo,": "Listo 😊,",
        "Claro,": "Claro que sí 😊,",
        "Por supuesto.": "Por supuesto, ya te ayudo 😊.",
        "Entendido.": "Entendido 😊.",
        "De acuerdo.": "Listo 😊.",
        "¿Me lo compartes?": "¿Me ayudas con eso porfa? 🙏",
        "¿Me los compartes?": "¿Me colaboras con esos datos porfa? 🙏",
        "¿Me lo confirmas?": "¿Me confirmas porfa? 🙏",
        "Gracias": "Gracias 😊",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    if text.strip().endswith("?") and "😊" not in text:
        text = text.rstrip("?") + " 😊?"
    return text

def extract_student_name(msg):
    msg = msg.strip().lower()
    blocked_phrases = [
        "si","sí","si por favor","sí por favor","por favor","ok","dale",
        "claro","perfecto","bueno","listo","de una","super","ok listo",
        "okk","okay","okey"
    ]
    if msg in blocked_phrases:
        return None
    if 1 <= len(msg.split()) <= 3:
        if all(c.isalpha() or c.isspace() for c in msg):
            return msg.title()
    return None

def extract_school(msg):
    msg_clean = msg.lower()
    patterns = [
        r"del colegio ([a-zA-Záéíóúñ ]+)",
        r"de colegio ([a-zA-Záéíóúñ ]+)",
        r"del col ([a-zA-Záéíóúñ ]+)",
        r"colegio ([a-zA-Záéíóúñ ]+)",
        r"gimnasio ([a-zA-Záéíóúñ ]+)",
        r"liceo ([a-zA-Záéíóúñ ]+)",
        r"instituto ([a-zA-Záéíóúñ ]+)",
    ]
    for p in patterns:
        m = re.search(p, msg_clean)
        if m:
            name = m.group(1).strip()
            name = re.split(r"[,.!?\n]| a las | a la | mañana | hoy | pasado mañana", name)[0]
            return name.title().strip()
    return None

def infer_time_period(raw_text: str, hour: int) -> int:
    t = raw_text.lower()
    if "am" in t or "a.m" in t:
        return hour
    if "pm" in t or "p.m" in t:
        return hour + 12 if hour < 12 else hour
    if any(x in t for x in ["de la mañana", "mañana "]):
        return hour
    if any(x in t for x in ["mediodia", "medio dia", "medio día"]):
        return 12
    if "de la tarde" in t:
        return hour + 12 if 1 <= hour <= 7 else hour
    if any(x in t for x in ["de la noche", "en la noche"]):
        return hour + 12 if hour < 12 else hour
    if hour <= 8:
        return hour
    if 9 <= hour <= 11:
        return hour
    if hour == 12:
        return 12
    if 1 <= hour <= 7:
        return hour + 12
    return hour
def detect_correction(msg: str) -> dict:
    t = msg.lower().strip()
    corrections = {
        "student_name": [
            "no es","no era","no perdón","no perdon","no, perdón",
            "me refería","quise decir","quise decirle","quise poner",
            "no el nombre","no es ese nombre"
        ],
        "school": [
            "no el colegio","no era el colegio","colegio no",
            "quise decir el colegio","me refería al colegio"
        ],
        "package": [
            "no el paquete","no era el paquete","me refería al paquete",
            "quise decir el paquete","no ese paquete"
        ],
        "datetime": [
            "no a las","no es a las","no es a la","quise decir a las",
            "me refería a las","no esa hora","no a esa hora",
            "no a esa fecha","me equivoqué de hora","no es mañana","no es hoy"
        ]
    }
    result = {}
    for field, words in corrections.items():
        for w in words:
            if w in t:
                result[field] = True
    return result

def apply_correction(session, correction_flags):
    if correction_flags.get("student_name"):
        session["student_name"] = None
    if correction_flags.get("school"):
        session["school"] = None
    if correction_flags.get("package"):
        session["package"] = None
    if correction_flags.get("datetime"):
        session["date"] = None
        session["time"] = None

def apply_student_name_fix(session, msg):
    text = msg.lower()

    noise = [
        "mañana", "pasado mañana", "a las", "a la", "hoy",
        "pm", "am", "de la tarde", "de la mañana", "de la noche"
    ]

    name = session.get("student_name")
    if not name:
        return

    cleaned = name.lower()

    for w in noise:
        cleaned = cleaned.replace(w, "")

    cleaned = re.sub(r"\d+", "", cleaned)
    cleaned = cleaned.strip()

    if not re.fullmatch(r"[a-záéíóúñ ]{2,30}", cleaned):
        return

    session["student_name"] = cleaned.title()

def update_session_with_info(msg, session):
    text = msg.lower().strip()

    correction_flags = detect_correction(msg)
    if correction_flags:
        apply_correction(session, correction_flags)

    extracted = ai_extract(msg)
    new_name = extracted.get("customer_name")
    new_school = extracted.get("school_name")
    new_package = extracted.get("package")
    new_datetime = extracted.get("datetime")
    new_size = extracted.get("party_size")

    if correction_flags:
        if new_datetime:
            try:
                dt = datetime.fromisoformat(new_datetime).astimezone(LOCAL_TZ)
                raw_hour = int(dt.strftime("%H"))
                inferred_hour = infer_time_period(msg, raw_hour)
                dt = dt.replace(hour=inferred_hour)
                session["date"] = dt.strftime("%Y-%m-%d")
                session["time"] = dt.strftime("%H:%M")
            except:
                pass
        return

    invalid_name_phrases = [
        "quiero","cita","reserv","agendar","necesito","perdon",
        "perdón","referia","refería","hora","fecha","mañana","tarde",
        "noche","pm","am"
    ]

    if new_name:
        if not any(w in new_name.lower() for w in invalid_name_phrases):
            session["student_name"] = new_name

    if new_school:
        session["school"] = new_school

    if new_package:
        session["package"] = new_package

    if new_datetime:
        try:
            dt = datetime.fromisoformat(new_datetime).astimezone(LOCAL_TZ)
            session["date"] = dt.strftime("%Y-%m-%d")
            session["time"] = dt.strftime("%H:%M")
        except:
            pass

    if new_size:
        session["party_size"] = new_size

    apply_student_name_fix(session, msg)

def build_missing_fields_message(session):
    missing = []
    if not session["student_name"]:
        missing.append("el nombre del estudiante")
    if not session["school"]:
        missing.append("el colegio")
    if not session["package"]:
        missing.append("el paquete")
    if not session["date"]:
        missing.append("la fecha en que deseas la cita")
    if not session["time"]:
        missing.append("la hora")
    if len(missing) == 0:
        return None
    if len(missing) == 1:
        return f"Listo, solo me falta {missing[0]}. ¿Me lo compartes?"
    if len(missing) == 2:
        return f"Perfecto, me falta {missing[0]} y {missing[1]}. ¿Me los compartes?"
    joined = ", ".join(missing[:-1]) + " y " + missing[-1]
    return f"Perfecto, me falta {joined}. ¿Me los compartes?"

def finish_booking(session):
    name = session["student_name"]
    school = session["school"]
    pkg = session["package"]
    date = session["date"]
    time = session["time"]
    return (
        f"Listo, tu cita quedó agendada para {name} en el {school}, "
        f"paquete {pkg}, el día {date} a las {time}. ¿Deseas confirmar?"
    )

def is_confirmation_message(msg: str) -> bool:
    text = msg.lower().strip()
    for p in INTENTS["confirmation"]["patterns"]:
        if p in text:
            return True
    return False

def continue_booking_process(msg, session):
    update_session_with_info(msg, session)
    missing_message = build_missing_fields_message(session)
    if missing_message:
        return missing_message
    if is_confirmation_message(msg):
        required = [
            session["student_name"],
            session["school"],
            session["package"],
            session["date"],
            session["time"]
        ]
        if all(required):
            return handle_confirmation(msg, session)
        return "Antes de confirmar necesito toda la información completa 😊"
    return finish_booking(session)

def process_message(msg, session):
    update_session_with_info(msg, session)

    ctx = contextual_intent(msg)
    if ctx:
        ans = contextual_handler(ctx, session)
        if ans:
            return natural_tone(ans)

    intent = detect_intent(msg)

    if intent == "confirmation" and session["info_mode"] and not session["booking_started"]:
        intent = "booking_request"

    if session["booking_started"] and is_confirmation_message(msg):
        return natural_tone(handle_confirmation(msg, session))

    auto = auto_finalize_if_ready(session)
    if auto:
        return natural_tone(auto)

    if not intent:
        return ""

    handler_name = INTENTS[intent]["handler"]
    handler = globals()[handler_name]
    resp = handler(msg, session)

    return natural_tone(resp)

INTENTS["greeting"]["patterns"] = [
    "hola","holaa","holaaa","buenas","buenos dias","buenos días",
    "buen dia","buen día","buenas tardes","buenas noches","ola","holi",
    "holis","hello","alo","aló","alo?","que mas","qué más","q mas",
    "que tal","que hubo","qué hubo","k hubo","disculpa","una pregunta",
    "consulta","hola una pregunta","buen dia una pregunta",
    "buenos dias una consulta","quisiera saber","vi un poster",
    "vi un anuncio","vi su aviso","vi la publicidad","tienen info",
    "informacion","información","quisiera informacion",
    "quisiera información"
]

INTENTS["package_info"]["patterns"] = [
    "cuanto vale","cuánto vale","cuanto cuesta","cuánto cuesta",
    "precio","precio del paquete","valor del paquete","cuanto es el de",
    "cuánto es el de","paquete","kit escolar","el de psicologia",
    "el de psicología","el de psico","trae psicologia","trae psicología",
    "incluye psicologia","incluye psicología","el verde","verde",
    "de color verde","el azul","azul","de color azul","el amarillo",
    "amarillo","de color amarillo","45k","45 mil","45mil","60k",
    "60 mil","60mil","75k","75 mil","75mil","esencial","salud activa",
    "activa","bienestar total","bienestar","total","completo",
    "que paquetes tienen","qué paquetes tienen","ofrecen paquetes",
    "tienen paquetes","como funcionan los paquetes","examenes escolares",
    "exámenes escolares","paquetes escolares"
]

INTENTS["booking_request"]["patterns"] = [
    "quiero reservar","quiero una cita","quiero agendar",
    "quiero sacar una cita","quiero sacar cita","quiero hacer una cita",
    "quiero hacer cita","necesito una cita","necesito agendar",
    "necesito reservar","necesito sacar cita","quiero reservar el paquete",
    "quiero reservar el de","quiero agendar una cita para",
    "quiero agendar cita para","quiero reservar para mi hijo",
    "quiero reservar para mi hija","quiero el examen",
    "quiero hacer el examen","quiero hacer el examen escolar",
    "quiero hacer los exámenes","quiero hacer el examen del colegio",
    "me pueden reservar","me puedes reservar","me pueden agendar",
    "me puedes agendar","me ayudan a reservar","me ayudas a reservar",
    "me ayudas a agendar","agendar cita","agendar una cita",
    "reservar examen","reservar el examen","reservar examen escolar",
    "reservar paquete","agendar paquete","quiero pedir la cita",
    "quiero pedir cita","quiero separar cita","quiero separar el examen",
    "quiero una cita para mi hijo","quiero una cita para mi hija",
    "necesito cita para mi hijo","necesito cita para mi hija"
]

INTENTS["modify"]["patterns"] = [
    "cambiar cita","cambiar la cita","quiero cambiar la cita",
    "necesito cambiar la cita","puedo cambiar la cita","cambiar hora",
    "cambiar la hora","quiero otra hora","me sirve otra hora",
    "puedo mover la hora","cambiar fecha","cambiar la fecha",
    "quiero otra fecha","mover fecha","puedo mover la fecha",
    "quiero mover la cita","necesito mover la cita","quiero reagendar",
    "quiero re agendar","quiero re-agendar","reagendar cita",
    "mover cita","reagendarr","reagenda","re agendar cita","otra hora",
    "otra fecha"
]

INTENTS["cancel"]["patterns"] = [
    "cancelar","cancelar cita","cancelar la cita","quiero cancelar",
    "quiero cancelar la cita","quiero cancelar la cita de mi hijo",
    "quiero cancelar la cita de mi hija","anular","anular cita",
    "anular la cita","inhabilitar cita","quitar la cita",
    "me gustaría cancelar","necesito cancelar","me ayudas a cancelar",
    "cancelarr","cancelala","cancela la","cancela eso",
    "ya no quiero la cita"
]

INTENTS["confirmation"]["patterns"] = [
    "confirmo","sí confirmo","si confirmo","confirmar","confirmada",
    "confirmado","si","sí","ok","dale","listo","perfecto","super",
    "claro","de una","por supuesto","si está bien","sí está bien",
    "si esta bien","sí esta bien","está bien","esta bien","si claro",
    "sí claro","si dale","sí dale","si por favor","sí por favor",
    "si gracias","sí gracias","si es para mi hijo","si es para mi hija",
    "si está correcto","sí está correcto","ok listo","okay","okk",
    "okey","gracias si","si gracias","si señora","si señor","ah bueno",
    "perfecto gracias"
]

from dateutil import parser

@app.post("/whatsapp")
async def whatsapp_reply(request: Request):
    form = await request.form()
    incoming_msg = form.get("Body", "").strip()
    phone = form.get("From", "").replace("whatsapp:", "").strip()
    session = get_session(phone)
    response_text = process_message(incoming_msg, session)
    if not response_text:
        return Response(content=str(MessagingResponse().message(
            "Disculpa, no entendí bien. ¿Me lo repites por favor?"
        )), media_type="application/xml")
    twilio_resp = MessagingResponse()
    twilio_resp.message(response_text)
    return Response(content=str(twilio_resp), media_type="application/xml")

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    res = supabase.table("reservations").select("*").order("datetime", desc=True).execute()
    rows = res.data or []
    fixed = []
    weekly_count = 0
    now = datetime.now(LOCAL_TZ)
    week_ago = now - timedelta(days=7)
    for r in rows:
        iso = r.get("datetime")
        row = r.copy()
        if iso:
            dt = parser.isoparse(iso).astimezone(LOCAL_TZ)
            row["date"] = dt.strftime("%Y-%m-%d")
            row["time"] = dt.strftime("%H:%M")
            if dt >= week_ago:
                weekly_count += 1
        else:
            row["date"] = "-"
            row["time"] = "-"
        fixed.append(row)
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "reservations": fixed,
        "weekly_count": weekly_count
    })

@app.post("/updateReservation")
async def update_reservation(update: dict):
    rid = update.get("reservation_id")
    if not rid:
        return {"success": False}
    fields = {k: v for k, v in update.items()
              if k != "reservation_id" and v not in ["", None, "-", "null"]}
    if fields:
        supabase.table("reservations").update(fields).eq("reservation_id", rid).execute()
    return {"success": True}

@app.post("/cancelReservation")
async def cancel_reservation(update: dict):
    supabase.table("reservations").update({"status": "cancelled"}).eq("reservation_id", update["reservation_id"]).execute()
    return {"success": True}

@app.post("/archiveReservation")
async def archive_reservation(update: dict):
    supabase.table("reservations").update({"status": "archived"}).eq("reservation_id", update["reservation_id"]).execute()
    return {"success": True}

@app.post("/markArrived")
async def mark_arrived(update: dict):
    supabase.table("reservations").update({"status": "arrived"}).eq("reservation_id", update["reservation_id"]).execute()
    return {"success": True}

@app.post("/markNoShow")
async def mark_no_show(update: dict):
    supabase.table("reservations").update({"status": "no_show"}).eq("reservation_id", update["reservation_id"]).execute()
    return {"success": True}

@app.post("/createReservation")
async def create_reservation(data: dict):
    save_reservation({
        "customer_name": data.get("customer_name", ""),
        "customer_email": data.get("customer_email", ""),
        "contact_phone": data.get("contact_phone", ""),
        "datetime": data.get("datetime", ""),
        "party_size": data.get("party_size", 1),
        "school_name": data.get("school_name", ""),
        "package": data.get("package", ""),
        "table_number": None,
    })
    return {"success": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
