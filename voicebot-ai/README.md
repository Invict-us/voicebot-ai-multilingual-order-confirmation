# VoiceBot AI 🤖📞

**Multilingual AI Voice Bot for Automated Order Confirmation**

Built for the Automaton AI Infosystem Hackathon — handles outbound calls in English, Hindi, Kannada, Marathi & Telugu using Twilio + FastAPI + optional OpenAI GPT-4o.

---

## 📁 Project Structure

```
voicebot-ai/
├── .env.example          # Template for all API keys
├── requirements.txt      # Python dependencies
├── main.py               # FastAPI backend (improved)
├── database.py           # SQLite schema + helpers
├── frontend/
│   └── index.html        # Dark-themed dashboard
└── logs/
    └── voicebot.log      # Rotating logs
```

---

## 🚀 Quick Start

### 1. Clone & Enter Directory
```bash
cd voicebot-ai
```

### 2. Create Virtual Environment
```bash
python -m venv venv
source venv/bin/activate        # macOS/Linux
venv\Scripts\activate         # Windows
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Configure Environment
```bash
cp .env.example .env
# Edit .env with your real Twilio credentials and BASE_URL
```

### 5. Run the Server
```bash
python main.py
```
Server starts at `http://localhost:8000`

---

## 🔑 Required API Keys & Platforms

### Tier 1 — Must Have (Core)
| Platform | What For | Get Key From |
|----------|----------|--------------|
| **Twilio** | Voice calls, phone numbers, TwiML | https://console.twilio.com |
| **Ngrok** | Expose localhost to internet for Twilio webhooks | https://ngrok.com |

### Tier 2 — Strongly Recommended
| Platform | What For | Get Key From |
|----------|----------|--------------|
| **OpenAI** | GPT-4o-mini for smart intent detection (falls back to keywords if absent) | https://platform.openai.com |
| **ElevenLabs** | Ultra-realistic Indian-language TTS voices (optional, falls back to Google/Polly) | https://elevenlabs.io |

### Tier 3 — Production / Scale
| Platform | What For |
|----------|----------|
| **PostgreSQL** (Railway/Neon/Supabase) | Replace SQLite for concurrent production loads |
| **Redis** (Upstash/Redis Cloud) | Distributed job queue + caching |
| **Docker + Render/Railway/AWS** | Containerized cloud hosting |

---

## 🔐 Twilio Setup Checklist

1. Buy a Twilio phone number with **Voice** capability.
2. Copy your **Account SID** and **Auth Token** into `.env`.
3. Set the Twilio number as `TWILIO_PHONE_NUMBER` in `.env`.
4. Run ngrok: `ngrok http 8000`
5. Copy the ngrok HTTPS URL into `BASE_URL` in `.env` (e.g. `https://abc123.ngrok-free.app`).
6. Restart the server.

---

## 🧠 How Intent Detection Works

1. **Primary**: OpenAI GPT-4o-mini classifies the customer's speech into `confirm / cancel / reschedule / unclear`.
2. **Fallback**: If no OpenAI key is provided, the bot falls back to fast keyword matching in all 5 languages.

---

## 📊 Features

- ✅ Outbound voice calls via Twilio
- ✅ 5-language support (en, hi, kn, mr, te)
- ✅ DTMF (keypad) + Speech input
- ✅ Smart AI intent detection (OpenAI)
- ✅ Auto-retry scheduling (2h / 3h / next day 10 AM)
- ✅ Call recording support
- ✅ Structured call logs per order
- ✅ Real-time dashboard with stats
- ✅ CSV export
- ✅ Scheduled callback management
- ✅ Health check endpoint

---

## 🛡️ Security Improvements in v2.1

- Twilio webhook signature validation ready
- Rotating log files (5 MB max, 5 backups)
- Input sanitization & SQL parameterization
- CORS middleware configured

---

## 📞 Webhook Flow

```
/call (Dashboard) → Twilio API → Customer Phone
                        ↓
            /webhook/trial_bypass (press any key)
                        ↓
            /webhook/greet (read order + gather response)
                        ↓
            /webhook/response (detect intent + reply)
                        ↓
            /webhook/status (call completed/failed/busy)
```

---

**Made for Hackathon 2024** · ADVIT™ AI Labs × AutomatonAI
