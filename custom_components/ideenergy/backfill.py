# Copyright (C) 2021-2026 Luis López <luis@cuarentaydos.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301,
# USA.

"""Backfill HA recorder statistics for a specific day.

This module is responsible for:
  - Reading existing statistics before and during the target window.
  - Computing running sums from the correct base.
  - Importing the new / updated hour blocks via async_add_external_statistics.
  - Adjusting the running sum of all subsequent hour blocks via
    async_adjust_statistics so the cumulative total stays consistent.
"""

from __future__ import annotations

import itertools
import logging
from datetime import datetime
from math import ceil
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant, dt_util
from homeassistant_historical_sensor import HistoricalState

from .const import DOMAIN

LOGGER = logging.getLogger(__name__)

# How far back (seconds) we look to find the last existing sum before backfill
_PRE_WINDOW_SECONDS = 14 * 86_400  # 14 days

# Home Assistant recorder API compatibility:
# - Newer versions expose `async_adjust_statistics`.
# - Older versions expose only `adjust_statistics` (sync).
try:
    from homeassistant.components.recorder.statistics import (
        async_adjust_statistics as _ha_async_adjust_statistics,
    )
except ImportError:
    _ha_async_adjust_statistics = None

# Backward compatibility symbol used by local tests/patching.
async_adjust_statistics = _ha_async_adjust_statistics

try:
    from homeassistant.components.recorder.statistics import (
        adjust_statistics as _ha_sync_adjust_statistics,
    )
except ImportError:
    _ha_sync_adjust_statistics = None


def _hour_block_ts(hist_state: HistoricalState) -> int:
    """Return the UTC start-of-hour timestamp (int) for *hist_state*.

    i-DE `PeriodValue.end` marks the end of the measured interval.
    We map each reading to the hour block that CONTAINS it, i.e. the block
    whose start is ``ceil(ts) // 3600 * 3600`` unless ``ts`` is exactly on
    the hour boundary (in which case it belongs to the PREVIOUS block).
    """
    secs_per_hour = 3_600
    ts = ceil(hist_state.timestamp)
    block = ts // secs_per_hour
    if ts % secs_per_hour == 0:
        block -= 1
    return block * secs_per_hour


def _row_start_ts(row: dict) -> int:
    """Normalise the 'start' field of a statistics row to an int timestamp."""
    raw = row.get("start")
    if isinstance(raw, datetime):
        return int(raw.timestamp())
    return int(raw)  # already a numeric timestamp in some HA versions


async def _get_stats_in_range(
    hass: HomeAssistant,
    statistic_id: str,
    start_dt: datetime,
    end_dt: datetime,
) -> list[dict]:
    raw = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start_dt,
        end_dt,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return (raw or {}).get(statistic_id) or []


async def _async_adjust_statistics_compat(
    hass: HomeAssistant,
    *,
    statistic_id: str,
    start_time: datetime,
    sum_adjustment: float,
    adjustment_unit_of_measurement: str,
) -> bool:
    """Adjust cumulative sums using whichever recorder API is available.

    Returns True when adjustment was executed, False when unsupported.
    """
    if async_adjust_statistics is not None:
        await async_adjust_statistics(
            hass,
            statistic_id=statistic_id,
            start_time=start_time,
            sum_adjustment=sum_adjustment,
            adjustment_unit_of_measurement=adjustment_unit_of_measurement,
        )
        return True

    if _ha_sync_adjust_statistics is not None:
        await get_instance(hass).async_add_executor_job(
            _ha_sync_adjust_statistics,
            hass,
            statistic_id,
            start_time,
            sum_adjustment,
            adjustment_unit_of_measurement,
        )
        return True

    return False


