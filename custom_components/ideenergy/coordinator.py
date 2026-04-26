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
from collections.abc import Callable
from datetime import date, datetime, timedelta
from logging import getLogger
from time import perf_counter
from typing import TYPE_CHECKING, Any

import ideenergy
from homeassistant.core import HomeAssistant, dt_util
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant_historical_sensor import HistoricalState

from .const import (
    DOMAIN,
    LOCAL_TZ,
    MANUAL_LAST_BACKFILL_STATUS_KEY,
    MANUAL_LAST_ERROR_KEY,
    MANUAL_LAST_REQUESTED_DATE_KEY,
    MANUAL_LAST_RESULT_SUMMARY_KEY,
    MANUAL_LAST_SUCCESS_TIME_KEY,
)
from .store import IDeEnergyConfigEntryState

if TYPE_CHECKING:
    pass

LOGGER = getLogger(__name__)


HISTORICAL_CONSUMPTION_LAST_SUCCESS_STORED_STATE_KEY = (
    "historical_consumption_last_success"
)
HISTORICAL_CONSUMPTION_LAST_ATTEMPT_STORED_STATE_KEY = (
    "historical_consumption_last_attempt"
)
HISTORICAL_GENERATION_LAST_SUCCESS_STORED_STATE_KEY = (
    "historical_generation_last_success"
)
HISTORICAL_GENERATION_LAST_ATTEMPT_STORED_STATE_KEY = (
    "historical_generation_last_attempt"
)


HISTORICAL_CONSUMPTION_LAST_SUCCESS_MAX_AGE = 2 * 60 * 60
HISTORICAL_CONSUMPTION_LAST_ATTEMPT_MAX_AGE = 5 * 60  # 5 minutes
HISTORICAL_GENERATION_LAST_SUCCESS_MAX_AGE = 2 * 60 * 60
HISTORICAL_GENERATION_LAST_ATTEMPT_MAX_AGE = 5 * 60  # 5 minutes

MEASURE_ACCUMULATED_KEY = "measure_accumulated"
MEASURE_INSTANT_KEY = "measure_instant"

HISTORICAL_PERIOD_LENGHT = timedelta(days=7)
SESSION_REFRESH_MIN_INTERVAL_SECONDS = 5 * 60


##
# IDeEnergyCoordinatorDataSet: types of data that can be registered in the
# coordinator to be fetched
class IDeEnergyCoordinatorDataSet(enum.Enum):
    HISTORICAL_CONSUMPTION = enum.auto()
    HISTORICAL_GENERATION = enum.auto()
    POWER_DEMAND_PEAKS = enum.auto()
    YESTERDAY_TOTAL = enum.auto()


##
# IDeEnergyDataCoordinatorData: data stored inside the coordinator
type IDeEnergyDataCoordinatorData = dict[
    IDeEnergyCoordinatorDataSet, list[HistoricalState] | float | None
]


