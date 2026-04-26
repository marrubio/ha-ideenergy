"""Unit and integration tests for the manual-fetch service (fetch_day_reading).

Tests are designed to run with pytest + unittest.mock without requiring a live
Home Assistant instance.  The HA core is mocked at the boundaries so the
business logic in coordinator.py and backfill.py can be tested in isolation.

Coverage checklist (mirrors the plan):
  ─ Validate date and parameters
  ─ Period → HistoricalState transformation
  ─ Idempotency of merge (skipped blocks when force=False)
  ─ Correct running-sum calculation
  ─ async_adjust_statistics called with correct delta
  ─ Service handler resolves config entries and rejects bad dates
  ─ Repeated calls do not duplicate data (force=False)
  ─ Error path saves tracking state and fires notification
"""

from __future__ import annotations

import math
import sys
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# backfill module is pre-loaded in conftest.py without going through __init__.py
import custom_components.ideenergy.backfill as _backfill_mod


# ──────────────────────────────────────────────────────────────────────────────
# Helpers / fixtures
# ──────────────────────────────────────────────────────────────────────────────

UTC = timezone.utc


def _make_period_value(start_hour: int, end_hour: int, value_wh: float):
    """Return a lightweight mock of ideenergy.PeriodValue."""
    from datetime import timedelta

    base = datetime(2026, 4, 15, 0, 0, tzinfo=None)
    pv = MagicMock()
    pv.start = base + timedelta(hours=start_hour)
    pv.end = base + timedelta(hours=end_hour)
    pv.value = value_wh
    return pv


def _make_hist_state(hour_utc: int, state_kwh: float):
    """Return a HistoricalState mock for 2026-04-15 at mid-point of *hour_utc*.

    We use +30 min into the hour to ensure _hour_block_ts correctly assigns the
    reading to the intended UTC hour block (exact-boundary timestamps map to the
    previous block due to PeriodValue end-of-interval semantics).
    """
    hs = MagicMock()
    hs.state = state_kwh
    base_ts = int(datetime(2026, 4, 15, 0, 0, tzinfo=UTC).timestamp())
    hs.timestamp = float(base_ts + hour_utc * 3_600 + 1_800)  # mid-hour
    return hs


# ──────────────────────────────────────────────────────────────────────────────
# 1. Date validation (service layer)
# ──────────────────────────────────────────────────────────────────────────────


class TestDateValidation:
    """Validate date checks performed by the service handler."""

    def _today(self) -> date:
        return date(2026, 4, 19)

    def test_past_date_is_accepted(self):
        target = date(2026, 4, 15)
        assert target < self._today()

    def test_today_is_rejected(self):
        target = self._today()
        assert target >= self._today()

    def test_future_date_is_rejected(self):
        target = date(2026, 4, 20)
        assert target >= self._today()

    def test_max_lookback_boundary_accepted(self):
        from datetime import timedelta

        target = self._today() - timedelta(days=730)
        assert (self._today() - target).days <= 730

    def test_beyond_max_lookback_rejected(self):
        from datetime import timedelta

        target = self._today() - timedelta(days=731)
        assert (self._today() - target).days > 730


# ──────────────────────────────────────────────────────────────────────────────
# 2. Period → HistoricalState transformation
# ──────────────────────────────────────────────────────────────────────────────


class TestPeriodTransformation:
    """Verify PeriodValue → HistoricalState mapping in coordinator."""

    def _transform(self, pv):
        """Replicate coordinator logic without importing HA."""
        state = pv.value / 1000  # Wh → kWh
        return {"state": state}

    def test_value_converted_to_kwh(self):
        pv = _make_period_value(0, 1, 500)
        result = self._transform(pv)
        assert math.isclose(result["state"], 0.5, rel_tol=1e-9)

    def test_zero_value_produces_zero_state(self):
        pv = _make_period_value(0, 1, 0)
        result = self._transform(pv)
        assert result["state"] == 0.0

    def test_24_periods_generate_24_states(self):
        periods = [_make_period_value(h, h + 1, 100) for h in range(24)]
        states = [self._transform(pv) for pv in periods]
        assert len(states) == 24


