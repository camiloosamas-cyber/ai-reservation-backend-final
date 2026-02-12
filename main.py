print(">>> STARTING ORIENTAL IPS BOT v3.4.0 ✅")

from dotenv import load_dotenv
load_dotenv()

import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

# Optional imports with fallbacks
try:
    import dateparser
    DATEPARSER_AVAILABLE = True
except ImportError:
    DATEPARSER_AVAILABLE = False

# ============================
# NUMBER WORD PARSER (1–31)
# ============================

NUM_WORDS = {
    "uno": 1, "una": 1,
    "dos": 2,
    "tres": 3,
    "cuatro": 4,
    "cinco": 5,
    "seis": 6,
    "siete": 7,
    "ocho": 8,
    "nueve": 9,
    "diez": 10,
    "once": 11,
    "doce": 12,
    "trece": 13,
    "catorce": 14,
    "quince": 15,
    "dieciseis": 16, "dieciséis": 16,
    "diecisiete": 17,
    "dieciocho": 18,
    "diecinueve": 19,
    "veinte": 20,
    "veintiuno": 21, "veinte y uno": 21,
    "veintidos": 22, "veintidós": 22, "veinte y dos": 22,
    "veintitres": 23, "veintitrés": 23, "veinte y tres": 23,
    "veinticuatro": 24, "veinte y cuatro": 24,
    "veinticinco": 25, "veinte y cinco": 25,
    "veintiseis": 26, "veintiséis": 26, "veinte y seis": 26,
    "veintisiete": 27, "veinte y siete": 27,
    "veintiocho": 28, "veinte y ocho": 28,
    "veintinueve": 29, "veinte y nueve": 29,
    "treinta": 30,
    "treinta y uno": 31, "treintaiuno": 31,
}

# Spanish weekday mapping (Monday=0, Sunday=6)
WEEKDAYS_ES = {
    "lunes": 0,
    "martes": 1,
    "miércoles": 2, "miercoles": 2,
    "jueves": 3,
    "viernes": 4,
    "sábado": 5, "sabado": 5,
    "domingo": 6
}

def words_to_number(text):
    t = text.lower().strip()
    t = (
        t.replace("á","a")
         .replace("é","e")
         .replace("í","i")
         .replace("ó","o")
         .replace("ú","u")
    )
    return NUM_WORDS.get(t)

try:
    from supabase import create_client, Client, PostgrestAPIError
    SUPABASE_AVAILABLE = True
except ImportError:
    SUPABASE_AVAILABLE = False
    print("WARNING: Supabase not available")

try:
    from twilio.twiml.messaging_response import MessagingResponse
    TWILIO_AVAILABLE = True
except ImportError:
    TWILIO_AVAILABLE = False
    print("WARNING: Twilio not available")

import unicodedata

def remove_accents(input_str):
    """Remove Spanish accents for robust matching"""
    nfkd_form = unicodedata.normalize('NFKD', input_str)
    return ''.join([c for c in nfkd_form if not unicodedata.combining(c)])

def get_greeting_by_time():
    """Return appropriate greeting based on current local time"""
    now = datetime.now(LOCAL_TZ)
    hour = now.hour
    if 6 <= hour < 12:
        return "Buenos días"
    elif 12 <= hour < 18:
        return "Buenas tardes"
    else:
        return "Buenas noches"

def normalize_j_y(name_str):
    """
    Normalize common J/Y variations in Colombian first names.
    Only applies to word-initial 'j' or 'y'.
    Examples:
      johana → yohana
      yenni → yenni (unchanged)
      jenni → yenni
    """
    words = name_str.split()
    normalized_words = []
    for word in words:
        lower_word = word.lower()
        # Common J→Y mappings (only if word starts with j/y and is likely a name)
        if lower_word.startswith('j') and len(lower_word) >= 3:
            # Try converting 'j' to 'y'
            candidate = 'y' + lower_word[1:]
            # Only apply if the 'y' version is in our known first names
            if remove_accents(candidate) in COMMON_FIRST_NAMES:
                normalized_words.append(candidate)
            else:
                normalized_words.append(lower_word)
        elif lower_word.startswith('y') and len(lower_word) >= 3:
            # Try converting 'y' to 'j' (less common, but possible)
            candidate = 'j' + lower_word[1:]
            if remove_accents(candidate) in COMMON_FIRST_NAMES:
                normalized_words.append(candidate)
            else:
                normalized_words.append(lower_word)
        else:
            normalized_words.append(lower_word)
    return " ".join(normalized_words)

# =============================================================================
# CONFIGURATION
# =============================================================================

os.chdir(os.path.dirname(os.path.abspath(__file__)))

TEST_MODE = os.getenv("TEST_MODE") == "1"

app = FastAPI(title="Oriental IPS WhatsApp Bot", version="3.4.0")

print("🚀 Oriental IPS Bot v3.4.0 - Production Ready")

# Timezone
try:
    LOCAL_TZ = ZoneInfo("America/Bogota")
except ZoneInfoNotFoundError:
    LOCAL_TZ = ZoneInfo("UTC")
    print("WARNING: Using UTC timezone")

# Static files and templates
try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
    templates = Jinja2Templates(directory="templates")
    print("✅ Static files and templates loaded")
except Exception as e:
    print(f"WARNING: Could not load static files: {e}")
    templates = None

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# =============================================================================
# EXTERNAL SERVICES
# =============================================================================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE = os.getenv("SUPABASE_SERVICE_ROLE")

supabase = None

if SUPABASE_AVAILABLE and SUPABASE_URL and SUPABASE_SERVICE_ROLE:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE)
        print("✅ Supabase connected")
    except Exception as e:
        print(f"ERROR: Supabase connection failed: {e}")
else:
    print("WARNING: Supabase credentials missing")

# Business configuration — MUST BE DEFINED
RESERVATION_TABLE = "reservations"
SESSION_TABLE = "sessions"
BUSINESS_ID = 2


# =============================================================================
# IN-MEMORY SESSION STORE (Fallback)
# =============================================================================

MEMORY_SESSIONS = {}

# =============================================================================
# SESSION MANAGEMENT
# =============================================================================

def create_new_session(phone):
    """Create a fresh session"""
    return {
        "phone": phone,
        "student_name": None,
        "school": None,
        "package": None,
        "date": None,
        "time": None,
        "age": None,
        "cedula": None,
        "booking_started": False,
        "booking_intro_shown": False,
        "greeted": False,
        "awaiting_confirmation": False,
        "first_user_message_processed": False,
        "fields_asked": [],
    }

def get_session(phone):
    """Retrieve or create session for phone number"""
    
    # Try database first
    if supabase:
        try:
            result = supabase.table(SESSION_TABLE).select("data").eq("phone", phone).maybe_single().execute()
            
            if result and result.data and result.data.get("data"):
                session = result.data["data"]
                session["phone"] = phone
                return session
        except Exception as e:
            print(f"Error loading session from DB: {e}")
    
    # Fallback to memory
    if phone not in MEMORY_SESSIONS:
        MEMORY_SESSIONS[phone] = create_new_session(phone)
    
    return MEMORY_SESSIONS[phone]

def save_session(session):
    """Save session to database and memory"""
    phone = session.get("phone")
    if not phone:
        return
    
    # Always save to memory
    MEMORY_SESSIONS[phone] = session
    
    # Try to save to database
    if supabase:
        try:
            data = {k: v for k, v in session.items() if k != "phone"}
            supabase.table(SESSION_TABLE).upsert({
                "phone": phone,
                "data": data,
                "last_updated": datetime.now(LOCAL_TZ).isoformat()
            }).execute()
        except Exception as e:
            print(f"Error saving session to DB: {e}")

# =============================================================================
# PACKAGE DATA
# =============================================================================

PACKAGES = {
    "esencial": {
        "name": "Paquete Cuidado Esencial",
        "price": "45.000",
        "description": "Medicina General, Optometria y Audiometria"
    },
    "activa": {
        "name": "Paquete Salud Activa",
        "price": "60.000",
        "description": "Medicina General, Optometria, Audiometria y Psicologia"
    },
    "bienestar": {
        "name": "Paquete Bienestar Total",
        "price": "75.000",
        "description": "Medicina General, Optometria, Audiometria, Psicologia y Odontologia"
    }
}

# Known schools in Yopal (case-insensitive, used for extraction)
KNOWN_SCHOOLS = [
    # Public / Official Institutions
    "institución educativa 1 de mayo",
    "institución educativa antonio nariño",
    "institución educativa braulio gonzález",
    "institución educativa camilo torres",
    "institución educativa camilo torres restrepo",
    "institución educativa carlos lleras restrepo",
    "institución educativa centro social la presentación",
    "institución educativa luis hernández vargas",
    "institución educativa megacolegio yopal",
    "institución educativa policarpa salavarrieta",
    "institución educativa picón arenal",
    "institución educativa santa teresa",
    "institución educativa santa teresita",
    "escuela santa bárbara",
    "escuela santa inés",
    "escuela el paraíso",
    
    # Private / Non-Official Schools
    "academia militar josé antonio páez",
    "colegio alianza pedagógica",
    "colegio antonio nariño",
    "colegio bethel yopal",
    "colegio braulio gonzález – sede campestre",
    "colegio campestre hispano inglés del casanare",
    "colegio campestre liceo literario del casanare",
    "colegio lucila piragauta",
    "colegio rafael pombo",
    "colegio san mateo apóstol",
    "gimnasio comfacasanare",
    "gimnasio de los llanos",
    "gimnasio loris malaguzzi",
    "instituto pilosophia",
    "liceo escolar los ángeles",
    "liceo gustavo matamoros león",
    "liceo moderno celestín freinet casanare",
    "liceo pedagógico colombo inglés",
    "liceo pedagógico san martín",
    
    # Technical / Vocational
    "instituto técnico empresarial de yopal",
    "instituto técnico ambiental san mateo",
    
    # Early Childhood
    "jardín infantil castillo de la alegría",
    "jardín infantil carrusel mágico",
    "jardin Infantil Soles y Lunas",
    "Soles y Lunas",
    
    # COMMON SHORT FORMS & TYPING VARIANTS (no accents, partial names)
    "gimnasio los llanos",
    "los llanos",
    "comfacasanare",
    "san jose",
    "santa teresita",
    "santa teresa",
    "rafael pombo",
    "lucila piragauta",
    "campestre hispano",
    "liceo literario",
    "megacolegio",
    "itey",
    "instituto tecnico",
    "jardin carrusel",
    "castillo alegria",
    "picon arenal",
    "braulio gonzalez",
    "camilo torres",
    "antonio narino",
]

