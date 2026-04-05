"""Tests for the creative fatigue analyzer (Module 2)."""

from unittest.mock import MagicMock, patch

import pytest

from app.models.schemas import CreativeBrief, CreativeMetric, CreativeStatus
from app.modules.creative import (
    ConceptTagger,
    CreativeAnalyzer,
    CreativeBriefGenerator,
    _rolling_avg,
    _rolling_peak,
    _peak_index,
    _score_to_status,
    format_creative_email_html,
    format_creative_slack_blocks,
    HEALTHY_MAX,
    MONITOR_MAX,
    FATIGUING_MAX,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _metric(
    creative_id: str = "cr1",
    concept_tag: str = "Playable/Merge",
    platform: str = "meta",
    ipm: float = 3.0,
    ctr: float = 2.5,
    spend: float = 200.0,
    impressions: int = 80_000,
    installs: int = 180,
    fatigue_score: float = 0.0,
    days_live: int = 14,
) -> CreativeMetric:
    return CreativeMetric(
        creative_id=creative_id,
        concept_tag=concept_tag,
        platform=platform,
        ipm=ipm,
        ctr=ctr,
        roas_d7=1.5,
        spend=spend,
        impressions=impressions,
        installs=installs,
        fatigue_score=fatigue_score,
        days_live=days_live,
        fatigue_curve_slope=-0.01,
    )


def _declining_history(
    days: int = 10,
    peak_ipm: float = 5.0,
    peak_ctr: float = 4.0,
    decline_per_day: float = 0.3,
) -> list[CreativeMetric]:
    """Build history with linearly declining IPM and CTR."""
    history = []
    for d in range(days):
        history.append(
            _metric(
                ipm=max(0.1, peak_ipm - decline_per_day * d),
                ctr=max(0.1, peak_ctr - decline_per_day * 0.5 * d),
                days_live=d + 1,
            )
        )
    return history


def _stable_history(days: int = 10, ipm: float = 3.0, ctr: float = 2.5):
    return [_metric(ipm=ipm, ctr=ctr, days_live=d + 1) for d in range(days)]


# ── Rolling helpers ──────────────────────────────────────────────────────────

class TestRollingHelpers:
    def test_rolling_avg_basic(self):
        assert abs(_rolling_avg([1, 2, 3, 4, 5], 3) - 4.0) < 0.01

    def test_rolling_avg_short(self):
        assert abs(_rolling_avg([10.0], 3) - 10.0) < 0.01

    def test_rolling_avg_empty(self):
        assert _rolling_avg([], 3) == 0.0

    def test_rolling_peak_basic(self):
        # Peak 3-day avg = (8+9+10)/3 = 9.0
        vals = [1, 2, 3, 8, 9, 10, 4, 3, 2]
        assert abs(_rolling_peak(vals, 3) - 9.0) < 0.01

    def test_rolling_peak_short(self):
        assert abs(_rolling_peak([7.0], 3) - 7.0) < 0.01

    def test_peak_index_basic(self):
        vals = [1, 2, 10, 9, 8, 1]
        idx = _peak_index(vals, 3)
        assert idx == 2  # window [10, 9, 8] avg = 9.0 is best


# ── Fatigue scoring ──────────────────────────────────────────────────────────

class TestFatigueScoring:
    def test_healthy_creative_low_score(self):
        """Stable metrics → score near 0."""
        analyzer = CreativeAnalyzer()
        creative = _metric(ipm=3.0, ctr=2.5)
        history = _stable_history(days=10, ipm=3.0, ctr=2.5)

        score = analyzer.calculate_fatigue_score(creative, history)
        assert score <= HEALTHY_MAX, f"Expected ≤ {HEALTHY_MAX}, got {score}"

    def test_declining_ipm_raises_score(self):
        """Sharply declining IPM → elevated fatigue score above healthy."""
        analyzer = CreativeAnalyzer()
        history = _declining_history(days=10, peak_ipm=5.0, decline_per_day=0.4)
        # Current is well below peak
        creative = _metric(ipm=0.5, ctr=1.0)

        score = analyzer.calculate_fatigue_score(creative, history)
        assert score > HEALTHY_MAX, f"Expected > {HEALTHY_MAX}, got {score}"

    def test_severe_fatigue_critical_score(self):
        """Massive IPM + CTR decline + old creative + concentration = critical."""
        analyzer = CreativeAnalyzer()
        # History peaked high, now current is almost zero
        history = _declining_history(
            days=30, peak_ipm=8.0, peak_ctr=6.0, decline_per_day=0.25
        )
        for h in history:
            h.spend = 10.0  # low spend in history
        creative = _metric(ipm=0.2, ctr=0.3, days_live=30, spend=500.0)

        score = analyzer.calculate_fatigue_score(creative, history)
        assert score > FATIGUING_MAX, f"Expected > {FATIGUING_MAX}, got {score}"

    def test_no_history_returns_zero(self):
        analyzer = CreativeAnalyzer()
        score = analyzer.calculate_fatigue_score(_metric(), [])
        assert score == 0.0

    def test_spend_concentration_factor(self):
        """If a creative has >30% of total campaign spend, factor kicks in."""
        analyzer = CreativeAnalyzer()
        # History creatives each have spend=10, creative has spend=200
        # Total = 10*5 + 200 = 250; 200/250 = 80% > 30%
        history = [_metric(spend=10.0) for _ in range(5)]
        creative = _metric(ipm=3.0, ctr=2.5, spend=200.0)

        score_concentrated = analyzer.calculate_fatigue_score(creative, history)

        # Now same but with lower concentration
        history_even = [_metric(spend=200.0) for _ in range(5)]
        # Total = 200*5 + 200 = 1200; 200/1200 = 16.7% < 30%
        score_even = analyzer.calculate_fatigue_score(creative, history_even)

        assert score_concentrated > score_even


# ── Concept tagging ──────────────────────────────────────────────────────────

class TestConceptTagging:
    def test_pattern_playable_merge(self):
        tagger = ConceptTagger()
        assert tagger.tag_by_pattern("Playable/Merge+Decorate") == "Playable/Merge"

    def test_pattern_playable_ugc(self):
        tagger = ConceptTagger()
        assert tagger.tag_by_pattern("Playable/UGC+Merge") == "Playable/UGC"

    def test_pattern_banner(self):
        tagger = ConceptTagger()
        assert tagger.tag_by_pattern("Banner/Garden") == "Banner/Garden"

    def test_pattern_with_variant(self):
        tagger = ConceptTagger()
        assert tagger.tag_by_pattern("Video/Gameplay+Kitchen_v2") == "Video/Gameplay"

    def test_fallback_strips_version(self):
        tagger = ConceptTagger()
        assert tagger.tag_by_pattern("UGC_garden_v3") == "UGC_garden"

    def test_fallback_strips_trailing_number(self):
        tagger = ConceptTagger()
        assert tagger.tag_by_pattern("merge_concept_42") == "merge_concept"

    def test_unknown_empty_string(self):
        tagger = ConceptTagger()
        assert tagger.tag_by_pattern("") == "unknown"

    def test_tag_all_pattern_based(self):
        tagger = ConceptTagger()
        creatives = [
            _metric(creative_id="c1", concept_tag="Playable/Merge+Decorate"),
            _metric(creative_id="c2", concept_tag="Playable/Merge+Kitchen"),
            _metric(creative_id="c3", concept_tag="Banner/UGC_v1"),
        ]
        result = tagger.tag_all(creatives)
        assert result["c1"] == "Playable/Merge"
        assert result["c2"] == "Playable/Merge"
        assert result["c3"] == "Banner/UGC"

    def test_tag_with_claude_fallback(self):
        """When API key is empty, falls back to patterns."""
        tagger = ConceptTagger()
        tagger._api_key = ""
        result = tagger.tag_with_claude(["Playable/Merge+Decorate"])
        assert result["Playable/Merge+Decorate"] == "Playable/Merge"


# ── Full analysis ────────────────────────────────────────────────────────────

class TestAnalyzeCreativeHealth:
    def test_returns_required_keys(self):
        analyzer = CreativeAnalyzer()
        metrics = [_metric(creative_id=f"cr{i}") for i in range(5)]
        result = analyzer.analyze_creative_health(metrics)

        assert "scored" in result
        assert "concepts" in result
        assert "critical" in result
        assert "monitor" in result
        assert "healthy" in result

    def test_healthy_creatives_count(self):
        analyzer = CreativeAnalyzer()
        metrics = [_metric(creative_id=f"cr{i}") for i in range(3)]
        result = analyzer.analyze_creative_health(metrics)
        # No history → score=0 → all healthy
        assert result["healthy"] == 3
        assert result["critical"] == []

    def test_concept_rollup(self):
        """Creatives sharing a concept are grouped together."""
        analyzer = CreativeAnalyzer()
        metrics = [
            _metric(creative_id="c1", concept_tag="Playable/Merge+Decorate"),
            _metric(creative_id="c2", concept_tag="Playable/Merge+Kitchen"),
            _metric(creative_id="c3", concept_tag="Banner/UGC_v1"),
        ]
        result = analyzer.analyze_creative_health(metrics)
        concepts = result["concepts"]

        assert "Playable/Merge" in concepts
        assert "Banner/UGC" in concepts
        assert len(concepts["Playable/Merge"]["creatives"]) == 2
        assert len(concepts["Banner/UGC"]["creatives"]) == 1


# ── Top performers & critical fatigues ───────────────────────────────────────

class TestConvenienceAccessors:
    def test_top_performers(self):
        analyzer = CreativeAnalyzer()
        metrics = [
            _metric(creative_id="low", ipm=1.0),
            _metric(creative_id="high", ipm=10.0),
            _metric(creative_id="mid", ipm=5.0),
        ]
        top = analyzer.get_top_performers(metrics, n=2)
        assert len(top) == 2
        assert top[0].creative_id == "high"
        assert top[1].creative_id == "mid"

    def test_critical_fatigues(self):
        analyzer = CreativeAnalyzer()
        scored = [
            (_metric(creative_id="ok"), 25.0),
            (_metric(creative_id="bad"), 85.0),
            (_metric(creative_id="worse"), 92.0),
        ]
        critical = analyzer.get_critical_fatigues(scored)
        assert len(critical) == 2
        ids = {cr.creative_id for cr, _ in critical}
        assert ids == {"bad", "worse"}


# ── Brief generation ─────────────────────────────────────────────────────────

class TestBriefGeneration:
    def test_rule_based_brief_structure(self):
        """Without API key, rule-based brief should be complete."""
        gen = CreativeBriefGenerator()
        gen._api_key = ""  # Force rule-based path

        data = {
            "platforms": ["meta", "applovin"],
            "fatigue_score": 85.0,
            "top_variant": "Merge+Decorate_v2",
            "days_live_avg": 21,
            "avg_ipm": 1.2,
            "avg_ctr": 1.5,
        }
        brief = gen.generate("Playable/Merge", data)

        assert isinstance(brief, CreativeBrief)
        assert brief.concept_name == "Playable/Merge"
        assert brief.status == CreativeStatus.retired  # score > 80
        assert "meta" in brief.platforms_affected
        assert "applovin" in brief.platforms_affected
        assert len(brief.recommendations) >= 2
        assert brief.what_worked != ""

    def test_rule_based_includes_applovin_guidance(self):
        gen = CreativeBriefGenerator()
        gen._api_key = ""

        data = {"platforms": ["applovin"], "fatigue_score": 50.0}
        brief = gen.generate("Test", data)

        has_applovin_tip = any("AppLovin" in r for r in brief.recommendations)
        assert has_applovin_tip

    def test_critical_score_includes_pause_recommendation(self):
        gen = CreativeBriefGenerator()
        gen._api_key = ""

        data = {"platforms": ["meta"], "fatigue_score": 90.0}
        brief = gen.generate("Test", data)

        has_pause = any("pause" in r.lower() for r in brief.recommendations)
        assert has_pause

    def test_analyzer_generate_creative_brief_proxy(self):
        analyzer = CreativeAnalyzer()
        analyzer.brief_gen._api_key = ""

        data = {"platforms": ["meta"], "fatigue_score": 45.0}
        brief = analyzer.generate_creative_brief("TestConcept", data)
        assert isinstance(brief, CreativeBrief)
        assert brief.concept_name == "TestConcept"


# ── Formatters ───────────────────────────────────────────────────────────────

class TestFormatters:
    def _sample_brief(self) -> CreativeBrief:
        return CreativeBrief(
            concept_name="Playable/Merge",
            status=CreativeStatus.fatigued,
            platforms_affected=["meta", "applovin"],
            what_worked="Strong hook in first 3 seconds.",
            recommendations=[
                "Test problem/solution format.",
                "Refresh visuals with new characters.",
            ],
            reference_assets=["asset_001.png"],
        )

    def test_slack_blocks_structure(self):
        brief = self._sample_brief()
        blocks = format_creative_slack_blocks(brief)

        assert isinstance(blocks, list)
        assert len(blocks) >= 2
        # First block is header
        assert blocks[0]["type"] == "header"
        assert "Playable/Merge" in blocks[0]["text"]["text"]
        # Second block has status and what_worked
        assert "Fatigued" in blocks[1]["text"]["text"]

    def test_slack_blocks_include_recommendations(self):
        brief = self._sample_brief()
        blocks = format_creative_slack_blocks(brief)

        rec_block = blocks[-1]
        assert "Recommendations" in rec_block["text"]["text"]
        assert "problem/solution" in rec_block["text"]["text"]

    def test_email_html_structure(self):
        brief = self._sample_brief()
        html = format_creative_email_html(brief)

        assert "<h2>" in html
        assert "Playable/Merge" in html
        assert "Fatigued" in html
        assert "<li>" in html
        assert "problem/solution" in html

    def test_email_html_includes_assets(self):
        brief = self._sample_brief()
        html = format_creative_email_html(brief)
        assert "asset_001.png" in html


# ── Score-to-status mapping ──────────────────────────────────────────────────

class TestScoreToStatus:
    def test_healthy(self):
        assert _score_to_status(0) == CreativeStatus.active
        assert _score_to_status(30) == CreativeStatus.active

    def test_fatigued(self):
        assert _score_to_status(31) == CreativeStatus.fatigued
        assert _score_to_status(80) == CreativeStatus.fatigued

    def test_critical_retired(self):
        assert _score_to_status(81) == CreativeStatus.retired
        assert _score_to_status(100) == CreativeStatus.retired
