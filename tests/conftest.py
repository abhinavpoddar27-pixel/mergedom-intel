"""
Shared pytest fixtures for the Mergedom Intel test suite.

Provides realistic mock data based on actual Mergedom game metrics:
- AppLovin ~$3,800/day, CPI $0.94
- Google Ads ~$260/day, CPI $1.07
- Facebook ~$380/day, CPI $14.05
- TapJoy/AdAction: volatile incentivised traffic
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

import pytest

from app.models.schemas import (
    Alert,
    AlertType,
    CampaignMetric,
    CreativeMetric,
    FeedbackEntry,
    Severity,
    WaterfallInstance,
)


# ── Campaign data (14 days, realistic Mergedom patterns) ─────────────────────

def _campaign(
    network: str, day_offset: int,
    spend: float, installs: int, cpi: float,
    roas_d7: float = 0.0, revenue_d7: float = 0.0,
) -> CampaignMetric:
    d = date.today() - timedelta(days=day_offset)
    impressions = int(spend * 200)
    clicks = int(impressions * 0.03)
    ipm = installs / impressions * 1000 if impressions > 0 else 0
    ctr = clicks / impressions * 100 if impressions > 0 else 0
    return CampaignMetric(
        date=d, network=network, campaign_id=f"c_{network}_{d}",
        campaign_name=f"Mergedom {network.title()} iOS",
        os="ios", country="US",
        spend=spend, installs=installs, impressions=impressions, clicks=clicks,
        cpi=round(cpi, 2), ipm=round(ipm, 2), ctr=round(ctr, 4),
        roas_d1=roas_d7 * 0.3, roas_d7=roas_d7, roas_d30=roas_d7 * 1.8,
        revenue_d1=revenue_d7 * 0.3, revenue_d7=revenue_d7, revenue_d30=revenue_d7 * 1.8,
    )


@pytest.fixture
def mock_campaign_data() -> list[CampaignMetric]:
    """14 days of realistic campaign data across 4 networks."""
    rows: list[CampaignMetric] = []
    random.seed(42)

    for day in range(14, 0, -1):
        noise = 1.0 + random.uniform(-0.1, 0.1)

        # AppLovin: high spend, low CPI, efficient
        rows.append(_campaign("applovin", day,
            spend=round(3800 * noise, 2), installs=round(4040 * noise),
            cpi=round(0.94 * (1 + random.uniform(-0.05, 0.05)), 2)))

        # Google Ads: moderate spend, moderate CPI
        rows.append(_campaign("google", day,
            spend=round(260 * noise, 2), installs=round(243 * noise),
            cpi=round(1.07 * (1 + random.uniform(-0.08, 0.08)), 2)))

        # Facebook/Meta: high CPI
        rows.append(_campaign("meta", day,
            spend=round(380 * noise, 2), installs=round(27 * noise),
            cpi=round(14.05 * (1 + random.uniform(-0.1, 0.1)), 2)))

        # TapJoy: volatile incentivised
        vol = 1.0 + random.uniform(-0.3, 0.3)
        rows.append(_campaign("tapjoy", day,
            spend=round(150 * vol, 2), installs=round(500 * vol),
            cpi=round(0.30 * (1 + random.uniform(-0.15, 0.15)), 2)))

    return rows


# ── Creative data (with known fatigue patterns) ──────────────────────────────

@pytest.fixture
def mock_creative_data() -> list[CreativeMetric]:
    """Creatives including healthy, fatiguing, and critical patterns."""
    return [
        # Healthy: stable high IPM
        CreativeMetric(creative_id="cr_healthy_1", concept_tag="Playable/Merge+Kitchen",
            platform="meta", ipm=4.2, ctr=3.1, roas_d7=1.8,
            spend=500, impressions=120000, installs=504, days_live=7, fatigue_score=0),
        CreativeMetric(creative_id="cr_healthy_2", concept_tag="Playable/Merge+Garden",
            platform="applovin", ipm=3.8, ctr=2.9, roas_d7=1.6,
            spend=400, impressions=100000, installs=380, days_live=10, fatigue_score=0),

        # Fatiguing: declining IPM, 18 days live
        CreativeMetric(creative_id="cr_fatigue_1", concept_tag="Banner/UGC_v2",
            platform="meta", ipm=1.5, ctr=1.2, roas_d7=0.9,
            spend=300, impressions=90000, installs=135, days_live=18,
            fatigue_score=0, fatigue_curve_slope=-0.15),
        CreativeMetric(creative_id="cr_fatigue_2", concept_tag="Video/Gameplay+Merge",
            platform="applovin", ipm=1.8, ctr=1.4, roas_d7=0.7,
            spend=250, impressions=80000, installs=144, days_live=21,
            fatigue_score=0, fatigue_curve_slope=-0.12),

        # Critical: very low IPM, 30+ days
        CreativeMetric(creative_id="cr_critical_1", concept_tag="Playable/Merge+Decorate",
            platform="meta", ipm=0.4, ctr=0.3, roas_d7=0.2,
            spend=600, impressions=150000, installs=60, days_live=35,
            fatigue_score=0, fatigue_curve_slope=-0.25),
    ]


@pytest.fixture
def mock_creative_history() -> dict[str, list[CreativeMetric]]:
    """Historical creative data for fatigue calculation."""
    def _declining(cid: str, tag: str, platform: str, peak_ipm: float) -> list[CreativeMetric]:
        return [
            CreativeMetric(creative_id=cid, concept_tag=tag, platform=platform,
                ipm=max(0.2, peak_ipm - 0.3 * d), ctr=max(0.1, 3.0 - 0.15 * d),
                spend=200, impressions=80000, installs=max(10, int((peak_ipm - 0.3 * d) * 80)),
                days_live=d + 1)
            for d in range(10)
        ]

    return {
        "cr_healthy_1": [
            CreativeMetric(creative_id="cr_healthy_1", concept_tag="Playable/Merge+Kitchen",
                platform="meta", ipm=4.0 + i * 0.05, ctr=3.0, spend=500,
                impressions=120000, installs=480, days_live=i + 1)
            for i in range(10)
        ],
        "cr_fatigue_1": _declining("cr_fatigue_1", "Banner/UGC_v2", "meta", 4.0),
        "cr_critical_1": _declining("cr_critical_1", "Playable/Merge+Decorate", "meta", 5.0),
    }


# ── Waterfall data (with optimization opportunities) ─────────────────────────

@pytest.fixture
def mock_waterfall_data() -> list[WaterfallInstance]:
    """Waterfall instances with raise/lower/ok signals."""
    return [
        # eCPM well above floor → should raise
        WaterfallInstance(network="Unity", geo_tier="T1", ad_format="rewarded",
            floor_price=10.0, ecpm=15.0, fill_rate=0.88, show_rate=0.92,
            revenue=450.0, date=date.today() - timedelta(days=1), time_block="8-12"),
        # eCPM below floor → should lower
        WaterfallInstance(network="IronSource", geo_tier="T1", ad_format="interstitial",
            floor_price=12.0, ecpm=9.0, fill_rate=0.45, show_rate=0.85,
            revenue=120.0, date=date.today() - timedelta(days=1), time_block="12-16"),
        # Fill rate very high → possibly too low floor
        WaterfallInstance(network="AppLovin", geo_tier="T2", ad_format="rewarded",
            floor_price=6.0, ecpm=7.0, fill_rate=0.97, show_rate=0.95,
            revenue=280.0, date=date.today() - timedelta(days=1), time_block="16-20"),
        # Within range → ok
        WaterfallInstance(network="TapJoy", geo_tier="T3", ad_format="banner",
            floor_price=2.0, ecpm=2.3, fill_rate=0.78, show_rate=0.88,
            revenue=35.0, date=date.today() - timedelta(days=1), time_block="4-8"),
    ]


# ── Email data ───────────────────────────────────────────────────────────────

@pytest.fixture
def mock_email_data() -> list[dict]:
    """Parsed email data: network rep + platform alert."""
    return [
        {
            "email_id": "e_unity_bonus",
            "sender": "rep@unity.com",
            "subject": "Q2 Spend Bonus Offer — 10% on $50K+",
            "body": "Hi Natasha, we're offering a 10% bonus on Q2 spend above $50K.",
            "date": date.today().isoformat(),
            "category": "network_rep",
            "extracted_insights": "Unity offering 10% bonus on Q2 spend above $50K",
            "related_campaign": "",
        },
        {
            "email_id": "e_google_alert",
            "sender": "google-ads-noreply@google.com",
            "subject": "Campaign budget exhaustion alert",
            "body": "Your campaign 'Mergedom Search iOS' exhausted its daily budget at 3 PM.",
            "date": date.today().isoformat(),
            "category": "platform_alert",
            "extracted_insights": "Google Ads budget exhaustion at 3 PM for Mergedom Search iOS",
            "related_campaign": "Mergedom Search iOS",
        },
        {
            "email_id": "e_meta_policy",
            "sender": "advertiser-support@meta.com",
            "subject": "Upcoming policy change: iOS 18 SKAN updates",
            "body": "Starting July 1, SKAN 5.0 will require updated conversion schemas.",
            "date": date.today().isoformat(),
            "category": "network_rep",
            "extracted_insights": "Meta SKAN 5.0 policy change effective July 1",
            "related_campaign": "",
        },
    ]


# ── Slack context ────────────────────────────────────────────────────────────

@pytest.fixture
def mock_slack_context() -> list[dict]:
    """Slack thread summaries related to campaigns."""
    return [
        {
            "thread_id": "t_meta_pause",
            "channel": "#ua-alerts",
            "summary": "Natasha pausing Meta iOS campaign due to CPI spike to $18",
            "related_campaigns": "Mergedom Meta iOS",
            "date": date.today().isoformat(),
        },
        {
            "thread_id": "t_applovin_scale",
            "channel": "#ua-daily-brief",
            "summary": "Discussion about scaling AppLovin budget to $5K/day",
            "related_campaigns": "Mergedom AppLovin iOS",
            "date": date.today().isoformat(),
        },
    ]


# ── Feedback history (30 days of reactions) ──────────────────────────────────

@pytest.fixture
def mock_feedback_history() -> list[FeedbackEntry]:
    """30 days of Slack reaction feedback across alert types."""
    entries: list[FeedbackEntry] = []
    random.seed(99)

    alert_types = ["anomaly", "creative_fatigue", "budget", "waterfall"]
    reactions = ["useful", "useful", "useful", "noted", "not_useful"]  # 60% useful

    for day in range(30, 0, -1):
        ts = datetime.now(tz=timezone.utc) - timedelta(days=day)
        for atype in alert_types:
            # anomaly: mostly useful; creative_fatigue: mixed; budget: useful; waterfall: often ignored
            if atype == "anomaly":
                reaction = random.choice(["useful", "useful", "useful", "noted"])
            elif atype == "waterfall":
                reaction = random.choice(["not_useful", "not_useful", "noted", "useful"])
            else:
                reaction = random.choice(reactions)

            entries.append(FeedbackEntry(
                alert_id=f"a_{atype}_{day}",
                reaction=reaction,
                timestamp=ts,
                action_taken="",
            ))

    return entries


# ── Alert fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def sample_critical_alert() -> Alert:
    return Alert(
        alert_id="alert_critical_1",
        alert_type=AlertType.anomaly,
        severity=Severity.critical,
        title="SPEND ↑ 85% on meta",
        body="Meta spend spiked to $700 vs $380 avg (3.5σ).",
        related_campaign="c_meta",
        confidence=92.0,
        timestamp=datetime.now(tz=timezone.utc),
    )


@pytest.fixture
def sample_high_alert() -> Alert:
    return Alert(
        alert_id="alert_high_1",
        alert_type=AlertType.anomaly,
        severity=Severity.high,
        title="ROAS ↓ 35% on google",
        body="Google D7 ROAS dropped from 1.5 to 0.95.",
        related_campaign="c_google",
        confidence=80.0,
        timestamp=datetime.now(tz=timezone.utc),
    )


@pytest.fixture
def sample_medium_alert() -> Alert:
    return Alert(
        alert_id="alert_medium_1",
        alert_type=AlertType.anomaly,
        severity=Severity.medium,
        title="CPI ↑ 12% on applovin",
        body="AppLovin CPI rose from $0.94 to $1.05.",
        related_campaign="c_applovin",
        confidence=65.0,
        timestamp=datetime.now(tz=timezone.utc),
    )
