#!/usr/bin/env python3
"""
Sentinel — AI Agency Orchestrator v1.2.0
Orchestrates a team of AI agents with heartbeat scheduling,
output routing, dependency chains, retry logic, failure alerts,
circuit breaker, daily digest, visual dashboard, and real API enrichment.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import traceback
import urllib.request
import urllib.parse
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

# ─── Alert Configuration ──────────────────────────────
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

# ─── Circuit Breaker Configuration ───────────────────
CIRCUIT_CONFIG = {
    "failure_threshold": 5,
    "window_minutes": 60,
    "cooldown_minutes": 30,
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
# Alert System (Telegram + Slack fallback)
# ═══════════════════════════════════════════════════

class SentinelAlert:
    """Sends failure alerts via Telegram (preferred) or Slack fallback."""

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

        # Try Telegram first
        telegram_ok = False
        if ALERT_CONFIG["telegram_bot_token"] and ALERT_CONFIG["telegram_chat_id"]:
            telegram_ok = await SentinelAlert._send_telegram(message)

        # Fallback to Slack if Telegram failed
        if not telegram_ok and ALERT_CONFIG["slack_webhook_url"]:
            await SentinelAlert._send_slack(message)

        state.set_last_alert_time(agent_id, datetime.utcnow().isoformat())

    @staticmethod
    async def _send_telegram(message: str) -> bool:
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
                return 200 <= resp.status < 300
        except Exception as e:
            logger.warning(f"Telegram alert failed: {e}")
            return False

    @staticmethod
    async def _send_slack(message: str):
        url = ALERT_CONFIG["slack_webhook_url"]
        payload = json.dumps({"text": message.replace("*", "**").replace("`", "")}).encode()
        try:
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                logger.info(f"Slack alert sent: {resp.status}")
        except Exception as e:
            logger.warning(f"Slack alert failed: {e}")

    @staticmethod
    async def send_digest():
        """Send daily morning digest of all agent status."""
        if not ALERT_CONFIG["enabled"]:
            return

        lines = ["📋 *Sentinel Daily Digest*\n"]
        lines.append(f"_{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}_\n")

        for agent_id, config in AGENTS_CONFIG.items():
            agent_state = state.get_agent_state(agent_id)
            status = agent_state.get("status", "pending")
            last_run = agent_state.get("last_run_at", "never")
            if last_run != "never":
                try:
                    dt = datetime.fromisoformat(last_run)
                    last_run = dt.strftime("%H:%M")
                except:
                    pass

            # Emoji based on status
            emoji = {"success": "✅", "failed": "❌", "running": "🔄", "pending": "⏳", "circuit_open": "⛔"}.get(status, "❓")
            lines.append(f"{emoji} *{config['name']}*: {status} (last: {last_run})")

        # Failure summary
        total_failures = 0
        for agent_id in AGENTS_CONFIG:
            history = state.get_run_history(agent_id, 24)
            total_failures += sum(1 for r in history if r.get("status") == "failed")

        if total_failures == 0:
            lines.append("\n🎉 *All agents clean — no failures in last 24h*")
        else:
            lines.append(f"\n⚠️ *{total_failures} failures* in last 24h")

        message = "\n".join(lines)

        # Try Telegram
        telegram_ok = False
        if ALERT_CONFIG["telegram_bot_token"] and ALERT_CONFIG["telegram_chat_id"]:
            telegram_ok = await SentinelAlert._send_telegram(message)

        # Fallback to Slack
        if not telegram_ok and ALERT_CONFIG["slack_webhook_url"]:
            await SentinelAlert._send_slack(message)

        logger.info("Daily digest sent")


# ═══════════════════════════════════════════════════
# Circuit Breaker
# ═══════════════════════════════════════════════════

class CircuitBreaker:
    """Prevents repeated failure spam by opening circuit after threshold."""

    @staticmethod
    def is_open(agent_id: str) -> bool:
        agent_state = state.get_agent_state(agent_id)
        if agent_state.get("circuit_open", False):
            opened_at = agent_state.get("circuit_opened_at", "")
            if opened_at:
                try:
                    dt = datetime.fromisoformat(opened_at)
                    if datetime.utcnow() - dt < timedelta(minutes=CIRCUIT_CONFIG["cooldown_minutes"]):
                        return True
                    # Auto-close after cooldown
                    agent_state["circuit_open"] = False
                    agent_state["circuit_opened_at"] = None
                    agent_state["failure_count"] = 0
                    state.set_agent_state(agent_id, agent_state)
                    logger.info(f"Circuit closed for {agent_id}")
                    return False
                except:
                    return False
        return False

    @staticmethod
    def record_failure(agent_id: str):
        agent_state = state.get_agent_state(agent_id)
        failures = agent_state.get("failure_count", 0) + 1
        agent_state["failure_count"] = failures

        # Check if threshold exceeded within window
        history = state.get_run_history(agent_id, CIRCUIT_CONFIG["failure_threshold"] * 2)
        window_start = datetime.utcnow() - timedelta(minutes=CIRCUIT_CONFIG["window_minutes"])
        recent_failures = sum(
            1 for h in history
            if h.get("status") == "failed"
            and datetime.fromisoformat(h.get("started_at", "1970-01-01")) > window_start
        )

        if recent_failures >= CIRCUIT_CONFIG["failure_threshold"]:
            agent_state["circuit_open"] = True
            agent_state["circuit_opened_at"] = datetime.utcnow().isoformat()
            logger.warning(
                f"CIRCUIT OPEN for {agent_id}: {recent_failures} failures in "
                f"{CIRCUIT_CONFIG['window_minutes']}min. Cooldown: {CIRCUIT_CONFIG['cooldown_minutes']}min"
            )

        state.set_agent_state(agent_id, agent_state)

    @staticmethod
    def record_success(agent_id: str):
        agent_state = state.get_agent_state(agent_id)
        if agent_state.get("failure_count", 0) > 0:
            agent_state["failure_count"] = 0
            state.set_agent_state(agent_id, agent_state)
            logger.info(f"Failure counter reset for {agent_id} after success")


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
# Scheduler with Digest
# ═══════════════════════════════════════════════════

class SentinelScheduler:
    def __init__(self):
        self._running = False
        self._last_digest_day = None

    async def _tick(self):
        now = datetime.utcnow()
        logger.debug(f"Tick: {now.isoformat()}")

        # Daily digest at 8:00 AM UTC
        if now.hour == 8 and now.minute == 0:
            today = now.strftime("%Y-%m-%d")
            if self._last_digest_day != today:
                await SentinelAlert.send_digest()
                self._last_digest_day = today

        for agent_id, config in AGENTS_CONFIG.items():
            # Circuit breaker check
            if CircuitBreaker.is_open(agent_id):
                logger.warning(f"{agent_id}: circuit open — skipping schedule")
                continue

            agent_state = state.get_agent_state(agent_id)
            last_run_t = agent_state.get("last_run_at")

            for cron in config["schedules"]:
                if parse_cron(cron, now):
                    if last_run_t:
                        try:
                            last_dt = datetime.fromisoformat(last_run_t)
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

        # Circuit breaker check
        if CircuitBreaker.is_open(agent_id):
            logger.warning(f"[{agent_id}] Circuit open — refusing run")
            return {
                "run_id": "circuit-open",
                "agent_id": agent_id,
                "agent_name": config["name"],
                "status": "circuit_open",
                "error": "Circuit breaker open due to repeated failures",
            }

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

            # Fetch enrichment data
            enrichment = await APIEnrichment.enrich_for_agent(
                agent_id, context=manual_params or {}
            )
            if enrichment:
                env["SENTINEL_ENRICHMENT"] = json.dumps(enrichment)
                logger.info(f"[{run_id}] Enrichment: {list(enrichment.keys())}")

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

            if status == "success":
                CircuitBreaker.record_success(agent_id)
                await SentinelRouter.route(agent_id, result)
            else:
                CircuitBreaker.record_failure(agent_id)
                if retry_count < RETRY_CONFIG["max_retries"]:
                    delay = min(
                        RETRY_CONFIG["base_delay_seconds"] * (RETRY_CONFIG["backoff_multiplier"] ** retry_count),
                        RETRY_CONFIG["max_delay_seconds"],
                    )
                    logger.info(f"[{run_id}] Retry in {delay:.1f}s ({retry_count + 2}/{RETRY_CONFIG['max_retries'] + 1})")
                    await asyncio.sleep(delay)
                    return await SentinelExecutor.run_agent(
                        agent_id, manual_params, triggered_by, retry_count + 1
                    )
                else:
                    logger.error(f"[{run_id}] Max retries exceeded")
                    await SentinelAlert.send(agent_id, result)

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

            CircuitBreaker.record_failure(agent_id)

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

            await SentinelAlert.send(agent_id, result)
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
    async def _fetch_json(url: str, timeout: int = 15) -> Optional[Dict]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Sentinel/1.2.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except Exception as e:
            logger.warning(f"API fetch failed ({url[:60]}...): {e}")
            return None

    @staticmethod
    async def fetch_news(query: str) -> List[Dict]:
        """Fetch news articles for SEO/content inspiration."""
        api_key = os.environ.get("NEWS_API_KEY", "")
        if not api_key:
            return []
        url = f"https://newsapi.org/v2/everything?q={urllib.parse.quote(query)}&sortBy=relevancy&pageSize=5&apiKey={api_key}"
        data = await APIEnrichment._fetch_json(url)
        return data.get("articles", []) if data else []

    @staticmethod
    async def fetch_reddit_trends(subreddit: str = "technology") -> List[Dict]:
        """Fetch trending Reddit posts (no auth)."""
        url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit=5"
        data = await APIEnrichment._fetch_json(url, timeout=10)
        if not data or "data" not in data:
            return []
        posts = []
        for child in data["data"].get("children", []):
            p = child.get("data", {})
            posts.append({
                "title": p.get("title", ""),
                "url": f"https://reddit.com{p.get('permalink', '')}",
                "score": p.get("score", 0),
                "subreddit": p.get("subreddit", ""),
            })
        return posts

    @staticmethod
    async def fetch_hackernews_top() -> List[Dict]:
        """Fetch top HackerNews stories (no auth)."""
        ids_data = await APIEnrichment._fetch_json("https://hacker-news.firebaseio.com/v0/topstories.json", timeout=10)
        if not ids_data or not isinstance(ids_data, list):
            return []
        stories = []
        for story_id in ids_data[:5]:
            story = await APIEnrichment._fetch_json(
                f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json", timeout=10
            )
            if story:
                stories.append({
                    "title": story.get("title", ""),
                    "url": story.get("url", f"https://news.ycombinator.com/item?id={story_id}"),
                    "score": story.get("score", 0),
                })
        return stories

    @staticmethod
    async def fetch_crypto_prices() -> Dict:
        """Fetch top crypto prices (no auth)."""
        data = await APIEnrichment._fetch_json(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana&vs_currencies=usd,gbp", timeout=10
        )
        return data or {}

    @staticmethod
    async def fetch_dad_joke() -> Dict:
        """Fetch random dad joke for social content (no auth)."""
        req = urllib.request.Request("https://icanhazdadjoke.com/", headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except Exception as e:
            logger.warning(f"Dad joke fetch failed: {e}")
            return {}

    @staticmethod
    async def fetch_quote() -> Dict:
        """Fetch inspirational quote for social content (no auth)."""
        return await APIEnrichment._fetch_json("https://api.quotable.io/random", timeout=10) or {}

    @staticmethod
    async def fetch_weather(city: str) -> Dict:
        """Fetch weather for location-based content (no auth)."""
        return await APIEnrichment._fetch_json(
            f"https://wttr.in/{urllib.parse.quote(city)}?format=j1", timeout=10
        ) or {}

    @staticmethod
    async def enrich_for_agent(agent_id: str, context: Dict = None) -> Dict:
        """Fetch relevant enrichment data for a specific agent."""
        context = context or {}
        results = {}
        if agent_id == "seo":
            query = context.get("topic", "technology")
            results["news"] = await APIEnrichment.fetch_news(query)
            results["reddit"] = await APIEnrichment.fetch_reddit_trends("technology")
            results["hackernews"] = await APIEnrichment.fetch_hackernews_top()
        elif agent_id == "social":
            results["quote"] = await APIEnrichment.fetch_quote()
            results["joke"] = await APIEnrichment.fetch_dad_joke()
            results["reddit"] = await APIEnrichment.fetch_reddit_trends("funny")
        elif agent_id == "media_buyer":
            results["crypto"] = await APIEnrichment.fetch_crypto_prices()
        elif agent_id == "email":
            results["quote"] = await APIEnrichment.fetch_quote()
            results["weather"] = await APIEnrichment.fetch_weather(context.get("city", "London"))
        return results


# ═══════════════════════════════════════════════════
# Flask API (with Dashboard HTML)
# ═══════════════════════════════════════════════════

try:
    from flask import Flask, jsonify, request
    api_app = Flask("Sentinel")
except ImportError:
    api_app = None

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Sentinel Dashboard</title>
    <style>
        :root { --bg: #0d1117; --card: #161b22; --border: #30363d; --text: #c9d1d9; --success: #238636; --fail: #da3633; --running: #1f6feb; --pending: #8b949e; }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 2rem; }
        h1 { font-size: 2rem; margin-bottom: 0.5rem; }
        .meta { color: #8b949e; margin-bottom: 2rem; font-size: 0.9rem; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1rem; }
        .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 1.25rem; }
        .card-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 0.75rem; }
        .agent-name { font-weight: 600; font-size: 1.1rem; }
        .status { padding: 0.25rem 0.6rem; border-radius: 999px; font-size: 0.75rem; font-weight: 500; text-transform: uppercase; }
        .status.success { background: rgba(35,134,54,0.2); color: #3fb950; }
        .status.failed { background: rgba(218,54,51,0.2); color: #f85149; }
        .status.running { background: rgba(31,111,235,0.2); color: #58a6ff; }
        .status.pending { background: rgba(139,148,158,0.2); color: #8b949e; }
        .status.circuit_open { background: rgba(210,153,34,0.2); color: #d29922; }
        .detail { font-size: 0.85rem; color: #8b949e; margin-top: 0.5rem; line-height: 1.5; }
        .detail strong { color: var(--text); }
        .history { margin-top: 0.75rem; font-size: 0.8rem; }
        .history-item { display: flex; justify-content: space-between; padding: 0.35rem 0; border-bottom: 1px solid var(--border); }
        .history-item:last-child { border-bottom: none; }
        .chain-list { margin-top: 1rem; padding-top: 1rem; border-top: 1px solid var(--border); }
        .chain-item { font-size: 0.85rem; color: #8b949e; padding: 0.25rem 0; }
        .refresh { position: fixed; top: 1rem; right: 1rem; background: var(--card); border: 1px solid var(--border); padding: 0.5rem 1rem; border-radius: 8px; font-size: 0.85rem; color: var(--text); }
        .error-banner { background: rgba(218,54,51,0.1); border: 1px solid rgba(218,54,51,0.3); padding: 1rem; border-radius: 8px; margin-bottom: 1rem; }
        .ok-banner { background: rgba(35,134,54,0.1); border: 1px solid rgba(35,134,54,0.3); padding: 1rem; border-radius: 8px; margin-bottom: 1rem; }
    </style>
</head>
<body>
    <h1>🛡️ Sentinel Dashboard</h1>
    <p class="meta">v1.2.0 • Auto-refresh every 30s</p>
    <div id="content">Loading...</div>
    <div class="refresh">Last updated: <span id="ts">--</span></div>

    <script>
    async function load() {
        try {
            const [status, history] = await Promise.all([
                fetch('/api/sentinel/status').then(r => r.json()),
                fetch('/api/sentinel/runs/history?limit=20').then(r => r.json())
            ]);
            render(status, history);
        } catch(e) {
            document.getElementById('content').innerHTML = '<div class="error-banner">Failed to load: ' + e.message + '</div>';
        }
        document.getElementById('ts').textContent = new Date().toLocaleTimeString();
    }

    function render(status, history) {
        const failures = status.agents.filter(a => a.status === 'failed' || a.status === 'circuit_open').length;
        let banner = failures > 0
            ? `<div class="error-banner">⚠️ ${failures} agent(s) need attention</div>`
            : `<div class="ok-banner">✅ All agents operational</div>`;

        let html = banner + '<div class="grid">';
        status.agents.forEach(a => {
            const runs = history.runs.filter(r => r.agent_id === a.id).slice(0, 5);
            let runsHtml = runs.map(r => {
                const emoji = r.status === 'success' ? '✅' : '❌';
                return `<div class="history-item"><span>${emoji} ${r.started_at?.slice(11,16)}</span><span>${r.elapsed_seconds}s</span></div>`;
            }).join('');

            let outputs = a.last_outputs && Object.keys(a.last_outputs).length > 0
                ? '<br><strong>Last outputs:</strong> ' + JSON.stringify(a.last_outputs).slice(0, 120)
                : '';

            html += `
                <div class="card">
                    <div class="card-header">
                        <span class="agent-name">${a.name}</span>
                        <span class="status ${a.status}">${a.status}</span>
                    </div>
                    <div class="detail">
                        <strong>Role:</strong> ${a.role}<br>
                        <strong>Last run:</strong> ${a.last_run_at ? a.last_run_at.slice(0,16).replace('T',' ') : 'never'}<br>
                        <strong>Duration:</strong> ${a.last_run_duration ?? '--'}s
                        ${a.retry_count ? `<br><strong>Retries:</strong> ${a.retry_count}` : ''}
                        ${outputs}
                    </div>
                    <div class="history">${runsHtml || '<span style="color:#555">No recent runs</span>'}</div>
                </div>
            `;
        });
        html += '</div>';

        if (status.chains && status.chains.length) {
            html += `<div class="card chain-list"><div class="agent-name">🔗 Dependency Chains</div>`;
            status.chains.forEach(c => html += `<div class="chain-item">${c}</div>`);
            html += '</div>';
        }

        document.getElementById('content').innerHTML = html;
    }

    load();
    setInterval(load, 30000);
    </script>
</body>
</html>
"""

