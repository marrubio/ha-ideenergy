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

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from logging import getLogger
from typing import TypeAlias

import ideenergy
from homeassistant.core import HomeAssistant, dt_util

# from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant_historical_sensor import HistoricalState

from .const import (
    LOCAL_TZ,
)
from .store import IDeEnergyConfigEntryState

LOGGER = getLogger(__name__)


ACCUMULATED_CONSUMPTION_LAST_SUCCESS_STORED_STATE_KEY = (
    "accumulated_consumption_last_success"
)
ACCUMULATED_CONSUMPTION_LAST_ATTEMPT_STORED_STATE_KEY = (
    "accumulated_consumption_last_attempt"
)
ACCUMULATED_GENERATION_LAST_SUCCESS_STORED_STATE_KEY = (
    "accumulated_generation_last_success"
)
ACCUMULATED_GENERATION_LAST_ATTEMPT_STORED_STATE_KEY = (
    "accumulated_generation_last_attempt"
)


ACCUMULATED_CONSUMPTION_LAST_SUCCESS_MAX_AGE = 2 * 60 * 60
ACCUMULATED_CONSUMPTION_LAST_ATTEMPT_MAX_AGE = 5 * 60
ACCUMULATED_GENERATION_LAST_SUCCESS_MAX_AGE = 2 * 60 * 60
ACCUMULATED_GENERATION_LAST_ATTEMPT_MAX_AGE = 5 * 60

MEASURE_ACCUMULATED_KEY = "measure_accumulated"
MEASURE_INSTANT_KEY = "measure_instant"

HISTORICAL_PERIOD_LENGHT = timedelta(days=7)


##
# IDeEnergyCoordinatorDataSet: types of data that can be registered in the
# coordinator to be fetched
class IDeEnergyCoordinatorDataSet(enum.Enum):
    ACCUMULATED_CONSUMPTION = enum.auto()
    ACCUMULATED_GENERATION = enum.auto()
    POWER_DEMAND_PEAKS = enum.auto()


##
# IDeEnergyDataCoordinatorData: data stored inside the coordinator
type IDeEnergyDataCoordinatorData = dict[
    IDeEnergyCoordinatorDataSet, list[HistoricalState] | None
]


