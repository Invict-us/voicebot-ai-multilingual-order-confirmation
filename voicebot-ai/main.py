import sys
import os
import re
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, HTTPException, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse, JSONResponse, Response, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from twilio.rest import Client
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse, Gather
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.memory import MemoryJobStore
import uvicorn

# ── Optional AI imports (graceful degradation if missing) ──
try:
    from openai import AsyncOpenAI
    HAS_OPENAI = True
except Exception:
    HAS_OPENAI = False

try:
    from elevenlabs import generate, set_api_key as eleven_set_key
    HAS_ELEVEN = True
except Exception:
    HAS_ELEVEN = False

# ── Load env ──
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

from database import init_db, get_db_connection, log_call_event

# ═══════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE       = os.getenv("TWILIO_PHONE_NUMBER", "")
BASE_URL           = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
SECRET_KEY         = os.getenv("SECRET_KEY", "dev-secret-key")

# ── Optional AI clients ──
openai_client: Optional[AsyncOpenAI] = None
if HAS_OPENAI and OPENAI_API_KEY:
    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

if HAS_ELEVEN and ELEVENLABS_API_KEY:
    eleven_set_key(ELEVENLABS_API_KEY)

# ── Logging ──
log_dir = Path(__file__).parent / "logs"
log_dir.mkdir(parents=True, exist_ok=True)

from logging.handlers import RotatingFileHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(
            str(log_dir / "voicebot.log"),
            maxBytes=5_000_000,
            backupCount=5,
            encoding="utf-8",
        ),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# ── Scheduler ──
scheduler = AsyncIOScheduler(jobstores={"default": MemoryJobStore()})

# ── Twilio clients ──
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN)

logger.info("[OK] Twilio SID loaded: %s...", TWILIO_ACCOUNT_SID[:6] if TWILIO_ACCOUNT_SID else "NOT SET")
logger.info("[OK] BASE_URL: %s", BASE_URL)
logger.info("[OK] OpenAI: %s | ElevenLabs: %s", HAS_OPENAI and bool(OPENAI_API_KEY), HAS_ELEVEN and bool(ELEVENLABS_API_KEY))

# ═══════════════════════════════════════════════════════
# LANGUAGE & VOICE MAPS
# ═══════════════════════════════════════════════════════
LANG_MAP = {
    "en": "en-IN",
    "hi": "hi-IN",
    "kn": "kn-IN",
    "mr": "mr-IN",
    "te": "te-IN",
}

VOICE_MAP = {
    "en": "Polly.Raveena",
    "hi": "Google.hi-IN-Wavenet-A",
    "kn": "Google.kn-IN-Standard-A",
    "mr": "Google.mr-IN-Standard-A",
    "te": "Google.te-IN-Standard-A",
}

# ElevenLabs voice IDs (optional premium voices)
ELEVEN_VOICE_MAP = {
    "en": "XB0fDUnXU5powFXDhCwa",   # Adam
    "hi": "XB0fDUnXU5powFXDhCwa",   # fallback
    "kn": "XB0fDUnXU5powFXDhCwa",
    "mr": "XB0fDUnXU5powFXDhCwa",
    "te": "XB0fDUnXU5powFXDhCwa",
}

PRESS_ANY_KEY_MSGS = {
    "en": "Press any key to continue.",
    "hi": "Jaari rakhne ke liye koi bhi key dabayen.",
    "kn": "Munduvariyalu yaavaadaaru key ottiri.",
    "mr": "Pudhe jaanyasaathi konatihi key daba.",
    "te": "Kagravataaniki edaina key nokkandi.",
}

RESCHEDULE_MENU_MSGS = {
    "en": (
        "When should we call you back? "
        "Press 1 to be called back in 2 hours. "
        "Press 2 to be called back in 3 hours. "
        "Press 3 to be called back tomorrow morning at 10 AM."
    ),
    "hi": (
        "Hum aapko kab wapas call karein? "
        "2 ghante baad callback ke liye 1 dabayen. "
        "3 ghante baad callback ke liye 2 dabayen. "
        "Kal subah 10 baje callback ke liye 3 dabayen."
    ),
    "kn": (
        "Naavu yaavaaga matte kare madabeku? "
        "2 ghante nantara callback saathi 1 ottiri. "
        "3 ghante nantara callback saathi 2 ottiri. "
        "Naaledina beLigge 10 ghantege callback saathi 3 ottiri."
    ),
    "mr": (
        "Aamhi tumhala kevha parat call karava? "
        "2 taasanantar callback saathi 1 daba. "
        "3 taasanantar callback saathi 2 daba. "
        "Udya sakali 10 vajata callback saathi 3 daba."
    ),
    "te": (
        "Maemu meeku eppudu tirigi call cheyyaali? "
        "2 gantala tarvata callback kosamu 1 nokkandi. "
        "3 gantala tarvata callback kosamu 2 nokkandi. "
        "Repu udayam 10 gantala callback kosamu 3 nokkandi."
    ),
}