if api_app:
    @api_app.route("/api/sentinel/health", methods=["GET"])
    def health():
        return jsonify({"status": "healthy", "version": "1.2.0", "timestamp": datetime.utcnow().isoformat()})

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
                "circuit_open": agent_state.get("circuit_open", False),
                "failure_count": agent_state.get("failure_count", 0),
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
                "circuit_open": agent_state.get("circuit_open", False),
            })
        return jsonify({
            "orchestrator": "Sentinel",
            "version": "1.2.0",
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
            "alerts": {k: v for k, v in ALERT_CONFIG.items() if "token" not in k and "webhook" not in k},
            "circuit_breaker": CIRCUIT_CONFIG,
        })

    @api_app.route("/dashboard", methods=["GET"])
    def dashboard():
        from flask import Response
        return Response(DASHBOARD_HTML, mimetype="text/html")

    @api_app.route("/api/sentinel/digest/trigger", methods=["POST"])
    def trigger_digest():
        """Manual trigger for daily digest (for testing)."""
        asyncio.create_task(SentinelAlert.send_digest())
        return jsonify({"status": "digest_triggered"})


# ═══════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════

async def main():
    parser = argparse.ArgumentParser(description="Sentinel — AI Agency Orchestrator v1.2.0")
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

    digest_parser = subparsers.add_parser("digest", help="Send daily digest now")

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

    elif args.command == "digest":
        await SentinelAlert.send_digest()

    else:
        parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
