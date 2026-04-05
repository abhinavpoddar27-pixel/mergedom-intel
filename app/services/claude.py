"""
Claude API intelligence service.

The central analysis layer — all briefs, recommendations, email parsing,
and thread classification flow through here.  Uses Sonnet for deep
analysis (daily briefs, budget, creative, waterfall) and Haiku for
fast parsing (emails, Slack threads, concept tagging).

Includes rate limiting (60 req/min for Sonnet), response caching
(1-hour TTL for identical inputs), and token-usage cost logging.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import date, datetime, timezone
from typing import Any, Optional

import anthropic

from app.config import get_settings
from app.models.schemas import (
    BudgetRecommendation,
    CreativeBrief,
    CreativeStatus,
    DailyBrief,
)

logger = logging.getLogger(__name__)

# ── Natasha's role context (shared system prompt prefix) ─────────────────────

_SYSTEM_CONTEXT = (
    "You are an AI analyst for Natasha, a UA & Monetization Manager "
    "at Mergedom (merge-genre F2P mobile game). She manages campaigns "
    "across Google Ads, Meta, AppLovin, and incentivized networks "
    "(TapJoy, AdJoy, FreeCash). She tracks ROAS D1/D7/D30, CPI, "
    "IPM, CTR. She also manages ad monetization via MAX mediation. "
    "Generate concise, actionable briefs. Use INR currency where "
    "applicable, USD for ad spend. Rank actions by predicted impact. "
    "Flag consecutive declines."
)

# ── Rate limiter ─────────────────────────────────────────────────────────────

_SONNET_RPM = 60
_RATE_WINDOW = 60.0  # seconds


class _RateLimiter:
    """Simple sliding-window rate limiter (thread-safe)."""

    def __init__(self, max_requests: int, window_secs: float) -> None:
        self._max = max_requests
        self._window = window_secs
        self._timestamps: list[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until a slot is available."""
        while True:
            with self._lock:
                now = time.monotonic()
                # Prune old timestamps
                self._timestamps = [
                    t for t in self._timestamps if now - t < self._window
                ]
                if len(self._timestamps) < self._max:
                    self._timestamps.append(now)
                    return
            # Wait a bit before retrying
            time.sleep(0.5)


# ── Response cache ───────────────────────────────────────────────────────────

_CACHE_TTL = 3600  # 1 hour


class _ResponseCache:
    """In-memory cache keyed by prompt hash with TTL eviction."""

    def __init__(self, ttl: int = _CACHE_TTL) -> None:
        self._ttl = ttl
        self._store: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(model: str, system: str, user: str) -> str:
        raw = f"{model}:{system}:{user}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, model: str, system: str, user: str) -> Optional[str]:
        key = self._key(model, system, user)
        with self._lock:
            entry = self._store.get(key)
            if entry and (time.monotonic() - entry[0]) < self._ttl:
                logger.debug("Cache hit for %s", key[:12])
                return entry[1]
            # Expired or missing
            if entry:
                del self._store[key]
        return None

    def put(self, model: str, system: str, user: str, response: str) -> None:
        key = self._key(model, system, user)
        with self._lock:
            self._store[key] = (time.monotonic(), response)


# ── Service ──────────────────────────────────────────────────────────────────