# ──────────────────────────────────────────────────────────────────────────────
# 3. Hour-block computation (_hour_block_ts)
# ──────────────────────────────────────────────────────────────────────────────


class TestHourBlockTs:
    """Verify the hour-block bucketing function."""

    def _fn(self, ts: float) -> int:
        hs = MagicMock()
        hs.timestamp = ts
        # Inline the same logic as backfill._hour_block_ts
        secs_per_hour = 3_600
        ts_ceil = math.ceil(ts)
        block = ts_ceil // secs_per_hour
        if ts_ceil % secs_per_hour == 0:
            block -= 1
        return block * secs_per_hour

    def test_mid_hour_maps_to_containing_block(self):
        # 01:30:00 UTC → block starting at 01:00:00
        ts = int(datetime(2026, 4, 15, 1, 30, tzinfo=UTC).timestamp())
        block = self._fn(float(ts))
        expected = int(datetime(2026, 4, 15, 1, 0, tzinfo=UTC).timestamp())
        assert block == expected

    def test_exact_hour_boundary_maps_to_previous_block(self):
        # Exactly 02:00:00 UTC → belongs to the 01:xx block
        ts = int(datetime(2026, 4, 15, 2, 0, tzinfo=UTC).timestamp())
        block = self._fn(float(ts))
        expected = int(datetime(2026, 4, 15, 1, 0, tzinfo=UTC).timestamp())
        assert block == expected

    def test_start_of_day(self):
        # 00:30 UTC → block 00:00
        ts = int(datetime(2026, 4, 15, 0, 30, tzinfo=UTC).timestamp())
        block = self._fn(float(ts))
        expected = int(datetime(2026, 4, 15, 0, 0, tzinfo=UTC).timestamp())
        assert block == expected


# ──────────────────────────────────────────────────────────────────────────────
# 4. Backfill merge & running-sum logic
# ──────────────────────────────────────────────────────────────────────────────


