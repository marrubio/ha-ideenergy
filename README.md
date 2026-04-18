# i-DE (Iberdrola Distribución) Custom Integration for Home Assistant

<!-- HomeAssistant badges -->
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)
[![hassfest validation](https://github.com/ldotlopez/ha-ideenergy/workflows/Validate%20with%20hassfest/badge.svg)](https://github.com/ldotlopez/ha-ideenergy/actions/workflows/hassfest.yml)
[![HACS validation](https://github.com/ldotlopez/ha-ideenergy/workflows/Validate%20with%20HACS/badge.svg)](https://github.com/ldotlopez/ha-ideenergy/actions/workflows/hacs.yml)

<!-- Code and releases -->
![GitHub Release (latest SemVer including pre-releases)](https://img.shields.io/github/v/release/ldotlopez/ha-ideenergy?include_prereleases)
[![CodeQL](https://github.com/ldotlopez/ha-ideenergy/actions/workflows/codeql-analysis.yml/badge.svg)](https://github.com/ldotlopez/ha-ideenergy/actions/workflows/codeql-analysis.yml)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/ambv/black)

[ideenergy](https://github.com/ldotlopez/ideenergy) integration for [home-assistant](https://home-assistant.io/)

i-DE (Iberdrola Distribución) Custom Integration for Home Assistant, providing sensors for Spanish Energy Distributor [i-DE](https://i-de.es).

This integration requires an **advanced** user profile on i-DE website.

**⚠️ Make sure to read the '[FAQ](https://github.com/ldotlopez/ha-ideenergy/blob/main/FAQ.md)', 'Dependencies', and 'Warnings' sections.**


## Features

* Integration with the Home Assistant Energy Panel.

* Daily consumption data by hourly slots (24 readings from the previous day).

* Historical sensors (both consumption and solar generation) with better (sub-kWh) precision. This data is not real-time and usually has a 24-hour to 48-hour offset.

* New device entities:
  * Total consumption of yesterday.
  * Last consumption refresh date.

* A Home Assistant notification is sent with the result of the i-DE API call.

* Support for multiple contracts (service points).

* Configuration through [Home Assistant Interface](https://developers.home-assistant.io/docs/config_entries_options_flow_handler) without the need to edit YAML files.

* API data retrieval is scheduled only at integration startup and once per day at 12:30, when previous-day data is expected to be available.

* Fully [asynchronous](https://developers.home-assistant.io/docs/asyncio_index) and integrated with Home Assistant.

## Adaptation notes

This adaptation no longer reads instant meter consumption. Real-time readings required multiple calls, waiting for data readiness, and handling fragile 24-hour session behavior.

The integration now reads previous-day consumption by hourly slots (24 values). This information is usually available after 10:00 the next day, so data retrieval is executed at 12:30 to provide additional margin.


## Dependencies

You must have an i-DE username and access to the client website. You may register here: [Área Clientes | I-DE - Grupo Iberdrola](https://www.i-de.es/consumidores/web/guest/login).

It is also necessary to have an "Advanced User" profile. If you do not already have one, you need to submit the request form from your [Profile Area](https://www.i-de.es/consumidores/web/home/personal-area/userData).


## Installation

### Using [HACS](https://hacs.xyz/) (recommended)

1. Copy this repository URL: [https://github.com/ldotlopez/ha-ideenergy](https://github.com/ldotlopez/ha-ideenergy/)

2. In the HACS section, add this repository as a custom one:


  - In the "Repository" field, paste the URL copied before.
  - On the "Category" select "Integration"
  - Click the "Download" button and download the latest version.

  ![Custom repository](https://user-images.githubusercontent.com/59612788/171965822-4a89c14e-9eb2-4134-8de2-1d3f380663e4.png)

3. Restart HA

4. Configure the integration

  - (Option A) Click the "Add integration" button → [![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ideenergy)

  - (Option B) Go to "Settings"  "Devices & Services" and click "+ ADD INTEGRATION" and select "i-de.es energy sensors".  
    ![image](https://user-images.githubusercontent.com/59612788/171966005-e58f6b88-a952-4033-82c6-b1d4ea665873.png)

5. Follow the configuration steps: provide your credentials for access to i-DE and select the contract that you want to monitor. (Should you need to add more contracts, just follow the previous step as many times as needed).


### Manually

1. Download/clone this repository: [https://github.com/ldotlopez/ha-ideenergy](https://github.com/ldotlopez/ha-ideenergy/)

2. Copy the `custom_components/ideenergy` folder into the `custom_components` folder of your Home Assistant installation.

3. Restart HA

4. Configure the integration

  - (Option A) Click on this button → [![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ideenergy)
  - (Option B) Go to "Settings" → "Devices & Services" and click "+ ADD INTEGRATION" and select "i-de.es energy sensors".  
    ![image](https://user-images.githubusercontent.com/59612788/171966005-e58f6b88-a952-4033-82c6-b1d4ea665873.png)

5. Follow the configuration steps: provide your credentials for access to i-DE and select the contract that you want to monitor. (Should you need to add more contracts, just follow the previous step as many times as needed).

## Snapshots

*Accumulated energy sensor*

![snapshot](screenshots/accumulated.png)

*Historical energy sensor*

![snapshot](screenshots/historical.png)

*Configuration wizard*

![snapshot](screenshots/configuration-1.png)
![snapshot](screenshots/configuration-2.png)

## Warnings
This extension provides a 'historical' sensor to incorporate data from the past into the Home Assistant database. For your own safety, the sensor is not enabled by default and must be enabled manually.

☠️ The historical sensor is based on a **highly experimental hack** and can break and/or corrupt your database and/or statistics. **Use at your own risk**.

## License

This project is licensed under the GNU General Public License v3.0 License - see the LICENSE file for details


## Disclaimer

THIS PROJECT IS NOT IN ANY WAY ASSOCIATED WITH OR RELATED TO THE IBERDROLA GROUP COMPANIES OR ANY OTHER. The information here and online is for educational and resource purposes only and therefore the developers do not endorse or condone any inappropriate use of it, and take no legal responsibility for the functionality or security of your devices.
