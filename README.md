# Mergedom UA Intelligence

Automated campaign intelligence system for mobile gaming UA & Monetization. Replaces a 45-60 minute morning Tableau routine with an AI-powered brief delivered to Slack and Gmail at 6:35 AM IST.

## What It Does

- **Morning Brief** — Aggregates spend, ROAS, CPI across Google Ads, Meta, AppLovin, and incentivized networks into a single Slack message with action items ranked by predicted impact.
- **Anomaly Detection** — Monitors 6 campaign metrics every 2 hours (9 AM–11 PM IST) and alerts on statistically significant deviations (z-score based, with weekend/network-specific suppression).
- **Creative Fatigue** — Scores every creative 0–100 based on IPM/CTR decline curves, groups by concept, and generates structured briefs when creatives show fatigue.
- **Waterfall Optimization** — Analyses MAX mediation floor prices vs eCPM and recommends adjustments with ±15%/day guardrails.
- **Budget Allocation** — Fits spend-response curves per network and recommends budget shifts to maximize portfolio efficiency.
- **Feedback Learning** — Tracks Slack emoji reactions to tune alert weights over time. Auto-suppresses noise after 30+ negative feedbacks.

## Quick Start

```bash
# 1. Clone and enter project
cd mergedom-intel

# 2. Copy environment template
cp .env.example .env

# 3. Fill in API credentials (see setup guide below)
nano .env

# 4. Install dependencies
pip install -r requirements.txt

# 5. Complete Gmail OAuth flow (interactive)
python scripts/setup_gmail.py

# 6. Verify all API connections
python scripts/test_connections.py

# 7. Create Airtable tables
python scripts/setup_airtable.py

# 8. Backfill 30 days of historical data (~5 min)
python scripts/seed_historical.py

# 9. Start the server
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Docker Deployment

```bash
# Build and run
docker-compose up -d

# Check health
curl http://localhost:8000/health

# View logs
docker-compose logs -f app

# Stop
docker-compose down
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Status, uptime, last brief time, alerts today |
| `/status` | GET | All 9 service statuses + 8 scheduler jobs |
| `/trigger/daily-brief` | POST | Manually trigger the morning brief |
| `/trigger/anomaly-check` | POST | Manually trigger spend pacing check |
| `/trigger/creative-analysis` | POST | Manually trigger creative fatigue scan |
| `/metrics` | GET | Prometheus-compatible metrics |

## Scheduled Jobs

| Job | Schedule (IST) | Module |
|-----|---------------|--------|
| Morning Brief | 6:35 AM daily | `daily_brief.py` |
| Spend Pacing Check | Every 2h, 9 AM–11 PM | `daily_brief.py` |
| Waterfall Optimization | 7:00 AM daily | `waterfall.py` |
| Budget Review | 7:30 AM daily | `budget.py` |
| Creative Fatigue | Friday 5:00 PM | `creative.py` |
| Gmail Scan | Every 4 hours | `gmail.py` |
| Feedback Report | Sunday 8:00 PM | `feedback.py` |
| Escalation Check | Every 30 minutes | `coordinator.py` |

## API Credentials Setup Guide

### Singular