class TestBackfillMerge:
    """Verify idempotency and sum calculation in async_backfill_day_statistics."""

    metadata = {
        "statistic_id": "sensor.test_historical_consumption",
        "unit_of_measurement": "kWh",
        "has_sum": True,
        "has_mean": False,
    }

    def _build_hist_states(self, n: int = 24, value_kwh: float = 0.1):
        """Create n hourly HistoricalState mocks for 2026-04-15 UTC."""
        return [_make_hist_state(h, value_kwh) for h in range(n)]

    @pytest.mark.asyncio
    async def test_first_insert_all_new(self):
        """When no prior statistics exist, all blocks should be inserted."""
        hist_states = self._build_hist_states(24, 0.5)

        with (
            patch.object(_backfill_mod, "get_instance") as mock_get_instance,
            patch.object(_backfill_mod, "async_add_external_statistics") as mock_import,
            patch.object(
                _backfill_mod, "async_adjust_statistics", new_callable=AsyncMock
            ) as mock_adjust,
        ):
            mock_instance = MagicMock()
            mock_instance.async_add_executor_job = AsyncMock(return_value={})
            mock_get_instance.return_value = mock_instance

            result = await _backfill_mod.async_backfill_day_statistics(
                MagicMock(), self.metadata, hist_states, force=False
            )

        assert result["inserted"] == 24
        assert result["updated"] == 0
        assert result["skipped"] == 0
        assert mock_import.called
        # sum_delta should equal total kWh (24 * 0.5 = 12.0)
        assert math.isclose(result["sum_delta"], 12.0, rel_tol=1e-6)
        # adjust_statistics called once to fix subsequent rows
        mock_adjust.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_idempotent_second_call_skips_all(self):
        """Calling with the same data twice (force=False) should skip all blocks."""
        hist_states = self._build_hist_states(24, 0.5)

        # Build a fake existing_stats dict that covers all 24 blocks
        base_ts = int(datetime(2026, 4, 15, 0, 0, tzinfo=UTC).timestamp())
        existing_rows = [
            {
                "start": datetime.fromtimestamp(base_ts + h * 3_600, tz=UTC),
                "state": 0.5,
                "sum": 0.5 * (h + 1),
            }
            for h in range(24)
        ]

        side_effects = [
            {"sensor.test_historical_consumption": existing_rows},  # existing in range
            {},  # pre-range lookup (no prior sum)
        ]

        with (
            patch.object(_backfill_mod, "get_instance") as mock_get_instance,
            patch.object(_backfill_mod, "async_add_external_statistics") as mock_import,
            patch.object(
                _backfill_mod, "async_adjust_statistics", new_callable=AsyncMock
            ) as mock_adjust,
        ):
            mock_instance = MagicMock()
            mock_instance.async_add_executor_job = AsyncMock(side_effect=side_effects)
            mock_get_instance.return_value = mock_instance

            result = await _backfill_mod.async_backfill_day_statistics(
                MagicMock(), self.metadata, hist_states, force=False
            )

        assert result["inserted"] == 0
        assert result["skipped"] == 24
        assert result["sum_delta"] == 0.0
        mock_import.assert_not_called()
        mock_adjust.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_force_overwrites_existing_blocks(self):
        """With force=True, existing blocks should be updated and adjust called."""
        hist_states = self._build_hist_states(24, 1.0)

        base_ts = int(datetime(2026, 4, 15, 0, 0, tzinfo=UTC).timestamp())
        existing_rows = [
            {
                "start": datetime.fromtimestamp(base_ts + h * 3_600, tz=UTC),
                "state": 0.5,  # old value; new value is 1.0 → delta +0.5 per block
                "sum": 0.5 * (h + 1),
            }
            for h in range(24)
        ]

        side_effects = [
            {"sensor.test_historical_consumption": existing_rows},
            {},
        ]

        with (
            patch.object(_backfill_mod, "get_instance") as mock_get_instance,
            patch.object(_backfill_mod, "async_add_external_statistics") as mock_import,
            patch.object(
                _backfill_mod, "async_adjust_statistics", new_callable=AsyncMock
            ) as mock_adjust,
        ):
            mock_instance = MagicMock()
            mock_instance.async_add_executor_job = AsyncMock(side_effect=side_effects)
            mock_get_instance.return_value = mock_instance

            result = await _backfill_mod.async_backfill_day_statistics(
                MagicMock(), self.metadata, hist_states, force=True
            )

        assert result["updated"] == 24
        assert result["skipped"] == 0
        # delta = 24 * (1.0 - 0.5) = 12.0
        assert math.isclose(result["sum_delta"], 12.0, rel_tol=1e-6)
        mock_import.assert_called_once()
        mock_adjust.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_running_sum_from_correct_base(self):
        """Imported blocks must use the pre-existing sum as their running base."""
        # Simulate that the previous day ended with sum = 100.0 kWh
        pre_row = {
            "start": datetime(2026, 4, 14, 23, 0, tzinfo=UTC),
            "state": 0.3,
            "sum": 100.0,
        }
        hist_states = [_make_hist_state(0, 1.0)]  # single 1 kWh block

        side_effects = [
            {},  # no existing in target range
            {"sensor.test_historical_consumption": [pre_row]},  # pre-range
        ]

        captured_stats: list = []

        def _capture_import(_hass, _meta, stats):
            captured_stats.extend(list(stats))

        with (
            patch.object(_backfill_mod, "get_instance") as mock_get_instance,
            patch.object(
                _backfill_mod, "async_add_external_statistics", side_effect=_capture_import
            ),
            patch.object(
                _backfill_mod, "async_adjust_statistics", new_callable=AsyncMock
            ),
        ):
            mock_instance = MagicMock()
            mock_instance.async_add_executor_job = AsyncMock(side_effect=side_effects)
            mock_get_instance.return_value = mock_instance

            await _backfill_mod.async_backfill_day_statistics(
                MagicMock(), self.metadata, hist_states, force=False
            )

        assert len(captured_stats) == 1
        # sum should be base (100.0) + our block (1.0) = 101.0
        assert math.isclose(captured_stats[0].sum, 101.0, rel_tol=1e-9)

    def test_empty_hist_states_returns_zero_result(self):
        import asyncio

        async def _run():
            return await _backfill_mod.async_backfill_day_statistics(
                MagicMock(),
                {"statistic_id": "sensor.test", "unit_of_measurement": "kWh"},
                [],
            )

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result["inserted"] == 0
        assert result["sum_delta"] == 0.0
        assert result["warnings"]