def say_in_language(twiml_node, message: str, language: str) -> None:
    """Render TTS in the target language. Uses ElevenLabs if configured, else Twilio native."""
    tts_lang = LANG_MAP.get(language, "en-IN")
    voice = VOICE_MAP.get(language, VOICE_MAP["en"])
    twiml_node.say(message, voice=voice, language=tts_lang)


# ═══════════════════════════════════════════════════════
# INTENT DETECTION (Keyword + Optional OpenAI GPT)
# ═══════════════════════════════════════════════════════
YES_WORDS = {
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm",
    "confirmed", "correct", "right", "fine", "accept", "go ahead",
    "absolutely", "definitely", "of course", "affirmative",
    "han", "haan", "ji", "ji han", "theek hai", "sahi hai", "pushti",
    "houdu", "sari", "oppige",
    "ho", "hoy", "barobar", "theek",
    "avunu", "sare", "oppukuntanu",
}

NO_WORDS = {
    "no", "nope", "nah", "cancel", "cancelled", "don't", "dont",
    "not", "refuse", "reject", "negative",
    "nahi", "nahin", "na", "radd",
    "beda", "illa", "raddu",
    "nako",
    "vaddu", "ledu",
}

LATER_WORDS = {
    "later", "reschedule", "call back", "callback", "wait",
    "not now", "some time", "another time", "postpone",
    "baad mein", "baad", "rukiye",
    "nantara", "munde",
    "nantar", "pudhe",
    "tarvata", "agandi",
}


def detect_intent_keyword(text: str, language: str = "en") -> str:
    if not text:
        return "unclear"
    normalised = text.lower().strip()
    for ch in [".", ",", "!", "?", "|", "||"]:
        normalised = normalised.replace(ch, "")
    logger.info("[DEBUG] detect_intent | raw='%s' | normalised='%s' | lang=%s", text, normalised, language)
    for word in YES_WORDS:
        if word in normalised:
            logger.info("[OK] Intent -> confirm (matched '%s')", word)
            return "confirm"
    for word in NO_WORDS:
        if word in normalised:
            logger.info("[OK] Intent -> cancel (matched '%s')", word)
            return "cancel"
    for word in LATER_WORDS:
        if word in normalised:
            logger.info("[OK] Intent -> reschedule (matched '%s')", word)
            return "reschedule"
    logger.info("[WARN] Intent -> unclear (no match for '%s')", normalised)
    return "unclear"


async def detect_intent_ai(text: str, language: str = "en") -> str:
    """Use OpenAI GPT-4o-mini for robust multilingual intent classification."""
    if not openai_client or not text:
        return detect_intent_keyword(text, language)

    lang_names = {"en": "English", "hi": "Hindi", "kn": "Kannada", "mr": "Marathi", "te": "Telugu"}
    lang_name = lang_names.get(language, "English")

    prompt = f"""You are an intent classifier for an Indian voice-order bot.
The customer spoke in {lang_name}. Classify their intent into exactly one of:
- confirm   (customer wants to confirm/accept the order)
- cancel    (customer wants to cancel/reject the order)
- reschedule (customer wants a callback later)
- unclear   (ambiguous or no clear intent)

Customer utterance: "{text}"
Respond with ONLY the single word label."""

    try:
        resp = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=10,
        )
        label = resp.choices[0].message.content.strip().lower()
        if label in ("confirm", "cancel", "reschedule", "unclear"):
            logger.info("[OK] AI Intent -> %s (OpenAI)", label)
            return label
    except Exception as e:
        logger.warning("[WARN] OpenAI intent failed: %s. Falling back to keywords.", e)

    return detect_intent_keyword(text, language)


# ═══════════════════════════════════════════════════════
# IST HELPERS
# ═══════════════════════════════════════════════════════
IST_OFFSET = timedelta(hours=5, minutes=30)


def _ist_now() -> datetime:
    return datetime.utcnow() + IST_OFFSET


def _ist_to_utc(ist_dt: datetime) -> datetime:
    return ist_dt - IST_OFFSET


def fmt_ist(utc_dt: datetime) -> str:
    ist = utc_dt + IST_OFFSET
    return ist.strftime("%d %b %Y, %I:%M %p IST")


# ═══════════════════════════════════════════════════════
# TWILIO WEBHOOK SECURITY
# ═══════════════════════════════════════════════════════
async def validate_twilio_request(request: Request) -> bool:
    """Validate that the incoming webhook actually came from Twilio."""
    if not TWILIO_AUTH_TOKEN:
        return True  # dev mode
    url = str(request.url)
    post_data = await request.form()
    signature = request.headers.get("X-Twilio-Signature", "")
    return twilio_validator.validate(url, post_data, signature)


