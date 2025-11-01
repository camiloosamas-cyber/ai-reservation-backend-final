from fastapi import FastAPI, Request, WebSocket, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime
import json, os, asyncio
import dateparser   # NEW ✅ automatic natural language date parsing

# ✅ Supabase
from supabase import create_client, Client

# ✅ OpenAI
from openai import OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ✅ Twilio reply builder
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
# SUPABASE DB INIT
# ---------------------------------------------------------
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE")
)

TABLE_LIMIT = 10  # restaurant capacity


def to_iso(dt_str: str):
    """ Converts natural language datetime ('tomorrow at 8pm') → ISO format """
    try:
        parsed = dateparser.parse(dt_str)
        return parsed.isoformat()
    except:
        return None


def assign_table(date_str: str):
    """ Returns first free table at that datetime """

    booked = supabase.table("reservations") \
        .select("table_number") \
        .eq("datetime", date_str).execute()

    taken = {row["table_number"] for row in booked.data}

    for i in range(1, TABLE_LIMIT + 1):
        t = f"T{i}"
        if t not in taken:
            return t

    return None


def save_reservation(data):
    """ Insert record into Supabase """

    # ✅ Convert datetime into ISO format if needed
    iso_dt = data.get("datetime")

    if not iso_dt or "T" not in iso_dt:
        iso_dt = to_iso(iso_dt)

    if not iso_dt:
        return "❌ Invalid date/time. Please specify date and time."

    table = assign_table(iso_dt)
    if not table:
        return "❌ No tables available at that time."

    supabase.table("reservations").insert({
        "customer_name": data["customer_name"],
        "customer_email": data.get("customer_email", ""),
        "contact_phone": data.get("contact_phone", ""),
        "datetime": iso_dt,
        "party_size": int(data["party_size"]),
        "table_number": table,
        "notes": data.get("notes", ""),
        "status": "confirmed"
    }).execute()

    readable = datetime.fromisoformat(iso_dt).strftime("%A %I:%M %p")

    return (
        f"✅ Reservation confirmed!\n"
        f"👤 {data['customer_name']}\n"
        f"👥 {data['party_size']} people\n"
        f"🗓 {readable}\n"
        f"🍽 Table: {table}"
    )


# ---------------------------------------------------------
# DASHBOARD PAGE
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home():
    return "<h3>✅ Backend running</h3><p>Go to /dashboard</p>"


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    res = supabase.table("reservations").select("*").order("datetime", desc=True).execute()
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "reservations": res.data},
    )


# ---------------------------------------------------------
# WHATSAPP WEBHOOK — AI creates JSON to extract info
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp_webhook(Body: str = Form(...)):

    print("📩 Incoming:", Body)

    resp = MessagingResponse()

    prompt = """
You extract reservation details.

⚠️ ALWAYS return valid JSON only.

REQUIRED FORMAT:
{
 "customer_name": "",
 "customer_email": "",
 "contact_phone": "",
 "party_size": "",
 "datetime": "YYYY-MM-DDTHH:MM:SS",
 "notes": ""
}

- Convert natural language time to ISO 8601 format.
- If ANY field is missing, respond ONLY with:
{"ask":"<question you need>"}
"""

    try:
        result = client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": Body},
            ]
        )

        output = result.choices[0].message.content.strip()
        if output.startswith("```"):
            output = output.replace("```json", "").replace("```", "").strip()

        data = json.loads(output)

    except Exception as e:
        print("❌ JSON Parse Error:", e)
        resp.message("❌ I couldn't understand. Try again.")
        return Response(content=str(resp), media_type="application/xml")

    if "ask" in data:
        resp.message(data["ask"])
        return Response(content=str(resp), media_type="application/xml")

    confirmation_msg = save_reservation(data)
    resp.message(confirmation_msg)

    asyncio.create_task(notify_refresh())  # live update dashboard
    return Response(content=str(resp), media_type="application/xml")


# ---------------------------------------------------------
# API CALLED FROM DASHBOARD
# ---------------------------------------------------------
@app.post("/createReservation")
async def create_reservation(payload: dict):
    confirmation = save_reservation(payload)
    asyncio.create_task(notify_refresh())
    return {"success": True, "message": confirmation}


@app.post("/updateReservation")
async def update_reservation(update: dict):

    supabase.table("reservations") \
        .update({
            "datetime": update.get("datetime"),
            "party_size": update.get("party_size"),
            "table_number": update.get("table_number"),
            "notes": update.get("notes"),
            "status": update.get("status", "updated"),
        }) \
        .eq("reservation_id", update["reservation_id"]) \
        .execute()

    asyncio.create_task(notify_refresh())
    return {"success": True}


@app.post("/cancelReservation")
async def cancel_reservation(update: dict):

    supabase.table("reservations") \
        .update({"status": "cancelled"}) \
        .eq("reservation_id", update["reservation_id"]) \
        .execute()

    asyncio.create_task(notify_refresh())
    return {"success": True}


# ---------------------------------------------------------
# WEBSOCKET LIVE REFRESH
# ---------------------------------------------------------
clients = []


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
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
