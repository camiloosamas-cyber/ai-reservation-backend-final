import os, re, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from twilio.twiml.messaging_response import MessagingResponse
import dateparser
from supabase import create_client, Client as SupabaseClient

os.chdir(os.path.dirname(os.path.abspath(__file__)))
TEST_MODE = os.getenv("TEST_MODE") == "1"

try:
    LOCAL_TZ = ZoneInfo("America/Bogota")
except ZoneInfoNotFoundError:
    LOCAL_TZ = ZoneInfo("UTC")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: SupabaseClient = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(title="Oriental IPS WhatsApp Bot", version="1.0.0")
app.add_middleware(CORSOMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)
app.mount("/static", StaticFiles(directory="static"), name="static")

SESSIONS = {}
REQUIRED_FIELDS = ["student_name","school","age","cedula","package","date","time"]

PACKAGE_INFO = {
    "esencial":{"price":"45.000","label":"cuidado esencial",
        "full":"Paquete Cuidado Esencial — 45.000 COP\nIncluye Medicina General, Optometría y Audiometría."},
    "activa":{"price":"60.000","label":"salud activa",
        "full":"Paquete Salud Activa — 60.000 COP\nIncluye Medicina General, Optometría, Audiometría y Psicología."},
    "bienestar":{"price":"75.000","label":"bienestar total",
        "full":"Paquete Bienestar Total — 75.000 COP\nIncluye Medicina General, Optometría, Audiometría, Psicología y Odontología."}
}

FAQ = {
    "ubicados":"Estamos ubicados en Calle 31 #29–61, Yopal.",
    "pago":"Aceptamos Nequi y efectivo.",
    "dur":"El examen dura entre 30 y 45 minutos.",
    "llevar":"Debes traer el documento del estudiante.",
    "domingo":"Sí, atendemos todos los días de 6am a 8pm."
}

RELEVANT = [
    "cita","reserv","paquete","precio","precios","colegio","estudiante","examen",
    "fecha","hora","ubic","pago","nequi","dur","llevar","domingo","esencial",
    "activa","bienestar","total","psico","optometr","audio","medicina","odont"
]

def log(msg):
    print(f"[LOG] {msg}")

def twiml(msg):
    if TEST_MODE: return Response(content=msg, media_type="text/plain")
    r = MessagingResponse(); r.message(msg)
    return Response(content=str(r), media_type="application/xml")

def get_session(phone):
    if phone not in SESSIONS:
        SESSIONS[phone]={"booking_started":False,"student_name":None,"school":None,
            "age":None,"cedula":None,"package":None,"date":None,"time":None}
    return SESSIONS[phone]

def reset_session(phone):
    SESSIONS[phone]={"booking_started":False,"student_name":None,"school":None,
        "age":None,"cedula":None,"package":None,"date":None,"time":None}

def is_relevant(text):
    t=text.lower()
    return any(k in t for k in RELEVANT)

def extract_package(t):
    t=t.lower()
    if any(x in t for x in["esencial","verde","45k","45000","kit escolar"]):return"esencial"
    if any(x in t for x in["activa","salud activa","azul","psico","60000"]):return"activa"
    if any(x in t for x in["bienestar","total","75k","completo","odont"]):return"bienestar"
    return None

SCHOOL_PATTERNS=[
    r"colegio ([a-zA-Záéíóúñ0-9\s]+)",
    r"instituto ([a-zA-Záéíóúñ0-9\s]+)",
    r"gimnasio ([a-zA-Záéíóúñ0-9\s]+)"
]

def extract_school(t):
    s=t.lower()
    for p in SCHOOL_PATTERNS:
        m=re.search(p,s)
        if m:return m.group(1).strip().title()
    return None

def extract_student_name(t):
    s=t.lower().strip()
    if s.startswith("hola")or s.startswith("buenos")or s.startswith("buenas"):
        return None
    m=re.search(r"(se llama|el nombre es)\s+([a-zA-Záéíóúñ\s]+)",s)
    if m:return m.group(2).strip().title()
    return None