# Sort schools by length (longest first) for accurate matching
KNOWN_SCHOOLS = sorted(KNOWN_SCHOOLS, key=len, reverse=True)

# Common first names in Colombia (Yopal region)
COMMON_FIRST_NAMES = {
    "alejandro", "alejo", "adrián", "aarón", "abel", "abraham", "agustín", "alan", "alberto",
    "albán", "aldair", "alexander", "alexis", "alfonso", "alfredo", "álvaro", "amador", "anderson",
    "andrés", "ángel", "aníbal", "anthony", "antonio", "ariel", "armando", "arley", "arturo",
    "bairon", "baltazar", "benjamín", "bernardo", "bladimir", "boris", "brayan", "brayhan", "breiner",
    "bruno", "camilo", "carlos", "carmelo", "césar", "cristian", "cristóbal", "daniel", "dante",
    "darío", "darian", "dario", "david", "deiver", "deiby", "delfín", "diego", "dilan", "dilan mateo",
    "dilan steven", "dilan andrés", "dimitri", "dixon", "duarte", "duberney", "duvan", "duván",
    "edgar", "edinson", "edison", "eduardo", "edwin", "efrén", "eider", "einer", "eivar", "elías",
    "elkin", "emerson", "emilio", "emmanuel", "enrique", "erick", "ernesto", "esneider", "esteban",
    "eugenio", "evaristo", "ezequiel", "fabián", "facundo", "felipe", "félix", "fernando", "flavio",
    "francisco", "franco", "fredy", "froilán", "gabriel", "gael", "germán", "gerson", "gildardo",
    "gonzalo", "gregorio", "guillermo", "gustavo", "harold", "héctor", "helmer", "henry", "hernán",
    "hilario", "hugo", "ignacio", "ismael", "iván", "jacob", "jaime", "jairo", "jampier", "javier",
    "jean", "jefferson", "jerónimo", "jesús", "jhon", "jhonatan", "jhon fredy", "jhon anderson",
    "jhon edwar", "joaquín", "joel", "jonatan", "jordán", "jorge", "josé", "joseph", "josué", "juan",
    "juan camilo", "juan esteban", "juan josé", "juan sebastián", "juan david", "juan manuel", "julio",
    "julián", "justo", "kevin", "keider", "kendry", "kendall", "kenneth", "kenny", "kevin andrés",
    "kevin steven", "leandro", "leo", "leonardo", "leonel", "lorenzo", "luciano", "luis", "luis ángel",
    "luis carlos", "luis eduardo", "luis fernando", "luis miguel", "manuel", "marco", "marcos", "mario",
    "martín", "mateo", "matías", "mauricio", "maximiliano", "medardo", "miguel", "miguel ángel", "miller",
    "moisés", "nicolás", "néstor", "nilson", "noel", "óscar", "omar", "orlando", "pablo", "pascual",
    "patricio", "pedro", "rafael", "ramiro", "randall", "raúl", "reinaldo", "rené", "richard", "rigo",
    "rigoberto", "roberto", "rodolfo", "rogelio", "román", "ronald", "rubén", "ruddy", "rudy", "salomón",
    "salvador", "samuel", "samuel andrés", "samuel david", "santiago", "sebastián", "segundo", "sergio",
    "sneyder", "stiven", "steven", "simón", "teófilo", "thiago", "tobías", "tomás", "ulises", "valentín",
    "valerio", "vicente", "víctor", "wálter", "wílmar", "wilmer", "william", "wilson", "xavier", "yahir",
    "yair", "yamid", "yeferson", "yeison", "yorman", "yulian", "yurgen", "zacarías", "abigaíl", "adela",
    "adelaida", "adriana", "aída", "ainhoa", "alba", "albertina", "alejandra", "alexandra", "alina",
    "alicia", "alison", "allison", "amalia", "ambar", "amelia", "amparo", "ana", "ana belén", "ana carolina",
    "ana cristina", "ana isabel", "ana lucía", "ana maría", "ana milena", "ana paola", "ana patricia",
    "ana sofía", "andrea", "ángela", "angélica", "anita", "antonia", "aracely", "aranzazu", "arelis",
    "arleth", "astrid", "aura", "aurora", "azucena", "bárbara", "beatriz", "belén", "benedicta", "bernarda",
    "bertha", "betty", "blanca", "blanca inés", "brenda", "brianna", "briggitte", "carina", "carla",
    "carlota", "carlina", "carmenza", "carmen", "carmen rosa", "carolina", "casandra", "catalina",
    "cecilia", "celeste", "celia", "celmira", "cindy", "clara", "clara inés", "clarisa", "claudia",
    "claudia patricia", "claudia marcela", "claudia milena", "clemencia", "cloe", "concepción", "constanza",
    "consuelo", "coral", "cristina", "dafne", "dalia", "damaris", "dana", "dana valentina", "daniela",
    "danna", "danna sofía", "dayana", "débora", "deyanira", "diana", "diana carolina", "diana paola",
    "diana patricia", "diana marcela", "diana lorena", "dilia", "dolores", "dominga", "dora", "edith",
    "elena", "elianna", "elisa", "elisabeth", "elka", "ella", "elsa", "elvia", "elvira", "emilia", "emma",
    "erika", "ermelinda", "esmeralda", "esperanza", "estefanía", "estela", "esther", "eugenia", "eva",
    "evelin", "evelyn", "fabiola", "fara", "fatima", "fernanda", "fidela", "filomena", "floresmira", "flor",
    "flor ángela", "flor alba", "flor amarilis", "flor elena", "flor marina", "florinda", "francy", "francia",
    "francy lorena", "gabriela", "genoveva", "geraldine", "gina", "giselle", "gladis", "glenda", "gloria",
    "graciela", "grecia", "greta", "guadalupe", "hanna", "haydee", "helena", "helena sofía", "hilda",
    "iliana", "inés", "ingrid", "irene", "iris", "isabel", "isabela", "isabella", "isidora", "iveth",
    "ivonne", "jacqueline", "jazmín", "jenifer", "jennifer", "jessica", "jimena", "joana", "johanna",
    "josefa", "josefina", "joyce", "judith", "julia", "juliana", "julieta", "july", "karen", "karina",
    "karla", "katherine", "katherin", "katerine", "katrina", "kelly", "keila", "kimberly", "koral", "lana",
    "lara", "laura", "laura sofía", "layla", "leidy", "leidy johana", "leidy tatiana", "leonor", "leydi",
    "lia", "liana", "libia", "lida", "lidia", "ligia", "liliana", "lina", "lina maría", "lina marcela",
    "lina sofía", "linda", "liseth", "lissette", "livia", "lola", "lorena", "lorna", "lourdes", "lucero",
    "lucía", "lucila", "lucrecia", "luisa", "luisa fernanda", "luisa maría", "luna", "luz", "luz adriana",
    "luz ángela", "luz amanda", "luz dary", "luz elena", "luz marina", "luz mery", "magaly", "magda",
    "magdalena", "maite", "maira", "malena", "manuela", "marcela", "margarita", "maría", "maría alejandra",
    "maría camila", "maría cristina", "maría del carmen", "maría del mar", "maría elena", "maría fernanda",
    "maría isabel", "maría josé", "maría laura", "maría paula", "maría victoria", "mariana", "maritza",
    "martha", "martina", "mateo", "matilde", "mayra", "melani", "melany", "melba", "melissa", "mercedes",
    "mery", "micaela", "michelle", "milena", "mireya", "mónica", "nancy", "narda", "natalia", "natividad",
    "nayeli", "nayibe", "nazareth", "nelly", "nicole", "nidia", "nidia esperanza", "noelia", "nora", "norma",
    "nubia", "nury", "ofelia", "olga", "olga lucía", "olga marina", "olivia", "otilia", "pamela", "paola",
    "paola andrea", "pasión", "patricia", "paula", "paulina", "piedad", "pilar", "rafaela", "raiza",
    "raquel", "rebeca", "regina", "rita", "rocío", "rosa", "rosa ángela", "rosa alicia", "rosa elena",
    "rosa maría", "rosalba", "rosalía", "rosario", "roxana", "ruby", "ruth", "salomé", "samara", "samira",
    "sandra", "sandra patricia", "sara", "sarita", "sebastiana", "selena", "serena", "sharon", "sheyla",
    "silvia", "sofía", "sonia", "stella", "susana", "tatiana", "teresa", "teresita", "tiffany", "trinidad",
    "valentina", "valeria", "vanessa", "vera", "verónica", "vicenta", "victoria", "vilma", "vivian",
    "viviana", "wendy", "ximena", "xiomara", "yadira", "yamile", "yennifer", "yenny", "yiceth", "yina",
    "yohana", "johana", "yolanda", "yuleidy", "yuliana", "yurani", "zulma"
}