class ClaudeService:
    """Intelligence layer — all Claude API interactions."""

    def __init__(self) -> None:
        cfg = get_settings()
        self._api_key = cfg.anthropic_api_key
        self._model_sonnet = cfg.claude.model_daily
        self._model_haiku = cfg.claude.model_parse

        self._client: Optional[anthropic.Anthropic] = None
        if self._api_key:
            self._client = anthropic.Anthropic(api_key=self._api_key)

        self._sonnet_limiter = _RateLimiter(_SONNET_RPM, _RATE_WINDOW)
        self._cache = _ResponseCache()

        # Cumulative token counters for cost tracking
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._lock = threading.Lock()

    # ── Low-level call ────────────────────────────────────────────────────

    def _call(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 2048,
        use_cache: bool = True,
    ) -> str:
        """Send a message to Claude and return the text response.

        Applies rate limiting (Sonnet only), caching, and logs token usage.
        """
        if not self._client:
            raise ClaudeServiceError("Claude API key not configured")

        # Cache check
        if use_cache:
            cached = self._cache.get(model, system, user)
            if cached is not None:
                return cached

        # Rate limit Sonnet
        if model == self._model_sonnet:
            self._sonnet_limiter.acquire()

        logger.info("Claude API call: model=%s tokens=%d", model, max_tokens)

        resp = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )

        text = resp.content[0].text

        # Log token usage
        in_tok = resp.usage.input_tokens
        out_tok = resp.usage.output_tokens
        with self._lock:
            self._total_input_tokens += in_tok
            self._total_output_tokens += out_tok
        logger.info(
            "Claude response: in=%d out=%d total_in=%d total_out=%d",
            in_tok, out_tok, self._total_input_tokens, self._total_output_tokens,
        )

        if use_cache:
            self._cache.put(model, system, user, text)

        return text

    def _call_json(
        self, model: str, system: str, user: str, max_tokens: int = 2048,
    ) -> dict:
        """Call Claude and parse the response as JSON."""
        text = self._call(model, system, user, max_tokens)
        return _extract_json(text)

    # ── 1. Daily Brief ────────────────────────────────────────────────────

    def generate_daily_brief(self, data: dict) -> DailyBrief:
        """Generate the morning intelligence brief using Sonnet.

        *data* should contain: campaign_metrics, anomalies,
        email_insights, slack_context, feedback_history, creative_health.
        """
        system = (
            f"{_SYSTEM_CONTEXT}\n\n"
            "Generate a daily intelligence brief as JSON with EXACTLY "
            "these keys:\n"
            '- "spend_summary": dict mapping network name to yesterday\'s '
            "spend (number) — include trend arrow in key like "
            '"Meta ↑" or "AppLovin ↓"\n'
            '- "roas_tracker": dict mapping network name to D7 ROAS (number), '
            "flag consecutive declines in key\n"
            '- "action_items": list of strings, each starting with '
            '"[HIGH]", "[MED]", or "[LOW]" priority tag\n'
            '- "inbox_context": list of relevant email summaries from '
            "last 24 hours\n"
            '- "system_confidence": number 0-100 based on data completeness '
            "and feedback history\n"
            "\nReturn ONLY valid JSON."
        )

        user = (
            f"Today: {date.today().isoformat()}\n\n"
            f"Campaign metrics (by network):\n{json.dumps(data.get('campaign_metrics', {}), indent=2, default=str)}\n\n"
            f"Anomalies detected:\n{json.dumps(data.get('anomalies', []), indent=2, default=str)}\n\n"
            f"Email insights (last 24h):\n{json.dumps(data.get('email_insights', []), indent=2, default=str)}\n\n"
            f"Slack context:\n{json.dumps(data.get('slack_context', []), indent=2, default=str)}\n\n"
            f"Feedback history:\n{json.dumps(data.get('feedback_history', {}), indent=2, default=str)}\n\n"
            f"Creative health:\n{json.dumps(data.get('creative_health', {}), indent=2, default=str)}"
        )

        result = self._call_json(
            self._model_sonnet, system, user, max_tokens=2048
        )

        return DailyBrief(
            date=date.today(),
            spend_summary=result.get("spend_summary", {}),
            roas_tracker=result.get("roas_tracker", {}),
            action_items=result.get("action_items", []),
            inbox_context=result.get("inbox_context", []),
            system_confidence=float(result.get("system_confidence", 0)),
            raw_data=data,
        )

    # ── 2. Creative Brief ─────────────────────────────────────────────────

    def generate_creative_brief(self, concept_data: dict) -> CreativeBrief:
        """Generate a creative brief for a fatiguing concept using Sonnet."""
        concept_name = concept_data.get("concept_name", "Unknown")

        system = (
            f"{_SYSTEM_CONTEXT}\n\n"
            "You are a senior creative strategist. Generate a creative "
            "brief as JSON with keys:\n"
            '- "what_worked": string describing elements that performed well\n'
            '- "recommendations": list of 3-5 specific actionable strings\n'
            '- "reference_assets": list of asset descriptions to reference\n'
            '- "status": one of "active", "fatigued", "paused", "retired"\n'
            "\nReturn ONLY valid JSON."
        )

        user = (
            f"Concept: {concept_name}\n"
            f"Platforms: {', '.join(concept_data.get('platforms', []))}\n"
            f"Fatigue score: {concept_data.get('fatigue_score', 0)}/100\n"
            f"Days live avg: {concept_data.get('days_live_avg', 0)}\n"
            f"Current IPM: {concept_data.get('avg_ipm', 0):.2f}\n"
            f"Peak IPM: {concept_data.get('peak_ipm', 0):.2f}\n"
            f"Current CTR: {concept_data.get('avg_ctr', 0):.2f}%\n"
            f"Top variant: {concept_data.get('top_variant', 'N/A')}\n"
        )

        result = self._call_json(self._model_sonnet, system, user, max_tokens=1024)

        status_str = result.get("status", "fatigued")
        try:
            status = CreativeStatus(status_str)
        except ValueError:
            status = CreativeStatus.fatigued

        return CreativeBrief(
            concept_name=concept_name,
            status=status,
            platforms_affected=concept_data.get("platforms", []),
            what_worked=result.get("what_worked", ""),
            recommendations=result.get("recommendations", []),
            reference_assets=result.get("reference_assets", []),
        )

    # ── 3. Email parsing ──────────────────────────────────────────────────

    def parse_email(self, subject: str, body: str, sender: str) -> dict:
        """Parse an email using Haiku.  Returns category, insights, etc."""
        system = (
            "You are a mobile gaming UA analyst. Parse this email and "
            "return JSON with keys:\n"
            '- "category": one of "network_rep", "platform_alert", '
            '"billing", "creative_delivery", "stakeholder_request", "other"\n'
            '- "insights": one-sentence summary\n'
            '- "deadlines": list of deadline strings (or empty list)\n'
            '- "related_campaigns": list of campaign names mentioned\n'
            '- "action_required": boolean\n'
            "\nReturn ONLY valid JSON."
        )

        user = f"From: {sender}\nSubject: {subject}\n\n{body[:3000]}"

        return self._call_json(self._model_haiku, system, user, max_tokens=512)

    # ── 4. Slack thread classification ────────────────────────────────────

    def classify_slack_thread(self, messages: list[str]) -> dict:
        """Classify a Slack thread using Haiku."""
        system = (
            "You are a Slack thread classifier for a mobile gaming UA team. "
            "Analyse the thread and return JSON with keys:\n"
            '- "is_campaign_related": boolean\n'
            '- "campaign_name": string or null\n'
            '- "is_manual_override": boolean (user pausing/stopping something)\n'
            '- "should_suppress_alerts": boolean\n'
            '- "thread_summary": one-sentence summary\n'
            "\nReturn ONLY valid JSON."
        )

        user = "Thread messages:\n" + "\n---\n".join(messages[:20])

        return self._call_json(self._model_haiku, system, user, max_tokens=512)

    # ── 5. Budget allocation ──────────────────────────────────────────────

    def suggest_budget_allocation(
        self, spend_data: dict
    ) -> list[BudgetRecommendation]:
        """Generate budget reallocation recommendations using Sonnet."""
        system = (
            f"{_SYSTEM_CONTEXT}\n\n"
            "Analyse the spend-response data and recommend budget shifts. "
            "Return JSON: a list of objects, each with keys:\n"
            '- "network": string\n'
            '- "current_spend": number\n'
            '- "recommended_spend": number\n'
            '- "current_marginal_roas": number\n'
            '- "projected_marginal_roas": number\n'
            '- "confidence": number 0-1\n'
            "\nReturn ONLY a JSON array."
        )

        user = json.dumps(spend_data, indent=2, default=str)

        text = self._call(self._model_sonnet, system, user, max_tokens=1024)
        parsed = _extract_json(text)

        # parsed might be a list or a dict with a list inside
        items: list[dict] = []
        if isinstance(parsed, list):
            items = parsed
        elif isinstance(parsed, dict):
            for v in parsed.values():
                if isinstance(v, list):
                    items = v
                    break

        return [
            BudgetRecommendation(
                network=str(item.get("network", "")),
                current_spend=float(item.get("current_spend", 0)),
                recommended_spend=float(item.get("recommended_spend", 0)),
                current_marginal_roas=float(item.get("current_marginal_roas", 0)),
                projected_marginal_roas=float(item.get("projected_marginal_roas", 0)),
                confidence=float(item.get("confidence", 0)),
            )
            for item in items
            if isinstance(item, dict)
        ]

    # ── 6. Waterfall suggestion ───────────────────────────────────────────

    def generate_waterfall_suggestion(self, waterfall_data: dict) -> dict:
        """Generate waterfall optimization suggestions using Sonnet."""
        system = (
            f"{_SYSTEM_CONTEXT}\n\n"
            "Analyse the MAX mediation waterfall data and suggest floor "
            "price optimizations. Return JSON with keys:\n"
            '- "suggestions": list of objects with "network", "geo_tier", '
            '"current_floor", "recommended_floor", "rationale", "confidence"\n'
            '- "summary": one-sentence overall assessment\n'
            "\nReturn ONLY valid JSON."
        )

        user = json.dumps(waterfall_data, indent=2, default=str)

        return self._call_json(self._model_sonnet, system, user, max_tokens=1024)

    # ── Token usage report ────────────────────────────────────────────────

    def get_token_usage(self) -> dict[str, int]:
        """Return cumulative token usage counters."""
        with self._lock:
            return {
                "total_input_tokens": self._total_input_tokens,
                "total_output_tokens": self._total_output_tokens,
            }


# ── Exceptions ───────────────────────────────────────────────────────────────

class ClaudeServiceError(Exception):
    """Raised when a Claude API operation fails."""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_json(text: str) -> Any:
    """Extract JSON from Claude's response text.

    Handles responses wrapped in markdown code fences or with leading text.
    """
    # Strip markdown fences
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Remove first line (```json) and last line (```)
        lines = cleaned.split("\n")
        lines = lines[1:]  # drop opening fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    # Try direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Find first { or [ and last } or ]
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start = cleaned.find(start_char)
        end = cleaned.rfind(end_char)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue

    logger.warning("Could not parse JSON from Claude response: %s…", text[:200])
    return {}