def extract_age(t):
    m=re.search(r"(\d{1,2})\s*años",t.lower()); 
    if m:return m.group(1)
    m=re.search(r"edad\s*(\d{1,2})",t.lower())
    if m:return m.group(1)
    return None

def extract_cedula(t):
    m=re.search(r"\b(\d{5,12})\b",t)
    return m.group(1)if m else None

def extract_date(text):
    d=dateparser.parse(text,settings={"TIMEZONE":"America/Bogota","TO_TIMEZONE":"America/Bogota"})
    if not d:return None
    d=d.astimezone(LOCAL_TZ)
    today=datetime.now(LOCAL_TZ).replace(hour=0,minute=0,second=0,microsecond=0)
    if d<today:return None
    return d.strftime("%Y-%m-%d")

def extract_time(t):
    m=re.search(r"(\d{1,2})(?:[:\.](\d{2}))?\s*(am|pm)?",t.lower())
    if not m:return None
    h=int(m.group(1)); mnt=int(m.group(2))if m.group(2)else 0; ap=m.group(3)
    if ap=="pm"and h<12:h+=12
    if ap=="am"and h==12:h=0
    if h<6 or h>20:return None
    return f"{h:02d}:{mnt:02d}"

def faq_answer(t):
    s=t.lower()
    if"ubic" in s:return FAQ["ubicados"]
    if"pago" in s or"nequi" in s:return FAQ["pago"]
    if"dur" in s:return FAQ["dur"]
    if"llevar" in s:return FAQ["llevar"]
    if"domingo" in s:return FAQ["domingo"]
    return None

def package_info(pkg):
    return PACKAGE_INFO[pkg]["full"]+"\n\n¿Deseas agendar una cita?"
def detect_intent_booking(t):
    s=t.lower()
    return any(x in s for x in["agendar","reserv","cita","quiero una cita"])

def update_session(text,session):
    updated=False
    pkg=extract_package(text)
    if pkg and not session["package"]:
        session["package"]=pkg; session["booking_started"]=True; updated=True

    sn=extract_student_name(text)
    if sn and not session["student_name"]:
        session["student_name"]=sn; session["booking_started"]=True; updated=True

    sc=extract_school(text)
    if sc and not session["school"]:
        session["school"]=sc; session["booking_started"]=True; updated=True

    ag=extract_age(text)
    if ag and not session["age"]:
        session["age"]=ag; session["booking_started"]=True; updated=True

    cd=extract_cedula(text)
    if cd and not session["cedula"]:
        session["cedula"]=cd; session["booking_started"]=True; updated=True

    dt=extract_date(text)
    if dt and not session["date"]:
        session["date"]=dt; session["booking_started"]=True; updated=True

    tm=extract_time(text)
    if tm and not session["time"]:
        session["time"]=tm; session["booking_started"]=True; updated=True

    return updated

def missing_fields(s):
    return[f for f in REQUIRED_FIELDS if not s[f]]

def ask_missing(s):
    miss=missing_fields(s)
    if not miss:return None

    pkg=s["package"]
    if pkg:
        price=PACKAGE_INFO[pkg]["price"]
        lbl=PACKAGE_INFO[pkg]["label"]
    else:
        price=""; lbl=""

    if miss==["date"]:
        return f"perfecto 😊, {lbl} {price}, solo necesito la fecha ..."

    if"student_name"in miss:return"¿Cuál es el nombre completo del estudiante?"
    if"school"in miss:return"¿De qué colegio es el estudiante?"
    if"age"in miss:return"¿Qué edad tiene el estudiante?"
    if"cedula"in miss:return"Por favor indícame la cédula del estudiante."
    if"package"in miss:return"¿Qué paquete deseas? Tenemos Esencial, Salud Activa y Bienestar Total."
    if"date"in miss:return"¿Qué fecha te gustaría?"
    if"time"in miss:return"¿A qué hora deseas agendar? (entre 6am y 8pm)"
    return None

