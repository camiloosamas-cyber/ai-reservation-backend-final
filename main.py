from fastapi import FastAPI, Request, WebSocket, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import json, os, asyncio

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

TABLE_LIMIT = 10  # restaurant tables


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
    table = assign_table(data["datetime"])
    if not table:
        return "❌ No tables available at that time."

    supabase.table("reservations").insert({
        "customer_name": data["customer_name"],
        "customer_email": data.get("customer_email", ""),
        "contact_phone": data.get("contact_phone", ""),
        "datetime": data["datetime"],
        "party_size": int(data["party_size"]),
        "table_number": table,
        "notes": data.get("notes", ""),
        "status": "confirmed"
    }).execute()

    dt = datetime.fromisoformat(data["datetime"])
    formatted = dt.strftime("%A %I:%M %p")

    return (
        f"✅ Reservation confirmed!\n"
        f"👤 {data['customer_name']}\n"
        f"👥 {data['party_size']} people\n"
        f"🗓 {formatted}\n"
        f"🍽 Table: {table}"
    )

# ---------------------------------------------------------
# DASHBOARD ROUTE
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
# WHATSAPP WEBHOOK
# ---------------------------------------------------------
@app.post("/whatsapp")
async def whatsapp_webhook(Body: str = Form(...)):

    print("📩 Incoming:", Body)
    resp = MessagingResponse()

    prompt = """
You extract restaurant reservation details from WhatsApp.

RETURN ONLY VALID JSON. No explanation.

Valid JSON structure:
{
 "customer_name": "",
 "customer_email": "",
 "contact_phone": "",
 "party_size": "",
 "datetime": "",
 "notes": ""
}

If missing information:
{"ask":"What detail is missing?"}
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
        resp.message("❌ Error, try again.")
        return Response(content=str(resp), media_type="application/xml")

    if "ask" in data:
        resp.message(data["ask"])
        return Response(content=str(resp), media_type="application/xml")

    confirmation_msg = save_reservation(data)
    resp.message(confirmation_msg)

    # 🔥 Auto-refresh dashboard (trigger websocket refresh)
    asyncio.create_task(notify_refresh())

    return Response(content=str(resp), media_type="application/xml")


# ---------------------------------------------------------
# WEBSOCKET AUTO REFRESH
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
    """ sends refresh ping to dashboard clients """
    for ws in clients:
        try:
            await ws.send_text("refresh")
        except:
            pass
