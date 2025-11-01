from fastapi import FastAPI, Request, WebSocket, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from supabase import create_client, Client
from datetime import datetime, timedelta
import json
import os

# ---------------- INIT ----------------
app = FastAPI()

# Static + templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ✅ Connect to Supabase using Render env vars
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE = os.getenv("SUPABASE_SERVICE_ROLE")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE)

# ✅ OpenAI init
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# ---------------- UTIL ----------------
def parse_dt(s: str):
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except:
        return None


def get_reservations_db():
    response = supabase.table("reservations").select("*").order("datetime", desc=True).execute()
    return response.data


def get_analytics():
    reservations = get_reservations_db()
    if not reservations:
        return {"weekly_count": 0, "avg_party_size": 0, "peak_time": "N/A", "cancel_rate": 0}

    now = datetime.now()
    week_ago = now - timedelta(days=7)

    weekly = 0
    party_vals = []
    times = []
    cancelled = 0

    for r in reservations:
        if r.get("party_size"):
            party_vals.append(int(r["party_size"]))

        dt = parse_dt(r.get("datetime", ""))
        if dt:
            if dt > week_ago:
                weekly += 1
            times.append(dt.strftime("%H:%M"))

        if r.get("status") == "cancelled":
            cancelled += 1

    avg = round(sum(party_vals) / len(party_vals), 1) if party_vals else 0
    peak = max(set(times), key=times.count) if times else "N/A"
    cancel_rate = round((cancelled / len(reservations)) * 100, 1)

    return {
        "weekly_count": weekly,
        "avg_party_size": avg,
        "peak_time": peak,
        "cancel_rate": cancel_rate,
    }


# ---------------- ROUTES ----------------
@app.get("/", response_class=HTMLResponse)
def home():
    return "<h3>✅ Backend running (Supabase mode)</h3><p>Go to <a href='/dashboard'>/dashboard</a></p>"


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "reservations": get_reservations_db(),
            **get_analytics(),
            "parse_dt": parse_dt,
            "timedelta": timedelta,
        },
    )


@app.post("/createReservation")
async def create_reservation(request: Request):
    data = await request.json()

    supabase.table("reservations").insert({
        "customer_name": data["customer_name"],
        "customer_email": data["customer_email"],
        "contact_phone": data["contact_phone"],
        "datetime": data["datetime"],
        "party_size": data["party_size"],
        "table_number": data.get("table_number", ""),
        "notes": data.get("notes", "")
    }).execute()

    await notify({"type": "refresh"})
    return {"message": "created"}


@app.post("/whatsapp")
async def whatsapp_webhook(Body: str = Form(...)):
    print("📩 Incoming:", Body)

    prompt = """
You MUST reply ONLY in JSON. NO extra text.

If missing data, return:
{"ask": "<question>"}

If all data is present, return:
{
  "customer_name": "",
  "customer_email": "",
  "contact_phone": "",
  "party_size": "",
  "datetime": "",
  "table_number": "",
  "notes": ""
}
"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": Body}
            ]
        )
        raw = resp.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "")
        data = json.loads(raw)

    except:
        return Response("<Response><Message>Sorry, repeat?</Message></Response>", media_type="application/xml")

    if "ask" in data:
        return Response(f"<Response><Message>{data['ask']}</Message></Response>", media_type="application/xml")

    supabase.table("reservations").insert(data).execute()
    await notify({"type": "refresh"})

    return Response(
        f"<Response><Message>✅ Reservation created for {data['customer_name']} on {data['datetime']}.</Message></Response>",
        media_type="application/xml"
    )


# ---------------- WEBSOCKETS ----------------
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


async def notify(message: dict):
    for ws in clients:
        try:
            await ws.send_text(json.dumps(message))
        except:
            pass