# ═══════════════════════════════════════════════════════
# SCHEDULED CALLBACK
# ═══════════════════════════════════════════════════════
async def fire_scheduled_callback(order_id: str):
    logger.info("[TIME] Firing scheduled callback for order %s", order_id)
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM orders WHERE order_id=?", (order_id,))
        row = cursor.fetchone()
        conn.close()
        conn = None

        if not row:
            logger.error("[ERR] Scheduled callback: order %s not found", order_id)
            return

        phone = row["customer_phone"]
        language = row["language"]
        service_type = row["service_type"]

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE orders SET status='pending', speech_result=NULL, intent=NULL, "
            "notes='Callback attempt', updated_at=? WHERE order_id=?",
            (datetime.utcnow().isoformat(), order_id),
        )
        conn.commit()
        conn.close()
        conn = None

        # Run blocking Twilio call in thread pool
        loop = asyncio.get_event_loop()
        call = await loop.run_in_executor(
            None,
            lambda: twilio_client.calls.create(
                to=phone,
                from_=TWILIO_PHONE,
                url=(
                    f"{BASE_URL}/webhook/trial_bypass"
                    f"?order_id={order_id}&language={language}&service_type={service_type}"
                ),
                status_callback=f"{BASE_URL}/webhook/status?order_id={order_id}",
                status_callback_event=["completed", "failed", "no-answer", "busy"],
            ),
        )

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE orders SET call_sid=? WHERE order_id=?", (call.sid, order_id))
        conn.commit()
        conn.close()
        conn = None

        log_call_event(order_id, "scheduled_callback_placed", f"SID {call.sid}", call.sid)
        logger.info("[OK] Scheduled callback placed – SID %s", call.sid)

    except Exception as e:
        logger.error("[ERR] Scheduled callback error for %s: %s", order_id, e)
        try:
            if conn:
                conn.close()
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE orders SET status='failed', notes=? WHERE order_id=?",
                (f"Callback error: {e}", order_id),
            )
            conn.commit()
            conn.close()
        except Exception as db_err:
            logger.error("[ERR] Failed to update DB after callback error: %s", db_err)


