## Step 1: Integration Removal and Data Cleanup
Before cleaning up the database, the integration must be fully removed from the Home Assistant core to prevent it from re-creating entities.

1.  **Delete Old Integration:** Navigate to **Settings > Devices & Services**, locate the integration, and select **Delete**.
2.  **Restart Home Assistant:** Perform a full restart (**Developer Tools > YAML > Restart**) to ensure all entities are purged from the current state.
3.  **Navigate to Statistics:** Go to **Developer Tools > Statistics**.
4.  **Search for Sensors:** Type `sensor.es` into the search filter.
5.  **Clean Statistics:** **Delete** all associated statistics entries **except** for those labeled as "Historical Consumption." Keeping these is vital for preserving your long-term energy data.

---

## Step 2: Service Shutdown and File Removal
To prevent file-in-use errors and data corruption, stop the core service.

1.  **Stop Home Assistant.**
2.  Access your configuration folder (via SSH or Samba) and delete the following:
    * `config/custom_components/ideenergy`
    * Any files matching `config/.storage/ideenergy_*`
3.  **Manual Registry Cleanup:** Open `.storage/core.entity_registry` and `.storage/core.device_registry`. Carefully remove any JSON blocks referencing `sensor.es12345...`.

---

## Step 3: Database Migration (SQLite)
You must manually rename the historical metadata to match the new schema requirements (`sensor:es..._historical_consumption`).

**Note the new schema is using ':' instead of '.' and it should end with `_historical_consumption` **

1.  Open your `home-assistant_v2.db` using an SQLite client.
2.  **Identify the metadata ID:**
    ```sql
    SELECT * FROM statistics_meta WHERE statistic_id LIKE 'sensor.es%';
    ```
You should get something like this:
```
sqlite> select * from statistics_meta where statistic_id  like 'sensor.es%';
42|sensor.es1234567890123456xy_accumulated|recorder|kWh||1||0|energy
113|sensor.es1234567890123456xy_instant_power_demand|recorder|W||0||1|power
143|sensor.es1234567890123456xy_historical|recorder|kWh|0|1|Historical Consumption Statistics|0|energy
```

In the example, the row 143 is the one we are looking for.

3.  **Update the record:** Use the ID found in the previous step (e.g., `143`) to update the metadata:
    ```sql
    UPDATE statistics_meta
    SET
        statistic_id = 'sensor:es1234567890123456xy_historical_consumption',
        name = 'ES1234567890123456XY Historical Consumption',
        source = 'sensor',
        unit_class = 'energy'
    WHERE id = 143;
    ```

---

## Step 4: Staged Installation
1.  **Start Home Assistant.**
2.  Verify the renamed statistic is visible and correctly formatted.
3.  **Note the date** of the last registered month in your energy dashboard.
4.  **Stop Home Assistant** again.

---

## Step 5: Install Version 3.0.0 Alpha
1.  Copy the new `3.0.0 alpha` files into `config/custom_components/ideenergy`.
2.  **Calculate the Data Gap:** Determine the number of days between your last registered consumption and today.
3.  **Modify the Coordinator:** Open `custom_components/ideenergy/coordinator.py`.
4.  Find the variable `HISTORICAL_PERIOD_LENGHT` and update it to match the number of days needed to fill the gap.

---

## Step 6: Final Configuration
1.  **Start Home Assistant.**
2.  Add the integration via the UI and complete the configuration flow.
3.  Confirm that the energy consumption gap has been successfully backfilled in the Energy Dashboard.