1. Log in to [Singular Dashboard](https://app.singular.net)
2. Go to **Settings > API Keys**
3. Copy your API key

```
SINGULAR_API_KEY=your_key_here
```

### Slack Bot

1. Go to [api.slack.com/apps](https://api.slack.com/apps) > **Create New App** > **From scratch**
2. Name: `Mergedom Intel`, select your workspace
3. **OAuth & Permissions** — add Bot Token Scopes:
   - `channels:read`, `channels:history`
   - `chat:write`, `chat:write.public`
   - `reactions:read`
   - `users:read`
   - `im:write`
4. **Install to Workspace** — copy **Bot User OAuth Token**
5. **Socket Mode** — enable it, create an **App-Level Token** with `connections:write` scope
6. **Basic Information** — copy **Signing Secret**

```
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_SIGNING_SECRET=...
```

7. Create these channels and invite the bot:
   - `#ua-daily-brief`
   - `#ua-alerts`
   - `#creative-intel`
   - `#monetization-ops`
   - `#system-feedback`

### Gmail OAuth2

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create project **Mergedom Intel**
3. **APIs & Services > Library** — enable **Gmail API**
4. **APIs & Services > Credentials** — create **OAuth 2.0 Client ID** (Desktop app)
5. Copy Client ID and Client Secret to `.env`

```
GMAIL_CLIENT_ID=...
GMAIL_CLIENT_SECRET=...
GMAIL_USER_EMAIL=natasha@yourcompany.com
```

6. Run the interactive OAuth flow:

```bash
python scripts/setup_gmail.py
```

7. Copy the printed `GMAIL_REFRESH_TOKEN=...` into `.env`

### Meta Marketing API

1. Go to [developers.facebook.com](https://developers.facebook.com) > **My Apps** > **Create App** (Business type)
2. Add the **Marketing API** product
3. **Tools > Graph API Explorer**:
   - Select your app
   - Add permissions: `ads_read`, `ads_management`
   - Generate a **User Access Token**
   - Exchange for a long-lived token (60 days)
4. Get your **Ad Account ID** from [Business Manager](https://business.facebook.com) > **Business Settings > Accounts > Ad Accounts** (format: `act_XXXXXXXXX`, omit the `act_` prefix)

```
META_APP_ID=...
META_APP_SECRET=...
META_ACCESS_TOKEN=...
META_AD_ACCOUNT_ID=...
```

### AppLovin

1. Log in to [dash.applovin.com](https://dash.applovin.com)
2. **Account > Keys**
3. Copy **Report Key** and **SDK Key**

```
APPLOVIN_API_KEY=...
APPLOVIN_SDK_KEY=...
```

### Google Ads

1. Apply for a **Developer Token** at [ads.google.com/apis](https://ads.google.com/apis)
2. Create OAuth2 credentials (same Google Cloud project as Gmail, or separate)
3. Generate refresh token:

```bash
pip install google-ads
python -m google_ads.generate_refresh_token
```

4. Copy your Customer ID from the Google Ads UI (format: `XXX-XXX-XXXX`)

```
GOOGLE_ADS_DEVELOPER_TOKEN=...
GOOGLE_ADS_CLIENT_ID=...
GOOGLE_ADS_CLIENT_SECRET=...
GOOGLE_ADS_REFRESH_TOKEN=...
GOOGLE_ADS_CUSTOMER_ID=...
```

### MAX (AppLovin Mediation)

Uses the same AppLovin dashboard. Go to **MAX > API Keys** if a separate key is required, otherwise the Report Key works.

```
MAX_API_KEY=...
```

### Airtable

1. Go to [airtable.com/create/tokens](https://airtable.com/create/tokens)
2. Create a **Personal Access Token** with scopes:
   - `data.records:read`
   - `data.records:write`
   - `schema.bases:read`
   - `schema.bases:write`
3. Create a new **Base** in Airtable — copy the Base ID from the URL (`https://airtable.com/appXXXXXXXXXXXXXX`)

```
AIRTABLE_API_KEY=pat...
AIRTABLE_BASE_ID=appXXXXXXXXXXXXXX
```

4. Run the setup script to create all 10 tables:

```bash
python scripts/setup_airtable.py
```

### Claude API (Anthropic)

1. Go to [console.anthropic.com](https://console.anthropic.com) > **API Keys**
2. Create a new key

```
ANTHROPIC_API_KEY=sk-ant-...
```

## Project Structure

```
mergedom-intel/
├── app/
│   ├── main.py              # FastAPI + APScheduler (8 jobs, 6 endpoints)
│   ├── config.py             # Pydantic Settings with 10 nested groups
│   ├── models/schemas.py     # 8 Pydantic models + 3 enums
│   ├── services/
│   │   ├── singular.py       # Primary data: async reports, campaign + creative
│   │   ├── meta_ads.py       # Graph API v18.0, creative thumbnails
│   │   ├── applovin.py       # Campaign + MAX mediation reports
│   │   ├── google_ads.py     # GAQL queries, billing alerts
│   │   ├── max_reporting.py  # Waterfall: network × geo × format × time
│   │   ├── slack_bot.py      # Block Kit, reactions, DMs, throttling
│   │   ├── gmail.py          # Inbox scan, drafts, send (internal only)
│   │   ├── airtable.py       # 10 tables, batch upsert, learning weights
│   │   └── claude.py         # Sonnet (briefs) + Haiku (parsing), rate limited
│   ├── modules/
│   │   ├── daily_brief.py    # Module 1: Morning brief orchestrator
│   │   ├── creative.py       # Module 2: Fatigue scoring + concept tagging
│   │   ├── waterfall.py      # Module 3: Floor price optimization
│   │   ├── budget.py         # Module 4: Spend-response curves + allocation
│   │   └── coordinator.py    # Module 5: Escalation matrix + dedup
│   ├── pipelines/
│   │   ├── data_pull.py      # 8-source parallel ingestion
│   │   ├── anomaly.py        # Z-score + consecutive decline detection
│   │   └── feedback.py       # Adaptive weights + weekly accuracy report
│   └── utils/
│       ├── formatters.py     # Slack Block Kit, HTML, INR currency, trends
│       └── helpers.py        # Percentage change, currency formatting
├── tests/                    # 192 tests across 8 test files
├── scripts/
│   ├── setup_airtable.py     # Creates 10 Airtable tables
│   ├── setup_gmail.py        # Interactive OAuth2 flow
│   ├── setup_slack.py        # Channel verification + permission test
│   ├── test_connections.py   # Tests all 9 API connections
│   ├── seed_historical.py    # Backfills 30 days from Singular
│   └── healthcheck.sh        # Monitoring script
├── deploy/
│   ├── railway.toml          # Railway deployment config
│   ├── render.yaml           # Render deployment config
│   └── systemd/              # VPS deployment (systemd service)
├── Dockerfile                # Multi-stage, non-root, health check
├── docker-compose.yml        # 512MB limit, json logging
├── requirements.txt          # Pinned dependencies
└── .env.example              # All 30 environment variables
```

## Monitoring

### Health Check

```bash
# Quick check
curl http://localhost:8000/health

# Scripted (exits 1 if unhealthy)
./scripts/healthcheck.sh
```

### Prometheus Metrics

```bash
curl http://localhost:8000/metrics
```

Exposes: `mergedom_uptime_seconds`, `mergedom_connected_services`, `mergedom_scheduler_jobs`, `mergedom_alerts_today`, `mergedom_claude_input_tokens`, `mergedom_claude_output_tokens`.

### Service Status

```bash
curl http://localhost:8000/status | python -m json.tool
```

## Testing

```bash
# Run all 192 tests
python -m pytest tests/ -v

# Run specific module tests
python -m pytest tests/test_anomaly.py -v
python -m pytest tests/test_creative.py -v
python -m pytest tests/test_daily_brief.py -v
python -m pytest tests/test_waterfall.py -v
python -m pytest tests/test_budget.py -v
python -m pytest tests/test_coordinator.py -v
```

## VPS Deployment (systemd)

```bash
# Create user
sudo useradd -r -s /sbin/nologin mergedom

# Deploy code
sudo mkdir -p /opt/mergedom-intel
sudo cp -r . /opt/mergedom-intel/
cd /opt/mergedom-intel

# Create virtualenv
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Install service
sudo cp deploy/systemd/mergedom-intel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable mergedom-intel
sudo systemctl start mergedom-intel

# Check status
sudo systemctl status mergedom-intel
sudo journalctl -u mergedom-intel -f
```