# ═══════════════════════════════════════════════════════
# DB HELPERS
# ═══════════════════════════════════════════════════════
def _ensure_db_columns():
    conn = get_db_connection()
    cursor = conn.cursor()
    for col, typedef in [
        ("intent", "TEXT"),
        ("speech_result", "TEXT"),
        ("updated_at", "TEXT"),
        ("notes", "TEXT"),
        ("call_sid", "TEXT"),
        ("scheduled_at", "TEXT"),
        ("scheduled_label", "TEXT"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE orders ADD COLUMN {col} {typedef}")
        except Exception:
            pass
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════
# FASTAPI LIFESPAN & APP
# ═══════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _ensure_db_columns()
    scheduler.start()
    logger.info("[OK] VoiceBot AI v2 started – database initialised, scheduler running")
    yield
    scheduler.shutdown(wait=False)
    logger.info("[STOP] Scheduler stopped")


app = FastAPI(title="VoiceBot AI", version="2.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── serve frontend ──
frontend_dir = Path(__file__).parent / "frontend"
frontend_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")


# ═══════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════
@app.get("/", response_class=HTMLResponse)
async def root():
    index_path = frontend_dir / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>index.html not found in frontend/</h1>", status_code=404)
    return index_path.read_text(encoding="utf-8")


@app.get("/health")
async def health():
    """Health check for uptime monitoring & load balancers."""
    return JSONResponse({
        "status": "ok",
        "version": "2.1.0",
        "twilio_configured": bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_PHONE),
        "openai_configured": bool(openai_client),
        "elevenlabs_configured": bool(HAS_ELEVEN and ELEVENLABS_API_KEY),
        "scheduler_running": scheduler.running,
    })


@app.post("/call")
async def trigger_call(
    background_tasks: BackgroundTasks,
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    order_id: str = Form(...),
    order_details: str = Form(...),
    service_type: str = Form(...),
    language: str = Form(default="en"),
):
    conn = None
    try:
        if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_PHONE:
            logger.error("[ERR] Twilio credentials missing in environment")
            raise HTTPException(
                status_code=500,
                detail="Twilio credentials not configured. Check .env file.",
            )

        order_id = order_id.strip().replace(" ", "-")

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT order_id FROM orders WHERE order_id=?", (order_id,))
        if cursor.fetchone():
            order_id = f"{order_id}-{int(datetime.utcnow().timestamp())}"
            logger.warning("[WARN] Duplicate order_id – renamed to %s", order_id)

        cursor.execute(
            """
            INSERT INTO orders
                (order_id, customer_name, customer_phone,
                 order_details, service_type, language, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                order_id, customer_name.strip(), customer_phone.strip(),
                order_details.strip(), service_type, language,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
        conn.close()
        conn = None

        logger.info("[DB] Order %s saved for %s", order_id, customer_name)
        log_call_event(order_id, "call_triggered", f"Lang={language}, Service={service_type}")

        background_tasks.add_task(
            make_outbound_call,
            customer_phone.strip(), order_id, order_details.strip(),
            service_type, language, customer_name.strip(),
        )

        logger.info("[CALL] Call queued for %s (%s) – Order %s", customer_name, customer_phone, order_id)
        return JSONResponse({
            "success": True,
            "message": f"Call initiated for {customer_name}",
            "order_id": order_id,
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error("[ERR] /call error: %s", e, exc_info=True)
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")


def make_outbound_call(
    phone, order_id, order_details, service_type, language, customer_name
):
    """Synchronous background task (runs in thread pool)."""
    conn = None
    try:
        url = (
            f"{BASE_URL}/webhook/trial_bypass"
            f"?order_id={order_id}&language={language}&service_type={service_type}"
        )
        logger.info("[CALL] Twilio URL: %s", url)

        call = twilio_client.calls.create(
            to=phone,
            from_=TWILIO_PHONE,
            url=url,
            status_callback=f"{BASE_URL}/webhook/status?order_id={order_id}",
            status_callback_event=["completed", "failed", "no-answer", "busy"],
            record=True,
            recording_status_callback=f"{BASE_URL}/webhook/recording?order_id={order_id}",
        )
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE orders SET call_sid=? WHERE order_id=?", (call.sid, order_id))
        conn.commit()
        conn.close()
        conn = None

        log_call_event(order_id, "outbound_call_placed", f"SID {call.sid}", call.sid)
        logger.info("[OK] Twilio call SID: %s", call.sid)

    except Exception as e:
        logger.error("[ERR] Twilio dial error: %s", e, exc_info=True)
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE orders SET status='failed', notes=? WHERE order_id=?",
                (str(e), order_id),
            )
            conn.commit()
            conn.close()
            conn = None
        except Exception as db_err:
            logger.error("[ERR] Failed to update DB after dial error: %s", db_err)


# ═══════════════════════════════════════════════════════
# WEBHOOKS
# ═══════════════════════════════════════════════════════
@app.get("/webhook/trial_bypass")
@app.post("/webhook/trial_bypass")
async def webhook_trial_bypass(request: Request):
    params = dict(request.query_params)
    order_id = params.get("order_id", "UNKNOWN")
    language = params.get("language", "en")
    service_type = params.get("service_type", "all")

    greet_url = (
        f"{BASE_URL}/webhook/greet"
        f"?order_id={order_id}&language={language}&service_type={service_type}"
    )

    vr = VoiceResponse()
    gather = Gather(
        num_digits=1,
        finish_on_key="",
        action=greet_url,
        method="POST",
        timeout=10,
    )
    prompt = PRESS_ANY_KEY_MSGS.get(language, PRESS_ANY_KEY_MSGS["en"])
    say_in_language(gather, prompt, language)
    vr.append(gather)
    vr.redirect(greet_url)
    return Response(content=str(vr), media_type="application/xml")


@app.get("/webhook/greet")
@app.post("/webhook/greet")
async def webhook_greet(request: Request):
    params = dict(request.query_params)
    order_id = params.get("order_id", "UNKNOWN")
    language = params.get("language", "en")
    service_type = params.get("service_type", "all")

    logger.info("[GREET] order_id=%s language=%s", order_id, language)
    log_call_event(order_id, "greet_webhook_hit", f"lang={language}")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM orders WHERE order_id=?", (order_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        logger.error("[ERR] Order not found in DB: '%s'", order_id)
        vr = VoiceResponse()
        say_in_language(vr, "Sorry, we could not find your order. Goodbye.", "en")
        vr.hangup()
        return Response(content=str(vr), media_type="application/xml")

    order_details = row["order_details"]
    customer_name = row["customer_name"]

    greetings = {
        "en": (
            f"Hello {customer_name}! "
            f"This is a call to confirm your order. "
            f"Your order details are: {order_details}. "
            f"Please say YES or press 1 to confirm. "
            f"Say NO or press 2 to cancel. "
            f"Say LATER or press 3 to reschedule."
        ),
        "hi": (
            f"Namaste {customer_name}! "
            f"Yeh aapke order ki pushti ke liye call hai. "
            f"Aapka order hai: {order_details}. "
            f"Pushti ke liye haan bolein ya 1 dabayen. "
            f"Radd karne ke liye nahi bolein ya 2 dabayen. "
            f"Baad mein call ke liye baad mein bolein ya 3 dabayen."
        ),
        "kn": (
            f"Namaskara {customer_name}! "
            f"Nimma order drudheekarisalu ee kare madalaagide. "
            f"Nimma order: {order_details}. "
            f"Drudheekarisalu houdu endi heli athava 1 ottiri. "
            f"Raddu madalu beda endi heli athava 2 ottiri. "
            f"Nantara karegagi nantara endi heli athava 3 ottiri."
        ),
        "mr": (
            f"Namaskar {customer_name}! "
            f"Tumchya orderchi pushti karnyasaathi ha call aahe. "
            f"Tumchi order: {order_details}. "
            f"Pushti karnyasaathi ho mhana kiva 1 daba. "
            f"Radd karnyasaathi nahi mhana kiva 2 daba. "
            f"Nantar callsaathi nantar mhana kiva 3 daba."
        ),
        "te": (
            f"Halo {customer_name}! "
            f"Mee order nirdhaarinchataniki ee call cheestunnamu. "
            f"Mee order: {order_details}. "
            f"Nirdhaarinchataniki avunu ani cheppandi leda 1 nokkandi. "
            f"Raddu cheyyataniki vaddu ani cheppandi leda 2 nokkandi. "
            f"Tarvata call kosamu tarvata ani cheppandi leda 3 nokkandi."
        ),
    }

    message = greetings.get(language, greetings["en"])
    tts_lang = LANG_MAP.get(language, "en-IN")

    vr = VoiceResponse()
    gather = Gather(
        input="speech dtmf",
        action=f"{BASE_URL}/webhook/response?order_id={order_id}&language={language}",
        method="POST",
        timeout=20,
        speech_timeout="3",
        language=tts_lang,
        num_digits=1,
        finish_on_key="",
    )
    say_in_language(gather, message, language)
    vr.append(gather)
    vr.redirect(
        f"{BASE_URL}/webhook/no_response"
        f"?order_id={order_id}&language={language}&attempt=1"
    )
    return Response(content=str(vr), media_type="application/xml")


@app.post("/webhook/response")
async def webhook_response(
    request: Request,
    SpeechResult: Optional[str] = Form(default=None),
    Digits: Optional[str] = Form(default=None),
    Confidence: Optional[str] = Form(default=None),
):
    params = dict(request.query_params)
    order_id = params.get("order_id", "UNKNOWN")
    language = params.get("language", "en")

    logger.info(
        "[VOICE] Response for %s | Speech: '%s' | DTMF: '%s' | Confidence: %s",
        order_id, SpeechResult, Digits, Confidence,
    )
    log_call_event(order_id, "customer_response", f"speech={SpeechResult}, dtmf={Digits}")

    raw_input = ""
    if Digits == "1":
        raw_input = "yes"
    elif Digits == "2":
        raw_input = "no"
    elif Digits == "3":
        raw_input = "later"
    elif SpeechResult and SpeechResult.strip():
        raw_input = SpeechResult.strip()

    if not raw_input:
        vr = VoiceResponse()
        vr.redirect(
            f"{BASE_URL}/webhook/no_response"
            f"?order_id={order_id}&language={language}&attempt=1"
        )
        return Response(content=str(vr), media_type="application/xml")

    # Use AI intent detection if available, else keyword
    intent = await detect_intent_ai(raw_input, language)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE orders SET speech_result=?, intent=?, updated_at=? WHERE order_id=?",
        (raw_input, intent, datetime.utcnow().isoformat(), order_id),
    )
    conn.commit()
    conn.close()

    confirm_msgs = {
        "en": "Your order has been confirmed. Thank you! Have a great day. Goodbye!",
        "hi": "Aapka order pushti ho gaya hai. Dhanyavaad! Alvida!",
        "kn": "Nimma order drudheekarisalaagide. Dhanyavadagalu! Shubhavaagali!",
        "mr": "Tumchi order pushti jhali aahe. Dhanyavaad! Niroop!",
        "te": "Mee order nirdhaarinchababindi. Dhanyavaadaalu! Shubhamaina roju!",
    }
    cancel_msgs = {
        "en": "Your order has been cancelled. We hope to serve you again soon. Goodbye!",
        "hi": "Aapka order radd kar diya gaya hai. Phir milenge. Alvida!",
        "kn": "Nimma order raddugolisakalaagide. Dhanyavadagalu! Matte sigona!",
        "mr": "Tumchi order radd keli aahe. Lavkarch bhetu. Niroop!",
        "te": "Mee order raddu cheyyababindi. Tvarlone malli kaluddam!",
    }
    unclear_msgs = {
        "en": "Sorry, I did not catch that. Please say YES to confirm, NO to cancel, or press 1, 2, or 3.",
        "hi": "Maaf karein, samajh nahi paya. Pushti ke liye haan ya 1, radd ke liye nahi ya 2 dabayen.",
        "kn": "Kshamisi, arthaagalilla. Drudheekarisalu houdu athava 1, raddu madalu beda athava 2 ottiri.",
        "mr": "Maaf kara, samajale nahi. Pushtisaathi ho kiva 1, raddsaathi nahi kiva 2 daba.",
        "te": "Kshaminchaandi, artham kaaledu. Nirdhaarinchataniki avunu leda 1, raddu cheyyataniki vaddu leda 2 nokkandi.",
    }

    status_map = {
        "confirm": "confirmed",
        "cancel": "cancelled",
        "reschedule": "rescheduled",
        "unclear": "unclear",
    }

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE orders SET status=?, updated_at=? WHERE order_id=?",
        (status_map.get(intent, "unclear"), datetime.utcnow().isoformat(), order_id),
    )
    conn.commit()
    conn.close()

    logger.info("[OK] Order %s -> intent=%s -> status=%s", order_id, intent, status_map.get(intent, "unclear"))
    log_call_event(order_id, "intent_detected", f"intent={intent}, status={status_map.get(intent, 'unclear')}")

    vr = VoiceResponse()

    if intent == "confirm":
        say_in_language(vr, confirm_msgs.get(language, confirm_msgs["en"]), language)
        vr.hangup()

    elif intent == "cancel":
        say_in_language(vr, cancel_msgs.get(language, cancel_msgs["en"]), language)
        vr.hangup()

    elif intent == "reschedule":
        gather = Gather(
            input="dtmf",
            action=f"{BASE_URL}/webhook/reschedule_time?order_id={order_id}&language={language}",
            method="POST",
            timeout=15,
            num_digits=1,
            finish_on_key="",
        )
        msg = RESCHEDULE_MENU_MSGS.get(language, RESCHEDULE_MENU_MSGS["en"])
        say_in_language(gather, msg, language)
        vr.append(gather)
        vr.redirect(
            f"{BASE_URL}/webhook/reschedule_fallback"
            f"?order_id={order_id}&language={language}"
        )

    else:
        say_in_language(vr, unclear_msgs.get(language, unclear_msgs["en"]), language)
        vr.redirect(
            f"{BASE_URL}/webhook/greet"
            f"?order_id={order_id}&language={language}&service_type=all"
        )

    return Response(content=str(vr), media_type="application/xml")


@app.post("/webhook/reschedule_time")
async def webhook_reschedule_time(
    request: Request,
    Digits: Optional[str] = Form(default=None),
):
    params = dict(request.query_params)
    order_id = params.get("order_id", "UNKNOWN")
    language = params.get("language", "en")

    callback_utc: Optional[datetime] = None

    if Digits == "1":
        callback_utc = datetime.utcnow() + timedelta(hours=2)
    elif Digits == "2":
        callback_utc = datetime.utcnow() + timedelta(hours=3)
    elif Digits == "3":
        tomorrow_ist = _ist_now().date() + timedelta(days=1)
        tomorrow_10am_ist = datetime.combine(
            tomorrow_ist, datetime.min.time()
        ).replace(hour=10, minute=0, second=0, microsecond=0)
        callback_utc = _ist_to_utc(tomorrow_10am_ist)

    confirm_schedule_msgs = {
        "en": "Got it! We will call you back {time}. Have a great day! Goodbye.",
        "hi": "Theek hai! Hum aapko {time} wapas call karenge. Alvida!",
        "kn": "Sari! Naavu nimage {time} matte kare madutteve. Dhanyavadagalu!",
        "mr": "Theek aahe! Aamhi tumhala {time} parat call karu. Dhanyavaad!",
        "te": "Sare! Maemu meeku {time} tirigi call chestaamu. Dhanyavaadaalu!",
    }
    invalid_choice_msgs = {
        "en": "Sorry, that was not a valid choice. We will call you back in 2 hours. Goodbye.",
        "hi": "Maaf karein, galat choice. Hum 2 ghante baad call karenge. Alvida.",
        "kn": "Kshamisi, tappu choice. Naavu 2 ghante nantara kare madutteve.",
        "mr": "Maaf kara, chukichaa choice. Aamhi 2 tasanantar call karu.",
        "te": "Kshaminchaandi, tappu choice. Maemu 2 gantala tarvata call chestaamu.",
    }

    vr = VoiceResponse()

    if not callback_utc:
        callback_utc = datetime.utcnow() + timedelta(hours=2)
        say_in_language(vr, invalid_choice_msgs.get(language, invalid_choice_msgs["en"]), language)
    else:
        ist_label = fmt_ist(callback_utc)
        msg_tmpl = confirm_schedule_msgs.get(language, confirm_schedule_msgs["en"])
        say_in_language(vr, msg_tmpl.format(time=ist_label), language)

    ist_label = fmt_ist(callback_utc)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE orders SET scheduled_at=?, scheduled_label=?, status='rescheduled', updated_at=? WHERE order_id=?",
        (callback_utc.isoformat(), ist_label, datetime.utcnow().isoformat(), order_id),
    )
    conn.commit()
    conn.close()

    job_id = f"callback_{order_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    scheduler.add_job(
        fire_scheduled_callback,
        trigger="date",
        run_date=callback_utc,
        args=[order_id],
        id=job_id,
        misfire_grace_time=300,
    )
    log_call_event(order_id, "reschedule_set", f"run_at={callback_utc.isoformat()}, label={ist_label}")
    logger.info("[SCHED] APScheduler job '%s' registered for %s", job_id, callback_utc)

    vr.hangup()
    return Response(content=str(vr), media_type="application/xml")


@app.get("/webhook/reschedule_fallback")
@app.post("/webhook/reschedule_fallback")
async def webhook_reschedule_fallback(request: Request):
    params = dict(request.query_params)
    order_id = params.get("order_id", "UNKNOWN")
    language = params.get("language", "en")

    fallback_msgs = {
        "en": "No problem. We will call you back in about 2 hours. Goodbye!",
        "hi": "Koi baat nahi. Hum lagbhag 2 ghante baad aapko call karenge. Alvida!",
        "kn": "Prashne illa. Naavu 2 ghante nantara kare madutteve. Dhanyavadagalu!",
        "mr": "Theek aahe. Aamhi 2 tasanantar call karu. Dhanyavaad!",
        "te": "Parvaaledu. Maemu 2 gantala tarvata call chestaamu. Dhanyavaadaalu!",
    }

    callback_utc = datetime.utcnow() + timedelta(hours=2)
    ist_label = fmt_ist(callback_utc)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE orders SET scheduled_at=?, scheduled_label=?, status='rescheduled', updated_at=? WHERE order_id=?",
        (callback_utc.isoformat(), ist_label, datetime.utcnow().isoformat(), order_id),
    )
    conn.commit()
    conn.close()

    job_id = f"callback_{order_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(
        fire_scheduled_callback,
        trigger="date",
        run_date=callback_utc,
        args=[order_id],
        id=job_id,
        misfire_grace_time=300,
    )

    log_call_event(order_id, "reschedule_fallback", f"run_at={callback_utc.isoformat()}")
    vr = VoiceResponse()
    say_in_language(vr, fallback_msgs.get(language, fallback_msgs["en"]), language)
    vr.hangup()
    return Response(content=str(vr), media_type="application/xml")


@app.get("/webhook/no_response")
@app.post("/webhook/no_response")
async def webhook_no_response(request: Request):
    params = dict(request.query_params)
    order_id = params.get("order_id", "UNKNOWN")
    language = params.get("language", "en")
    attempt = int(params.get("attempt", 1))

    retry_msgs = {
        "en": "We did not hear your response. Please say YES or press 1 to confirm, NO or press 2 to cancel.",
        "hi": "Hamne aapki baat nahi suni. Pushti ke liye haan ya 1 dabayen, radd ke liye nahi ya 2 dabayen.",
        "kn": "Naavu nimma uttara keLalilla. Drudheekarisalu houdu athava 1, raddu madalu beda athava 2 ottiri.",
        "mr": "Aamhala uttara aikoo aale nahi. Pushtisaathi ho kiva 1, raddsaathi nahi kiva 2 daba.",
        "te": "Mee jawabu vinaledu. Nirdhaarinchataniki avunu leda 1, raddu cheyyataniki vaddu leda 2 nokkandi.",
    }
    no_response_msgs = {
        "en": "We could not get a response from you. We will try again later. Goodbye.",
        "hi": "Aapse koi uttara nahi mila. Hum baad mein punah prayas karenge. Alvida.",
        "kn": "Nimmiinda yaavudu uttara sigalilla. Naavu nantara matte prayatnisuththeve.",
        "mr": "Tumchyakadun milala nahi. Aamhi nantar punha praytna karu. Dhanyavaad.",
        "te": "Mee nundi spondana raaledu. Maemu tarvata malli prayanisataamu.",
    }

    vr = VoiceResponse()

    if attempt < 3:
        say_in_language(vr, retry_msgs.get(language, retry_msgs["en"]), language)
        vr.redirect(
            f"{BASE_URL}/webhook/greet"
            f"?order_id={order_id}&language={language}"
            f"&service_type=all&attempt={attempt + 1}"
        )
    else:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE orders SET status='no_response', updated_at=? WHERE order_id=?",
            (datetime.utcnow().isoformat(), order_id),
        )
        conn.commit()
        conn.close()
        say_in_language(vr, no_response_msgs.get(language, no_response_msgs["en"]), language)
        vr.hangup()
        log_call_event(order_id, "no_response_final", f"attempts={attempt}")

    return Response(content=str(vr), media_type="application/xml")


@app.post("/webhook/status")
async def webhook_status(
    request: Request,
    CallStatus: Optional[str] = Form(default=None),
    CallDuration: Optional[str] = Form(default=None),
):
    params = dict(request.query_params)
    order_id = params.get("order_id", "UNKNOWN")
    logger.info("[STAT] Call status for %s: %s (duration=%ss)", order_id, CallStatus, CallDuration)
    log_call_event(order_id, "twilio_status", f"status={CallStatus}, duration={CallDuration}s")

    if CallStatus in ("no-answer", "busy", "failed"):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE orders SET status=?, notes=? WHERE order_id=? AND status='pending'",
            (CallStatus.replace("-", "_"), f"Twilio status: {CallStatus}", order_id),
        )
        conn.commit()
        conn.close()

    return PlainTextResponse("OK")


@app.post("/webhook/recording")
async def webhook_recording(
    request: Request,
    RecordingUrl: Optional[str] = Form(default=None),
    RecordingDuration: Optional[str] = Form(default=None),
):
    """Receive call recording URL from Twilio."""
    params = dict(request.query_params)
    order_id = params.get("order_id", "UNKNOWN")
    logger.info("[REC] Recording for %s: %s (%ss)", order_id, RecordingUrl, RecordingDuration)
    if RecordingUrl:
        log_call_event(order_id, "recording_available", f"url={RecordingUrl}, duration={RecordingDuration}s")
    return PlainTextResponse("OK")


# ═══════════════════════════════════════════════════════
# DATA ENDPOINTS
# ═══════════════════════════════════════════════════════
@app.get("/orders")
async def get_orders():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM orders ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()
    return JSONResponse([dict(row) for row in rows])


@app.get("/orders/{order_id}")
async def get_order(order_id: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM orders WHERE order_id=?", (order_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    return JSONResponse(dict(row))


@app.get("/orders/{order_id}/logs")
async def get_order_logs(order_id: str):
    """Fetch structured call logs for a specific order."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM call_logs WHERE order_id=? ORDER BY created_at DESC",
        (order_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return JSONResponse([dict(row) for row in rows])


@app.delete("/scheduled/{job_id}")
async def cancel_scheduled_job(job_id: str):
    job = scheduler.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found or already fired")

    order_id = job.args[0] if job.args else None
    scheduler.remove_job(job_id)

    if order_id:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE orders SET status='cancelled', notes=?, updated_at=? WHERE order_id=?",
            ("Scheduled callback cancelled by operator", datetime.utcnow().isoformat(), order_id),
        )
        conn.commit()
        conn.close()
        log_call_event(order_id, "schedule_cancelled", f"job_id={job_id}")

    return JSONResponse({
        "success": True,
        "job_id": job_id,
        "order_id": order_id,
        "message": "Scheduled callback cancelled successfully",
    })


@app.get("/scheduled")
async def get_scheduled():
    jobs = []
    for job in scheduler.get_jobs():
        order_id = job.args[0] if job.args else None
        order_row = None
        if order_id:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT customer_name, customer_phone, order_details, language, service_type "
                "FROM orders WHERE order_id=?",
                (order_id,),
            )
            order_row = cursor.fetchone()
            conn.close()

        jobs.append({
            "job_id": job.id,
            "order_id": order_id,
            "run_at": job.next_run_time.isoformat() if job.next_run_time else None,
            "customer_name": order_row["customer_name"] if order_row else None,
            "customer_phone": order_row["customer_phone"] if order_row else None,
            "order_details": order_row["order_details"] if order_row else None,
            "language": order_row["language"] if order_row else None,
            "service_type": order_row["service_type"] if order_row else None,
        })

    jobs.sort(key=lambda j: j["run_at"] or "")
    return JSONResponse(jobs)


@app.get("/stats")
async def get_stats():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            COUNT(*)                                               AS total,
            SUM(CASE WHEN status='confirmed'   THEN 1 ELSE 0 END) AS confirmed,
            SUM(CASE WHEN status='cancelled'   THEN 1 ELSE 0 END) AS cancelled,
            SUM(CASE WHEN status='rescheduled' THEN 1 ELSE 0 END) AS rescheduled,
            SUM(CASE WHEN status='pending'     THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN status='no_response' THEN 1 ELSE 0 END) AS no_response
        FROM orders
    """)
    row = cursor.fetchone()
    conn.close()
    return JSONResponse(dict(row))


@app.get("/export/csv")
async def export_csv():
    """Export all orders to CSV for reporting."""
    import csv
    import io
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM orders ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        raise HTTPException(status_code=404, detail="No orders to export")

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows([dict(row) for row in rows])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=voicebot_orders.csv"},
    )


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