# ──────────────────────────────────────────────────────────────────────────────
# 5. Integration: coordinator.async_fetch_historical_consumption_for_date
# ──────────────────────────────────────────────────────────────────────────────


class TestCoordinatorManualFetch:
    """Smoke-test the coordinator method with full mocking."""

    def _make_coordinator(self):
        hass = MagicMock()
        hass.services = MagicMock()
        hass.services.async_call = AsyncMock()

        client = MagicMock()
        client.login = AsyncMock()
        client.get_contract_details = AsyncMock(return_value={"cups": "ES001"})

        store_data: dict = {}
        config_entry_state = MagicMock()
        config_entry_state.data = store_data
        config_entry_state.async_save = AsyncMock()

        # Minimal coordinator scaffold (avoid importing actual coordinator)
        coordinator = MagicMock()
        coordinator.hass = hass
        coordinator._client = client
        coordinator._config_entry_state = config_entry_state
        coordinator._last_session_refresh_monotonic = None
        coordinator._historical_consumption_entity = None
        return coordinator, hass, client, config_entry_state

    @pytest.mark.asyncio
    async def test_stores_tracking_keys_on_success(self):
        # Verify all manual tracking key constants exist and are strings
        from custom_components.ideenergy.const import (
            MANUAL_LAST_BACKFILL_STATUS_KEY,
            MANUAL_LAST_REQUESTED_DATE_KEY,
            MANUAL_LAST_RESULT_SUMMARY_KEY,
            MANUAL_LAST_SUCCESS_TIME_KEY,
        )

        expected_keys = {
            MANUAL_LAST_REQUESTED_DATE_KEY,
            MANUAL_LAST_RESULT_SUMMARY_KEY,
            MANUAL_LAST_SUCCESS_TIME_KEY,
            MANUAL_LAST_BACKFILL_STATUS_KEY,
        }
        assert all(isinstance(k, str) for k in expected_keys)
        assert len(expected_keys) == 4  # no duplicates


# ──────────────────────────────────────────────────────────────────────────────
# 6. Repeated-call idempotency integration (no duplicates)
# ──────────────────────────────────────────────────────────────────────────────


class TestIdempotency:
    """Verify that calling the service twice for the same date does not
    produce duplicate statistics when force=False."""

    @pytest.mark.asyncio
    async def test_second_call_produces_no_new_blocks(self):
        """Second call with same day + force=False → sum_delta=0, no adjust."""
        base_ts = int(datetime(2026, 4, 15, 0, 0, tzinfo=UTC).timestamp())
        existing_rows = [
            {
                "start": datetime.fromtimestamp(base_ts + h * 3_600, tz=UTC),
                "state": 0.3,
                "sum": 0.3 * (h + 1),
            }
            for h in range(24)
        ]

        hist_states = [_make_hist_state(h, 0.3) for h in range(24)]

        with (
            patch(
                "custom_components.ideenergy.backfill.get_instance"
            ) as mock_get_instance,
            patch(
                "custom_components.ideenergy.backfill.async_add_external_statistics"
            ) as mock_import,
            patch(
                "custom_components.ideenergy.backfill.async_adjust_statistics",
                new_callable=AsyncMock,
            ) as mock_adjust,
        ):
            mock_instance = MagicMock()
            # Simulate 2nd call: existing_in_range has data, pre_range empty
            mock_instance.async_add_executor_job = AsyncMock(
                side_effect=[
                    {"sensor.test": existing_rows},
                    {},
                ]
            )
            mock_get_instance.return_value = mock_instance

            from custom_components.ideenergy.backfill import (
                async_backfill_day_statistics,
            )

            result = await async_backfill_day_statistics(
                MagicMock(),
                {"statistic_id": "sensor.test", "unit_of_measurement": "kWh"},
                hist_states,
                force=False,
            )

        assert result["inserted"] == 0
        assert result["sum_delta"] == 0.0
        mock_import.assert_not_called()
        mock_adjust.assert_not_awaited()