# Common last names in Colombia (Yopal region)
COMMON_LAST_NAMES = {
    "abad","abello","abondano","abril","acebedo","acevedo","acha","acuña","adarme","adrada",
    "affonso","afanador","agamez","agudelo","aguilar","aguirre","aguillón","ahumada","aiza",
    "aldana","alegría","alarcón","albarracín","albornoz","alcázar","alfaro","alferez","almanza",
    "almeida","almonacid","almosny","altahona","altamar","alturo","alveiro","alvarado","alvarino",
    "álvarez","amado","amaya","amézquita","amorocho","amortegui","andrade","angel","angulo",
    "anacona","anchico","andocilla","anillo","anzola","antezana","antolinez","aparicio","aponte",
    "apraez","arango","aragón","aranda","ararat","araque","arbeláez","arboleda","arboledas",
    "arciniegas","arce","archila","ardila","ardón","arévalo","arguello","argumedo","arias",
    "aricapa","arieta","arisa","aristizábal","ariza","arjona","armenta","armenteros","armesto",
    "armeni","arnaiz","arnedo","arrieta","arriola","arroyave","arteaga","artiles","arzayús",
    "ascencio","asprilla","aspuac","astorga","astorquiza","atencia","atuesta","aubad","avendaño",
    "avilés","ayala","ayola","ayuela","ayucar","bacca","bachiller","badal","badillo","badel",
    "bahamón","baiocco","balaguera","balanta","balcázar","baleta","baloco","balsalobre","baltán",
    "bambague","banguero","banquet","bañol","barahona","barbosa","barcena","barrera","barragán",
    "barreneche","barrero","barreto","barrieta","barrios","barsallo","bartolo","basabe","bascur",
    "basualdo","bastidas","batista","bautista","bazurto","bedoya","behaine","bejarano","belalcázar",
    "beltrán","benavente","benavides","benjumea","benzaquen","bendeck","berbeo","berenguer","bermón",
    "bermúdez","bernate","bernal","bernini","berrioza","berrío","bertel","betancourt","betin","beuth",
    "beytia","biel","bielsa","bihun","bilbao","bitar","blandón","blanquicett","blaya","blanco",
    "bocanegra","bocarejo","bocanumenth","bochagova","bohórquez","bojacá","boldt","bolivar","bolona",
    "bolívar","bongiorno","bonilla","bonnett","boquet","borda","borja","borray","borrero","borromeo",
    "botache","botero","boutureira","brahim","brand","bravo","briceño","briñez","brito","brun","brunal",
    "buchelli","buenaventura","buelvas","buendía","bueno","buesaquillo","bueso","buitrago","buitrón",
    "buonpane","burbano","buriticá","burgos","busch","bustacara","bustamante","bustos","caballero",
    "cabeza","cabra","cabrales","cabrera","caceres","cadavid","cadena","caguazango","caicedo","caimoy",
    "cajamarc","cajiao","calambas","calambéz","calderín","calderón","calero","calindo","calpa","calvache",
    "calvachi","calvijo","calvo","camacho","camargo","camaño","cambar","camejo","camero","camiña","campaño",
    "campillo","campiño","campuzano","canabal","canales","canchala","canchila","candelo","canelón","canizares",
    "canales","canaval","cangrejo","canillas","caniza","cano","canosa","cantillo","canzio","cárcamo","caraña",
    "carantón","carballo","carbonell","carcamo","cárdens","cardiel","cardona","cardozo","carhuamaca","carillo",
    "carmenza","carmona","carnaza","carpa","carranza","carrasco","carrero","carreño","carrillo","carrión",
    "carrizosa","carro","cartagena","carvajal","casares","casadiego","casallas","casas","casasbuenas",
    "casasola","casimiro","catalán","catambuco","caycedo","ceballos","cediel","cedrés","ceferino","celada",
    "celis","celtic","cenén","cepeda","cera","cerchiaro","cerón","cerquera","cerrano","cervantes","ceta",
    "chalá","chacón","chalarca","chamorro","charria","chaparro","charry","chávez","chego","chegwin","chequer",
    "chico","chica","chimapira","chinche","chinchilla","chingate","chiquillo","chiriví","chocontá","chogo",
    "chucuri","chucue","chucurí","cifuentes","cirolla","ciro","clavero","clavijo","clímaco","cobo","cobos",
    "cochero","cochise","coconubo","cocomá","coda","codina","coello","cogollos","coicue","cojines","colindres",
    "colmenares","colmenarez","colocho","colonia","colón","colpas","colorado","combariza","condia","coneo",
    "confaloniere","congote","conde","conrado","contreras","congote","copa","corchuelo","cordobés","córdova",
    "córdoba","coronado","coronel","corral","corrales","correa","corredor","corró","cortázar","cortés","cortizo",
    "cossio","cote","cotero","covacho","cova","coya","coyaima","cristancho","cruzado","cuadrado","cuadros",
    "cuartas","cubela","cuberos","cubilla","cubillos","cuéllar","cuenca","cueros","cuero","cuevas","cuenú",
    "cuentas","cumbal","cunampia","cundar","cuni","cuquejo","cura","cure","currea","currelí","cusa","cuya",
    "cyrino","daza","delgado","díaz","diez","dimate","dízz","domínguez","donado","doncel","duarte","duque",
    "durán","echeverri","echavarría","eljach","escalante","escalona","escamilla","escandón","escobar","escorcia",
    "españa","espinal","esquivel","estepa","estupiñán","evans","fajardo","farías","farfán","ferro","fierro",
    "figueredo","flechas","flórez","fonseca","forero","franco","frasser","frías","fuentes","galán","galindo",
    "gallego","gallo","galvis","gamarra","gamboa","garay","garavito","garces","garibello","garzón","garzon",
    "gaviria","gélvez","giraldo","góez","gómez","góngora","gonzález","gordo","gordillo","górriz","goyeneche",
    "guaca","guacaneme","guáqueta","guarín","guayazán","guecha","güengue","guevara","guisa","gutiérrez",
    "guzmán","haro","henao","hereira","herrera","herrán","hidalgo","higuita","hinestroza","holguín",
    "hormiga","hoyos","huertas","hurtado","ibañez","ibarra","ibargüen","illera","íñiguez","inestrosa",
    "insignares","irriarte","isaza","jaimes","jáuregui","jerez","jeronimo","jhaime","jiménez","jiraldo",
    "jurado","ladino","lagos","laguado","lamarca","lamilla","lancheros","landinez","lara","largacha",
    "lascarro","lasso","latorre","laverde","leal","león","lerma","lesmes","leyva","lezama","liévano",
    "linares","llamas","llinás","lloreda","llorente","lobatón","lobo","lozano","lozada","lucena",
    "lugo","luján","luna","macías","madera","madroñero","madrid","maestre","maldonado","manjarrez",
    "manrique","mantilla","manzo","maradiaga","marenco","marín","marino","marmolejo","marroquín",
    "marrugo","martelo","martínez","martín","maryory","massa","mateus","matiz","maturana","maya",
    "mayorga","mazabel","mejía","meléndez","meneses","mendoza","mendivelso","mendieta","menjura",
    "mesa","mestra","mestre","metke","meza","millán","mina","mira","miranda","mogollón","molina",
    "molleja","moncada","moncayo","mondragón","monroy","monsalve","montaña","montañez","montañés",
    "montenegro","montero","montes","montoya","morales","morán","moreno","morillo","morlán","morón",
    "mora","moscote","mosquera","motta","moyano","muelas","muñoz","murcia","murgas","murillo","mussa",
    "najar","naranjo","narváez","navarro","navarrete","navas","negrete","neira","nerio","nieves",
    "nieto","niño","nivia","noguera","noriega","nova","novoa","núñez","obando","ocampo","octavio",
    "olarte","olaya","oleas","oliveros","omaña","oñate","ontiveros","oquendo","ordóñez","orjuela",
    "ormaza","orrego","ortega","ortiz","ospina","osorno","osorio","ossa","otero","otálora","oviedo",
    "oyola","pabón","pacheco","padilla","páez","palacios","palencia","pallares","palma","pardo",
    "paredes","parra","paternina","patiño","pava","pavón","payares","pecero","pedraza","peña",
    "perdomo","pérez","perilla","pereira","perea","perico","pestana","petro","pico","pinchao",
    "pineda","pinilla","pinzón","pinto","piraquive","pizarro","plazas","plata","plaza","polo","pomares",
    "pombo","porras","portela","portillo","posada","posso","prado","pradilla","preciado","prieto",
    "puello","puentes","puga","pulido","pupo","quiceno","quijano","quimbayo","quintero","quiñones",
    "quiroga","quirós","rada","rafael","ramírez","ramón","rangel","rasgado","rave","rayo","realpe",
    "reátiga","rebolledo","recalde","reina","restrepo","retamoso","revelo","reyes","riaño","ricaurte",
    "rico","rincón","rios","ríos","rivadeneira","rivera","riveros","robayo","roberto","robles",
    "roca","rodríguez","rojas","roldán","romero","romo","ronderos","rondón","roque","rosales",
    "rosero","rosso","rousseau","rubiano","ruiz","saavedra","sabogal","sacristán","saénz","sáenz",
    "sáez","saiz","salamanca","salas","salazar","salcedo","salgado","salinas","salmerón","samudio",
    "samarra","sánchez","sandoval","sanguino","santacruz","santa","santafé","santamaría","santander",
    "santiago","santofimio","santos","sarabia","saray","saravia","sarmiento","sarria","segovia",
    "segura","sepúlveda","sevilla","sierra","silgado","silva","silvera","simanca","simón","sinisterra",
    "sierralta","silos","solano","solarte","solís","solórzano","sosa","soto","sotelo","subía","suárez",
    "sua","suaza","suescún","supelano","taborda","tabares","tacuma","tafur","tamayo","támara","tambon",
    "tapiquihua","tarazona","tatis","tautiva","tejada","téllez","tello","tena","tenero","tinjacá",
    "tique","tirado","tobar","toledo","tolosa","torijano","toro","torralba","torrealba","torreglosa",
    "torres","torrijos","tovar","triana","triviño","trochez","trujillo","tuberquia","turriago","tuta",
    "ulloa","umaña","upegui","urbano","urdaneta","ureña","uribe","urquijo","urueta","useche","ustate",
    "valbuena","valdés","valderrama","valdivia","valdivieso","valencia","valera","valero","valladares",
    "valle","vallecilla","vallesilla","vanegas","varela","vargas","varón","velandia","velarde",
    "velasco","velásquez","vélez","veloza","vences","vera","vergara","verraco","vesga","vianchá",
    "vidal","vidales","vides","villabona","villadiego","villafañe","villagra","villalba","villalobos",
    "villamil","villamizar","villaneda","villanueva","villaquirán","villarraga","villarreal","villate",
    "villavicencio","villegas","vinasco","virviescas","viveros","vizcaíno","yepes","yépez","yopasa",
    "yotagri","yusti","yustres","yucuma","zabaleta","zabrano","zacipa","zafra","zambrano","zamora",
    "zapata","zárate","zarur","zea","zegarra","zerda","zipaquirá","zorilla","zubiría","zuluaga","zurita"

}

