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


from zoneinfo import ZoneInfo

CONF_CONTRACT = "contract"
CONFIG_ENTRY_VERSION = (
    3  # v2.1.3 2025-02-14
    # Change to 4 after v3 release, No migration provided, intentional
    # 4 v3.0.0a0
)
DOMAIN = "ideenergy"
LOCAL_TZ = ZoneInfo("Europe/Madrid")

# Hour and minute (local time, Europe/Madrid) at which the daily data fetch runs.
# i-DE typically publishes yesterday's data mid-morning; 12:30 is a safe window.
UPDATE_HOUR = 12
UPDATE_MINUTE = 30
