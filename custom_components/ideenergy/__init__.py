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

import ideenergy
from homeassistant.config_entries import ConfigEntry, ConfigEntryNotReady
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_time_change
from homeassistant.loader import async_get_loaded_integration

from .const import CONF_CONTRACT, DOMAIN, LOCAL_TZ, UPDATE_HOUR, UPDATE_MINUTE
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
    await coordinator.async_config_entry_first_refresh()
    if not coordinator.last_update_success:
        raise ConfigEntryNotReady

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