# Normalize name sets for accent-insensitive matching
COMMON_FIRST_NAMES = {remove_accents(n.lower()) for n in COMMON_FIRST_NAMES}
COMMON_LAST_NAMES = {remove_accents(n.lower()) for n in COMMON_LAST_NAMES}

# Session storage: remembers partial booking data per WhatsApp user
user_sessions = {}

# =============================================================================
# FAQ RESPONSES
# =============================================================================

FAQ = {
    "ubicacion": "Estamos ubicados en Calle 17 #16-53, Yopal, frente al Hospital San José.",
    "pago": "Aceptamos Nequi y efectivo.",
    "duracion": "Los exámenes médicos escolares toman aproximadamente 30 minutos por estudiante.",
    "llevar": "Debes traer el documento de identidad del estudiante (Tarjeta de Identidad o Cédula).",
    "horario": "Atendemos de lunes a viernes de 6:00 a.m. a 8:00 p.m.",
    "agendar": "Para garantizar una atención rápida y organizada, es necesario agendar la cita previamente. Si quieres, puedo ayudarte a reservarla ahora mismo 😊"
}

# =============================================================================
# EXTRACTION FUNCTIONS
# =============================================================================

def extract_package(msg):
    """Extract package from message, including 'opcion 1/2/3', prices, and names."""
    text = msg.lower().strip()
    
    # Normalize accents and extra spaces
    text = (
        text.replace("á", "a")
            .replace("é", "e")
            .replace("í", "i")
            .replace("ó", "o")
            .replace("ú", "u")
    )

    # Helper: check if a standalone digit (1, 2, 3) appears in a relevant context
    def matches_option_number(num_str, target_num):
        # Match "1", "2", "3" only if near keywords like "paquete", "opcion", "quiero", etc.
        pattern = rf"\b{num_str}\b"
        m = re.search(pattern, text)
        if m:
            # Check context around the number
            start = max(0, m.start() - 20)
            end = min(len(text), m.end() + 20)
            context = text[start:end]
            if any(kw in context for kw in ["paquete", "opcion", "opción", "quiero", "elijo", "tomo", "la", "el", "numero", "número"]):
                return True
        return False

    # --- OPTION 1: Cuidado Esencial ---
    if (any(k in text for k in ["esencial", "verde", "45k", "45000", "45.000", "45,000", "45 mil"])
        or re.search(r"\b(?:opcion|opción)\s*[11]\b", text)
        or matches_option_number("1", 1)):
        return "Paquete Cuidado Esencial"

    # --- OPTION 2: Salud Activa ---
    if (any(k in text for k in ["activa", "salud activa", "azul", "psico", "psicologia", "60k", "60000", "60.000", "60,000", "60 mil"])
        or re.search(r"\b(?:opcion|opción)\s*[22]\b", text)
        or matches_option_number("2", 2)):
        return "Paquete Salud Activa"

    # --- OPTION 3: Bienestar Total ---
    if (any(k in text for k in ["bienestar", "total", "amarillo", "completo", "odonto", "75k", "75000", "75.000", "75,000", "75 mil"])
        or re.search(r"\b(?:opcion|opción)\s*[33]\b", text)
        or matches_option_number("3", 3)):
        return "Paquete Bienestar Total"

    return None

def extract_student_name(msg, current_name):
    """Extract student name using common first/last name lists"""
    text = msg.strip()
    if not text:
        return None
    lower = text.lower()

    # Skip if name exists and user not changing it
    if current_name and not any(k in lower for k in ["cambiar", "otro nombre", "se llama", "nombre"]):
        return None

    # Skip greetings
    if lower in ["hola", "buenos dias", "buenas tardes", "buenas noches", "buenas"]:
        return None

    # Pattern: "se llama X"
    if "se llama" in lower:
        parts = lower.split("se llama", 1)
        if len(parts) > 1:
            candidate = parts[1].strip()
            for stop in ["del", "de", "tiene", "edad", "colegio", "cedula", "su", "el", "la", ",", "."]:
                if f" {stop}" in candidate:
                    candidate = candidate.split(f" {stop}")[0]
                    break
            candidate = candidate.strip().rstrip(".,!?")
            if candidate and len(candidate.split()) >= 2:
                return candidate.title()

    # Pattern: "mi hijo/mi hija [Full Name]"
    if "mi hijo" in lower or "mi hija" in lower:
        # Match everything after "mi hijo/hija" up to a stop word or end
        m = re.search(r"mi\s+(?:hijo|hiijo|hija)\s+((?:[a-záéíóúñ]+\s*)+?)(?=\s*(?:del|de|tiene|edad|colegio|paquete|documento|id|cita|$))", lower)
        if m:
            full_candidate = m.group(1).strip()
            # Split into words and validate from left to right
            words = full_candidate.split()
            valid_name_parts = []
            for word in words:
                clean_word = remove_accents(word.lower())
                # Accept if it's a known first name, last name, or looks like a name (starts with letter, not too short)
                if (clean_word in COMMON_FIRST_NAMES or 
                    clean_word in COMMON_LAST_NAMES or 
                    (len(clean_word) >= 2 and clean_word.isalpha())):
                    valid_name_parts.append(word)
                else:
                    break  # Stop at first non-name word
            if valid_name_parts:
                # Require at least one known first name in the sequence
                if any(remove_accents(w.lower()) in COMMON_FIRST_NAMES for w in valid_name_parts):
                    return " ".join(valid_name_parts).title()

    # Pattern: "nombre es X"
    if "nombre" in lower:
        m = re.search(r"nombre\s*:?\s*es\s+([a-záéíóúñ\s]+)", lower)
        if m:
            name = m.group(1).strip()
            name = re.split(r"\s+(?:de|del|tiene|anos?|colegio)", name)[0].strip()
            if name and len(name.split()) >= 2:
                return name.title()

    # ✅ Support [First + Last] and [First + First] (compound names)
    words = text.split()
    for i in range(len(words)):
        word1_clean = remove_accents(words[i].lower().strip(".,!?"))
        if word1_clean in COMMON_FIRST_NAMES:
            for j in range(i + 1, min(i + 3, len(words))):
                word2_clean = remove_accents(words[j].lower().strip(".,!?"))
                if word2_clean in COMMON_FIRST_NAMES or word2_clean in COMMON_LAST_NAMES:
                    full_name = " ".join(words[i:j+1]).title()
                    return full_name

    # Fallback: single first name anywhere in message
    words = text.split()
    for word in words:
        clean = remove_accents(word.lower().strip(".,!?"))
        if clean in COMMON_FIRST_NAMES:
            return word.title()
                    
    # Fallback: try first two words of message if they look like names
    lines = [line.strip() for line in msg.split("\n") if line.strip()]
    if lines:
        first_line = lines[0]
        name_parts = first_line.split()[:2]
        if len(name_parts) == 2:
            w1_norm = remove_accents(name_parts[0].lower())
            w2_norm = remove_accents(name_parts[1].lower())
            if w1_norm in COMMON_FIRST_NAMES and w2_norm in COMMON_LAST_NAMES:
                return f"{name_parts[0]} {name_parts[1]}".title()
                
    return None

def extract_school(msg):
    """Extract school name using KNOWN_SCHOOLS with flexible partial matching"""
    text = msg.lower().strip()
    normalized_text = (
        text
        .replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
        .replace("ñ", "n")
        .replace("ü", "u")
    )

    # Normalize all known schools once
    normalized_schools = []
    for school in KNOWN_SCHOOLS:
        norm = (
            school.lower()
            .replace("á", "a")
            .replace("é", "e")
            .replace("í", "i")
            .replace("ó", "o")
            .replace("ú", "u")
            .replace("ñ", "n")
        )
        normalized_schools.append((school, norm))

    # 1. Try exact phrase match (longest first)
    for original, norm_school in normalized_schools:
        if re.search(rf"\b{re.escape(norm_school)}\b", normalized_text):
            return original.title()

    # 2. Try: does user input appear *inside* a known school?
    # e.g., user says "alianza pedagogica" → matches "colegio alianza pedagogica"
    for original, norm_school in normalized_schools:
        if normalized_text in norm_school:
            return original.title()

    # 3. Try: does a known school appear *inside* user input?
    # (less likely, but safe)
    for original, norm_school in normalized_schools:
        if norm_school in normalized_text:
            return original.title()

    # 4. Keyword-based fallback (your existing logic)
    keyword_pattern = r"(colegio|gimnasio|liceo|instituto|escuela|jard[íi]n)\s+([a-záéíóúñ\s]+)"
    m = re.search(keyword_pattern, text)
    if m:
        school_phrase = m.group(2).strip()
        cleaned = re.split(
            r"\s+(?:\d|paquete|para|el|la|edad|años|anos|cedula|documento|tiene|del|de\s+(?:colegio|gimnasio))",
            school_phrase,
            maxsplit=1
        )[0].strip()
        if len(cleaned) > 3:
            clean_norm = (
                cleaned.lower()
                .replace("á", "a")
                .replace("é", "e")
                .replace("í", "i")
                .replace("ó", "o")
                .replace("ú", "u")
                .replace("ñ", "n")
            )
            for original, norm_school in normalized_schools:
                if clean_norm in norm_school or norm_school in clean_norm:
                    return original.title()
            return cleaned.title()

    return None

