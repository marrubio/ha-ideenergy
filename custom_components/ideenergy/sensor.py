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


# TODO:
# Maybe we need to mark some function as callback but I'm not sure whose.


import itertools
from datetime import datetime, timedelta
from functools import cached_property
from logging import getLogger
from math import ceil
from typing import cast

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback, dt_util
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify
from homeassistant_historical_sensor import (
    HistoricalSensor,
    HistoricalState,
    hass_get_last_statistic,
)

from .coordinator import IDeEnergyCoordinatorDataSet, IDeEnergyDataCoordinator
from .data import IntegrationIDeEnergyConfigEntry

PLATFORM = "sensor"

LOGGER = getLogger(__name__)


class IDeEnergySensor(CoordinatorEntity, HistoricalSensor, SensorEntity):
    I_DE_PLATFORM: str = PLATFORM
    I_DE_ENTITY_NAME: str
    I_DE_DATA_SET: set
    coordinator: IDeEnergyDataCoordinator

    def __init__(
        self,
        *args,
        hass: HomeAssistant,
        device_info: DeviceInfo,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self._attr_has_entity_name = True
        self._attr_name = self.I_DE_ENTITY_NAME
        self._attr_device_info = device_info

        self._attr_unique_id = _build_entity_unique_id(
            device_info, self.I_DE_ENTITY_NAME
        )

        self._attr_state_attributes = {}

    @cached_property
    def unique_id(self) -> str:
        cups = dict(self.device_info["identifiers"])["cups"]
        name = self.I_DE_ENTITY_NAME
        return slugify(f"{cups}-{name}", separator="-")

    # ==
    # Entity
    # ==
    async def async_added_to_hass(self) -> None:
        LOGGER.info(f"{self.entity_id} added to hass")
        await super().async_added_to_hass()

        for x in self.I_DE_DATA_SET:
            self.coordinator.activate_dataset(x)

        # Register this entity with the coordinator so the manual-fetch
        # service can retrieve StatisticMetaData for backfill.
        if hasattr(self, "get_statistic_metadata"):
            self.coordinator.register_historical_consumption_entity(self)

        # Do NOT refresh on startup to avoid API calls during initialization.
        # Only scheduled (12:30) and manual (service) calls will fetch data.
        LOGGER.info(f"{self.entity_id} registered (no startup refresh)")

    async def async_will_remove_from_hass(self) -> None:
        for x in self.I_DE_DATA_SET:
            self.coordinator.deactivate_dataset(x)

        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.hass.async_create_task(self.async_write_historical())

    # It's a coordinator entity, do nothing
    async def async_update_historical(self) -> None:
        pass

    # ==
    # Historical sensor
    # ==
    @property
    def historical_states(self) -> list[HistoricalState]:
        return cast(
            list[HistoricalState],
            self.coordinator.data[IDeEnergyCoordinatorDataSet.HISTORICAL_CONSUMPTION],
        )

    def get_statistic_metadata(self) -> StatisticMetaData:
        meta = super().get_statistic_metadata()
        device_name = self.device_info.get("name") if self.device_info else None
        meta["name"] = (
            f"{device_name} {self.I_DE_ENTITY_NAME}"
            if device_name
            else self.I_DE_ENTITY_NAME
        )
        meta["has_sum"] = True
        return meta

    async def async_calculate_statistic_data(
        self, hist_states: list[HistoricalState], *, latest: dict | None = None
    ) -> list[StatisticData]:
        #
        # Filter out invalid states
        #

        n_original_hist_states = len(hist_states)
        hist_states = [x for x in hist_states if x.state not in (0, None)]
        if len(hist_states) != n_original_hist_states:
            LOGGER.warning(
                f"{self.entity_id}: "
                + "found some weird values in historical statistics"
            )

        #
        # Group historical states by hour block
        #

        def hour_block_for_hist_state(hist_state: HistoricalState) -> datetime:
            secs_per_hour = 60 * 60

            ts = ceil(hist_state.timestamp)
            block = ts // secs_per_hour
            leftover = ts % secs_per_hour

            if leftover == 0:
                block = block - 1

            return block * secs_per_hour

        latest = await hass_get_last_statistic(self.hass, self.get_statistic_metadata())

        #
        # Get last sum sum from latest
        #
        def extract_last_sum(latest) -> float:
            return float(latest["sum"]) if latest else 0

        try:
            total_accumulated = extract_last_sum(latest)
        except (KeyError, ValueError):
            LOGGER.error(
                f"{self.entity_id}: [bug] statistics broken (lastest={latest!r})"
            )
            return []

        start_point_local_dt = dt_util.as_local(
            dt_util.utc_from_timestamp(latest.get("start", 0) if latest else 0)
        )

        LOGGER.debug(
            f"{self.entity_id}: "
            + f"calculating statistics using {total_accumulated:.2f} as base accumulated "
            + f"(registed at {start_point_local_dt})"
        )

        #
        # Calculate statistic data
        #

        ret = []

        for hour_block, collection_it in itertools.groupby(
            hist_states, key=hour_block_for_hist_state
        ):
            collection = list(collection_it)

            # hour_mean = statistics.mean([x.state for x in collection])
            hour_accumulated = sum([x.state for x in collection])
            total_accumulated = total_accumulated + hour_accumulated

            ret.append(
                StatisticData(
                    start=dt_util.utc_from_timestamp(hour_block),
                    state=hour_accumulated,
                    # mean=hour_mean,
                    sum=total_accumulated,
                )
            )

        return ret


class HistoricalConsumption(IDeEnergySensor):
    I_DE_PLATFORM = PLATFORM
    I_DE_ENTITY_NAME = "Historical Consumption"
    I_DE_DATA_SET = {IDeEnergyCoordinatorDataSet.HISTORICAL_CONSUMPTION}

    def get_statistic_metadata(self):
        meta = super().get_statistic_metadata()
        meta["unit_class"] = SensorDeviceClass.ENERGY
        meta["unit_of_measurement"] = UnitOfEnergy.KILO_WATT_HOUR
        # External statistics format (colon separator) so they appear in
        # the Energy dashboard selector.  source must match the domain prefix.
        if self.entity_id:
            meta["statistic_id"] = self.entity_id.replace(".", ":", 1)
        meta["source"] = "sensor"
        return meta

    async def async_write_historical(self) -> None:
        """Write via async_add_external_statistics for Energy dashboard."""
        if not self.historical_states:
            LOGGER.warning(f"{self.entity_id}: no historical states to write")
            return

        hist_states = sorted(self.historical_states, key=lambda x: x.timestamp)
        metadata = self.get_statistic_metadata()
        latest = await hass_get_last_statistic(self.hass, metadata)

        if latest is not None:
            cutoff = latest["start"] + 3600
            hist_states = [x for x in hist_states if x.timestamp > cutoff]

        if not hist_states:
            return

        stats = await self.async_calculate_statistic_data(
            hist_states, latest=latest
        )
        if stats:
            async_add_external_statistics(self.hass, metadata, stats)

    @property
    def historical_states(self) -> list[HistoricalState] | None:
        return self.coordinator.data[IDeEnergyCoordinatorDataSet.HISTORICAL_CONSUMPTION]


class HistoricalGeneration(IDeEnergySensor):
    I_DE_PLATFORM = PLATFORM
    I_DE_ENTITY_NAME = "Historical Generation"
    I_DE_DATA_SET = {IDeEnergyCoordinatorDataSet.HISTORICAL_GENERATION}

    def get_statistic_metadata(self):
        meta = super().get_statistic_metadata()
        meta["unit_class"] = SensorDeviceClass.ENERGY
        meta["unit_of_measurement"] = UnitOfEnergy.KILO_WATT_HOUR
        if self.entity_id:
            meta["statistic_id"] = self.entity_id.replace(".", ":", 1)
        meta["source"] = "sensor"
        return meta

    async def async_write_historical(self) -> None:
        """Write via async_add_external_statistics for Energy dashboard."""
        if not self.historical_states:
            LOGGER.warning(f"{self.entity_id}: no historical states to write")
            return

        hist_states = sorted(self.historical_states, key=lambda x: x.timestamp)
        metadata = self.get_statistic_metadata()
        latest = await hass_get_last_statistic(self.hass, metadata)

        if latest is not None:
            cutoff = latest["start"] + 3600
            hist_states = [x for x in hist_states if x.timestamp > cutoff]

        if not hist_states:
            return

        stats = await self.async_calculate_statistic_data(
            hist_states, latest=latest
        )
        if stats:
            async_add_external_statistics(self.hass, metadata, stats)

    @property
    def historical_states(self) -> list[HistoricalState] | None:
        return self.coordinator.data[IDeEnergyCoordinatorDataSet.HISTORICAL_GENERATION]


class YesterdayTotal(CoordinatorEntity, SensorEntity):
    coordinator: IDeEnergyDataCoordinator

    def __init__(
        self,
        *args,
        hass: HomeAssistant,
        device_info: DeviceInfo,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self._attr_has_entity_name = True
        self._attr_name = "Yesterday Total"
        self._attr_device_info = device_info

        self._attr_unique_id = _build_entity_unique_id(
            device_info, "Yesterday Total"
        )
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_extra_state_attributes = {
            "LAST_REFRESH": None,
            "YESTERDAY_DATE": None,
        }

    async def async_added_to_hass(self) -> None:
        LOGGER.info(f"{self.entity_id} added to hass")
        await super().async_added_to_hass()
        self.coordinator.activate_dataset(
            IDeEnergyCoordinatorDataSet.YESTERDAY_TOTAL
        )
        await self.coordinator.async_request_refresh()

    async def async_will_remove_from_hass(self) -> None:
        self.coordinator.deactivate_dataset(
            IDeEnergyCoordinatorDataSet.YESTERDAY_TOTAL
        )
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._attr_extra_state_attributes = {
            "LAST_REFRESH": self.coordinator.yesterday_total_last_refresh,
            "YESTERDAY_DATE": self.coordinator.yesterday_total_query_date,
        }
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> float | None:
        return cast(
            float | None,
            self.coordinator.data[IDeEnergyCoordinatorDataSet.YESTERDAY_TOTAL],
        )

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {
            "LAST_REFRESH": self.coordinator.yesterday_total_last_refresh,
            "YESTERDAY_DATE": self.coordinator.yesterday_total_query_date,
        }


##
# Migrate this to attributes in a general sensor
# Using statistics for the isolated points representing demand peaks has no sense

# class PowerDemandPeaks(IDeEnergySensor):
#     I_DE_PLATFORM = PLATFORM
#     I_DE_ENTITY_NAME = "Power Demand Peaks"
#     I_DE_DATA_SET = {IDeEnergyCoordinatorDataSet.POWER_DEMAND_PEAKS}

#     # def __init__(self, *args, **kwargs):
#     #     super().__init__(*args, **kwargs)
#     #     self._attr_device_class = SensorDeviceClass.ENERGY
#     #     self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
#     #
#     #     # TOTAL vs TOTAL_INCREASING:
#     #     #
#     #     # It's recommended to use state class total without last_reset whenever
#     #     # possible, state class total_increasing or total with last_reset should only be
#     #     # used when state class total without last_reset does not work for the sensor.
#     #     # https://developers.home-assistant.io/docs/core/entity/sensor/#how-to-choose-state_class-and-last_reset
#     #
#     #     # The sensor's value never resets, e.g. a lifetime total energy consumption or
#     #     # production: state_class total, last_reset not set or set to None
#     #
#     #     self._attr_state_class = SensorStateClass.TOTAL

#     def get_statistic_metadata(self):
#         meta = super().get_statistic_metadata()
#         meta["unit_class"] = SensorDeviceClass.POWER
#         meta["unit_of_measurement"] = UnitOfPower.KILO_WATT
#         return meta

#     @property
#     def historical_states(self) -> list[HistoricalState] | None:
#         return self.coordinator.data[
#             IDeEnergyCoordinatorDataSet.POWER_DEMAND_PEAKS
#         ]  # ty:ignore[non-subscriptable]


class LastRefreshTime(CoordinatorEntity, SensorEntity):
    """Sensor that exposes the last successful data fetch timestamp."""

    coordinator: IDeEnergyDataCoordinator

    def __init__(
        self,
        *args,
        hass: HomeAssistant,
        device_info: DeviceInfo,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self._attr_has_entity_name = True
        self._attr_name = "Last Refresh Time"
        self._attr_device_info = device_info
        self._attr_unique_id = _build_entity_unique_id(device_info, "Last Refresh Time")
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_icon = "mdi:clock-check-outline"

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.yesterday_total_last_refresh_dt


# ══════════════════════════════════════════════════════════════════════════════
# Manual-fetch diagnostic sensors
# ══════════════════════════════════════════════════════════════════════════════

class ManualLastRefreshTime(CoordinatorEntity, SensorEntity):
    """Timestamp of the last successful manual fetch."""

    coordinator: IDeEnergyDataCoordinator

    def __init__(self, *, hass: HomeAssistant, coordinator, device_info: DeviceInfo, **kwargs):
        super().__init__(coordinator, **kwargs)
        self._attr_has_entity_name = True
        self._attr_name = "Manual Last Refresh Time"
        self._attr_device_info = device_info
        self._attr_unique_id = _build_entity_unique_id(device_info, "Manual Last Refresh Time")
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_icon = "mdi:clock-edit-outline"
        self._attr_entity_registry_enabled_default = True

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.manual_last_success_time_dt


class ManualLastRequestedDate(CoordinatorEntity, SensorEntity):
    """Last date requested via the manual-fetch service (YYYY-MM-DD text)."""

    coordinator: IDeEnergyDataCoordinator

    def __init__(self, *, hass: HomeAssistant, coordinator, device_info: DeviceInfo, **kwargs):
        super().__init__(coordinator, **kwargs)
        self._attr_has_entity_name = True
        self._attr_name = "Manual Last Requested Date"
        self._attr_device_info = device_info
        self._attr_unique_id = _build_entity_unique_id(device_info, "Manual Last Requested Date")
        self._attr_icon = "mdi:calendar-search"
        self._attr_entity_registry_enabled_default = True

    @property
    def native_value(self) -> str | None:
        return self.coordinator.manual_last_requested_date


class ManualLastResult(CoordinatorEntity, SensorEntity):
    """Human-readable summary of the last manual fetch result."""

    coordinator: IDeEnergyDataCoordinator

    def __init__(self, *, hass: HomeAssistant, coordinator, device_info: DeviceInfo, **kwargs):
        super().__init__(coordinator, **kwargs)
        self._attr_has_entity_name = True
        self._attr_name = "Manual Last Result"
        self._attr_device_info = device_info
        self._attr_unique_id = _build_entity_unique_id(device_info, "Manual Last Result")
        self._attr_icon = "mdi:text-box-check-outline"
        self._attr_entity_registry_enabled_default = True

    @property
    def native_value(self) -> str | None:
        return self.coordinator.manual_last_result_summary


class ManualLastBackfillStatus(CoordinatorEntity, SensorEntity):
    """Status of the backfill step from the last manual fetch."""

    coordinator: IDeEnergyDataCoordinator

    def __init__(self, *, hass: HomeAssistant, coordinator, device_info: DeviceInfo, **kwargs):
        super().__init__(coordinator, **kwargs)
        self._attr_has_entity_name = True
        self._attr_name = "Manual Last Backfill Status"
        self._attr_device_info = device_info
        self._attr_unique_id = _build_entity_unique_id(device_info, "Manual Last Backfill Status")
        self._attr_icon = "mdi:database-refresh-outline"
        self._attr_entity_registry_enabled_default = True

    @property
    def native_value(self) -> str | None:
        return self.coordinator.manual_last_backfill_status


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntegrationIDeEnergyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    # entity_description = (
    #     SensorEntityDescription(
    #         key="ideenergy",
    #         name="i-de energy",
    #         icon="mdi:energy",
    #     ),
    # )

    entity_registry = er.async_get(hass)
    old_unique_ids = {
        _build_entity_unique_id(entry.runtime_data.device_info, "Yesterday Power"),
        _build_entity_unique_id(entry.runtime_data.device_info, "Yesterday Consumption"),
    }
    for registry_entry in er.async_entries_for_config_entry(
        entity_registry, entry.entry_id
    ):
        if registry_entry.unique_id in old_unique_ids:
            entity_registry.async_remove(registry_entry.entity_id)

    IDeClasses = [
        HistoricalConsumption,
        HistoricalGeneration,
        YesterdayTotal,
        LastRefreshTime,
    ]
    async_add_entities(
        [
            IDeClass(
                hass=hass,
                coordinator=entry.runtime_data.coordinator,
                device_info=entry.runtime_data.device_info,
                # entity_description=entity_description,
            )
            for IDeClass in IDeClasses
        ]
    )

    # Manual-fetch diagnostic sensors use keyword-only init
    manual_diagnostic_classes = [
        ManualLastRefreshTime,
        ManualLastRequestedDate,
        ManualLastResult,
        ManualLastBackfillStatus,
    ]
    async_add_entities(
        [
            cls(
                hass=hass,
                coordinator=entry.runtime_data.coordinator,
                device_info=entry.runtime_data.device_info,
            )
            for cls in manual_diagnostic_classes
        ]
    )


def _build_entity_unique_id(device_info: DeviceInfo, entity_unique_name: str) -> str:
    cups = dict(device_info["identifiers"])["cups"]
    return slugify(f"{cups}-{entity_unique_name}", separator="-")
