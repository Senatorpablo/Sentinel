#!/usr/bin/env python3
"""
Sentinel — AI Agency Orchestrator v1.1.0
Orchestrates a team of AI agents with heartbeat scheduling,
output routing, dependency chains, retry logic, failure alerts,
and real API enrichment.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# ─── Paths ──────────────────────────────────────────
SENTINEL_ROOT = Path("/opt/sentinel")
CONFIG_DIR = SENTINEL_ROOT / "config"
LOGS_DIR = SENTINEL_ROOT / "logs"
STATE_DIR = SENTINEL_ROOT / "state"

for d in [CONFIG_DIR, LOGS_DIR, STATE_DIR]:
    d.mkdir(parents=True, exist_ok=True)

LOG_FILE = LOGS_DIR / "sentinel.log"

# ─── Logging ────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("Sentinel")

# ─── Retry Configuration ───────────────────────────
RETRY_CONFIG = {
    "max_retries": 3,
    "base_delay_seconds": 2.0,
    "backoff_multiplier": 2.0,
    "max_delay_seconds": 30.0,
}

# ─── Alert Configuration ────────────────────────────
ALERT_CONFIG = {
    "telegram_bot_token": os.environ.get("SENTINEL_TELEGRAM_BOT_TOKEN", ""),
    "telegram_chat_id": os.environ.get("SENTINEL_TELEGRAM_CHAT_ID", ""),
    "slack_webhook_url": os.environ.get("SENTINEL_SLACK_WEBHOOK_URL", ""),
    "enabled": bool(
        os.environ.get("SENTINEL_TELEGRAM_BOT_TOKEN", "")
        or os.environ.get("SENTINEL_SLACK_WEBHOOK_URL", "")
    ),
    "cooldown_minutes": 5,
}


# ═══════════════════════════════════════════════════
# Persistent State
# ═══════════════════════════════════════════════════

class SentinelState:
    def __init__(self, path: Path):
        self.path = path
        self._data: Dict[str, Any] = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path) as f:
                    self._data = json.load(f)
            except Exception as e:
                logger.warning(f"State load failed: {e}; starting fresh")
                self._data = {}
        else:
            self._data = {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self._data, f, indent=2, default=str)

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value: Any):
        self._data[key] = value
        self.save()

    def get_agent_state(self, agent_id: str) -> Dict:
        return self.get(f"agent:{agent_id}", {})

    def set_agent_state(self, agent_id: str, state: Dict):
        self.set(f"agent:{agent_id}", state)

    def get_run_history(self, agent_id: str, limit: int = 50) -> List[Dict]:
        history = self.get(f"history:{agent_id}", [])
        return history[-limit:]

    def record_run(self, agent_id: str, result: Dict):
        key = f"history:{agent_id}"
        history = self.get(key, [])
        history.append(result)
        if len(history) > 200:
            history = history[-200:]
        self.set(key, history)

    def get_retry_count(self, agent_id: str) -> int:
        return self.get_agent_state(agent_id).get("retry_count", 0)

    def set_retry_count(self, agent_id: str, count: int):
        agent_state = self.get_agent_state(agent_id)
        agent_state["retry_count"] = count
        self.set_agent_state(agent_id, agent_state)

    def get_last_alert_time(self, agent_id: str) -> Optional[str]:
        return self.get(f"last_alert:{agent_id}", None)

    def set_last_alert_time(self, agent_id: str, ts: str):
        self.set(f"last_alert:{agent_id}", ts)


state = SentinelState(STATE_DIR / "sentinel.json")


# ═══════════════════════════════════════════════════
# Alert System
# ═══════════════════════════════════════════════════

class SentinelAlert:
    """Sends failure alerts via Telegram (preferred) or Slack."""

    @staticmethod
    async def send(agent_id: str, result: Dict, is_retry: bool = False):
        if not ALERT_CONFIG["enabled"]:
            return

        # Cooldown check
        last_alert = state.get_last_alert_time(agent_id)
        if last_alert:
            last_dt = datetime.fromisoformat(last_alert)
            if datetime.utcnow() - last_dt < timedelta(minutes=ALERT_CONFIG["cooldown_minutes"]):
                return

        config = AGENTS_CONFIG.get(agent_id, {})
        agent_name = config.get("name", agent_id)
        run_id = result.get("run_id", "unknown")
        status = result.get("status", "unknown")
        error = result.get("error", result.get("stdout_tail", "No details"))[:500]
        retry_text = f" (Retry attempt)" if is_retry else ""

        message = (
            f"🚨 *Sentinel Alert*{retry_text}\n\n"
            f"*Agent:* {agent_name}\n"
            f"*Status:* `{status}`\n"
            f"*Run ID:* `{run_id}`\n"
            f"*Time:* {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
            f"```\n{error}\n```"
        )

        # Try Telegram first (we're already on Telegram)
        if ALERT_CONFIG["telegram_bot_token"] and ALERT_CONFIG["telegram_chat_id"]:
            await SentinelAlert._send_telegram(message)
        # Fallback to Slack
        elif ALERT_CONFIG["slack_webhook_url"]:
            await SentinelAlert._send_slack(message)

        state.set_last_alert_time(agent_id, datetime.utcnow().isoformat())

    @staticmethod
    async def _send_telegram(message: str):
        import urllib.request
        import urllib.parse
        token = ALERT_CONFIG["telegram_bot_token"]
        chat_id = ALERT_CONFIG["telegram_chat_id"]
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown",
        }).encode()
        try:
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                logger.info(f"Telegram alert sent: {resp.status}")
        except Exception as e:
            logger.warning(f"Telegram alert failed: {e}")

    @staticmethod
    async def _send_slack(message: str):
        import urllib.request
        import json as _json
        url = ALERT_CONFIG["slack_webhook_url"]
        payload = _json.dumps({"text": message.replace("*", "**").replace("`", "")}).encode()
        try:
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                logger.info(f"Slack alert sent: {resp.status}")
        except Exception as e:
            logger.warning(f"Slack alert failed: {e}")


# ═══════════════════════════════════════════════════
# Agent Configurations
# ═══════════════════════════════════════════════════

AGENTS_CONFIG = {
    "seo": {
        "id": "seo",
        "name": "SEO Specialist",
        "role": "Research keywords, analyse competitors, recommend blog topics",
        "schedules": ["0 7 * * *", "0 8 * * 1"],
        "working_dir": "/opt/full-stack-agency",
        "command": [
            "--env=AGENCY_ROOT=/opt/full-stack-agency",
            "--env=PYTHONPATH=/opt/full-stack-agency",
            "/opt/full-stack-agency/venv/bin/python3",
            "agents/seo/scripts/run.py",
        ],
        "outputs": ["topic", "keywords", "competitor_urls", "blog_outline"],
        "downstream": ["social", "email"],
    },
    "email": {
        "id": "email",
        "name": "Email List Manager",
        "role": "Manage email lists, craft sequences, segment audiences",
        "schedules": ["15 7 * * *", "0 9 * * 1"],
        "working_dir": "/opt/full-stack-agency",
        "command": [
            "--env=AGENCY_ROOT=/opt/full-stack-agency",
            "--env=PYTHONPATH=/opt/full-stack-agency",
            "/opt/full-stack-agency/venv/bin/python3",
            "agents/email/scripts/run.py",
        ],
        "outputs": ["sequences_sent", "audience_segments", "open_rate"],
        "downstream": [],
    },
    "media_buyer": {
        "id": "media_buyer",
        "name": "Media Buyer",
        "role": "Run ad campaigns, manage budgets, optimise ROAS",
        "schedules": ["0 8 * * *", "0 */6 * * *", "0 10 * * 1"],
        "working_dir": "/opt/full-stack-agency",
        "command": [
            "--env=AGENCY_ROOT=/opt/full-stack-agency",
            "--env=PYTHONPATH=/opt/full-stack-agency",
            "/opt/full-stack-agency/venv/bin/python3",
            "agents/media-buyer/scripts/run.py",
        ],
        "outputs": ["campaign_status", "spend", "roas", "impressions"],
        "downstream": [],
    },
    "social": {
        "id": "social",
        "name": "Social Media Manager",
        "role": "Create content, schedule posts, engage audience",
        "schedules": ["0 9 * * *", "0 10 * * 1"],
        "working_dir": "/opt/full-stack-agency",
        "command": [
            "--env=AGENCY_ROOT=/opt/full-stack-agency",
            "--env=PYTHONPATH=/opt/full-stack-agency",
            "/opt/full-stack-agency/venv/bin/python3",
            "agents/social/scripts/run.py",
        ],
        "outputs": ["posts_scheduled", "engagement_rate", "follower_delta"],
        "downstream": [],
    },
}

# Dependency routing chains
CHAINS_CONFIG = {
    "seo_blog_to_social": {
        "trigger": "seo",
        "on_status": "success",
        "when_output_has": "topic",
        "route_to": "social",
        "with_params": {
            "post_topic": "{{outputs.topic}}",
            "keywords": "{{outputs.keywords}}",
            "blog_url": "{{outputs.blog_url|default('')}}",
        },
        "description": "When SEO publishes a blog, auto-create social posts",
    },
    "seo_blog_to_email": {
        "trigger": "seo",
        "on_status": "success",
        "when_output_has": "topic",
        "route_to": "email",
        "with_params": {
            "sequence_topic": "{{outputs.topic}}",
            "keywords": "{{outputs.keywords}}",
        },
        "description": "When SEO publishes a blog, auto-create email sequence",
    },
}


# ═══════════════════════════════════════════════════
# Cron Parser
# ═══════════════════════════════════════════════════

def parse_cron(cron_expr: str, now: datetime) -> bool:
    parts = cron_expr.split()
    if len(parts) != 5:
        return False
    minute, hour, day_month, month, day_week = parts

    def matches(field: str, current_val: int) -> bool:
        if field == "*":
            return True
        if "/" in field:
            base, step = field.split("/")
            if base == "*":
                return current_val % int(step) == 0
            return current_val in range(int(base), 60, int(step))
        if "," in field:
            return current_val in [int(x) for x in field.split(",")]
        if "-" in field:
            start, end = field.split("-")
            return int(start) <= current_val <= int(end)
        return current_val == int(field)

    return (
        matches(minute, now.minute)
        and matches(hour, now.hour)
        and matches(day_month, now.day)
        and matches(month, now.month)
        and matches(day_week, now.weekday())
    )


# ═══════════════════════════════════════════════════
# Scheduler
# ═══════════════════════════════════════════════════

class SentinelScheduler:
    def __init__(self):
        self._running = False

    async def _tick(self):
        now = datetime.utcnow()
        logger.debug(f"Tick: {now.isoformat()}")

        for agent_id, config in AGENTS_CONFIG.items():
            agent_state = state.get_agent_state(agent_id)
            last_run_t = agent_state.get("last_run_at")

            for cron in config["schedules"]:
                if parse_cron(cron, now):
                    if last_run_t:
                        from datetime import datetime as _dt
                        try:
                            last_dt = _dt.fromisoformat(last_run_t)
                            if last_dt.replace(second=0, microsecond=0) == now.replace(second=0, microsecond=0):
                                continue
                        except:
                            pass

                    logger.info(f"{agent_id}: schedule triggered ({cron})")
                    await SentinelExecutor.run_agent(agent_id)
                    break

    async def start(self):
        self._running = True
        logger.info("Sentinel scheduler started")
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"Tick error: {e}\n{traceback.format_exc()}")
            await asyncio.sleep(60)

    def stop(self):
        self._running = False
        logger.info("Sentinel scheduler stopped")


# ═══════════════════════════════════════════════════
# Executor with Retry
# ═══════════════════════════════════════════════════

class SentinelExecutor:
    @staticmethod
    async def run_agent(
        agent_id: str,
        manual_params: Optional[Dict] = None,
        triggered_by: Optional[str] = None,
        retry_count: int = 0,
    ) -> Dict:
        config = AGENTS_CONFIG.get(agent_id)
        if not config:
            raise ValueError(f"Unknown agent: {agent_id}")

        run_id = f"{agent_id}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{os.urandom(2).hex()}"
        logger.info(f"[{run_id}] Starting {config['name']} (attempt {retry_count + 1})")

        agent_state = state.get_agent_state(agent_id)
        agent_state["last_run_at"] = datetime.utcnow().isoformat()
        agent_state["current_run_id"] = run_id
        agent_state["status"] = "running"
        state.set_agent_state(agent_id, agent_state)

        start_time = time.time()

        try:
            cmd = []
            env = os.environ.copy()
            wd = config.get("working_dir", str(SENTINEL_ROOT))

            for token in config["command"]:
                if token.startswith("--env="):
                    kv = token[len("--env="):]
                    k, v = kv.split("=", 1)
                    env[k] = v
                else:
                    cmd.append(token)

            if triggered_by == "weekly":
                cmd.append("--weekly")
            elif triggered_by == "pacing":
                cmd.append("--pacing")

            env["SENTINEL_RUN_ID"] = run_id
            env["SENTINEL_AGENT_ID"] = agent_id
            env["SENTINEL_AGENT_NAME"] = config["name"]
            env["SENTINEL_RETRY_COUNT"] = str(retry_count)
            if triggered_by:
                env["SENTINEL_TRIGGERED_BY"] = triggered_by
            if manual_params:
                env["SENTINEL_INPUT_PARAMS"] = json.dumps(manual_params)

            logger.info(f"[{run_id}] Exec: {' '.join(cmd)}")
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                cwd=wd,
            )

            stdout, _ = await process.communicate()
            stdout_text = stdout.decode("utf-8", errors="replace")

            elapsed = time.time() - start_time
            exit_code = process.returncode
            outputs = SentinelExecutor._extract_outputs(stdout_text)

            status = "success" if exit_code == 0 else "failed"
            if exit_code != 0:
                logger.error(f"[{run_id}] Exit code {exit_code}")

            result = {
                "run_id": run_id,
                "agent_id": agent_id,
                "agent_name": config["name"],
                "status": status,
                "exit_code": exit_code,
                "started_at": datetime.utcnow().isoformat(),
                "elapsed_seconds": round(elapsed, 2),
                "outputs": outputs,
                "stdout_tail": stdout_text[-2000:],
                "triggered_by": triggered_by,
                "retry_count": retry_count,
            }

            agent_state = state.get_agent_state(agent_id)
            agent_state["status"] = status
            agent_state["last_run_id"] = run_id
            agent_state["last_run_duration"] = round(elapsed, 2)
            agent_state["current_run_id"] = None
            agent_state["last_outputs"] = outputs
            state.set_agent_state(agent_id, agent_state)
            state.record_run(agent_id, result)

            logger.info(f"[{run_id}] {status} ({elapsed:.1f}s)")

            # ─── Retry on failure ─────────────────────
            if status == "failed" and retry_count < RETRY_CONFIG["max_retries"]:
                delay = min(
                    RETRY_CONFIG["base_delay_seconds"] * (RETRY_CONFIG["backoff_multiplier"] ** retry_count),
                    RETRY_CONFIG["max_delay_seconds"],
                )
                logger.info(f"[{run_id}] Retrying in {delay:.1f}s (attempt {retry_count + 2}/{RETRY_CONFIG['max_retries'] + 1})")
                await asyncio.sleep(delay)
                return await SentinelExecutor.run_agent(
                    agent_id, manual_params, triggered_by, retry_count + 1
                )

            # ─── Alert on final failure ─────────────────
            if status == "failed" and retry_count >= RETRY_CONFIG["max_retries"]:
                logger.error(f"[{run_id}] Max retries exceeded for {agent_id}")
                await SentinelAlert.send(agent_id, result, is_retry=False)

            # ─── Route on success ─────────────────────
            if status == "success":
                await SentinelRouter.route(agent_id, result)

            return result

        except Exception as e:
            elapsed = time.time() - start_time
            error_msg = f"{e}\n{traceback.format_exc()}"
            logger.error(f"[{run_id}] Exception: {error_msg[:500]}")

            result = {
                "run_id": run_id,
                "agent_id": agent_id,
                "agent_name": config["name"],
                "status": "failed",
                "error": error_msg[:2000],
                "started_at": datetime.utcnow().isoformat(),
                "elapsed_seconds": round(elapsed, 2),
                "outputs": {},
                "retry_count": retry_count,
            }

            agent_state = state.get_agent_state(agent_id)
            agent_state["status"] = "failed"
            agent_state["current_run_id"] = None
            state.set_agent_state(agent_id, agent_state)
            state.record_run(agent_id, result)

            # Retry on exception too
            if retry_count < RETRY_CONFIG["max_retries"]:
                delay = min(
                    RETRY_CONFIG["base_delay_seconds"] * (RETRY_CONFIG["backoff_multiplier"] ** retry_count),
                    RETRY_CONFIG["max_delay_seconds"],
                )
                logger.info(f"[{run_id}] Exception retry in {delay:.1f}s")
                await asyncio.sleep(delay)
                return await SentinelExecutor.run_agent(
                    agent_id, manual_params, triggered_by, retry_count + 1
                )

            # Final failure alert
            await SentinelAlert.send(agent_id, result, is_retry=False)
            return result

    @staticmethod
    def _extract_outputs(stdout: str) -> Dict:
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("SENTINEL_OUTPUTS "):
                try:
                    return json.loads(line[len("SENTINEL_OUTPUTS "):])
                except json.JSONDecodeError:
                    logger.warning(f"Failed to parse outputs: {line[:200]}")
        return {}


# ═══════════════════════════════════════════════════
# Router (Dependency Chains)
# ═══════════════════════════════════════════════════

class SentinelRouter:
    @staticmethod
    async def route(source_agent_id: str, result: Dict):
        for chain_id, chain in CHAINS_CONFIG.items():
            if chain["trigger"] != source_agent_id:
                continue
            if chain.get("on_status") and result["status"] != chain["on_status"]:
                continue

            if chain.get("when_output_has"):
                key = chain["when_output_has"]
                outputs = result.get("outputs", {})
                if key not in outputs or not outputs[key]:
                    logger.debug(f"Chain {chain_id}: no {key}")
                    continue

            params = SentinelRouter._render_params(
                chain.get("with_params", {}),
                result.get("outputs", {}),
            )

            target = chain["route_to"]
            logger.info(f"Chain {chain_id}: {source_agent_id} -> {target}")
            asyncio.create_task(
                SentinelExecutor.run_agent(
                    target,
                    manual_params=params,
                    triggered_by=f"chain:{chain_id}",
                )
            )

    @staticmethod
    def _render_params(template: Dict, outputs: Dict) -> Dict:
        rendered = {}
        for key, val in template.items():
            if isinstance(val, str) and val.startswith("{{") and val.endswith("}}"):
                inner = val[2:-2].strip()
                parts = inner.split("|")
                path = parts[0]
                default = ""
                if len(parts) > 1 and "default" in parts[1]:
                    d = parts[1].split("default")[1].strip()
                    if d.startswith("(") and d.endswith(")"):
                        default = d[1:-1].strip("'\"")

                if path.startswith("outputs."):
                    k = path.split(".", 1)[1]
                    rendered[key] = outputs.get(k, default)
                else:
                    rendered[key] = outputs.get(path, default)
            else:
                rendered[key] = val
        return rendered


# ═══════════════════════════════════════════════════
# API Enrichment Module
# ═══════════════════════════════════════════════════

class APIEnrichment:
    """Fetches real data from public APIs for agent enrichment."""

    @staticmethod
    async def fetch_news(query: str, api_key: str = "") -> List[Dict]:
        """Fetch news articles for SEO/content inspiration."""
        if not api_key:
            return []
        import urllib.request
        import json as _json
        url = f"https://newsapi.org/v2/everything?q={urllib.parse.quote(query)}&sortBy=relevancy&pageSize=5&apiKey={api_key}"
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = _json.loads(resp.read())
                return data.get("articles", [])
        except Exception as e:
            logger.warning(f"NewsAPI fetch failed: {e}")
            return []

    @staticmethod
    async def fetch_trending_searches(geo: str = "US") -> List[str]:
        """Fetch trending Google searches (requires SerpAPI or similar)."""
        return []  # Placeholder — requires paid API

    @staticmethod
    async def fetch_weather(city: str) -> Dict:
        """Fetch weather for location-based content."""
        import urllib.request
        import json as _json
        url = f"https://wttr.in/{city}?format=j1"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return _json.loads(resp.read())
        except Exception as e:
            logger.warning(f"Weather fetch failed: {e}")
            return {}

    @staticmethod
    async def fetch_random_quote() -> Dict:
        """Fetch inspirational quote for social content."""
        import urllib.request
        import json as _json
        try:
            with urllib.request.urlopen("https://api.quotable.io/random", timeout=10) as resp:
                return _json.loads(resp.read())
        except Exception as e:
            logger.warning(f"Quote fetch failed: {e}")
            return {}


# ═══════════════════════════════════════════════════
# Flask API
# ═══════════════════════════════════════════════════

try:
    from flask import Flask, jsonify, request
    api_app = Flask("Sentinel")
except ImportError:
    api_app = None

if api_app:
    @api_app.route("/api/sentinel/health", methods=["GET"])
    def health():
        return jsonify({"status": "healthy", "version": "1.1.0", "timestamp": datetime.utcnow().isoformat()})

    @api_app.route("/api/sentinel/agents", methods=["GET"])
    def list_agents():
        agents = []
        for agent_id, config in AGENTS_CONFIG.items():
            agent_state = state.get_agent_state(agent_id)
            agents.append({
                "id": agent_id,
                "name": config["name"],
                "role": config["role"],
                "schedules": config["schedules"],
                "downstream": config.get("downstream", []),
                "status": agent_state.get("status", "pending"),
                "last_run_at": agent_state.get("last_run_at"),
                "last_run_id": agent_state.get("last_run_id"),
                "last_outputs": agent_state.get("last_outputs", {}),
                "retry_count": agent_state.get("retry_count", 0),
            })
        return jsonify({"agents": agents})

    @api_app.route("/api/sentinel/agents/<agent_id>/trigger", methods=["POST"])
    def trigger_agent(agent_id):
        if agent_id not in AGENTS_CONFIG:
            return jsonify({"error": f"Agent not found: {agent_id}"}), 404
        params = request.get_json(silent=True) or {}
        trigger_type = request.args.get("type", "manual")
        asyncio.create_task(
            SentinelExecutor.run_agent(agent_id, manual_params=params, triggered_by=trigger_type)
        )
        return jsonify({"status": "triggered", "agent_id": agent_id, "trigger_type": trigger_type})

    @api_app.route("/api/sentinel/runs/history", methods=["GET"])
    def get_run_history():
        agent_id = request.args.get("agent")
        limit = int(request.args.get("limit", 20))
        if agent_id:
            history = state.get_run_history(agent_id, limit)
        else:
            history = []
            for aid in AGENTS_CONFIG:
                history.extend(state.get_run_history(aid, limit))
            history = sorted(history, key=lambda x: x.get("started_at", ""), reverse=True)[:limit]
        return jsonify({"runs": history, "count": len(history)})

    @api_app.route("/api/sentinel/chains", methods=["GET"])
    def list_chains():
        chains = [{"id": k, **v} for k, v in CHAINS_CONFIG.items()]
        return jsonify({"chains": chains})

    @api_app.route("/api/sentinel/status", methods=["GET"])
    def full_status():
        agents_status = []
        for agent_id, config in AGENTS_CONFIG.items():
            agent_state = state.get_agent_state(agent_id)
            agents_status.append({
                "id": agent_id,
                "name": config["name"],
                "role": config["role"],
                "status": agent_state.get("status", "pending"),
                "last_run_at": agent_state.get("last_run_at"),
                "last_run_duration": agent_state.get("last_run_duration"),
                "last_outputs": agent_state.get("last_outputs", {}),
                "current_run_id": agent_state.get("current_run_id"),
                "retry_count": agent_state.get("retry_count", 0),
            })
        return jsonify({
            "orchestrator": "Sentinel",
            "version": "1.1.0",
            "timestamp": datetime.utcnow().isoformat(),
            "agents": agents_status,
            "chains": list(CHAINS_CONFIG.keys()),
            "retry_policy": RETRY_CONFIG,
            "alerts_enabled": ALERT_CONFIG["enabled"],
        })

    @api_app.route("/api/sentinel/config", methods=["GET"])
    def get_config():
        return jsonify({
            "retry": RETRY_CONFIG,
            "alerts": {k: v for k, v in ALERT_CONFIG.items() if k != "slack_webhook_url"},
        })


# ═══════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════

async def main():
    parser = argparse.ArgumentParser(description="Sentinel — AI Agency Orchestrator v1.1.0")
    parser.add_argument("--config", help="Path to config dir", default=str(CONFIG_DIR))
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run a single agent")
    run_parser.add_argument("agent", help="Agent ID")
    run_parser.add_argument("--param", action="append", help="key=value")
    run_parser.add_argument("--weekly", action="store_true")
    run_parser.add_argument("--pacing", action="store_true")

    subparsers.add_parser("scheduler", help="Start heartbeat scheduler")

    api_parser = subparsers.add_parser("api", help="Start API server")
    api_parser.add_argument("--host", default="0.0.0.0")
    api_parser.add_argument("--port", type=int, default=9092)

    args = parser.parse_args()

    if args.command == "run":
        params = {}
        if args.param:
            for p in args.param:
                k, v = p.split("=", 1)
                params[k] = v
        triggered_by = "weekly" if args.weekly else ("pacing" if args.pacing else "manual")
        result = await SentinelExecutor.run_agent(args.agent, params, triggered_by)
        print(json.dumps(result, indent=2))

    elif args.command == "scheduler":
        scheduler = SentinelScheduler()
        try:
            await scheduler.start()
        except (KeyboardInterrupt, asyncio.CancelledError):
            scheduler.stop()

    elif args.command == "api":
        if not api_app:
            logger.error("Flask not installed: pip install flask")
            sys.exit(1)
        api_app.run(host=args.host, port=args.port, threaded=True)

    else:
        parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
