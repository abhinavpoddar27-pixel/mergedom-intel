"""
Module 2 — Creative Performance Analyzer.

Computes per-creative fatigue scores, groups creatives into concepts,
detects concept-level fatigue, and generates actionable creative briefs
via Claude.

Fatigue score (0-100):
    IPM decline rate     × 0.40
  + CTR decline rate     × 0.30
  + days-since-peak age  × 0.15
  + spend concentration  × 0.15

Thresholds:
    0-30   Healthy  (green)  — no action
   31-60   Monitor  (yellow) — weekly summary
   61-80   Fatiguing (orange) — daily brief
   81-100  Critical (red)    — immediate Slack + Gmail escalation
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from typing import Any, Optional

import anthropic

from app.config import get_settings
from app.models.schemas import CreativeBrief, CreativeMetric, CreativeStatus

logger = logging.getLogger(__name__)

# ── Thresholds ───────────────────────────────────────────────────────────────

HEALTHY_MAX = 30
MONITOR_MAX = 60
FATIGUING_MAX = 80
# > 80 = critical

SPEND_CONCENTRATION_THRESHOLD = 0.30  # 30 %


# ── Concept Tagger ───────────────────────────────────────────────────────────

# Known creative-name patterns from Mergedom Singular data.
# Format: "Category/SubType+Variant" e.g. "Playable/Merge+Decorate"
_NAME_PATTERN = re.compile(
    r"^(?P<category>[^/]+)/(?P<concept>[^+]+)(?:\+(?P<variant>.+))?$"
)


class ConceptTagger:
    """Groups creatives into concepts by name pattern and (optionally) Claude."""

    def __init__(self) -> None:
        cfg = get_settings()
        self._api_key = cfg.anthropic_api_key
        self._model = cfg.claude.model_parse  # haiku for fast tagging

    # ── Pattern-based grouping ────────────────────────────────────────────

    @staticmethod
    def tag_by_pattern(name: str) -> str:
        """Extract concept tag from a Singular creative name.

        Examples:
            "Playable/Merge+Decorate"  → "Playable/Merge"
            "Banner/UGC_v1"            → "Banner/UGC"
            "some_unknown_name_v3"     → "some_unknown_name"
        """
        m = _NAME_PATTERN.match(name.strip())
        if m:
            concept = m.group("concept")
            # Strip trailing version suffixes from the concept part
            concept = re.sub(r"[_\s]?v\d+.*$", "", concept, flags=re.IGNORECASE)
            concept = re.sub(r"[_\s]?\d+$", "", concept)
            return f"{m.group('category')}/{concept}" if concept else m.group("category")

        # Fallback: strip trailing version/variant suffixes
        stripped = re.sub(r"[_\s]?v\d+.*$", "", name, flags=re.IGNORECASE)
        stripped = re.sub(r"[_\s]?\d+$", "", stripped)
        return stripped if stripped else "unknown"

    # ─��� Claude-assisted tagging ───────────────────────────────────────────

    def tag_with_claude(self, names: list[str]) -> dict[str, str]:
        """Use Claude Haiku to classify a batch of creative names into concepts.

        Returns ``{creative_name: concept_tag}``.  Falls back to
        pattern-based tagging if the API is unavailable.
        """
        if not self._api_key or not names:
            return {n: self.tag_by_pattern(n) for n in names}

        prompt = (
            "You are a mobile gaming UA creative analyst.  Given the "
            "following list of ad creative names, group them into concept "
            "families.  Return ONLY valid JSON: {\"creative_name\": "
            "\"concept_tag\", ...}.  Keep concept tags short (2-3 words)."
            "\n\nCreative names:\n" + "\n".join(f"- {n}" for n in names)
        )
        try:
            client = anthropic.Anthropic(api_key=self._api_key)
            resp = client.messages.create(
                model=self._model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text
            # Extract JSON from response
            start = text.index("{")
            end = text.rindex("}") + 1
            return json.loads(text[start:end])
        except Exception as exc:
            logger.warning("Claude tagging failed, falling back to patterns: %s", exc)
            return {n: self.tag_by_pattern(n) for n in names}

    # ── Batch tagging ─────────────────────────────────────────────────────

    def tag_all(
        self, creatives: list[CreativeMetric], use_claude: bool = False
    ) -> dict[str, str]:
        """Return ``{creative_id: concept_tag}`` for every creative.

        Tries pattern-based tagging first; sends unresolved names to
        Claude if ``use_claude`` is True.
        """
        result: dict[str, str] = {}
        unresolved: list[str] = []

        for cr in creatives:
            tag = self.tag_by_pattern(cr.concept_tag or cr.creative_id)
            if tag == "unknown":
                unresolved.append(cr.concept_tag or cr.creative_id)
            result[cr.creative_id] = tag

        if use_claude and unresolved:
            claude_tags = self.tag_with_claude(unresolved)
            # Merge Claude results back
            name_to_id: dict[str, str] = {}
            for cr in creatives:
                name_to_id[cr.concept_tag or cr.creative_id] = cr.creative_id
            for name, tag in claude_tags.items():
                cid = name_to_id.get(name)
                if cid:
                    result[cid] = tag

        return result


# ── Brief Generator ──────────────────────────────────────────────────────────

class CreativeBriefGenerator:
    """Generates structured creative briefs using Claude Sonnet."""

    def __init__(self) -> None:
        cfg = get_settings()
        self._api_key = cfg.anthropic_api_key
        self._model = cfg.claude.model_daily  # sonnet for deep analysis

    def generate(
        self,
        concept_name: str,
        concept_data: dict[str, Any],
    ) -> CreativeBrief:
        """Generate a ``CreativeBrief`` for a fatiguing concept.

        *concept_data* should contain keys: platforms, metrics, top_variant,
        fatigue_score, days_live_avg.  Falls back to a rule-based brief
        when the API is unavailable.
        """
        platforms = concept_data.get("platforms", [])
        fatigue_score = concept_data.get("fatigue_score", 0.0)
        status = _score_to_status(fatigue_score)

        if not self._api_key:
            return self._rule_based_brief(concept_name, concept_data, status)

        prompt = self._build_prompt(concept_name, concept_data)
        try:
            client = anthropic.Anthropic(api_key=self._api_key)
            resp = client.messages.create(
                model=self._model,
                max_tokens=1024,
                system=(
                    "You are a senior mobile gaming UA creative strategist. "
                    "Generate a concise, actionable creative brief in JSON "
                    "with keys: what_worked (string), recommendations "
                    "(list of strings), reference_assets (list of strings)."
                ),
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text
            start = text.index("{")
            end = text.rindex("}") + 1
            data = json.loads(text[start:end])

            return CreativeBrief(
                concept_name=concept_name,
                status=status,
                platforms_affected=platforms,
                what_worked=str(data.get("what_worked", "")),
                recommendations=data.get("recommendations", []),
                reference_assets=data.get("reference_assets", []),
            )
        except Exception as exc:
            logger.warning("Claude brief generation failed: %s", exc)
            return self._rule_based_brief(concept_name, concept_data, status)

    @staticmethod
    def _build_prompt(concept_name: str, data: dict) -> str:
        return (
            f"Creative concept '{concept_name}' is showing fatigue.\n\n"
            f"Platforms: {', '.join(data.get('platforms', []))}\n"
            f"Average fatigue score: {data.get('fatigue_score', 0):.0f}/100\n"
            f"Average days live: {data.get('days_live_avg', 0):.0f}\n"
            f"Top performing variant: {data.get('top_variant', 'N/A')}\n"
            f"Current IPM: {data.get('avg_ipm', 0):.2f}\n"
            f"Current CTR: {data.get('avg_ctr', 0):.2f}%\n"
            f"Peak IPM: {data.get('peak_ipm', 0):.2f}\n\n"
            "Based on this data, generate a creative brief with:\n"
            "1. What elements of this concept worked well\n"
            "2. 3-5 specific recommendations for new variations\n"
            "3. Platform-specific guidance if applicable\n"
        )

    @staticmethod
    def _rule_based_brief(
        concept_name: str,
        data: dict,
        status: CreativeStatus,
    ) -> CreativeBrief:
        """Deterministic brief when Claude is unavailable."""
        platforms = data.get("platforms", [])
        recommendations: list[str] = []

        if data.get("fatigue_score", 0) > 80:
            recommendations.append(
                "Pause lowest-performing variations immediately."
            )
        recommendations.append(
            "Test new hook styles — try problem/solution or UGC format."
        )
        recommendations.append(
            "Refresh visual elements while keeping proven gameplay loop."
        )
        if "applovin" in [p.lower() for p in platforms]:
            recommendations.append(
                "For AppLovin: playable ads outperform video — prioritise."
            )

        return CreativeBrief(
            concept_name=concept_name,
            status=status,
            platforms_affected=platforms,
            what_worked=f"Concept '{concept_name}' performed well initially.",
            recommendations=recommendations,
            reference_assets=[],
        )


# ── Slack Block Kit formatter ────────────────────────────────────────────────

def format_creative_slack_blocks(brief: CreativeBrief) -> list[dict]:
    """Convert a ``CreativeBrief`` into Slack Block Kit blocks."""
    status_emoji = {
        CreativeStatus.active: ":large_green_circle:",
        CreativeStatus.fatigued: ":large_orange_circle:",
        CreativeStatus.paused: ":double_vertical_bar:",
        CreativeStatus.retired: ":red_circle:",
    }
    emoji = status_emoji.get(brief.status, ":white_circle:")

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{emoji} Creative Brief: {brief.concept_name}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Status:* {brief.status.value.title()}\n"
                    f"*Platforms:* {', '.join(brief.platforms_affected) or 'N/A'}\n"
                    f"*What worked:* {brief.what_worked}"
                ),
            },
        },
    ]

    if brief.recommendations:
        rec_text = "\n".join(f"• {r}" for r in brief.recommendations)
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Recommendations:*\n{rec_text}"},
            }
        )

    return blocks


# ── Email HTML formatter ─────────────────────────────────────────────────────

def format_creative_email_html(brief: CreativeBrief) -> str:
    """Convert a ``CreativeBrief`` into an HTML email body."""
    recs_html = "".join(f"<li>{r}</li>" for r in brief.recommendations)
    assets_html = "".join(f"<li>{a}</li>" for a in brief.reference_assets)

    return (
        f"<h2>Creative Brief: {brief.concept_name}</h2>"
        f"<p><strong>Status:</strong> {brief.status.value.title()}</p>"
        f"<p><strong>Platforms:</strong> "
        f"{', '.join(brief.platforms_affected) or 'N/A'}</p>"
        f"<p><strong>What worked:</strong> {brief.what_worked}</p>"
        f"<h3>Recommendations</h3><ul>{recs_html}</ul>"
        + (f"<h3>Reference Assets</h3><ul>{assets_html}</ul>" if assets_html else "")
    )


# ── Main Analyzer ────────────────────────────────────────────────────────────

class CreativeAnalyzer:
    """Orchestrates creative health analysis, concept tagging, and briefing."""

    def __init__(self) -> None:
        self.tagger = ConceptTagger()
        self.brief_gen = CreativeBriefGenerator()

    # ── Fatigue scoring ───────────────────────────────────────────────────

    @staticmethod
    def calculate_fatigue_score(
        creative: CreativeMetric,
        history: list[CreativeMetric],
    ) -> float:
        """Compute fatigue score (0-100) for a single creative.

        Uses the history to determine peak performance and decline rates.
        """
        if not history:
            return 0.0

        # --- IPM decline (40 %) ---
        ipm_values = [h.ipm for h in history] + [creative.ipm]
        peak_ipm = _rolling_peak(ipm_values, window=3)
        current_ipm = _rolling_avg(ipm_values, window=3)
        ipm_decline = (
            max(0.0, (peak_ipm - current_ipm) / peak_ipm * 100)
            if peak_ipm > 0
            else 0.0
        )

        # --- CTR decline (30 %) ---
        ctr_values = [h.ctr for h in history] + [creative.ctr]
        peak_ctr = _rolling_peak(ctr_values, window=3)
        current_ctr = _rolling_avg(ctr_values, window=3)
        ctr_decline = (
            max(0.0, (peak_ctr - current_ctr) / peak_ctr * 100)
            if peak_ctr > 0
            else 0.0
        )

        # --- Days since peak (15 %) ---
        peak_idx = _peak_index(ipm_values, window=3)
        days_since = len(ipm_values) - 1 - peak_idx
        age_factor = min(days_since, 30) / 30 * 100

        # --- Spend concentration (15 %) ---
        total_spend = sum(h.spend for h in history) + creative.spend
        creative_spend = creative.spend
        concentration = 0.0
        if total_spend > 0:
            ratio = creative_spend / total_spend
            if ratio > SPEND_CONCENTRATION_THRESHOLD:
                concentration = ratio * 100

        score = (
            ipm_decline * 0.40
            + ctr_decline * 0.30
            + age_factor * 0.15
            + concentration * 0.15
        )
        return round(min(100.0, max(0.0, score)), 1)

    # ── Full analysis ─────────────────────────────────────────────────────

    def analyze_creative_health(
        self,
        metrics: list[CreativeMetric],
        history_by_id: Optional[dict[str, list[CreativeMetric]]] = None,
    ) -> dict[str, Any]:
        """Analyse all creatives and return a structured health report.

        Returns a dict with keys:
            scored    — list of (CreativeMetric, fatigue_score) tuples
            concepts  — {concept_tag: {creatives, avg_score, status, …}}
            critical  — list of creatives with score > 80
            monitor   — list of creatives with score 31-60
            healthy   — count of creatives with score ≤ 30
        """
        history_by_id = history_by_id or {}

        scored: list[tuple[CreativeMetric, float]] = []
        for cr in metrics:
            hist = history_by_id.get(cr.creative_id, [])
            fs = self.calculate_fatigue_score(cr, hist)
            scored.append((cr, fs))

        # Tag concepts
        concept_map = self.tagger.tag_all(metrics)

        # Roll up to concept level
        concept_data: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "creatives": [],
                "scores": [],
                "platforms": set(),
                "total_spend": 0.0,
                "ipm_values": [],
                "ctr_values": [],
                "days_live": [],
            }
        )
        for cr, fs in scored:
            tag = concept_map.get(cr.creative_id, "unknown")
            c = concept_data[tag]
            c["creatives"].append(cr.creative_id)
            c["scores"].append(fs)
            c["platforms"].add(cr.platform)
            c["total_spend"] += cr.spend
            c["ipm_values"].append(cr.ipm)
            c["ctr_values"].append(cr.ctr)
            c["days_live"].append(cr.days_live)

        concepts: dict[str, dict[str, Any]] = {}
        for tag, c in concept_data.items():
            avg_score = sum(c["scores"]) / len(c["scores"]) if c["scores"] else 0.0
            concepts[tag] = {
                "creatives": c["creatives"],
                "avg_score": round(avg_score, 1),
                "status": _score_to_status(avg_score).value,
                "platforms": sorted(c["platforms"]),
                "total_spend": round(c["total_spend"], 2),
                "avg_ipm": round(
                    sum(c["ipm_values"]) / len(c["ipm_values"]), 2
                )
                if c["ipm_values"]
                else 0.0,
                "avg_ctr": round(
                    sum(c["ctr_values"]) / len(c["ctr_values"]), 2
                )
                if c["ctr_values"]
                else 0.0,
                "days_live_avg": round(
                    sum(c["days_live"]) / len(c["days_live"]), 0
                )
                if c["days_live"]
                else 0,
            }

        critical = [(cr, fs) for cr, fs in scored if fs > FATIGUING_MAX]
        monitor = [(cr, fs) for cr, fs in scored if HEALTHY_MAX < fs <= MONITOR_MAX]
        healthy_count = sum(1 for _, fs in scored if fs <= HEALTHY_MAX)

        return {
            "scored": scored,
            "concepts": concepts,
            "critical": critical,
            "monitor": monitor,
            "healthy": healthy_count,
        }

    # ── Concept-level brief ───────────────────────────────────────────────

    def generate_creative_brief(
        self, concept_name: str, concept_data: dict
    ) -> CreativeBrief:
        """Proxy to the brief generator."""
        return self.brief_gen.generate(concept_name, concept_data)

    # ── Convenience accessors ─────────────────────────────────────────────

    def tag_concepts(
        self, creatives: list[CreativeMetric], use_claude: bool = False
    ) -> dict[str, str]:
        """Return ``{creative_id: concept_tag}``."""
        return self.tagger.tag_all(creatives, use_claude=use_claude)

    @staticmethod
    def get_top_performers(
        metrics: list[CreativeMetric], n: int = 10
    ) -> list[CreativeMetric]:
        """Return the *n* creatives with the highest IPM."""
        return sorted(metrics, key=lambda m: m.ipm, reverse=True)[:n]

    @staticmethod
    def get_critical_fatigues(
        scored: list[tuple[CreativeMetric, float]],
    ) -> list[tuple[CreativeMetric, float]]:
        """Filter scored list to only critical fatigue (score > 80)."""
        return [(cr, fs) for cr, fs in scored if fs > FATIGUING_MAX]


# ── Private helpers ──────────────────────────────────────────────────────────

def _rolling_avg(values: list[float], window: int = 3) -> float:
    """Average of the last *window* values."""
    tail = values[-window:] if len(values) >= window else values
    return sum(tail) / len(tail) if tail else 0.0


def _rolling_peak(values: list[float], window: int = 3) -> float:
    """Best rolling-*window* average across the whole series."""
    if len(values) < window:
        return max(values) if values else 0.0
    best = 0.0
    for i in range(len(values) - window + 1):
        avg = sum(values[i : i + window]) / window
        if avg > best:
            best = avg
    return best


def _peak_index(values: list[float], window: int = 3) -> int:
    """Index of the start of the best rolling-*window* average."""
    if len(values) < window:
        return 0
    best_idx = 0
    best_avg = 0.0
    for i in range(len(values) - window + 1):
        avg = sum(values[i : i + window]) / window
        if avg > best_avg:
            best_avg = avg
            best_idx = i
    return best_idx


def _score_to_status(score: float) -> CreativeStatus:
    if score <= HEALTHY_MAX:
        return CreativeStatus.active
    if score <= FATIGUING_MAX:
        return CreativeStatus.fatigued
    return CreativeStatus.retired  # critical → treat as needing retirement