def clean_school_display_name(school: str) -> str:
    school = school.strip()
    if school.lower().startswith("colegio "):
        return school[8:].strip()
    return school

def extract_age(msg):
    """Extract age from digits OR Spanish word numbers (1–18)."""
    text = msg.lower()

    # 1) Direct digit age: "12 años"
    m = re.search(r"\b(\d{1,2})\s*(anos|años)\b", text)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 18:
            return n

    # 2) Word-based with 'tiene' or 'edad': "tiene ocho"
    m = re.search(r"(tiene|edad)\s+([a-záéíóúñ]+)", text)
    if m:
        candidate = m.group(2).strip()
        num = words_to_number(candidate)
        if num and 1 <= num <= 18:
            return num

    # 3) Word-based age directly: "ocho años", "quince años"
    m = re.search(r"([a-záéíóúñ]+)\s+(anos|años)", text)
    if m:
        candidate = m.group(1).strip()
        num = words_to_number(candidate)
        if num and 1 <= num <= 18:
            return num

    return None

def extract_cedula(msg):
    """Extract cedula (ID number) from message"""
    # Colombian ID: 7-12 digits
    m = re.search(r"\b(\d{7,12})\b", msg)
    if m:
        return m.group(0)
    return None

def check_availability(date_str: str, time_str: str) -> bool:
    """
    Check if a time slot has fewer than 4 bookings (max capacity).
    Assumes datetime is stored as "YYYY-MM-DD HH:MM" in Supabase.
    """
    if not supabase:
        return True  # assume available if DB down

    try:
        # Extract hour (e.g., "15" from "15:30")
        hour = time_str.split(':')[0]
        target_hour_pattern = f"{date_str} {hour}:"

        # Count how many bookings exist for this hour
        result = (
            supabase.table(RESERVATION_TABLE)
            .select("datetime", count="exact")
            .eq("business_id", BUSINESS_ID)
            .like("datetime", target_hour_pattern)
            .execute()
        )

        count = len(result.data) if result.data else 0
        return count < 4

    except Exception as e:
        print(f"Availability check error: {e}")
        return True  # assume available on error

def extract_date(msg, session):
    """Extract date from message, including relative weekdays."""
    text = msg.lower()
    # Hardcode today as 2026-02-04 for testing (adjust if needed)
    today = datetime.now(LOCAL_TZ).date()
    weekday_today = today.weekday()  # Monday=0

    # 1) Quick natural dates
    if "mañana" in text or "manana" in text:
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    if "hoy" in text:
        return today.strftime("%Y-%m-%d")
    if "pasado mañana" in text or "pasado manana" in text:
        return (today + timedelta(days=2)).strftime("%Y-%m-%d")

    # 2) WEEKDAY HANDLING: "viernes", "este lunes", "próximo miércoles"
    norm_text = (
        text.replace("á", "a")
            .replace("é", "e")
            .replace("í", "i")
            .replace("ó", "o")
            .replace("ú", "u")
    )

    for es_day, target_wd in WEEKDAYS_ES.items():
        pattern = rf"(?:este\s+|proximo\s+|próximo\s+|para\s+el\s+|el\s+)?{es_day}\b"
        if re.search(pattern, norm_text):
            days_ahead = target_wd - weekday_today
            if days_ahead <= 0:
                days_ahead += 7
            if re.search(rf"(?:proximo\s+|próximo\s+){es_day}", norm_text):
                if days_ahead <= 6:
                    days_ahead += 7
            candidate_date = today + timedelta(days=days_ahead)
            return candidate_date.strftime("%Y-%m-%d")

    # 3) DAY-IN-WORDS + MONTH (e.g., "quince de febrero")
    m = re.search(
        r"([a-záéíóúñ]+)\s+de\s+("
        r"enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
        r"septiembre|octubre|noviembre|diciembre"
        r")",
        text
    )
    if m:
        day_word = m.group(1)
        month_word = m.group(2)
        day_num = words_to_number(day_word)
        if day_num:
            month_map = {
                "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
                "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10,
                "noviembre": 11, "diciembre": 12
            }
            month_num = month_map[month_word]
            year = today.year
            try:
                parsed = datetime(2026, month_num, day_num).date()
                return parsed.strftime("%Y-%m-%d")
            except:
                pass

    # 4) Numeric date patterns (your existing logic)
    date_patterns = [
        r"(\d{1,2})\s+de\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)",
        r"(\d{1,2})\s+(ene|feb|mar|abr|may|jun|jul|ago|sep|oct|nov|dic)",
        r"(\d{1,2})[/\-](\d{1,2})",
        r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})"
    ]
    for pattern in date_patterns:
        m = re.search(pattern, text)
        if m:
            day = int(m.group(1))
            month_str = m.group(2) if len(m.groups()) > 1 else None
            month_map = {
                "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
                "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
                "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
                "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12
            }
            if month_str:
                month = month_map.get(month_str.lower())
                if month:
                    try:
                        parsed_date = datetime(2026, month, day).date()
                        if parsed_date < today:
                            parsed_date = parsed_date.replace(year=year + 1)
                        return parsed_date.strftime("%Y-%m-%d")
                    except ValueError:
                        pass
            if len(m.groups()) == 2 and day <= 12:
                month = int(m.group(2))
                try:
                    parsed_date = datetime(2026, month, day).date()
                    if parsed_date < today:
                        parsed_date = parsed_date.replace(year=year + 1)
                    return parsed_date.strftime("%Y-%m-%d")
                except ValueError:
                    pass

    # 5) Fallback to dateparser
    if DATEPARSER_AVAILABLE:
        try:
            dt = dateparser.parse(
                msg,
                languages=["es"],
                settings={
                    "TIMEZONE": "America/Bogota",
                    "RETURN_AS_TIMEZONE_AWARE": True,
                    "PREFER_DATES_FROM": "future",
                    "DATE_ORDER": "DMY"
                }
            )
            if dt:
                local_dt = dt.astimezone(LOCAL_TZ)
                parsed_date = local_dt.date()
                # Skip year rollover in test mode — always use 2026
                pass
                return parsed_date.strftime("%Y-%m-%d")
        except Exception as e:
            print("Dateparser error:", e)

    return session.get("date")

def extract_time(msg, session):
    """Extract time with full Colombian support: digits, am/pm, and word phrases."""
    text = msg.lower()
    text = (
        text.replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
    )

    def valid_hour(h):
        return 6 <= h <= 20

    # 1) Match "3 pm", "3:00 pm", "15:00", "a las 3 pm"
    m = re.search(
        r"(?:a\s+las\s+|las\s+)?(\d{1,2})(?:[:\.](\d{2}))?"
        r"\s*(?:de\s+la\s+)?(mañana|manana|tarde|pm|am|p\.m\.|a\.m\.)",
        text
    )
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        period = m.group(3).lower()

        if period in ["pm", "tarde", "p.m."]:
            if hour != 12:
                hour += 12
        elif period in ["am", "mañana", "manana", "a.m."]:
            if hour == 12:
                hour = 0

        if valid_hour(hour):
            return f"{hour:02d}:{minute:02d}"

    # 2) Match 24-hour format: "15:00", "9:30"
    m = re.search(r"(?:a\s+las\s+|las\s+)?(\d{1,2})[:\.](\d{2})", text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2))
        if valid_hour(hour):
            return f"{hour:02d}:{minute:02d}"

    # 3) Match standalone hour: "a las 3"
    m = re.search(r"(?:a\s+las|las)\s+(\d{1,2})", text)
    if m:
        hour = int(m.group(1))
        if valid_hour(hour):
            return f"{hour:02d}:00"

    # 4) Word-based: "tres y veinte"
    base_hour = None
    words = text.split()
    for w in words:
        n = words_to_number(w)
        if n is not None and 0 <= n <= 24:
            base_hour = n
            break
    if base_hour is not None:
        minute = 0
        if "y media" in text:
            minute = 30
        elif "y cuarto" in text or "y quince" in text:
            minute = 15
        elif "y veinte" in text:
            minute = 20
        elif "menos cuarto" in text:
            base_hour -= 1
            minute = 45
        elif "menos diez" in text:
            base_hour -= 1
            minute = 50
        if valid_hour(base_hour):
            return f"{base_hour:02d}:{minute:02d}"

    return session.get("time")  # fallback

# =============================================================================
# SESSION UPDATE
# =============================================================================