def build_summary(s):
    pkg=s["package"]
    lbl=PACKAGE_INFO[pkg]["label"]
    price=PACKAGE_INFO[pkg]["price"]
    return (
        "Ya tengo toda la información:\n\n"
        f"👤 Estudiante: {s['student_name']}\n"
        f"🎒 Colegio: {s['school']}\n"
        f"📦 Paquete: {lbl} {price}\n"
        f"📅 Fecha: {s['date']}\n"
        f"⏰ Hora: {s['time']}\n"
        f"🧒 Edad: {s['age']}\n"
        f"🪪 Cédula: {s['cedula']}\n\n"
        "¿Deseas confirmar esta cita? (Responde \"Confirmo\")"
    )

def assign_table():
    if not supabase:return"T1"
    try:
        r=supabase.table("reservations").select("table_number").eq("business_id",2).execute()
        used=[x["table_number"]for x in r.data]
        n=len(used)+1
        return f"T{n}"
    except:return"T1"

def insert_reservation(phone,s):
    if not supabase:return True,"T1"
    tbl=assign_table()
    dt=datetime.strptime(f"{s['date']} {s['time']}","%Y-%m-%d %H:%M").astimezone(LOCAL_TZ).isoformat()
    try:
        supabase.table("reservations").insert({
            "student_name":s["student_name"],"phone":phone,"datetime":dt,
            "school":s["school"],"package":s["package"],"age":s["age"],
            "cedula":s["cedula"],"business_id":2,"table_number":tbl,"status":"confirmado"
        }).execute()
        return True,tbl
    except Exception as e:
        log(f"Supabase error: {e}")
        return False,str(e)

def handle_message(phone,text):
    log(f"MSG from {phone}: {text}")
    s=get_session(phone)
    t=text.strip()

    if any(t.lower().startswith(x)for x in["hola","buenos","buenas"])and not s["booking_started"]:
        log("Saludo detectado")
        return"Buenos días, estás comunicado con Oriental IPS. ¿En qué te podemos ayudar?"

    faq=faq_answer(t)
    if faq and not s["booking_started"]:
        log("FAQ detectado")
        return faq

    pkg_temp=extract_package(t)
    if pkg_temp and not s["booking_started"]:
        log("Paquete fuera de reserva detectado")
        return package_info(pkg_temp)

    if detect_intent_booking(t):
        s["booking_started"]=True
        log("Intento de reserva detectado")

    updated=update_session(t,s)
    if updated:log(f"Campos actualizados: {json.dumps(s)}")

    if not s["booking_started"]and not is_relevant(t):
        log("Mensaje irrelevante — silencio")
        return""

    if t.lower()=="confirmo":
        if all(s[f]for f in REQUIRED_FIELDS):
            ok,tbl=insert_reservation(phone,s)
            if ok:
                nm=s["student_name"]; pkg=s["package"]; lbl=PACKAGE_INFO[pkg]["label"]
                d=s["date"]; h=s["time"]
                reset_session(phone)
                log("Cita confirmada")
                return(
                    f"✅ ¡Cita confirmada!\n"
                    f"El estudiante {nm} tiene su cita para el paquete {lbl}.\n"
                    f"Fecha: {d} a las {h}.\n"
                    f"Te atenderemos en la mesa {tbl}.\n"
                    "¡Te esperamos! 😊"
                )
            else:
                return"Hubo un error registrando la cita. Intenta nuevamente."
        else:
            return"Aún faltan datos para poder confirmar la cita."

    miss=ask_missing(s)
    if miss:
        log(f"Falta: {miss}")
        return miss

    if all(s[f]for f in REQUIRED_FIELDS):
        log("Resumen generado")
        return build_summary(s)

    if not is_relevant(t):
        log("Irrelevante en flujo — silencio")
        return""

    return""
@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    form = await request.form()
    text = form.get("Body", "")
    phone = form.get("From", "").replace("whatsapp:", "")
    log(f"Webhook received: {phone} -> {text}")

    reply = handle_message(phone, text)

    if reply == "":
        if TEST_MODE:
            return Response(content="", media_type="text/plain")
        r = MessagingResponse()
        return Response(content=str(r), media_type="application/xml")

    return twiml(reply)

@app.get("/")
def root():
    return {"status": "Oriental IPS WhatsApp Bot running"}
