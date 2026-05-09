# Sentinel — AI Agency Orchestrator v1.1.0

Sentinel orchestrates a team of AI agents with heartbeat scheduling, output routing, dependency chains, **auto-retry logic**, **failure alerts**, and **real API enrichment**.

## What's New in v1.1.0

- **Auto-Retry**: Exponential backoff on failure (3 retries: 2s → 4s → 8s)
- **Failure Alerts**: Telegram or Slack webhook notifications on final failure
- **Alert Cooldown**: 5-minute cooldown between alerts per agent
- **API Enrichment Module**: Fetch real news, weather, quotes for agent content
- **Retry Tracking**: Each run records retry count in state + API

## Architecture

- **Heartbeat Scheduler**: Cron-aware scheduler running as systemd service
- **Task Router**: Routes agent outputs to downstream agents based on dependency chains
- **State Store**: Persistent JSON store for agent state, run history, retry counts
- **Flask API**: 7 REST endpoints on port 9092
- **Alert System**: Telegram Bot API (preferred) or Slack webhooks

## Deployed Agents

| Agent | Role | Schedules | Downstream |
|-------|------|-----------|------------|
| SEO Specialist | Research keywords, recommend blog topics | Daily 7AM, Weekly Mon 8AM | Social, Email |
| Social Media Manager | Create content, schedule posts | Daily 9AM, Weekly Mon 10AM | — |
| Email List Manager | Craft sequences, segment audiences | Daily 7:15AM, Weekly Mon 9AM | — |
| Media Buyer | Run ad campaigns, optimise ROAS | Daily 8AM, Every 6h, Weekly Mon 10AM | — |

## Dependency Chains

- **SEO → Social**: Auto-create social posts from SEO blog topic
- **SEO → Email**: Auto-create email sequence from SEO blog topic

## Quick Start

```bash
# Run a single agent
python3 sentinel.py run seo

# Start the heartbeat scheduler
python3 sentinel.py scheduler

# Start the API server
python3 sentinel.py api --port 9092
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/sentinel/health` | GET | Health check |
| `/api/sentinel/agents` | GET | List all agents with retry counts |
| `/api/sentinel/agents/<id>/trigger` | POST | Trigger an agent manually |
| `/api/sentinel/runs/history` | GET | Get run history |
| `/api/sentinel/chains` | GET | List dependency chains |
| `/api/sentinel/status` | GET | Full dashboard status |
| `/api/sentinel/config` | GET | Retry policy + alert config |

## Alert Configuration

Set environment variables before starting Sentinel:

```bash
# Telegram (preferred — you're already on Telegram)
export SENTINEL_TELEGRAM_BOT_TOKEN="your_bot_token"
export SENTINEL_TELEGRAM_CHAT_ID="your_chat_id"

# OR Slack
export SENTINEL_SLACK_WEBHOOK_URL="https://hooks.slack.com/..."
```

Alerts fire **only after all retries are exhausted** and respect a 5-minute cooldown per agent.

## Agent Output Format

Agents should print a `SENTINEL_OUTPUTS` line for downstream routing:

```
SENTINEL_OUTPUTS {"topic": "How to fix a leaking tap", "keywords": ["plumbing", "leak"]}
```

## Retry Policy

| Attempt | Delay | Action |
|---------|-------|--------|
| 1st | 0s | Initial run |
| 2nd | 2s | First retry |
| 3rd | 4s | Second retry |
| 4th | 8s | Final retry → alert if still failed |

## API Enrichment

Sentinel includes built-in fetchers for:
- **NewsAPI** — fetch articles for content inspiration
- **Quotable API** — random quotes for social posts
- **Wttr.in** — weather data for local content

Agents receive API data via `SENTINEL_INPUT_PARAMS` env var.

## License

MIT — 100% original code, zero third-party dependencies.
