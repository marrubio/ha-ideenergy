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


from logging import getLogger

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

LOGGER = getLogger(__name__)

STORAGE_VERSION = 1


class IDeEnergyConfigEntryState:
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry):
        self._store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{config_entry.entry_id}")
        self.data: dict | None = None

    async def async_load(self) -> dict:
        """Load data from storage."""
        data = await self._store.async_load()
        if data is None:
            data = {}

        if not isinstance(data, dict):
            LOGGER.warning("invalid data stored, using defaults")
            data = {}

        self.data = data
        return self.data

    async def async_save(self) -> None:
        """Save data to storage."""
        await self._store.async_save(self.data)


# # In your entity
# async def async_update(self):
#     store = self.hass.data[DOMAIN][self._entry_id]["store"]
#     store._data["counter"] += 1
#     await store.async_save()