class IDeEnergyDataCoordinator(DataUpdateCoordinator[IDeEnergyDataCoordinatorData]):
    def __init__(
        self,
        hass: HomeAssistant,
        client: ideenergy.Client,
        config_entry_state: IDeEnergyConfigEntryState,
        datasets: set[IDeEnergyCoordinatorDataSet] | None = None,
        update_interval: timedelta = timedelta(seconds=30),
    ):
        name = f"{client} coordinator" if client else "i-de coordinator"
        self.datasets = datasets or set()

        super().__init__(hass, LOGGER, name=name, update_interval=update_interval)
        self.data = {k: None for k in IDeEnergyCoordinatorDataSet}

        self._client = client
        self._config_entry_state = config_entry_state

    async def _async_setup(self) -> None:
        """Set up the coordinator

        This is the place to set up your coordinator,
        or to load data, that only needs to be loaded once.

        This method will be called automatically during
        coordinator.async_config_entry_first_refresh.
        """
        # await self._client.login()
        pass

    async def _async_update_data(self) -> IDeEnergyDataCoordinatorData:
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.

        See: https://developers.home-assistant.io/docs/integration_fetching_data/
        """

        # Raising 'asyncio.TimeoutError' or 'aiohttp.ClientError' are already
        # handled by the data update coordinator.

        # Raising ConfigEntryAuthFailed will cancel future updates
        # and start a config flow with SOURCE_REAUTH (async_step_reauth)

        # Raise UpdateFailed is something were wrong

        dsstr = ", ".join([x.name for x in self.datasets])
        LOGGER.debug(f"update datasets: {dsstr}")

        updated_data = {}

        fns = {
            IDeEnergyCoordinatorDataSet.ACCUMULATED_CONSUMPTION: self._async_get_accumulated_consumption,
            IDeEnergyCoordinatorDataSet.ACCUMULATED_GENERATION: self._async_get_accumulated_generation,
            IDeEnergyCoordinatorDataSet.POWER_DEMAND_PEAKS: self._async_get_power_demand_peaks,
        }
        for ds, fn in fns.items():
            if ds in self.datasets:
                try:
                    updated_data[ds] = await fn()
                except ideenergy.ClientError:
                    LOGGER.exception(f"{ds.name}: error updating")
                    continue

                if updated_data[ds] is None:
                    LOGGER.warning(f"{ds.name}: update returned None")

        data = self.data | {k: v for k, v in updated_data.items() if v is not None}
        return data

    async def _async_get_direct_reading_data(self) -> dict[str, int | float]:
        data = await self._client.get_measure()
        return {
            MEASURE_ACCUMULATED_KEY: data.accumulate,
            MEASURE_INSTANT_KEY: data.instant,
        }

    async def _async_get_accumulated_consumption(self) -> list[HistoricalState] | None:
        return await self._async_get_accumulated_generic(
            self._client.get_historical_consumption,
            dataset=IDeEnergyCoordinatorDataSet.ACCUMULATED_CONSUMPTION,
            last_success_state_key=ACCUMULATED_CONSUMPTION_LAST_SUCCESS_STORED_STATE_KEY,
            last_attempt_state_key=ACCUMULATED_CONSUMPTION_LAST_ATTEMPT_STORED_STATE_KEY,
            last_attempt_max_age=ACCUMULATED_CONSUMPTION_LAST_ATTEMPT_MAX_AGE,
            last_success_max_age=ACCUMULATED_CONSUMPTION_LAST_SUCCESS_MAX_AGE,
        )

    async def _async_get_accumulated_generation(self) -> list[HistoricalState] | None:
        return await self._async_get_accumulated_generic(
            self._client.get_historical_generation,
            dataset=IDeEnergyCoordinatorDataSet.ACCUMULATED_GENERATION,
            last_success_state_key=ACCUMULATED_GENERATION_LAST_SUCCESS_STORED_STATE_KEY,
            last_success_max_age=ACCUMULATED_GENERATION_LAST_SUCCESS_MAX_AGE,
            last_attempt_state_key=ACCUMULATED_GENERATION_LAST_ATTEMPT_STORED_STATE_KEY,
            last_attempt_max_age=ACCUMULATED_GENERATION_LAST_ATTEMPT_MAX_AGE,
        )

    async def _async_get_power_demand_peaks(self) -> list[HistoricalState] | None:
        def historical_power_demand_as_historical_state(
            dai: ideenergy.DemandAtInstant,
        ) -> HistoricalState | None:
            dt = dai.dt.replace(tzinfo=LOCAL_TZ)
            # last_reset = dai.start.replace(tzinfo=LOCAL_TZ)

            try:
                return HistoricalState(
                    state=dai.value / 1000,
                    timestamp=dt_util.as_timestamp(dt),
                    # attributes={"last_reset": last_reset},
                )
            except Exception:
                LOGGER.error(f"invalid DemandAtInstant '{dai!r}'")
                return None

        data = await self._client.get_historical_power_demand()
        hist_states = [
            historical_power_demand_as_historical_state(dai) for dai in data.demands
        ]
        hist_states = [hs for hs in hist_states if hs is not None]

        return hist_states

    async def _async_get_accumulated_generic(
        self,
        afn: Callable,
        *,
        dataset=IDeEnergyCoordinatorDataSet,
        last_attempt_max_age: float,
        last_attempt_state_key: str,
        last_success_max_age: float,
        last_success_state_key: str,
    ) -> list[HistoricalState] | None:

        def as_historical_state(
            pv: ideenergy.PeriodValue,
        ) -> HistoricalState | None:
            dt = pv.end.replace(tzinfo=LOCAL_TZ)
            last_reset = pv.start.replace(tzinfo=LOCAL_TZ)

            try:
                return HistoricalState(
                    state=pv.value / 1000,
                    timestamp=dt_util.as_timestamp(dt),
                    attributes={"last_reset": last_reset},
                )
            except Exception:
                LOGGER.error(f"invalid PeriodValue '{pv!r}'")
                return None

        if self.state_timestamp_is_too_recent(
            last_success_state_key,
            last_success_max_age,
        ):
            LOGGER.debug(f"{self._client}: current data for {dataset} is too recent")
            return None

        if self.state_timestamp_is_too_recent(
            last_attempt_state_key,
            last_attempt_max_age,
        ):
            LOGGER.debug(f"{self._client}: last attempt for {dataset} is too recent")
            return None

        end = datetime.today()
        start = end - HISTORICAL_PERIOD_LENGHT

        try:
            data = await afn(start=start, end=end)
        except Exception:
            await self.async_save_timestamp_at_state(last_attempt_state_key)
            raise

        await self.async_save_timestamp_at_state(last_success_state_key)

        hist_states = [as_historical_state(pv) for pv in data.periods]
        hist_states = [hs for hs in hist_states if hs is not None]
        return hist_states

    async def async_save_timestamp_at_state(
        self, key: str, timestamp: float | None = None
    ) -> None:
        timestamp = timestamp or dt_util.as_timestamp(datetime.now())
        self._config_entry_state.data[key] = timestamp
        await self._config_entry_state.async_save()

    def state_timestamp_is_too_recent(self, key: str, max_age: float) -> bool:
        now_ts = dt_util.as_timestamp(datetime.now())

        try:
            prev = float(self._config_entry_state.data[key])
        except (TypeError, ValueError, KeyError):
            prev = 0

        return now_ts - prev <= max_age


# def period_item_with_tz_info(item):
#     item.start = item.start.replace(tzinfo=LOCAL_TZ)
#     item.end = item.end.replace(tzinfo=LOCAL_TZ)
#
#     return item


# def dated_item_with_tz_info(item):
#     item.dt = item.dt.replace(tzinfo=LOCAL_TZ)
#
#     return item
