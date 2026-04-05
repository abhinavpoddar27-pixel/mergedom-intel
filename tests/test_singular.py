"""Tests for the Singular API client."""

import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.schemas import CampaignMetric, CreativeMetric
from app.services.singular import SingularClient, SingularAPIError


class TestConnection:
    def test_client_initialises(self):
        client = SingularClient()
        assert client.api_key is not None
        assert client.revenue_data_available is True

    def test_auth_header(self):
        client = SingularClient()
        client.api_key = "test_key_123"
        headers = client._headers()
        assert headers["Authorization"] == "test_key_123"

    @pytest.mark.asyncio
    async def test_request_sends_auth(self):
        client = SingularClient()
        client.api_key = "my_key"

        import httpx
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok"}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.request = AsyncMock(return_value=mock_resp)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_instance

            result = await client._request("GET", "https://api.singular.net/test")
            call_kwargs = mock_instance.request.call_args
            assert call_kwargs.kwargs["headers"]["Authorization"] == "my_key"


class TestCampaignPull:
    def test_normalize_campaign_row(self):
        client = SingularClient()
        row = {
            "date": "2025-06-01",
            "source": "meta",
            "unified_campaign_id": "c_meta_123",
            "unified_campaign_name": "Mergedom Meta iOS",
            "os": "ios",
            "country_field": "US",
            "adn_cost": "380.00",
            "adn_impressions": "76000",
            "adn_clicks": "2280",
            "adn_installs": "27",
            "revenue_1d": "10",
            "revenue_7d": "55",
            "revenue_30d": "120",
        }
        m = client._normalize_campaign_row(row)
        assert isinstance(m, CampaignMetric)
        assert m.network == "meta"
        assert m.campaign_id == "c_meta_123"
        assert m.spend == 380.00
        assert m.installs == 27
        assert m.cpi == round(380 / 27, 4)
        assert m.ipm > 0
        assert m.ctr > 0
        assert m.date == date(2025, 6, 1)
        assert m.revenue_d7 == 55.0
        assert m.roas_d7 == round(55 / 380, 4)

    def test_normalize_handles_missing_fields(self):
        client = SingularClient()
        m = client._normalize_campaign_row({})
        assert m.spend == 0.0
        assert m.installs == 0
        assert m.cpi == 0.0
        assert m.date is None

    def test_normalize_uses_tracker_installs_fallback(self):
        """When adn_installs is missing/None, falls back to tracker_installs."""
        client = SingularClient()
        row = {
            "adn_cost": "100",
            "adn_installs": None,
            "tracker_installs": "50",
        }
        m = client._normalize_campaign_row(row)
        assert m.installs == 50


class TestCreativePull:
    def test_normalize_creative_row(self):
        client = SingularClient()
        row = {
            "creative_id": "cr_456",
            "creative_name": "Playable/Merge+Kitchen",
            "source": "applovin",
            "adn_cost": "200",
            "adn_impressions": "80000",
            "adn_clicks": "2000",
            "adn_installs": "180",
            "revenue_7d": "300",
        }
        cr = client._normalize_creative_row(row)
        assert isinstance(cr, CreativeMetric)
        assert cr.creative_id == "cr_456"
        assert cr.concept_tag == "Playable/Merge+Kitchen"
        assert cr.platform == "applovin"
        assert cr.ipm == round(180 / 80000 * 1000, 2)
        assert cr.ctr == round(2000 / 80000 * 100, 4)
        assert cr.spend == 200.0


class TestRetry:
    @pytest.mark.asyncio
    async def test_retry_on_timeout(self):
        client = SingularClient()
        client.api_key = "test"

        import httpx
        call_count = 0

        async def mock_request(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise httpx.TimeoutException("timeout")
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"ok": True}
            resp.raise_for_status = MagicMock()
            return resp

        with patch("httpx.AsyncClient") as MockClient:
            mock_inst = AsyncMock()
            mock_inst.request = mock_request
            mock_inst.__aenter__ = AsyncMock(return_value=mock_inst)
            mock_inst.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_inst

            result = await client._request("GET", "https://test.com")
            assert result == {"ok": True}
            assert call_count == 3


class TestZeroRevenue:
    def test_zero_revenue_flags_unavailable(self):
        client = SingularClient()
        assert client.revenue_data_available is True

        row = {
            "adn_cost": "500",
            "adn_impressions": "100000",
            "adn_clicks": "3000",
            "adn_installs": "400",
            "revenue_1d": "0",
            "revenue_7d": "0",
            "revenue_30d": "0",
        }
        m = client._normalize_campaign_row(row)

        assert client.revenue_data_available is False
        assert m.revenue_d7 == 0.0
        assert m.roas_d7 == 0.0
        assert m.cpi > 0  # CPI-based metrics still work

    def test_nonzero_revenue_keeps_flag_true(self):
        client = SingularClient()
        row = {
            "adn_cost": "500",
            "adn_installs": "400",
            "revenue_1d": "50",
            "revenue_7d": "200",
            "revenue_30d": "400",
        }
        client._normalize_campaign_row(row)
        assert client.revenue_data_available is True