def update_session_with_message(msg, session):
    """Extract all possible data from message and update session"""

    # ---------- NORMALIZATION ----------
    raw_msg = msg.lower()

    normalized_msg = (
        raw_msg
        .replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
    )

    FILLER_PHRASES = [
        "para el ",
        "para la ",
        "el dia ",
        "el día ",
        "dia ",
        "día ",
    ]

    COMMON_FIXES = {
        "febero": "febrero",        # Fix your normalization damage
        "febreo": "febrero",
        "febro": "febrero",
    
        "enero": "enero",
        "marzo": "marzo",
        "abril": "abril",
        "mayo": "mayo",
        "junio": "junio",
        "julio": "julio",
        "agosto": "agosto",
        "septiembre": "septiembre",
        "setiembre": "septiembre",  # common misspelling
        "octubre": "octubre",
        "noviembre": "noviembre",
        "diciembre": "diciembre",
        "diembre": "diciembre",     # your existing fix
    }

    for wrong, correct in COMMON_FIXES.items():
        normalized_msg = normalized_msg.replace(wrong, correct)

    for filler in FILLER_PHRASES:
        normalized_msg = normalized_msg.replace(filler, "")

    # If all fields are set, don't re-extract unless user says "cambiar"
    required_fields = ["student_name", "school", "package", "date", "time", "age", "cedula"]
    if all(session.get(f) for f in required_fields):
        if not any(kw in msg.lower() for kw in ["cambiar", "otro nombre", "se llama", "nombre es"]):
            return None  # Don't overwrite

    # ---------- EXTRACTION (USE RAW MSG FOR NAME, NORMALIZED FOR OTHERS) ----------
    pkg = extract_package(normalized_msg)
    name = extract_student_name(msg, session.get("student_name"))  # ← Use raw msg
    school = extract_school(normalized_msg)
    age = extract_age(normalized_msg)
    cedula = extract_cedula(normalized_msg)
    date = extract_date(msg, session)   # ← Raw msg for dateparser
    time = extract_time(normalized_msg, session)

    # ---------- UPDATE SESSION ----------
    updated = []

    if pkg:
        session["package"] = pkg
        updated.append("package")

    if name:
        session["student_name"] = name
        updated.append("name")

    if school:
        session["school"] = school
        updated.append("school")

    if age:
        session["age"] = age
        updated.append("age")

    if cedula:
        session["cedula"] = cedula
        updated.append("cedula")

    if date == "PAST_DATE":
        session.pop("date", None)
        return "PAST_DATE"
    elif date:
        session["date"] = date
        updated.append("date")

    if time == "INVALID_TIME":
        # Clear the invalid time so session isn't considered "complete"
        session.pop("time", None)
        return "INVALID_TIME"
    elif time:
        session["time"] = time
        updated.append("time")

    save_session(session)
    return updated

# =============================================================================
# MISSING FIELDS & PROMPTS
# =============================================================================

def get_missing_fields(session):
    """Get list of missing required fields"""
    required = ["student_name", "school", "package", "date", "time", "age", "cedula"]
    return [f for f in required if not session.get(f)]

def get_field_prompt(field):
    """Get prompt for specific missing field"""
    prompts = {
        "student_name": "Por favor, ¿me compartes el nombre completo del estudiante?",
        
        "school": "¿De qué colegio viene el estudiante, por favor?",
        
        "age": "Perfecto, ¿Qué edad tiene el estudiante?",
        
        "cedula": "Por favor, me compartes el número de documento del estudiante (TI o Cédula)? Gracias",
        
        "package": (
            "Con gusto te comparto nuestros tres paquetes disponibles para que elijas el que mejor se ajuste a tu necesidad:\n\n"
            "1. Cuidado Esencial – $45.000\n"
            "   Medicina General, Optometría y Audiometría\n\n"
            "2. Salud Activa – $60.000\n"
            "   Esencial + Psicología\n\n"
            "3. Bienestar Total – $75.000\n"
            "   Salud Activa + Odontología\n\n"
            "Por favor, indícame cuál paquete deseas elegir. Muchas gracias 😊"
        ),
        
        "date": (
            "¿Para qué fecha te gustaría agendar la cita, por favor?\n"
            "Puedes decirme “mañana”, “este viernes” o una fecha exacta como “15 de enero” 😊"
        ),
        
        "time": (
            "¿A qué hora prefieres la cita?\n"
            "Nuestro horario de atención es de 7:00 a.m. a 5:00 p.m.\n"
            "(Ejemplo: 10:00 a.m. o 3:00 p.m.)"
        ),
    }
    return prompts.get(field, "")

# =============================================================================
# SUMMARY & CONFIRMATION
# =============================================================================

def build_summary(session):
    """Build booking summary for confirmation"""
    
    # Get package details
    pkg_key = None
    for key, data in PACKAGES.items():
        if data["name"] == session["package"]:
            pkg_key = key
            break
    
    if not pkg_key:
        pkg_key = "esencial"
    
    pkg_data = PACKAGES[pkg_key]
    
    # Clean school name for display
    school_raw = session.get("school", "")
    school_clean = clean_school_display_name(school_raw)
    
    # Build the summary string
    summary = (
        "Perfecto, muchas gracias por enviarme toda la información.\n"
        "Este es el resumen de los datos que recibí:\n\n"
        f"👤 Estudiante: {session['student_name']}\n"
        f"🏫 Colegio: {school_clean}\n"
        f"📦 Paquete: {pkg_data['name']} (${pkg_data['price']})\n"
        f"📅 Fecha: {session['date']}\n"
        f"⏰ Hora: {session['time']}\n"
        f"🎂 Edad: {session['age']} años\n"
        f"🪪 Documento: {session['cedula']}\n\n"
        "Si todo está correcto, por favor escribe *CONFIRMO* para continuar con la reserva."
    )

    
    session["awaiting_confirmation"] = True
    save_session(session)
    
    return summary

# =============================================================================
# DATABASE OPERATIONS
# =============================================================================

def get_available_times_for_date(target_date: str, limit: int = 5) -> list[str]:
    """
    Given a date (YYYY-MM-DD), return up to `limit` available time slots
    between 7:00 and 17:00 in 30-minute intervals.
    Assumes Supabase 'datetime' column is TEXT in format 'YYYY-MM-DD HH:MM'
    """
    if not supabase:
        # Fallback: assume all times available
        all_slots = []
        for hour in range(7, 18):
            all_slots.append(f"{hour:02d}:00")
            if hour < 17:
                all_slots.append(f"{hour:02d}:30")
        return all_slots[:limit]

    try:
        # Query reservations where date part matches target_date
        # Since 'datetime' is TEXT, use LIKE or substring
        result = supabase.table(RESERVATION_TABLE).select("datetime").eq("business_id", BUSINESS_ID).like("datetime", f"{target_date}%").execute()

        booked_times = set()
        for row in (result.data or []):
            dt_text = row["datetime"]
            # Expected format: "2026-02-20 15:00"
            if " " in dt_text:
                time_part = dt_text.split(" ", 1)[1]
                # Normalize to HH:MM
                if len(time_part) == 4:  # "9:00" → "09:00"
                    time_part = "0" + time_part
                if ":" in time_part and len(time_part) >= 5:
                    hh_mm = time_part[:5]
                    booked_times.add(hh_mm)

        # Generate all possible slots (30-min intervals from 7am to 5pm)
        all_slots = []
        for hour in range(7, 18):  # 7 to 17 inclusive
            all_slots.append(f"{hour:02d}:00")
            if hour < 17:
                all_slots.append(f"{hour:02d}:30")

        # Filter out booked ones
        available = [slot for slot in all_slots if slot not in booked_times]
        return available[:limit]

    except Exception as e:
        print(f"Error checking availability: {e}")
        # Safe fallback: hourly slots
        return [f"{h:02d}:00" for h in range(7, 18)]

    # Before inserting, check availability
    if not check_availability(session["date"], session["time"]):
        alternatives = get_available_times_for_date(session["date"], limit=3)
        if alternatives:
            alt_text = "\n".join([f"- {t}" for t in alternatives[:3]])
            return (
                f"❌ Lo siento, ya no hay cupo el {session['date']} a las {session['time']}. "
                f"Pero tenemos disponibilidad ese mismo día a estas horas:\n{alt_text}\n\n"
                "¿Te gustaría elegir una de estas opciones?"
            )
        else:
            return (
                f"❌ Lo siento, ya no hay cupo el {session['date']} a las {session['time']}. "
                "¿Te gustaría que te sugiera fechas en otro día?"
            )

def insert_reservation(phone, session):
    try:
        # Validate required fields
        required = ["student_name", "school", "package", "date", "time", "age", "cedula"]
        missing = [f for f in required if not session.get(f)]
        if missing:
            return False, f"Missing fields: {missing}"

        # Build datetime
        dt_str = f"{session['date']} {session['time']}"
        try:
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        except ValueError as ve:
            return False, f"Invalid datetime format: '{dt_str}' → {ve}"

        dt_local = dt.replace(tzinfo=LOCAL_TZ)
        dt_text = dt_local.isoformat()  # e.g., "2026-02-12T15:00:00-05:00"

        # ALWAYS save to SUPABASE (even in test mode)
        result = supabase.table(RESERVATION_TABLE).insert({
            "customer_name": session["student_name"],
            "contact_phone": phone,
            "datetime": dt_text,
            "status": "confirmado",
            "business_id": BUSINESS_ID,
            "package": session["package"],
            "school_name": session["school"],
            "age": session["age"],
            "cedula": session["cedula"]
        }).execute()

        return True, "CONFIRMED"

    except Exception as e:
        print("\n\n❌ INSERT ERROR:")
        print(repr(e))
        import traceback
        traceback.print_exc()
        print("\n\n")
        return False, str(e)

    except Exception as e:
        print("\n\n❌ INSERT ERROR (TEST MODE):")
        print(repr(e))
        import traceback
        traceback.print_exc()
        print("\n\n")
        return False, str(e)

# List of all ways users ask if they need to schedule an appointment
APPOINTMENT_NEED_PATTERNS = [
    # Direct questions
    "tengo que agendar", "debo agendar", "necesito agendar", "es necesario agendar",
    "tengo que reservar", "debo reservar", "necesito reservar",
    "hay que reservar", "hay que sacar cita", "hay que sacar turno",
    "toca agendar", "toca reservar", "toca pedir cita", "toca pedir turno",
    "es obligatorio agendar", "es obligatorio reservar",
    "toca sacar cita"

    # Short / colloquial (but not too generic)
    "toca cita", "cita obligatoria", "es con cita",
    "atienden sin cita", "atienden sin agendar",
    "con cita previa", "agenda previa",
    "orden de llegada",

    # Indirect forms
    "puedo ir sin avisar", "puedo llegar directo",
    "puedo ir asi no mas", "puedo ir así no más",
    "puedo ir sin cita", "puedo ir en cualquier momento",

    # Negations
    "no toca agendar", "no hay que agendar",
    "no toca pedir cita", "no toca reservar",

    # Doubt / soft questions
    "sera que toca agendar", "será que toca agendar",
    "me toca pedir cita", "se debe pedir cita", "creo que se necesita cita",

    # Misspellings
    "ajendar", "ajendar cita", "nesecito cita",
    "tengo q agendar", "tengo k agendar",
    "sin cita previa",

    # Contextual
    "atienden con cita",
    "funciona con cita",
    "manejan cita previa"
]

