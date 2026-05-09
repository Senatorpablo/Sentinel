# Sentinel — AI Agency Orchestrator

Sentinel orchestrates a team of AI agents with heartbeat scheduling, output routing, dependency chains, and org-tree governance.

## Architecture

- **Heartbeat Scheduler**: Cron-like scheduler that wakes agents on schedule
- **Task Router**: Routes agent outputs to downstream agents based on dependency chains
- **State Store**: Persistent JSON store for agent state and run history
- **Flask API**: HTTP API for triggering agents, checking status, viewing history
- **Org Tree**: Simple dependency graph (SEO → Social → Email)

## Deployed Agents

| Agent | Role | Schedules | Downstream |
|-------|------|-----------|------------|
| SEO Specialist | Research keywords, recommend blog topics | Daily 7AM, Weekly Mon 8AM | Social, Email |
| Social Media Manager | Create content, schedule posts | Daily 9AM, Weekly Mon 10AM | — |
| Email List Manager | Craft sequences, segment audiences | Daily 7:15AM, Weekly Mon 9AM | — |
| Media Buyer | Run ad campaigns, optimise ROAS | Daily 8AM, Every 6h, Weekly Mon 10AM | — |

## Dependency Chains

- **SEO → Social**: When SEO publishes a blog, auto-create social posts
- **SEO → Email**: When SEO publishes a blog, auto-create email sequence

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
| `/api/sentinel/agents` | GET | List all agents |
| `/api/sentinel/agents/<id>/trigger` | POST | Trigger an agent manually |
| `/api/sentinel/runs/history` | GET | Get run history |
| `/api/sentinel/chains` | GET | List dependency chains |
| `/api/sentinel/status` | GET | Full dashboard status |

## Agent Output Format

Agents should print a `SENTINEL_OUTPUTS` line for downstream routing:

```
SENTINEL_OUTPUTS {"topic": "How to fix a leaking tap", "keywords": ["plumbing", "leak"]}
```

## License

MIT
