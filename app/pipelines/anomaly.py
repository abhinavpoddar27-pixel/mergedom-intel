"""
Anomaly detection engine.

Analyses campaign and creative metrics against rolling baselines to
surface statistically significant deviations.  Powers all alert
generation across the intelligence system.

Algorithm per (metric, network) pair:
  1. Compute 7-day rolling mean and standard deviation.
  2. Flag when current value deviates beyond a configurable threshold.
  3. Assign severity by deviation magnitude (CRITICAL > 3σ, HIGH > 2σ,
     MEDIUM > 1.5σ).
  4. Detect *consecutive* multi-day declines (≥ 3 days) for ROAS and
     promote severity.

Known patterns from live Mergedom data (suppressed automatically):
  • Weekend spend dips — normal after 3 previously-ignored alerts.
  • AdAction Interactive — wider thresholds (volatile spend).
  • Facebook CPI ~$14 — expected; not flagged.
  • Google AdWords daily spend swings of 20-30% — normal.
"""

from __future__ import annotations

import hashlib
import logging
import statistics
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Optional

from app.config import get_settings
from app.models.schemas import (
    Alert,
    AlertType,
    CampaignMetric,
    CreativeMetric,
    Severity,
)

logger = logging.getLogger(__name__)

# ── Tracked metrics ──────────────────────────────────────────────────────────

_CAMPAIGN_METRICS: list[tuple[str, str, str]] = [
    # (field_name, alert_type_prefix, direction)
    #   direction: "both" = spike+drop, "up" = spike only, "down" = drop only
    ("spend", "spend", "both"),
    ("installs", "spend", "both"),      # re-use spend bucket
    ("cpi", "cpi", "up"),               # only spikes matter
    ("ipm", "spend", "down"),           # only drops matter
    ("ctr", "spend", "down"),
    ("roas_d7", "roas_decline", "down"),
]

_CREATIVE_METRICS: list[tuple[str, str, str]] = [
    ("ipm", "creative_fatigue", "down"),
    ("ctr", "creative_fatigue", "down"),
    ("fatigue_score", "creative_fatigue", "up"),
]

# ── Known-pattern rules ─────────────────────────────────────���────────────────

_VOLATILE_NETWORKS = {"adaction interactive", "adaction"}
_VOLATILE_NETWORK_MULTIPLIER = 2.0          # 2× wider thresholds

_GOOGLE_NORMAL_SPEND_SWING = 0.30           # 30 % daily swing is normal
_FACEBOOK_EXPECTED_CPI = 14.0               # $14 CPI on FB is normal
_FACEBOOK_CPI_TOLERANCE = 5.0               # ± $5 around expected

_WEEKEND_DAYS = {5, 6}                      # Saturday=5, Sunday=6
_WEEKEND_SUPPRESSION_AFTER = 3              # suppress after N ignored

_CONSECUTIVE_DECLINE_DAYS = 3               # flag after 3 days declining


# ── Core class ───────────────────────────────────────────────────────────────