# =============================================================================
# FAQ HANDLER
# =============================================================================

def check_faq(msg):
    """Check if message is asking an FAQ question"""
    text = msg.lower()

    # Appointment requirement FAQ
    for pattern in APPOINTMENT_NEED_PATTERNS:
        if pattern in text:
            return "Para garantizar una atención rápida y organizada, es necesario agendar la cita previamente. Si quieres, puedo ayudarte a reservarla ahora mismo 😊"
    
    # Other FAQs
    if any(k in text for k in ["ubicad", "direcc", "donde", "dónde"]):
        return FAQ["ubicacion"]
    
    if any(k in text for k in ["pago", "nequi", "efectivo", "como pag"]):
        return FAQ["pago"]
    
    if any(k in text for k in ["dur", "demora", "cuanto tiempo"]):
        return FAQ["duracion"]
    
    if any(k in text for k in ["llevar", "traer", "documento", "necesito"]):
        return FAQ["llevar"]
    
    if any(k in text for k in ["horario", "atienden", "abren", "cierran"]):
        return FAQ["horario"]

    return None

# =============================================================================
# MAIN MESSAGE HANDLER
# =============================================================================

def process_message(msg, session):
    """Main conversation logic (STRICT FLOW VERSION)"""
    
    print("PROCESS MESSAGE:", msg)
    print("SESSION STATE:", session)

    first = not session.get("first_user_message_processed", False)
    
    text = msg.strip()
    lower = text.lower()
    normalized = (
        lower.replace("á", "a")
             .replace("é", "e")
             .replace("í", "i")
             .replace("ó", "o")
             .replace("ú", "u")
    )

    # --------------------------------------------------
    # EARLY AVAILABILITY CHECK (WORKS AT ANY TIME)
    # --------------------------------------------------
    CUPON_KEYWORDS = ["cupo", "disponible", "disponibilidad"]
    has_cupo = any(kw in normalized for kw in CUPON_KEYWORDS)
    is_question = "?" in text or any(q in normalized for q in ["tienen", "hay", "puedo"])

    if has_cupo and is_question:
        # Reset booking flow to treat this as a new inquiry
        session["booking_started"] = False
        session["fields_asked"] = []
        session["booking_intro_shown"] = False
        save_session(session)
        
        extracted_date = extract_date(msg, session)
        extracted_time = extract_time(msg, session)
        
        if extracted_date and extracted_time:
            if check_availability(extracted_date, extracted_time):
                return (
                    f"✅ Sí, hay cupo el {extracted_date} a las {extracted_time}. "
                    "¿Te gustaría agendar la cita?"
                )
            else:
                alternatives = get_available_times_for_date(extracted_date, limit=3)
                if alternatives:
                    alt_text = "\n".join([f"- {t}" for t in alternatives[:3]])
                    return (
                        f"❌ Lo siento, ya no hay cupo el {extracted_date} a las {extracted_time}. "
                        f"Pero tenemos disponibilidad ese mismo día a estas horas:\n{alt_text}\n\n"
                        "¿Te sirve alguna de estas opciones?"
                    )
                else:
                    return (
                        f"❌ Lo siento, ya no hay cupo el {extracted_date} a las {extracted_time}. "
                        "¿Te gustaría que te sugiera fechas en otro día?"
                    )
        else:
            return (
                "Por favor indícame la fecha y hora que deseas verificar "
                "(ej: 'mañana a las 3 pm' o '15 de febrero a las 4 pm')."
            )

    # --------------------------------------------------
    # Detect intent
    # --------------------------------------------------
    ACTION_VERBS = [
        "agendar", "reservar", "reserva", "cita",
        "apartar", "adquirir", "tomar", "hacer",
        "realizar", "sacar",
        "quiero", "gustaria", "gustaría"
    ]
    has_action = any(w in normalized for w in ACTION_VERBS)

    GREETING_PATTERNS = [
        "hola",
        "buenas",
        "buenos dias",
        "buenos días",
        "buen dia",
        "buen día",
        "buenas tardes",
        "buenas noches",
        "hi",
        "hello"
    ]
    cleaned = normalized.replace(",", "").replace(".", "").replace("!", "").strip()
    is_greeting = any(cleaned == g or cleaned.startswith(g) for g in GREETING_PATTERNS)

    INFO_TRIGGERS = [
        "que contiene", "que incluye", "informacion",
        "información", "paquetes", "examenes", "exámenes"
    ]
    is_info = any(p in normalized for p in INFO_TRIGGERS)

    # Positive/Negative confirmation responses
    POSITIVE_RESPONSES = [
        "si", "claro", "por supuesto", "obvio", "vale", "ok", "deseo", "quiero",
        "sí quiero", "si quiero", "me gustaria", "quisiera", "bueno", "dale"
    ]

    NEGATIVE_RESPONSES = [
        "no", "no gracias", "gracias no", "no necesito", "no por ahora", "tal vez después",
        "no quiero", "no me interesa", "otra vez", "después"
    ]

    SCHOOL_CONTEXT = [
        "examen", "examenes", "medico", "medicos",
        "colegio", "escolar", "ingreso"
    ]
    has_context = any(w in normalized for w in SCHOOL_CONTEXT)

    # Treat package mentions OR price patterns as booking intent
    PACKAGE_KEYWORDS = ["paquete", "45 mil", "60 mil", "75 mil", "esencial", "activa", "bienestar", "45k", "60k", "75k"]
    
    # Detect price-like numbers (e.g., 45000, 45.000, 45,000, 60000, etc.)
    has_price_pattern = bool(re.search(r"\b(?:45|60|75)[\.,]?\d{3}\b", lower))
    
    has_package_intent = any(kw in lower for kw in PACKAGE_KEYWORDS) or has_price_pattern

    # --------------------------------------------------
    # 0. GREETING + "NECESITO AGENDAR?" HANDLING
    # --------------------------------------------------
    
    faq_pre_answer = check_faq(text)
    
    # Caso: saludo + pregunta de si toca agendar
    if is_greeting and faq_pre_answer:
        greeting = get_greeting_by_time()
        return (
            f"{greeting}, 😊 Gracias por comunicarte con Oriental IPS.\n"
            "Para atenderte de la mejor manera y evitar esperas, sí manejamos cita previa.\n"
            "Si quieres, con gusto te ayudo a reservarla ahora mismo 🙌✨"
        )

    
    # Caso: solo pregunta de si toca agendar
    if faq_pre_answer:
        return faq_pre_answer
     
    # --------------------------------------------------
    # 1. BOOKING INTENT HANDLING (WITH OR WITHOUT GREETING)
    # --------------------------------------------------
    
    # CASE C: greeting + possible intent
    if is_greeting and not session.get("booking_started"):
        # Check if there's also booking intent
        if has_action or has_context or has_package_intent:
            # Extract data first
            update_session_with_message(msg, session)
            session["booking_started"] = True
            session["booking_intro_shown"] = False
            save_session(session)
            # Let Section 5 handle the adaptive intro (with greeting prefix)
            # So don't return here — continue to Section 5
        else:
            # Pure greeting
            session["greeted"] = True
            save_session(session)
            greeting = get_greeting_by_time()
            return f"{greeting}, 😊 Gracias por escribir a Oriental IPS. ¿En qué te puedo ayudar hoy?"

    # --------------------------------------------------
    # HANDLE YES/NO AFTER GREETING OR FAQ
    # --------------------------------------------------
    # Only consider yes/no if we're NOT already in a booking flow
    if not session.get("booking_started") and not session.get("awaiting_confirmation"):
        # Normalize and split into words
        clean_words = set(normalized.replace(",", "").replace(".", "").replace("!", "").split())

        # Check for "no" (whole word only)
        if any(neg in clean_words for neg in NEGATIVE_RESPONSES):
            return (
                "¡Entendido! 😊 Si en otro momento necesitas agendar una cita, "
                "no dudes en escribirnos. ¡Que tengas un excelente día! 🙌"
            )

        # Check for "yes" (whole word only)
        if any(pos in clean_words for pos in POSITIVE_RESPONSES):
            session["booking_started"] = True
            session["booking_intro_shown"] = False
            save_session(session)
            return (
                "¡Excelente! 😊 Para ayudarte a agendar la cita, por favor compárteme la siguiente información:\n\n"
                "- Nombre completo del estudiante\n"
                "- Colegio\n"
                "- Paquete\n"
                "- Fecha y hora\n"
                "- Edad del estudiante\n"
                "- Documento de identidad (Tarjeta de Identidad o Cédula)\n\n"
                "Puedes enviarme los datos poco a poco o todos en un solo mensaje."
            )

    # --------------------------------------------------
    # 2. INFO QUESTIONS (ALLOWED ANYTIME, BUT NO ACTION)
    # --------------------------------------------------
    if is_info and not has_action:
        pkg = extract_package(text)
        if pkg:
            for data in PACKAGES.values():
                if data["name"] == pkg:
                    return (
                        f"El *{data['name']}* incluye:\n{data['description']}\n\n"
                        f"Precio: ${data['price']} COP"
                    )

        return (
            "Claro 😊 Estos son nuestros *paquetes de exámenes médicos escolares*:\n\n"
            "🔹 *Cuidado Esencial* – $45.000 COP\n"
            "Medicina General, Optometría y Audiometría.\n\n"
            "🔹 *Salud Activa* – $60.000 COP\n"
            "Incluye el Esencial + Psicología.\n\n"
            "🔹 *Bienestar Total* – $75.000 COP\n"
            "Incluye el Activa + Odontología."
        )

    # --------------------------------------------------
    # 3. FAQ (ALLOWED AT ANY TIME)
    # --------------------------------------------------
    faq_answer = check_faq(text)
    if faq_answer:
        return faq_answer

    # --------------------------------------------------
    # 4. HANDLE AVAILABILITY QUESTIONS ("¿TIENEN CUPO...?")
    # --------------------------------------------------
    if not session.get("booking_started"):
        # Check for key words + question mark
        has_cupo = any(word in normalized for word in ["cupo", "disponible", "disponibilidad"])
        is_question = "?" in text or any(q in normalized for q in ["tienen", "hay", "puedo", "podria", "quisiera"])
        
        if has_cupo and is_question:
            extracted_date = extract_date(msg, session)
            extracted_time = extract_time(msg, session)
            
            if extracted_date and extracted_time:
                if check_availability(extracted_date, extracted_time):
                    return (
                        f"✅ Sí, hay cupo el {extracted_date} a las {extracted_time}. "
                        "¿Te gustaría agendar la cita?"
                    )
                else:
                    alternatives = get_available_times_for_date(extracted_date, limit=3)
                    if alternatives:
                        alt_text = "\n".join([f"- {t}" for t in alternatives[:3]])
                        return (
                            f"❌ Lo siento, ya no hay cupo el {extracted_date} a las {extracted_time}. "
                            f"Pero tenemos disponibilidad ese mismo día a estas horas:\n{alt_text}\n\n"
                            "¿Te sirve alguna de estas opciones?"
                        )
                    else:
                        return (
                            f"❌ Lo siento, ya no hay cupo el {extracted_date} a las {extracted_time}. "
                            "¿Te gustaría que te sugiera fechas en otro día?"
                        )
            else:
                return (
                    "Por favor indícame la fecha y hora que deseas verificar "
                    "(ej: 'mañana a las 3 pm' o '15 de febrero a las 4 pm')."
                )
    
    # --------------------------------------------------
    # 4. START BOOKING (ONCE)
    # --------------------------------------------------
    if not session.get("booking_started") and (has_action or has_context or has_package_intent):
        update_session_with_message(msg, session)
        session["booking_started"] = True
        session["booking_intro_shown"] = False
        save_session(session)
        
    # --------------------------------------------------
    # 5. SHOW DYNAMIC INTRO OR SUMMARY (IF ALL FIELDS PRESENT)
    # --------------------------------------------------
    if session.get("booking_started") and not session.get("booking_intro_shown") and first:
        # Re-extract to ensure school/name are captured
        update_session_with_message(msg, session)
        
        session["booking_intro_shown"] = True
        session["first_user_message_processed"] = True  # ← Critical!
        save_session(session)

        collected = []
        if session.get("student_name"): collected.append("el nombre")
        if session.get("school"): collected.append("el colegio")
        if session.get("package"): collected.append("el paquete")
        if session.get("date"): collected.append("la fecha")
        if session.get("time"): collected.append("la hora")
        if session.get("age"): collected.append("la edad")
        if session.get("cedula"): collected.append("el documento")

        missing = get_missing_fields(session)

        if len(collected) == 0:
            greeting_prefix = ""
            if is_greeting:
                greeting_word = get_greeting_by_time()
                greeting_prefix = f"{greeting_word}! "
            return (
                f"{greeting_prefix}¡Perfecto, muchas gracias! 😊 Para ayudarte a agendar la cita, por favor compárteme la siguiente información:\n\n"
                "- Nombre completo del estudiante\n"
                "- Colegio\n"
                "- Paquete\n"
                "- Fecha y hora\n"
                "- Edad del estudiante\n"
                "- Documento de identidad (Tarjeta de Identidad o Cédula)\n\n"
                "Puedes enviarme los datos poco a poco o todos en un solo mensaje."
            )
        else:
            collected_str = ", ".join(collected)
            missing_items = []
            if "student_name" in missing: missing_items.append("- Nombre completo del estudiante")
            if "school" in missing: missing_items.append("- Colegio")
            if "package" in missing: missing_items.append("- Paquete")
            if "date" in missing or "time" in missing: missing_items.append("- Fecha y hora")
            if "age" in missing: missing_items.append("- Edad del estudiante")
            if "cedula" in missing: missing_items.append("- Documento de identidad (Tarjeta de Identidad o Cédula)")
            missing_str = "\n".join(missing_items)

            greeting_prefix = ""
            if is_greeting:
                greeting_word = get_greeting_by_time()
                greeting_prefix = f"{greeting_word}! "

            return (
                f"{greeting_prefix}¡Perfecto, muchas gracias! 😊 Ya tengo {collected_str}. Para ayudarte a completar la cita, por favor compárteme la siguiente información pendiente:\n\n"
                f"{missing_str}\n\n"
                "Puedes enviarme los datos poco a poco o todos en un solo mensaje."
            )

    # --------------------------------------------------
    # 6. ASK NEXT MISSING FIELD (ONLY ONCE PER FIELD)
    # --------------------------------------------------
    if session.get("booking_started"):
        missing = get_missing_fields(session)
        if missing:
            fields_asked = set(session.get("fields_asked", []))
            for field in missing:
                if field not in fields_asked:
                    session["fields_asked"] = list(fields_asked | {field})
                    save_session(session)
                    return get_field_prompt(field)
            return None  # stay silent after one attempt per field


    # --------------------------------------------------
    # 7. SUMMARY + CONFIRMATION
    # --------------------------------------------------
    if session.get("booking_started") and not session.get("awaiting_confirmation"):
        if not get_missing_fields(session):
            return build_summary(session)

    if session.get("awaiting_confirmation") and "confirmo" in normalized:
            ok, table = insert_reservation(session["phone"], session)
            if ok:
                date = session.get("date")
                time = session.get("time")
                confirmation_message = (
                    "¡Gracias por agendar con nosotros! 🤗\n"
                    "Tu cita quedó confirmada:\n\n"
                    f"📅 Fecha: {date}\n"
                    f"⏰ Hora: {time}\n"
                    "📍 Oriental IPS – Calle 31 #29-61, Yopal\n\n"
                    "Por favor trae el documento del estudiante para una atención más rápida.\n"
                    "¡Será un gusto atenderte! 🙏✨"
                )
                phone = session["phone"]
                session.clear()
                session.update(create_new_session(phone))
                save_session(session)
                return confirmation_message
            return "❌ No pudimos completar la reserva. Intenta nuevamente."

    # --------------------------------------------------
    # 8. SILENT FALLBACK (do not reply)
    # --------------------------------------------------
    return None  # Bot stays silent