class IDeEnergyDataCoordinator(DataUpdateCoordinator[IDeEnergyDataCoordinatorData]):
    def __init__(
        self,
        hass: HomeAssistant,
        client: ideenergy.Client,
        config_entry_state: IDeEnergyConfigEntryState,
        update_interval: timedelta | None = None,
    ):
        name = f"{client} coordinator" if client else "i-de coordinator"
        super().__init__(hass, LOGGER, name=name, update_interval=update_interval)

        # Use dataset names as keys so all counter accesses are consistent.
        self.dataset_counter = {ds.name: 0 for ds in IDeEnergyCoordinatorDataSet}
        self.data = {k: None for k in IDeEnergyCoordinatorDataSet}

        self._client = client
        self._config_entry_state = config_entry_state
        self._yesterday_total_last_refresh: datetime | None = None
        self._yesterday_total_query_date: str | None = None
        # setup_entry already fetched contract details, so a fresh session exists
        # at startup and we can skip immediate re-login on first coordinator refresh.
        self._last_session_refresh_monotonic: float | None = perf_counter()

        # Reference to the HistoricalConsumption entity, registered after setup.
        # Used by the manual-fetch service to obtain statistic metadata for backfill.
        self._historical_consumption_entity: Any | None = None

    @property
    def yesterday_total_last_refresh(self) -> str | None:
        if self._yesterday_total_last_refresh is None:
            return None
        return self._yesterday_total_last_refresh.isoformat()

    @property
    def yesterday_total_last_refresh_dt(self) -> datetime | None:
        """Return the last successful refresh as an aware datetime (Europe/Madrid)."""
        return self._yesterday_total_last_refresh

    @property
    def yesterday_total_query_date(self) -> str | None:
        return self._yesterday_total_query_date

    # ── Manual-fetch tracking (backed by persistent store) ──────────────────

    @property
    def manual_last_success_time_dt(self) -> datetime | None:
        """Return the last successful manual fetch as an aware datetime or None."""
        ts = self._config_entry_state.data.get(MANUAL_LAST_SUCCESS_TIME_KEY)
        if ts is None:
            return None
        try:
            return dt_util.utc_from_timestamp(float(ts)).astimezone(LOCAL_TZ)
        except (TypeError, ValueError):
            return None

    @property
    def manual_last_requested_date(self) -> str | None:
        """Return the last manually requested date (ISO format) or None."""
        return self._config_entry_state.data.get(MANUAL_LAST_REQUESTED_DATE_KEY)

    @property
    def manual_last_result_summary(self) -> str | None:
        """Return a human-readable summary of the last manual fetch result."""
        return self._config_entry_state.data.get(MANUAL_LAST_RESULT_SUMMARY_KEY)

    @property
    def manual_last_backfill_status(self) -> str | None:
        """Return the backfill status string from the last manual fetch."""
        return self._config_entry_state.data.get(MANUAL_LAST_BACKFILL_STATUS_KEY)

    @staticmethod
    def _truncate_repr(value, max_len: int = 2000) -> str:
        text = repr(value)
        if len(text) <= max_len:
            return text
        return f"{text[:max_len]}... <truncated {len(text) - max_len} chars>"

    def _response_summary(self, response) -> dict[str, str | int | float | bool]:
        summary: dict[str, str | int | float | bool] = {
            "type": type(response).__name__,
        }

        for attr in ("status", "status_code", "ok"):
            if hasattr(response, attr):
                summary[attr] = getattr(response, attr)

        if isinstance(response, dict):
            summary["keys_count"] = len(response)
        elif isinstance(response, (list, tuple, set)):
            summary["items_count"] = len(response)

        if hasattr(response, "periods"):
            periods = getattr(response, "periods")
            try:
                summary["periods_count"] = len(periods)
            except TypeError:
                pass

        if hasattr(response, "demands"):
            demands = getattr(response, "demands")
            try:
                summary["demands_count"] = len(demands)
            except TypeError:
                pass

        return summary

    def _response_payload_details(self, response) -> dict[str, str]:
        details: dict[str, str] = {
            "repr": self._truncate_repr(response),
        }

        if isinstance(response, dict):
            details["dict"] = self._truncate_repr(response)
        elif isinstance(response, (list, tuple)):
            details["items"] = self._truncate_repr(response)

        for attr in ("periods", "demands", "body", "text", "content", "json"):
            if hasattr(response, attr):
                value = getattr(response, attr)
                if callable(value):
                    details[attr] = f"<callable {type(value).__name__}>"
                else:
                    details[attr] = self._truncate_repr(value)

        return details

    async def _async_notify(self, title: str, message: str) -> None:
        """Send a persistent notification to Home Assistant.

        Do not set a fixed notification_id so each notification is appended
        instead of replacing the previous one.
        """
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": title,
                "message": message,
            },
        )

    async def _async_api_call(
        self,
        api_name: str,
        afn: Callable,
        **kwargs,
    ):
        """Call the REST client logging request/response for diagnostics."""
        LOGGER.debug("API request %s: %r", api_name, kwargs)
        started = perf_counter()
        try:
            response = await afn(**kwargs)
        except Exception:
            elapsed_ms = (perf_counter() - started) * 1000
            LOGGER.exception(
                "API error %s (%.0f ms). request=%r",
                api_name,
                elapsed_ms,
                kwargs,
            )
            raise

        elapsed_ms = (perf_counter() - started) * 1000
        LOGGER.debug(
            "API response %s (%.0f ms). summary=%s",
            api_name,
            elapsed_ms,
            self._response_summary(response),
        )
        LOGGER.debug(
            "API response %s payload=%s",
            api_name,
            self._response_payload_details(response),
        )
        return response

    def activate_dataset(self, dataset: IDeEnergyCoordinatorDataSet) -> None:
        self.dataset_counter[dataset.name] += 1
        if self.dataset_counter[dataset.name] == 1:
            LOGGER.debug(f"dataset {dataset.name} enabled")
            # Fix a better place for this call, it's sub-optimal
            self.hass.async_create_task(self.async_request_refresh())

        LOGGER.debug(
            f"dataset {dataset.name} ref_count incremented (count={self.dataset_counter[dataset.name]})"
        )

    def deactivate_dataset(self, dataset: IDeEnergyCoordinatorDataSet) -> None:
        if self.dataset_counter[dataset.name] > 0:
            self.dataset_counter[dataset.name] -= 1

        LOGGER.debug(
            f"dataset {dataset.name} ref_count decremented (count={self.dataset_counter[dataset.name]})"
        )
        if self.dataset_counter[dataset.name] == 0:
            LOGGER.debug(f"dataset {dataset.name} disabled")

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

        active_datasets = [k for k, v in self.dataset_counter.items() if v > 0]
        dsstr = ", ".join(active_datasets)
        LOGGER.debug(f"datasets enabled: {dsstr}")

        updated_data = {}

        if (
            self.dataset_counter[IDeEnergyCoordinatorDataSet.HISTORICAL_CONSUMPTION.name]
            > 0
            or self.dataset_counter[IDeEnergyCoordinatorDataSet.YESTERDAY_TOTAL.name]
            > 0
        ):
            try:
                historical_consumption, yesterday_consumption = (
                    await self._async_get_historical_consumption_bundle()
                )
            except ideenergy.ClientError:
                LOGGER.exception(
                    "HISTORICAL_CONSUMPTION/YESTERDAY_TOTAL: error updating"
                )
            else:
                if (
                    self.dataset_counter[
                        IDeEnergyCoordinatorDataSet.HISTORICAL_CONSUMPTION.name
                    ]
                    > 0
                ):
                    updated_data[IDeEnergyCoordinatorDataSet.HISTORICAL_CONSUMPTION] = (
                        historical_consumption
                    )
                    if historical_consumption is None:
                        LOGGER.warning("HISTORICAL_CONSUMPTION: update returned None")

                if (
                    self.dataset_counter[
                        IDeEnergyCoordinatorDataSet.YESTERDAY_TOTAL.name
                    ]
                    > 0
                ):
                    updated_data[IDeEnergyCoordinatorDataSet.YESTERDAY_TOTAL] = (
                        yesterday_consumption
                    )
                    if yesterday_consumption is None:
                        LOGGER.warning("YESTERDAY_TOTAL: update returned None")

        fns = {
            IDeEnergyCoordinatorDataSet.HISTORICAL_GENERATION: self._async_get_historical_generation,
            IDeEnergyCoordinatorDataSet.POWER_DEMAND_PEAKS: self._async_get_power_demand_peaks,
        }
        for ds, fn in fns.items():
            if self.dataset_counter[ds.name] > 0:
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
        data = await self._async_api_call(
            "get_measure",
            self._client.get_measure,
        )
        return {
            MEASURE_ACCUMULATED_KEY: data.accumulate,
            MEASURE_INSTANT_KEY: data.instant,
        }

    async def _async_get_historical_consumption(self) -> list[HistoricalState] | None:
        historical_consumption, _ = await self._async_get_historical_consumption_bundle()
        return historical_consumption

    async def _async_get_historical_consumption_bundle(
        self,
    ) -> tuple[list[HistoricalState] | None, float | None]:
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

        has_cached_states = (
            self.data.get(IDeEnergyCoordinatorDataSet.HISTORICAL_CONSUMPTION) is not None
        )
        has_cached_yesterday_consumption = (
            self.data.get(IDeEnergyCoordinatorDataSet.YESTERDAY_TOTAL) is not None
        )

        if self.state_timestamp_is_too_recent(
            HISTORICAL_CONSUMPTION_LAST_SUCCESS_STORED_STATE_KEY,
            HISTORICAL_CONSUMPTION_LAST_SUCCESS_MAX_AGE,
        ):
            if has_cached_states and has_cached_yesterday_consumption:
                LOGGER.debug(
                    f"{self._client}: current data for {IDeEnergyCoordinatorDataSet.HISTORICAL_CONSUMPTION} is too recent"
                )
                return None, None

            LOGGER.debug(
                "%s: %s marked as recent but no cached states are loaded; forcing refresh",
                self._client,
                IDeEnergyCoordinatorDataSet.HISTORICAL_CONSUMPTION,
            )

        if self.state_timestamp_is_too_recent(
            HISTORICAL_CONSUMPTION_LAST_ATTEMPT_STORED_STATE_KEY,
            HISTORICAL_CONSUMPTION_LAST_ATTEMPT_MAX_AGE,
        ):
            if has_cached_states and has_cached_yesterday_consumption:
                LOGGER.debug(
                    f"{self._client}: last attempt for {IDeEnergyCoordinatorDataSet.HISTORICAL_CONSUMPTION} is too recent"
                )
                return None, None

            LOGGER.debug(
                "%s: %s last attempt is recent but no cached states are loaded; forcing refresh",
                self._client,
                IDeEnergyCoordinatorDataSet.HISTORICAL_CONSUMPTION,
            )

        local_now = dt_util.now().astimezone(LOCAL_TZ)
        yesterday_date = (local_now - timedelta(days=1)).date()
        start = datetime(
            year=yesterday_date.year,
            month=yesterday_date.month,
            day=yesterday_date.day,
        )
        end = start + timedelta(days=1)
        self._yesterday_total_query_date = yesterday_date.strftime("%d-%m-%Y")

        try:
            now_monotonic = perf_counter()
            should_refresh_session = (
                self._last_session_refresh_monotonic is None
                or now_monotonic - self._last_session_refresh_monotonic
                >= SESSION_REFRESH_MIN_INTERVAL_SECONDS
            )

            if should_refresh_session:
                # i-DE sessions can expire between coordinator updates. Re-login
                # before requesting consumption to avoid stale auth state.
                await self._async_api_call(
                    "login",
                    self._client.login,
                )

                # Contract context is session-scoped on i-DE. Refresh it after
                # login so consumption calls target the configured contract.
                await self._async_api_call(
                    "get_contract_details",
                    self._client.get_contract_details,
                )
                self._last_session_refresh_monotonic = perf_counter()
            else:
                LOGGER.debug(
                    "Skipping session refresh: previous login+contract refresh was %.1f seconds ago",
                    now_monotonic - self._last_session_refresh_monotonic,
                )

            data = await self._async_api_call(
                "get_historical_consumption",
                self._client.get_historical_consumption,
                start=start,
                end=end,
            )
        except Exception:
            await self.async_save_timestamp_at_state(
                HISTORICAL_CONSUMPTION_LAST_ATTEMPT_STORED_STATE_KEY
            )
            error_time = dt_util.now().astimezone(LOCAL_TZ).strftime("%d/%m/%Y %H:%M")
            await self._async_notify(
                title="i-DE: Error al obtener consumo",
                message=(
                    f"No se pudieron obtener los datos de consumo de i-DE.\n"
                    f"Fecha consultada: {self._yesterday_total_query_date}\n"
                    f"Hora del error: {error_time}"
                ),
            )
            raise

        await self.async_save_timestamp_at_state(
            HISTORICAL_CONSUMPTION_LAST_SUCCESS_STORED_STATE_KEY
        )
        self._yesterday_total_last_refresh = dt_util.now().astimezone(LOCAL_TZ)

        periods = getattr(data, "periods", None)
        if periods is None:
            periods = []

        if len(periods) != 24:
            LOGGER.warning(
                "get_historical_consumption returned %d periods for yesterday (expected 24)",
                len(periods),
            )

        hist_states = [as_historical_state(pv) for pv in periods]
        hist_states = [hs for hs in hist_states if hs is not None]

        total_raw = getattr(data, "total", None)
        yesterday_consumption = float(total_raw) if total_raw is not None else None

        refresh_time = self._yesterday_total_last_refresh.strftime("%d/%m/%Y %H:%M")
        periods_ok = len(hist_states)
        total_str = (
            f"{yesterday_consumption:.0f} Wh" if yesterday_consumption is not None else "desconocido"
        )
        await self._async_notify(
            title="i-DE: Consumo actualizado",
            message=(
                f"Datos de consumo cargados correctamente.\n"
                f"Fecha consultada: {self._yesterday_total_query_date}\n"
                f"Periodos recibidos: {periods_ok}/24\n"
                f"Total ayer: {total_str}\n"
                f"Hora de actualización: {refresh_time}"
            ),
        )

        return hist_states, yesterday_consumption

    async def _async_get_historical_generation(self) -> list[HistoricalState] | None:
        return await self._async_get_historical_generic(
            self._client.get_historical_generation,
            dataset=IDeEnergyCoordinatorDataSet.HISTORICAL_GENERATION,
            last_success_state_key=HISTORICAL_GENERATION_LAST_SUCCESS_STORED_STATE_KEY,
            last_success_max_age=HISTORICAL_GENERATION_LAST_SUCCESS_MAX_AGE,
            last_attempt_state_key=HISTORICAL_GENERATION_LAST_ATTEMPT_STORED_STATE_KEY,
            last_attempt_max_age=HISTORICAL_GENERATION_LAST_ATTEMPT_MAX_AGE,
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

        data = await self._async_api_call(
            "get_historical_power_demand",
            self._client.get_historical_power_demand,
        )
        hist_states = [
            historical_power_demand_as_historical_state(dai) for dai in data.demands
        ]
        hist_states = [hs for hs in hist_states if hs is not None]

        return hist_states

    async def _async_get_historical_generic(
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

        current_data = self.data.get(dataset)
        has_cached_states = current_data is not None

        if self.state_timestamp_is_too_recent(
            last_success_state_key,
            last_success_max_age,
        ):
            if not has_cached_states:
                LOGGER.debug(
                    "%s: %s marked as recent but no cached states are loaded; forcing refresh",
                    self._client,
                    dataset,
                )
            else:
                LOGGER.debug(f"{self._client}: current data for {dataset} is too recent")
                return None

        if self.state_timestamp_is_too_recent(
            last_attempt_state_key,
            last_attempt_max_age,
        ):
            if has_cached_states:
                LOGGER.debug(f"{self._client}: last attempt for {dataset} is too recent")
                return None

            LOGGER.debug(
                "%s: %s last attempt is recent but no cached states are loaded; forcing refresh",
                self._client,
                dataset,
            )

        end = datetime.today()
        start = end - HISTORICAL_PERIOD_LENGHT

        api_name = getattr(afn, "__name__", repr(afn))
        try:
            data = await self._async_api_call(
                api_name,
                afn,
                start=start,
                end=end,
            )
        except Exception:
            await self.async_save_timestamp_at_state(last_attempt_state_key)
            raise

        await self.async_save_timestamp_at_state(last_success_state_key)

        hist_states = [as_historical_state(pv) for pv in data.periods]
        hist_states = [hs for hs in hist_states if hs is not None]
        return hist_states

    # ── Entity registration ─────────────────────────────────────────────────

    def register_historical_consumption_entity(self, entity: Any) -> None:
        """Store a reference to the HistoricalConsumption sensor entity.

        Called by the entity itself in *async_added_to_hass*.  The reference is
        used later to retrieve the correct StatisticMetaData for backfill
        without hard-coding the statistic_id format.
        """
        self._historical_consumption_entity = entity
        LOGGER.debug("HistoricalConsumption entity registered for backfill: %s", entity.entity_id)

    # ── Manual-fetch service implementation ─────────────────────────────────

    async def async_fetch_historical_consumption_for_date(
        self,
        target_date: date,
        *,
        force: bool = False,
        notify: bool = True,
        backfill_statistics: bool = True,
    ) -> dict[str, Any]:
        """Fetch historical consumption for *target_date* on demand.

        Refreshes the i-DE session when needed, queries the API, optionally
        writes/updates recorder statistics via :func:`backfill.async_backfill_day_statistics`,
        saves manual-execution state to the persistent store, and optionally
        sends a persistent HA notification.

        Returns a dict with keys:
            date, periods, total, periods_count, warnings, backfill_status
        """
        # Lazy import to avoid circular dependency issues at module level
        from .backfill import async_backfill_day_statistics  # noqa: PLC0415

        query_date_str = target_date.strftime("%d-%m-%Y")
        start = datetime(
            year=target_date.year,
            month=target_date.month,
            day=target_date.day,
        )
        end = start + timedelta(days=1)

        # ── Session refresh ──────────────────────────────────────────────────
        try:
            now_monotonic = perf_counter()
            should_refresh_session = (
                self._last_session_refresh_monotonic is None
                or now_monotonic - self._last_session_refresh_monotonic
                >= SESSION_REFRESH_MIN_INTERVAL_SECONDS
            )
            if should_refresh_session or force:
                await self._async_api_call("login", self._client.login)
                await self._async_api_call(
                    "get_contract_details", self._client.get_contract_details
                )
                self._last_session_refresh_monotonic = perf_counter()
            else:
                LOGGER.debug(
                    "Manual fetch: skipping session refresh (%.1f s since last)",
                    now_monotonic - self._last_session_refresh_monotonic,
                )

            data = await self._async_api_call(
                "get_historical_consumption",
                self._client.get_historical_consumption,
                start=start,
                end=end,
            )
        except Exception as exc:
            error_msg = str(exc)
            exec_time = dt_util.now().astimezone(LOCAL_TZ).strftime("%d/%m/%Y %H:%M")

            self._config_entry_state.data[MANUAL_LAST_REQUESTED_DATE_KEY] = target_date.isoformat()
            self._config_entry_state.data[MANUAL_LAST_ERROR_KEY] = error_msg
            self._config_entry_state.data[MANUAL_LAST_RESULT_SUMMARY_KEY] = f"Error: {error_msg}"
            self._config_entry_state.data[MANUAL_LAST_BACKFILL_STATUS_KEY] = "no_ejecutado"
            await self._config_entry_state.async_save()

            if notify:
                await self._async_notify(
                    title="i-DE: Error en lectura manual",
                    message=(
                        f"No se pudieron obtener los datos.\n"
                        f"Fecha consultada: {query_date_str}\n"
                        f"Motivo: {error_msg}\n"
                        f"Hora del error: {exec_time}"
                    ),
                )
            raise

        # ── Parse response ───────────────────────────────────────────────────
        periods = getattr(data, "periods", None) or []
        warnings: list[str] = []

        def _as_historical_state(
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
                LOGGER.error("Manual fetch: invalid PeriodValue %r", pv)
                return None

        hist_states = [_as_historical_state(pv) for pv in periods]
        hist_states = [hs for hs in hist_states if hs is not None]

        total_raw = getattr(data, "total", None)
        total: float | None = float(total_raw) if total_raw is not None else None

        if len(hist_states) != 24:
            warnings.append(
                f"Se esperaban 24 periodos, se recibieron {len(hist_states)}"
            )

        if not hist_states:
            warnings.append("i-DE no devolvió datos para esta fecha")

        exec_time = dt_util.now().astimezone(LOCAL_TZ).strftime("%d/%m/%Y %H:%M")

        # ── Optional backfill ────────────────────────────────────────────────
        backfill_status = "desactivado"
        backfill_result: dict[str, Any] = {}

        if backfill_statistics and hist_states:
            if self._historical_consumption_entity is not None:
                try:
                    metadata = self._historical_consumption_entity.get_statistic_metadata()
                    backfill_result = await async_backfill_day_statistics(
                        self.hass, metadata, hist_states, force=force
                    )
                    backfill_status = (
                        f"OK — insertados: {backfill_result['inserted']}, "
                        f"actualizados: {backfill_result['updated']}, "
                        f"omitidos: {backfill_result['skipped']}"
                    )
                    if backfill_result.get("warnings"):
                        warnings.extend(backfill_result["warnings"])
                except Exception as exc:
                    backfill_status = f"Error: {exc}"
                    warnings.append(f"Backfill fallido: {exc}")
                    LOGGER.exception(
                        "Manual fetch: error during backfill for %s", target_date
                    )
            else:
                backfill_status = "Entidad HistoricalConsumption no disponible"
                warnings.append(backfill_status)
        elif backfill_statistics and not hist_states:
            backfill_status = "omitido (sin datos)"

        # ── Persist tracking state ───────────────────────────────────────────
        self._config_entry_state.data[MANUAL_LAST_REQUESTED_DATE_KEY] = target_date.isoformat()
        self._config_entry_state.data[MANUAL_LAST_ERROR_KEY] = None
        self._config_entry_state.data[MANUAL_LAST_SUCCESS_TIME_KEY] = (
            dt_util.as_timestamp(dt_util.now())
        )
        total_str = f"{total:.0f} Wh" if total is not None else "desconocido"
        self._config_entry_state.data[MANUAL_LAST_RESULT_SUMMARY_KEY] = (
            f"OK — {len(hist_states)}/24 periodos, total={total_str}"
        )
        self._config_entry_state.data[MANUAL_LAST_BACKFILL_STATUS_KEY] = backfill_status
        await self._config_entry_state.async_save()

        # ── Persistent notification ──────────────────────────────────────────
        if notify:
            warnings_text = (
                "\nAvisos:\n" + "\n".join(f"• {w}" for w in warnings)
                if warnings
                else ""
            )
            await self._async_notify(
                title="i-DE: Lectura manual completada",
                message=(
                    f"Fecha consultada: {query_date_str}\n"
                    f"Periodos recibidos: {len(hist_states)}/24\n"
                    f"Total del día: {total_str}\n"
                    f"Hora de ejecución: {exec_time}\n"
                    f"Estado del backfill: {backfill_status}"
                    f"{warnings_text}"
                ),
            )

        return {
            "date": target_date,
            "periods": hist_states,
            "total": total,
            "periods_count": len(hist_states),
            "warnings": warnings,
            "backfill_status": backfill_status,
        }

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