class AnomalyDetector:
    """Detects anomalies in campaign and creative metric time-series."""

    def __init__(self) -> None:
        cfg = get_settings()
        self.threshold = cfg.anomaly_std_dev_threshold  # default 1.5

    # ── Public API ────────────────────────────────────────────────────────

    def detect_campaign_anomalies(
        self,
        current: list[CampaignMetric],
        historical: list[CampaignMetric],
        weights: Optional[dict[str, float]] = None,
    ) -> list[Alert]:
        """Compare today's campaign rows against historical baselines.

        Returns a de-duplicated, ranked list of ``Alert`` objects.
        """
        weights = weights or {}
        alerts: list[Alert] = []

        # Group historical by (network, campaign_id)
        hist_by_key: dict[tuple[str, str], list[CampaignMetric]] = defaultdict(list)
        for row in historical:
            hist_by_key[(row.network, row.campaign_id)].append(row)

        for row in current:
            key = (row.network, row.campaign_id)
            hist = hist_by_key.get(key, [])
            if len(hist) < 3:
                # Not enough data for meaningful stats
                continue

            # Sort historical by date ascending
            hist_sorted = sorted(hist, key=lambda r: r.date or date.min)

            for field, alert_prefix, direction in _CAMPAIGN_METRICS:
                current_val = getattr(row, field, 0.0)
                hist_values = [float(getattr(r, field, 0.0)) for r in hist_sorted]

                alert = self._check_metric(
                    values=hist_values,
                    current=float(current_val),
                    field=field,
                    alert_prefix=alert_prefix,
                    direction=direction,
                    network=row.network,
                    campaign_id=row.campaign_id,
                    campaign_name=row.campaign_name,
                    row_date=row.date,
                )
                if alert is not None:
                    alerts.append(alert)

            # Consecutive ROAS decline
            roas_alert = self._check_consecutive_decline(
                hist_sorted, row, "roas_d7", "roas_decline_consecutive"
            )
            if roas_alert is not None:
                alerts.append(roas_alert)

        # Post-processing pipeline
        alerts = [a for a in alerts if not self._is_known_pattern(a)]
        alerts = self._deduplicate_alerts(alerts)
        alerts = self._apply_learned_weights(alerts, weights)
        alerts = self.rank_alerts(alerts)
        return alerts

    def detect_creative_anomalies(
        self,
        current: list[CreativeMetric],
        historical: list[CreativeMetric],
    ) -> list[Alert]:
        """Detect anomalies in creative-level metrics."""
        alerts: list[Alert] = []

        hist_by_id: dict[str, list[CreativeMetric]] = defaultdict(list)
        for row in historical:
            hist_by_id[row.creative_id].append(row)

        for row in current:
            hist = hist_by_id.get(row.creative_id, [])
            if len(hist) < 3:
                continue

            for field, alert_prefix, direction in _CREATIVE_METRICS:
                current_val = float(getattr(row, field, 0.0))
                hist_values = [float(getattr(r, field, 0.0)) for r in hist]

                alert = self._check_metric(
                    values=hist_values,
                    current=current_val,
                    field=field,
                    alert_prefix=alert_prefix,
                    direction=direction,
                    network=row.platform,
                    campaign_id=row.creative_id,
                    campaign_name=row.concept_tag,
                    row_date=None,
                )
                if alert is not None:
                    alerts.append(alert)

        alerts = self._deduplicate_alerts(alerts)
        alerts = self.rank_alerts(alerts)
        return alerts

    # ── Statistical core ──────────────────────────────────────────────────

    @staticmethod
    def _calculate_rolling_stats(
        values: list[float], window: int = 7
    ) -> tuple[float, float]:
        """Return (mean, stdev) of the last *window* values.

        If fewer than *window* values exist, uses all available.
        Returns ``(0.0, 0.0)`` when fewer than 2 data-points.
        """
        tail = values[-window:] if len(values) >= window else values
        if len(tail) < 2:
            return (0.0, 0.0)
        avg = statistics.mean(tail)
        std = statistics.stdev(tail)
        return (avg, std)

    # ── Metric checker ────────────────────────────────────────────────────

    def _check_metric(
        self,
        values: list[float],
        current: float,
        field: str,
        alert_prefix: str,
        direction: str,
        network: str,
        campaign_id: str,
        campaign_name: str,
        row_date: date | None,
    ) -> Alert | None:
        avg, std = self._calculate_rolling_stats(values)

        if std == 0:
            return None

        # Widen thresholds for volatile networks
        effective_threshold = self.threshold
        if network.lower().strip() in _VOLATILE_NETWORKS:
            effective_threshold *= _VOLATILE_NETWORK_MULTIPLIER

        deviation = (current - avg) / std

        # Direction filter
        if direction == "up" and deviation <= effective_threshold:
            return None
        if direction == "down" and deviation >= -effective_threshold:
            return None
        if direction == "both" and abs(deviation) <= effective_threshold:
            return None

        # Severity mapping
        abs_dev = abs(deviation)
        if abs_dev > 3.0:
            severity = Severity.critical
        elif abs_dev > 2.0:
            severity = Severity.high
        elif abs_dev >= 1.5:
            severity = Severity.medium
        else:
            return None  # below minimum threshold

        # Alert type
        if deviation > 0:
            alert_type_str = f"{alert_prefix}_spike"
        else:
            alert_type_str = f"{alert_prefix}_drop"

        # Confidence (base 60, +20 for data depth, +20 for magnitude)
        data_depth_bonus = min(20.0, len(values) / 14 * 20)
        magnitude_bonus = min(20.0, abs_dev / 4.0 * 20)
        confidence = round(60.0 + data_depth_bonus + magnitude_bonus, 1)

        title, body, action = _build_alert_text(
            field=field,
            direction="up" if deviation > 0 else "down",
            current=current,
            avg=avg,
            std_devs=abs_dev,
            network=network,
            campaign_name=campaign_name,
        )

        return Alert(
            alert_id=_make_alert_id(alert_type_str, campaign_id, field, row_date),
            alert_type=_map_alert_type(alert_type_str),
            severity=severity,
            title=title,
            body=f"{body}\n\nRecommended: {action}",
            channel_delivered="",
            timestamp=datetime.now(tz=timezone.utc),
            related_campaign=campaign_id,
            confidence=confidence,
        )

    # ── Consecutive decline detection ─────────────────────────────────────

    def _check_consecutive_decline(
        self,
        hist_sorted: list[CampaignMetric],
        current: CampaignMetric,
        field: str,
        alert_type_str: str,
    ) -> Alert | None:
        """Flag if *field* has declined for ≥ 3 consecutive days."""
        recent = hist_sorted[-(_CONSECUTIVE_DECLINE_DAYS - 1):]
        if len(recent) < _CONSECUTIVE_DECLINE_DAYS - 1:
            return None

        chain = [float(getattr(r, field, 0.0)) for r in recent]
        chain.append(float(getattr(current, field, 0.0)))

        # Check monotonic decline
        for i in range(1, len(chain)):
            if chain[i] >= chain[i - 1]:
                return None

        total_drop_pct = 0.0
        if chain[0] != 0:
            total_drop_pct = abs((chain[-1] - chain[0]) / chain[0]) * 100

        severity = Severity.high if total_drop_pct > 30 else Severity.medium
        confidence = min(95.0, 70.0 + total_drop_pct / 5)

        return Alert(
            alert_id=_make_alert_id(
                alert_type_str, current.campaign_id, field, current.date
            ),
            alert_type=AlertType.anomaly,
            severity=severity,
            title=f"{field.upper()} declining {len(chain)} consecutive days on {current.network}",
            body=(
                f"{current.campaign_name}: {field} has dropped from "
                f"{chain[0]:.4f} → {chain[-1]:.4f} ({total_drop_pct:.1f}% total) "
                f"over {len(chain)} consecutive days.\n\n"
                f"Recommended: Investigate root cause immediately — "
                f"check bid changes, audience saturation, or creative fatigue."
            ),
            channel_delivered="",
            timestamp=datetime.now(tz=timezone.utc),
            related_campaign=current.campaign_id,
            confidence=round(confidence, 1),
        )

    # ── Known-pattern suppression ───────────────────────��─────────────────

    @staticmethod
    def _is_known_pattern(alert: Alert) -> bool:
        """Return True if the alert matches a known benign pattern."""
        campaign = alert.related_campaign.lower()
        title_lower = alert.title.lower()
        body_lower = alert.body.lower()

        # Weekend spend dip
        now = datetime.now(tz=timezone.utc)
        if now.weekday() in _WEEKEND_DAYS:
            if "spend" in title_lower and "drop" in title_lower:
                logger.debug("Suppressing weekend spend dip: %s", alert.alert_id)
                return True

        # Facebook expected CPI
        if "facebook" in body_lower or "meta" in body_lower:
            if "cpi" in title_lower and "spike" in title_lower:
                # Suppress if the body mentions a value near expected
                try:
                    # Try to extract current value from body
                    for token in alert.body.split():
                        token_clean = token.strip("$,")
                        try:
                            val = float(token_clean)
                            if abs(val - _FACEBOOK_EXPECTED_CPI) <= _FACEBOOK_CPI_TOLERANCE:
                                logger.debug(
                                    "Suppressing expected Facebook CPI ($%.2f): %s",
                                    val, alert.alert_id,
                                )
                                return True
                        except ValueError:
                            continue
                except Exception:
                    pass

        # Google Ads normal spend fluctuation
        if "google" in body_lower or "adwords" in body_lower:
            if "spend" in title_lower:
                # Check if it's just a medium-severity spend swing
                if alert.severity == Severity.medium:
                    logger.debug(
                        "Suppressing normal Google Ads spend swing: %s",
                        alert.alert_id,
                    )
                    return True

        return False

    # ── Learned-weight adjustment ─────────────────────────────────────────

    @staticmethod
    def _apply_learned_weights(
        alerts: list[Alert], weights: dict[str, float]
    ) -> list[Alert]:
        """Scale confidence by the learned weight for each alert type.

        Weights > 1.0 boost, < 1.0 dampen.  Default weight is 1.0.
        """
        if not weights:
            return alerts

        adjusted: list[Alert] = []
        for alert in alerts:
            w = weights.get(alert.alert_type.value, 1.0)
            new_conf = min(100.0, alert.confidence * w)
            adjusted.append(alert.model_copy(update={"confidence": round(new_conf, 1)}))
        return adjusted

    # ── De-duplication ────────────────────────────────────────────────────

    @staticmethod
    def _deduplicate_alerts(alerts: list[Alert]) -> list[Alert]:
        """Keep the highest-severity alert per (campaign, type, signal).

        The *signal* discriminator is derived from the title so that,
        e.g., a z-score ROAS drop and a consecutive ROAS decline are
        treated as distinct signals for the same campaign.
        """
        severity_order = {
            Severity.critical: 4,
            Severity.high: 3,
            Severity.medium: 2,
            Severity.low: 1,
        }

        def _signal(a: Alert) -> str:
            t = a.title.lower()
            if "consecutive" in t:
                return "consecutive"
            return "zscore"

        best: dict[tuple[str, str, str], Alert] = {}
        for alert in alerts:
            key = (alert.related_campaign, alert.alert_type.value, _signal(alert))
            existing = best.get(key)
            if existing is None or severity_order.get(
                alert.severity, 0
            ) > severity_order.get(existing.severity, 0):
                best[key] = alert
        return list(best.values())

    # ── Ranking ───────────────────────────────────────────────────────────

    @staticmethod
    def rank_alerts(alerts: list[Alert]) -> list[Alert]:
        """Sort alerts by severity (desc), then confidence (desc)."""
        severity_order = {
            Severity.critical: 4,
            Severity.high: 3,
            Severity.medium: 2,
            Severity.low: 1,
        }
        return sorted(
            alerts,
            key=lambda a: (severity_order.get(a.severity, 0), a.confidence),
            reverse=True,
        )