# =============================================================================
# TWILIO WEBHOOK
# =============================================================================

@app.post("/whatsapp")
async def whatsapp_webhook(request: Request, WaId: str = Form(...), Body: str = Form(...)):
    phone = WaId.split(":")[-1].strip()
    user_msg = Body.strip()
    
    # --- LOAD SESSION ---
    session_data = user_sessions.get(phone, {})
    session_data["phone"] = phone  # ensure phone is in session

    # --- UPDATE SESSION WITH NEW MESSAGE (using your existing logic) ---
    result = update_session_with_message(user_msg, session_data)
    
    # Handle special return values from update_session_with_message
    if result == "PAST_DATE":
        response_text = "La fecha que indicaste ya pasó este año. ¿Te refieres a otro día?"
    elif result == "INVALID_TIME":
        response_text = "Lo siento, solo atendemos de 7am a 5pm. Por favor elige otra hora."
    else:
        # Normal flow
        response_text = process_message(user_msg, session_data)
    
    # Save session after processing
    user_sessions[phone] = session_data

    # Test mode - return plain text
    if TEST_MODE:
        return Response(content=response_text or "", media_type="text/plain")
    
    # Production mode - return Twilio XML
    if TWILIO_AVAILABLE:
        if response_text:
            twiml = MessagingResponse()
            twiml.message(response_text)
            return Response(content=str(twiml), media_type="application/xml")
        else:
            return Response(content="", media_type="text/plain")
    
    return Response(content=response_text or "", media_type="text/plain")

# =============================================================================
# WEB INTERFACE
# =============================================================================

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Home page / admin dashboard"""
    
    if templates:
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "version": app.version,
                "supabase_status": "Connected" if supabase else "Disconnected",
                "local_tz": str(LOCAL_TZ),
            },
        )
    
    # Fallback if no templates
    return HTMLResponse(content=f"<h1>Oriental IPS Bot v{app.version}</h1><p>Status: Running</p>")

# =============================================================================
# API ENDPOINTS
# =============================================================================

@app.get("/api/reservations")
async def get_reservations():
    """Get all future reservations"""
    if not supabase:
        return {"error": "Supabase not available"}

    try:
        now = datetime.now(LOCAL_TZ)
        
        result = (
            supabase.table(RESERVATION_TABLE)
            .select("*")
            .eq("business_id", BUSINESS_ID)
            .gte("datetime", now.isoformat())  # Only filter: >= today
            .order("datetime", desc=False)
            .execute()
        )
        
        return {"reservations": result.data}
        
    except Exception as e:
        return {"error": str(e)}

@app.get("/health")
def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "version": "3.4.0",
        "supabase": supabase is not None,
        "timezone": str(LOCAL_TZ),
        "active_sessions": len(MEMORY_SESSIONS)
    }

@app.get("/api/sessions")
def get_all_sessions():
    """Debug endpoint - view all active sessions"""
    return {"sessions": MEMORY_SESSIONS}

# =============================================================================
# STARTUP
# =============================================================================

print("✅ Oriental IPS Bot v3.4.0 ready!")
print(f"   - Supabase: {'Connected' if supabase else 'Not available'}")
print(f"   - Timezone: {LOCAL_TZ}")
print(f"   - Test mode: {TEST_MODE}")
