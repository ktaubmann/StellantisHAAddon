# Upstream-Herkunft

Dieses Paket ist ein unveränderter Vendor-Import aus
https://github.com/andreadegiovine/homeassistant-stellantis-vehicles
(MIT-Lizenz, siehe `LICENSE.upstream`).

- Commit: 7f75d7d4d06b01d6a29b911900902b003516146e (`develop`, 2026.10.1-beta.1)
- Version: 2026.10.1-beta.1 (laut `manifest.json`)
- Vorher: da32364 (2026.9.5-beta.1), davor 69fddda (2026.9.1)
- Übernommen: `stellantis.py`, `const.py`, `utils.py`, `exceptions.py`, `configs.json`, `manifest.json`, `otp/`, `translations/*.json` (alle 15 Sprachen)
- **Nicht** übernommen: `base.py`, `config_flow.py`, alle Plattform-Dateien (`sensor.py`, `button.py`, …), `frontend/`
- **Eigene Datei**: `base.py` — Ersatz für den HA-Coordinator, leitet an `bridge/` weiter

Beim Update auf da32364 im Shim nachgerüstet: `config_entries.ConfigEntry`,
`helpers.aiohttp_client.async_get_clientsession`, `ConfigEntry.async_start_reauth`
(→ Runtime „Login erforderlich“), `UnitOfTime.DAYS`, `SensorDeviceClass.DURATION`;
`async_update_entry` speichert jetzt selbst (Upstream ruft `_async_schedule_save` nicht mehr).

Beim Update auf 7f75d7d kein neuer Shim-Import nötig (geprüft per AST-Abgleich aller
`homeassistant.*`-Imports). Laufzeit-Abhängigkeiten, die `stellantis.py` jetzt zusätzlich nutzt:
`hass.loop.call_soon_threadsafe` + `coordinator.async_update_listeners()` (MQTT-Verbindungswechsel
aktualisiert die Entities sofort), `stellantis._mqtt_connected` (Bridge prüft es wie Upstream
zusätzlich zu `is_connected()`), `coordinator._commands_history[id]["service"]` (Fehlergrund aus
`resp_data`). Shim: `persistent_notification.async_dismiss` (Upstream löscht das
`vehicle_removed`-Repair-Issue, wir die Notification). Nicht portiert: `get_vehicle_rights` /
`async_lookup_supported_features` (Upstream nur für Diagnostics). Upstream hat `Pillow` aus dem
Manifest entfernt (HA Core bringt es mit) — `stellantis.py` importiert es weiter, also bleibt es
in unserer `requirements.in`.

Regel: Dateien aus der Liste "Übernommen" nicht editieren. Alles, was HA erwartet,
liefert `hass_shim/`. Upstream-Update = Dateien neu kopieren, Commit-Hash hier nachziehen.