# ── Helper functions ─────────────────────────────────────────────────────────

def _make_alert_id(
    alert_type: str, campaign_id: str, field: str, d: date | None
) -> str:
    date_str = d.isoformat() if d else "nodate"
    raw = f"{alert_type}:{campaign_id}:{field}:{date_str}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _map_alert_type(type_str: str) -> AlertType:
    """Map a free-form alert type string to the AlertType enum."""
    s = type_str.lower()
    if "creative" in s or "fatigue" in s:
        return AlertType.creative_fatigue
    if "budget" in s or "pacing" in s:
        return AlertType.budget
    if "waterfall" in s or "fill_rate" in s or "ecpm" in s:
        return AlertType.waterfall
    return AlertType.anomaly


def _build_alert_text(
    field: str,
    direction: str,
    current: float,
    avg: float,
    std_devs: float,
    network: str,
    campaign_name: str,
) -> tuple[str, str, str]:
    """Return (title, body, recommended_action)."""
    arrow = "↑" if direction == "up" else "↓"
    pct_change = ((current - avg) / avg * 100) if avg != 0 else 0.0

    title = f"{field.upper()} {arrow} {abs(pct_change):.0f}% on {network}"

    body = (
        f"{campaign_name}: {field} is {current:.4f} vs 7-day avg "
        f"{avg:.4f} ({abs(std_devs):.1f}σ {direction})."
    )

    if "roas" in field and direction == "down":
        action = (
            "Review bid strategy and audience targeting. Consider "
            "pausing lowest-ROAS ad sets and shifting budget to "
            "top performers."
        )
    elif "cpi" in field and direction == "up":
        action = (
            "Check for audience saturation or creative fatigue. "
            "Refresh creatives and review targeting overlap with "
            "other campaigns."
        )
    elif "spend" in field and direction == "up":
        action = (
            "Verify this is intentional budget increase. Check for "
            "runaway bids or campaign duplication."
        )
    elif "spend" in field and direction == "down":
        action = (
            "Check for delivery issues — ad account limits, "
            "disapproved ads, or audience exhaustion."
        )
    elif direction == "down":
        action = (
            f"Investigate declining {field} — check creative "
            f"freshness, bid competitiveness, and audience health."
        )
    else:
        action = (
            f"Review the {field} spike — confirm it reflects genuine "
            f"performance and not a tracking or attribution anomaly."
        )

    return title, body, action
