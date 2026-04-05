"""
FastAPI application entry-point — Mergedom UA Intelligence.

Boots the API server, registers scheduled jobs via APScheduler,
starts the Slack reaction listener, and exposes endpoints for
health monitoring and manual pipeline triggers.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import BackgroundTasks, FastAPI

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()
scheduler = AsyncIOScheduler(timezone=settings.app_timezone)

# ── Application state ────────────────────────────────────────────────────────

_state: dict[str, Any] = {
    "start_time": None,
    "last_brief": None,
    "last_anomaly_check": None,
    "connected_services": [],
}


# ── Scheduled job functions ──────────────────────────────────────────────────

def daily_brief_job() -> None:
    """Run the full morning intelligence brief pipeline."""
    try:
        from app.modules.daily_brief import DailyBriefModule
        module = DailyBriefModule()
        brief = module.run_daily_brief()
        _state["last_brief"] = datetime.now(tz=timezone.utc).isoformat()
    except Exception as exc:
        logger.error("Daily brief job failed: %s", exc)
        try:
            from app.modules.daily_brief import DailyBriefModule
            DailyBriefModule().handle_brief_failure(exc)
        except Exception:
            logger.critical("Brief failure handler also failed")


def anomaly_check_job() -> None:
    """Run the intraday spend-pacing check."""
    try:
        from app.modules.daily_brief import DailyBriefModule
        DailyBriefModule().run_intraday_check()
        _state["last_anomaly_check"] = datetime.now(tz=timezone.utc).isoformat()
    except Exception as exc:
        logger.error("Intraday check failed: %s", exc)


def creative_fatigue_job() -> None:
    """Run the creative fatigue analysis pipeline."""
    try:
        from app.modules.creative import CreativeAnalyzer
        from app.services.airtable import AirtableService
        airtable = AirtableService()
        metrics = airtable.get_recent_creative_metrics(days=7)
        if metrics:
            CreativeAnalyzer().analyze_creative_health(metrics)
    except Exception as exc:
        logger.error("Creative fatigue job failed: %s", exc)


def waterfall_optimization_job() -> None:
    """Run the waterfall optimization pipeline."""
    try:
        from app.modules.waterfall import WaterfallOptimizer
        WaterfallOptimizer().run_daily_waterfall_review()
    except Exception as exc:
        logger.error("Waterfall optimization failed: %s", exc)


def budget_allocation_job() -> None:
    """Run the budget allocation optimizer."""
    try:
        from app.modules.budget import BudgetOptimizer
        BudgetOptimizer().run_daily_budget_review()
    except Exception as exc:
        logger.error("Budget allocation failed: %s", exc)


def gmail_scan_job() -> None:
    """Periodic Gmail inbox scan for ad-network emails."""
    try:
        from app.services.gmail import GmailService
        svc = GmailService()
        if svc._service:
            emails = svc.scan_inbox(hours=4)
            if emails:
                from app.services.airtable import AirtableService
                airtable = AirtableService()
                for e in emails:
                    airtable.save_email_context(e)
    except Exception as exc:
        logger.error("Gmail scan failed: %s", exc)


def feedback_weekly_job() -> None:
    """Generate and post the weekly accuracy report."""
    try:
        from app.pipelines.feedback import FeedbackProcessor
        FeedbackProcessor().weekly_accuracy_report()
    except Exception as exc:
        logger.error("Weekly feedback report failed: %s", exc)


def escalation_check_job() -> None:
    """Check pending CRITICAL alert escalations."""
    try:
        from app.modules.coordinator import AlertCoordinator
        AlertCoordinator().check_pending_escalations()
    except Exception as exc:
        logger.error("Escalation check failed: %s", exc)


# ── Service connectivity probe ───────────────────────────────────────────────

def _probe_services() -> list[str]:
    """Check which services are reachable.  Returns list of names."""
    connected: list[str] = []
    cfg = get_settings()

    checks = [
        ("Singular", cfg.singular_api_key),
        ("Meta Ads", cfg.meta_access_token),
        ("AppLovin", cfg.applovin_api_key),
        ("Google Ads", cfg.google_ads_developer_token),
        ("MAX Mediation", cfg.max_api_key),
        ("Slack", cfg.slack_bot_token),
        ("Gmail", cfg.gmail_refresh_token),
        ("Airtable", cfg.airtable_api_key),
        ("Claude", cfg.anthropic_api_key),
    ]
    for name, key in checks:
        if key:
            connected.append(name)

    return connected


# ── Lifespan (startup / shutdown) ────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_app: FastAPI):
    _state["start_time"] = time.monotonic()

    tz = settings.app_timezone

    # ── Register scheduled jobs ───────────────────────────────────────────

    # Daily brief: 6:00 AM IST (data pull) merged with 6:35 AM post
    scheduler.add_job(
        daily_brief_job,
        trigger=CronTrigger(
            hour=settings.daily_brief_hour,
            minute=settings.daily_brief_minute,
            timezone=tz,
        ),
        id="daily_brief",
        name="Morning Intelligence Brief",
        replace_existing=True,
    )

    # Intraday anomaly check: every 2 hours 9AM-11PM IST
    scheduler.add_job(
        anomaly_check_job,
        trigger=CronTrigger(
            hour="9-23/2", minute=0, timezone=tz,
        ),
        id="intraday_check",
        name="Intraday Spend Pacing Check",
        replace_existing=True,
    )

    # Creative weekly: Friday 5 PM IST
    scheduler.add_job(
        creative_fatigue_job,
        trigger=CronTrigger(day_of_week="fri", hour=17, minute=0, timezone=tz),
        id="creative_weekly",
        name="Weekly Creative Fatigue Analysis",
        replace_existing=True,
    )

    # Waterfall daily: 7:00 AM IST
    scheduler.add_job(
        waterfall_optimization_job,
        trigger=CronTrigger(hour=7, minute=0, timezone=tz),
        id="waterfall_daily",
        name="Daily Waterfall Optimization",
        replace_existing=True,
    )

    # Budget daily: 7:30 AM IST
    scheduler.add_job(
        budget_allocation_job,
        trigger=CronTrigger(hour=7, minute=30, timezone=tz),
        id="budget_daily",
        name="Daily Budget Allocation Review",
        replace_existing=True,
    )

    # Gmail scan: every 4 hours
    scheduler.add_job(
        gmail_scan_job,
        trigger=IntervalTrigger(hours=4),
        id="gmail_scan",
        name="Gmail Inbox Scan",
        replace_existing=True,
    )

    # Feedback weekly: Sunday 8 PM IST
    scheduler.add_job(
        feedback_weekly_job,
        trigger=CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=tz),
        id="feedback_weekly",
        name="Weekly Feedback Accuracy Report",
        replace_existing=True,
    )

    # Escalation check: every 30 minutes
    scheduler.add_job(
        escalation_check_job,
        trigger=IntervalTrigger(minutes=30),
        id="escalation_check",
        name="Pending Escalation Check",
        replace_existing=True,
    )

    scheduler.start()

    # ── Start Slack listener ──────────────────────────────────────────────
    try:
        from app.services.slack_bot import SlackService
        slack = SlackService()
        slack.start_reaction_listener()
    except Exception:
        logger.warning("Slack reaction listener failed to start")

    # ── Probe services ────────────────────────────────────────────────────
    _state["connected_services"] = _probe_services()

    jobs = scheduler.get_jobs()
    logger.info(
        "Mergedom Intel system online. Connected: %s. Jobs: %d",
        _state["connected_services"],
        len(jobs),
    )

    yield

    scheduler.shutdown(wait=False)
    logger.info("Mergedom Intel shutdown complete")


# ── FastAPI app ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="Mergedom UA Intelligence",
    description="Automated campaign intelligence system for mobile gaming UA & Monetization.",
    version="0.1.0",
    lifespan=lifespan,
)


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    """Return service health status with uptime and last-brief timestamp."""
    uptime_s = time.monotonic() - _state["start_time"] if _state["start_time"] else 0
    hours = int(uptime_s // 3600)
    mins = int((uptime_s % 3600) // 60)

    alerts_today = 0
    try:
        from app.services.airtable import AirtableService
        alerts_today = AirtableService().get_alerts_today()
    except Exception:
        pass

    return {
        "status": "ok",
        "uptime": f"{hours}h {mins}m",
        "last_brief": _state.get("last_brief"),
        "last_anomaly_check": _state.get("last_anomaly_check"),
        "alerts_today": alerts_today,
    }


@app.get("/status")
async def service_status():
    """Return connectivity status of all external services."""
    connected = _probe_services()
    all_services = [
        "Singular", "Meta Ads", "AppLovin", "Google Ads",
        "MAX Mediation", "Slack", "Gmail", "Airtable", "Claude",
    ]
    return {
        "services": {
            svc: {"status": "connected" if svc in connected else "not_configured"}
            for svc in all_services
        },
        "connected_count": len(connected),
        "total_count": len(all_services),
        "scheduler_jobs": [
            {"id": j.id, "name": j.name, "next_run": str(j.next_run_time)}
            for j in scheduler.get_jobs()
        ],
    }


@app.post("/trigger/daily-brief")
async def trigger_daily_brief(background_tasks: BackgroundTasks):
    """Manually trigger the daily brief pipeline."""
    background_tasks.add_task(daily_brief_job)
    return {"status": "triggered", "job": "daily_brief"}


@app.post("/trigger/anomaly-check")
async def trigger_anomaly_check(background_tasks: BackgroundTasks):
    """Manually trigger the anomaly/pacing check."""
    background_tasks.add_task(anomaly_check_job)
    return {"status": "triggered", "job": "anomaly_check"}


@app.post("/trigger/creative-analysis")
async def trigger_creative_analysis(background_tasks: BackgroundTasks):
    """Manually trigger the creative fatigue analysis."""
    background_tasks.add_task(creative_fatigue_job)
    return {"status": "triggered", "job": "creative_analysis"}


@app.get("/metrics")
async def metrics():
    """Prometheus-compatible metrics endpoint."""
    uptime_s = time.monotonic() - _state["start_time"] if _state["start_time"] else 0
    connected = len(_state.get("connected_services", []))
    jobs = len(scheduler.get_jobs())

    alerts_today = 0
    try:
        from app.services.airtable import AirtableService
        alerts_today = AirtableService().get_alerts_today()
    except Exception:
        pass

    lines = [
        "# HELP mergedom_uptime_seconds System uptime in seconds",
        "# TYPE mergedom_uptime_seconds gauge",
        f"mergedom_uptime_seconds {uptime_s:.0f}",
        "# HELP mergedom_connected_services Number of connected services",
        "# TYPE mergedom_connected_services gauge",
        f"mergedom_connected_services {connected}",
        "# HELP mergedom_scheduler_jobs Number of registered scheduler jobs",
        "# TYPE mergedom_scheduler_jobs gauge",
        f"mergedom_scheduler_jobs {jobs}",
        "# HELP mergedom_alerts_today Alerts sent today",
        "# TYPE mergedom_alerts_today gauge",
        f"mergedom_alerts_today {alerts_today}",
    ]

    try:
        from app.services.claude import ClaudeService
        usage = ClaudeService().get_token_usage()
        lines.extend([
            "# HELP mergedom_claude_input_tokens Total Claude input tokens",
            "# TYPE mergedom_claude_input_tokens counter",
            f"mergedom_claude_input_tokens {usage['total_input_tokens']}",
            "# HELP mergedom_claude_output_tokens Total Claude output tokens",
            "# TYPE mergedom_claude_output_tokens counter",
            f"mergedom_claude_output_tokens {usage['total_output_tokens']}",
        ])
    except Exception:
        pass

    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain")
