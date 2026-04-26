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


import logging
import os
from datetime import date

import ideenergy
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryNotReady
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, ServiceCall, callback, dt_util
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_time_change
from homeassistant.loader import async_get_loaded_integration

from .const import (
    CONF_CONTRACT,
    DOMAIN,
    LOCAL_TZ,
    MANUAL_MAX_DAYS_BACK,
    SERVICE_FETCH_DAY_READING,
    UPDATE_HOUR,
    UPDATE_MINUTE,
)
from .coordinator import IDeEnergyDataCoordinator
from .data import IntegrationIDeEnergyConfigEntry, IntegrationIDeEnergyRunTimeData
from .store import IDeEnergyConfigEntryState

PLATFORMS: list[str] = ["sensor"]

LOGGER = logging.getLogger(__name__)


def setup_domain_data(hass: HomeAssistant) -> None:
    """Set up shared data for all config entries."""
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntegrationIDeEnergyConfigEntry,
) -> bool:
    """Set up this integration using UI."""
    setup_domain_data(hass)

    ##
    # Setup API
    client = get_i_de_energy_api(hass, entry)

    try:
        contract_details = await client.get_contract_details()
    except ideenergy.ClientError as e:
        LOGGER.debug(f"Unable to initialize integration: {e}")
        return False

    device_info = get_i_de_energy_device_info(contract_details)

    ##
    # Setup config entry state
    config_entry_state = IDeEnergyConfigEntryState(hass, entry)
    await config_entry_state.async_load()

    ##
    # Setup coordinator
    # https://developers.home-assistant.io/docs/integration_fetching_data
    # update_interval=None disables automatic polling; a daily time trigger is
    # registered below so the fetch runs at a predictable hour when i-DE data
    # is guaranteed to be available (avoids early-morning "no data yet" errors).
    coordinator = IDeEnergyDataCoordinator(
        hass=hass,
        client=client,
        config_entry_state=config_entry_state,
        update_interval=None,
    )
    # Do NOT call async_config_entry_first_refresh() to avoid API calls on startup.
    # Scheduled refresh at 12:30 and manual calls only.

    ##
    # Setup integration runtime data
    entry.runtime_data = IntegrationIDeEnergyRunTimeData(
        coordinator=coordinator,
        # config_entry_state=config_entry_state,
        integration=async_get_loaded_integration(hass, entry.domain),
        device_info=device_info,
    )

    ##
    # Schedule daily refresh at UPDATE_HOUR:UPDATE_MINUTE (local time)
    @callback
    def _schedule_daily_refresh(now=None) -> None:  # noqa: ARG001
        LOGGER.debug(
            "Scheduled daily refresh triggered at %s:%02d (Europe/Madrid)",
            UPDATE_HOUR,
            UPDATE_MINUTE,
        )
        hass.async_create_task(coordinator.async_request_refresh())

    entry.async_on_unload(
        async_track_time_change(
            hass,
            _schedule_daily_refresh,
            hour=UPDATE_HOUR,
            minute=UPDATE_MINUTE,
            second=0,
        )
    )

    ##
    # Forward setups
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    ##
    # Register manual-fetch service (only once for the whole domain)
    _async_register_services(hass)

    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: IntegrationIDeEnergyConfigEntry,
) -> bool:
    """Handle removal of an entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(
    hass: HomeAssistant,
    entry: IntegrationIDeEnergyConfigEntry,
) -> None:
    """Reload config entry."""
    await hass.config_entries.async_reload(entry.entry_id)


# ──────────────────────────────────────────────────────────────────────────────
# Manual-fetch service
# ──────────────────────────────────────────────────────────────────────────────

_FETCH_DAY_READING_SCHEMA = vol.Schema(
    {
        vol.Required("date"): cv.date,
        vol.Optional("entry_id"): cv.string,
        vol.Optional("notify", default=True): cv.boolean,
        vol.Optional("force", default=False): cv.boolean,
        vol.Optional("backfill_statistics", default=True): cv.boolean,
    }
)


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    """Register domain services (idempotent — safe to call per config entry)."""
    if hass.services.has_service(DOMAIN, SERVICE_FETCH_DAY_READING):
        return

    async def _handle_fetch_day_reading(call: ServiceCall) -> None:
        target_date: date = call.data["date"]
        entry_id: str | None = call.data.get("entry_id")
        notify: bool = call.data["notify"]
        force: bool = call.data["force"]
        backfill_statistics: bool = call.data["backfill_statistics"]

        # ── Validate date ────────────────────────────────────────────────────
        today = dt_util.now().astimezone(LOCAL_TZ).date()
        if target_date >= today:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="date_not_in_past",
            )
        if (today - target_date).days > MANUAL_MAX_DAYS_BACK:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="date_too_far_back",
            )

        # ── Resolve config entries ────────────────────────────────────────────
        matching: list[IntegrationIDeEnergyConfigEntry] = [
            e
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.runtime_data is not None
            and (entry_id is None or e.entry_id == entry_id)
        ]
        if not matching:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_active_entry",
            )

        # ── Execute ───────────────────────────────────────────────────────────
        for entry in matching:
            coordinator = entry.runtime_data.coordinator
            await coordinator.async_fetch_historical_consumption_for_date(
                target_date,
                force=force,
                notify=notify,
                backfill_statistics=backfill_statistics,
            )
            # Notify all coordinator entities so diagnostic sensors refresh
            coordinator.async_set_updated_data(coordinator.data)

    hass.services.async_register(
        DOMAIN,
        SERVICE_FETCH_DAY_READING,
        _handle_fetch_day_reading,
        schema=_FETCH_DAY_READING_SCHEMA,
    )
    LOGGER.debug("Registered service %s.%s", DOMAIN, SERVICE_FETCH_DAY_READING)


# async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry):
#     raise NotImplementedError()
#
#     api = get_i_de_energy_api(hass, entry)
#
#     try:
#         contract_details = await api.get_contract_details()
#     except ideenergy.ClientError as e:
#         LOGGER.debug(f"Unable to initialize integration: {e}")
#         return False
#
#     # update_integration(hass, entry, get_i_de_energy_device_info(contract_details))
#     return True


def get_i_de_energy_api(hass: HomeAssistant, entry: ConfigEntry):

    if bool(os.environ.get("HASS_I_DE_MOCK", "")):
        ClientCls = ideenergy.MockClient
    else:
        ClientCls = ideenergy.Client

    return ClientCls(
        session=async_get_clientsession(hass),
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        contract=entry.data[CONF_CONTRACT],
    )


def get_i_de_energy_device_info(contract_details):
    return DeviceInfo(
        identifiers={
            ("cups", contract_details["cups"]),
        },
        name=contract_details["cups"],
        manufacturer=contract_details["listContador"][0]["tipMarca"],
    )