async def async_backfill_day_statistics(
    hass: HomeAssistant,
    metadata: StatisticMetaData,
    hist_states: list[HistoricalState],
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Insert / update recorder statistics for *hist_states* (one day of data).

    Idempotent when *force* is False: hour blocks that already have a value
    are left untouched.  Set *force=True* to overwrite existing blocks.

    Returns a dict::

        {
            "inserted":  <int>,   # newly created hour blocks
            "updated":   <int>,   # overwritten blocks  (force=True only)
            "skipped":   <int>,   # existing blocks not overwritten
            "sum_delta": <float>, # net kWh change applied to the running sum
            "warnings":  [<str>], # non-fatal warnings
        }
    """
    statistic_id = metadata["statistic_id"]
    unit = metadata.get("unit_of_measurement") or "kWh"
    warnings: list[str] = []

    source = metadata.get("source") or DOMAIN

    if not hist_states:
        return {
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "sum_delta": 0.0,
            "warnings": ["Sin datos que importar"],
        }

    sorted_states = sorted(hist_states, key=lambda hs: hs.timestamp)

    # ── Time range of the backfill ───────────────────────────────────────────
    first_block_ts = _hour_block_ts(sorted_states[0])
    last_block_ts = _hour_block_ts(sorted_states[-1])
    backfill_start_dt = dt_util.utc_from_timestamp(first_block_ts)
    backfill_end_dt = dt_util.utc_from_timestamp(last_block_ts + 3_600)

    # ── Existing statistics inside our target window (for idempotency) ───────
    existing_rows = await _get_stats_in_range(
        hass, statistic_id, backfill_start_dt, backfill_end_dt
    )
    existing_by_block: dict[int, dict] = {
        _row_start_ts(row): row for row in existing_rows
    }

    # ── Last known sum BEFORE our window (base for running total) ────────────
    pre_start_dt = dt_util.utc_from_timestamp(first_block_ts - _PRE_WINDOW_SECONDS)
    pre_rows = await _get_stats_in_range(
        hass, statistic_id, pre_start_dt, backfill_start_dt
    )
    if pre_rows:
        last_pre_row = max(pre_rows, key=_row_start_ts)
        pre_existing_sum = float(last_pre_row.get("sum") or 0.0)
    else:
        pre_existing_sum = 0.0

    LOGGER.debug(
        "backfill %s: window=[%s, %s) pre_sum=%.4f existing_blocks=%d",
        statistic_id,
        backfill_start_dt.isoformat(),
        backfill_end_dt.isoformat(),
        pre_existing_sum,
        len(existing_by_block),
    )

    # ── Build StatisticData for each hour block ──────────────────────────────
    new_stats: list[StatisticData] = []
    inserted = updated = skipped = 0
    running_sum = pre_existing_sum
    sum_delta = 0.0

    for hour_block, collection_it in itertools.groupby(
        sorted_states, key=_hour_block_ts
    ):
        collection = list(collection_it)
        hour_accumulated = sum(
            hs.state for hs in collection if hs.state is not None
        )

        if hour_block in existing_by_block:
            if not force:
                # Advance running_sum using the existing value so subsequent
                # blocks (if any in this pass) get consistent sums.
                existing_state = float(
                    existing_by_block[hour_block].get("state") or 0.0
                )
                running_sum += existing_state
                skipped += 1
                continue
            else:
                old_state = float(
                    existing_by_block[hour_block].get("state") or 0.0
                )
                sum_delta += hour_accumulated - old_state
                updated += 1
        else:
            sum_delta += hour_accumulated
            inserted += 1

        running_sum += hour_accumulated
        new_stats.append(
            StatisticData(
                start=dt_util.utc_from_timestamp(hour_block),
                state=hour_accumulated,
                sum=running_sum,
            )
        )

    if not new_stats:
        msg = (
            "Todos los bloques ya existen. "
            "Usa force=true para sobreescribir."
        )
        LOGGER.debug("backfill %s: %s", statistic_id, msg)
        return {
            "inserted": 0,
            "updated": 0,
            "skipped": skipped,
            "sum_delta": 0.0,
            "warnings": [msg],
        }

    # ── Import statistics ────────────────────────────────────────────────────
    # async_add_external_statistics requires statistic_id in colon format
    # ("sensor:object_id") and source must match the domain prefix.
    async_add_external_statistics(hass, metadata, new_stats)

    LOGGER.debug(
        "backfill %s: imported %d blocks (inserted=%d updated=%d skipped=%d sum_delta=%.4f)",
        statistic_id,
        len(new_stats),
        inserted,
        updated,
        skipped,
        sum_delta,
    )

    # ── Adjust running sum for all blocks AFTER our window ───────────────────
    if sum_delta != 0.0:
        try:
            adjusted = await _async_adjust_statistics_compat(
                hass,
                statistic_id=statistic_id,
                start_time=backfill_end_dt,
                sum_adjustment=sum_delta,
                adjustment_unit_of_measurement=unit,
            )
            if adjusted:
                LOGGER.debug(
                    "backfill %s: adjusted subsequent stats by %.4f %s from %s",
                    statistic_id,
                    sum_delta,
                    unit,
                    backfill_end_dt.isoformat(),
                )
            else:
                msg = (
                    "Backfill importado, pero esta versión de Home Assistant "
                    "no soporta ajuste automático del acumulado posterior."
                )
                warnings.append(msg)
                LOGGER.warning("backfill %s: %s", statistic_id, msg)
        except Exception:
            msg = (
                f"Backfill importado, pero falló el ajuste del acumulado posterior "
                f"(delta={sum_delta:.4f} {unit}). "
                f"Verifica el historial manualmente."
            )
            warnings.append(msg)
            LOGGER.exception("backfill %s: async_adjust_statistics failed", statistic_id)

    return {
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "sum_delta": sum_delta,
        "warnings": warnings,
    }
