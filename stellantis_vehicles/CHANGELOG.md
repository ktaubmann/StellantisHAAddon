## 0.2.3

Upstream integration updated from develop da32364 (2026.9.5-beta.1) to develop 7f75d7d
(2026.10.1-beta.1).

- `remote_commands` and the command buttons follow the vehicle MQTT connection right away
  instead of on the next poll; a refused MQTT connection no longer counts as connected
- Failed commands show the vehicle's reason in `command_status` (e.g. door open, charging
  already in progress) instead of a bare result code
- OTP: a failed activation is reported as an error instead of producing an invalid code
- Token refresh timers are no longer re-armed while the add-on shuts down
- The "vehicle no longer linked" notification disappears once the vehicle answers again
- Updated French, German, Italian and Polish translations

## 0.2.2

- Keep browser-login handling in sync with the login worker: conservative
  provider rejection detection, bounded cleanup and safe phase/URL diagnostics.
  Each page step has 60 s, the whole login 240 s.
- The login page shows only known failure reasons (rejected login, failed page
  step, deadline); raw browser error text with URLs is no longer displayed.
- Restore structured ForgeRock and console diagnostics without logging raw
  page text, response bodies or console contents.

## 0.2.1

- Fix: installing or updating failed with `403 denied` from `ghcr.io/taubenhorst/…`.
  The GitHub account was renamed to `ktaubmann`; the add-on now pulls its image from
  `ghcr.io/ktaubmann/stellantis-vehicles-{arch}`
- Repository, support and image source links point to `ktaubmann/StellantisHAAddon`

## 0.2.0

Upstream integration updated from 2026.9.1 (69fddda) to develop da32364 (after 2026.9.5-beta.1).

- New entities: maintenance sensors (mileage / days before maintenance), native 80 % charge
  limit (binary sensor `battery_charging_limit`, buttons `charge_limit_on` / `charge_limit_off`),
  binary sensor `command_pending`
- While a command waits for the vehicle's answer (max. 60 s), a second one is rejected and
  logged; buttons no longer turn unavailable meanwhile
- Own charge limit (number/switch) is unavailable and does not send a stop command while the
  vehicle's native limit is active
- Token handling from upstream: retry once after HTTP 401, OAuth/MQTT token refresh with backoff
  and timers that survive unexpected errors, reauth started directly by the refresh timers
- MQTT: connect/disconnect serialized, commands check the connection first, the retry after an
  "invalid token" answer resends the right command, "vehicle asleep" (901) no longer in the history
- Logs: sensitive data filter attached once and bounded (upstream #414), also masks OTP PIN,
  ABRP token, GPS position, e-mail and password
- Fix: a stale range of 0 from a sleeping car no longer zeroes the battery level
- Command history limited to 50 entries; device manufacturer from the API brand
- Slovenian translation (15 languages)
- Stored config is written by the entry update itself (upstream no longer triggers the save)

## 0.1.1

- All 14 upstream translations with per-key English fallback; `language` option offers all of them
- Browser login uses the locale of the account's country instead of a fixed `de-DE`
- Retained offline status is delivered before a clean disconnect (entities go unavailable when the add-on stops)
- Number commands are clamped to their min/max; charge limit and refresh interval cannot be set out of range via MQTT
- Entities are rebuilt when remote commands are switched on or off; option `remote_commands: false` wins at start
- Inconsistent stored last-charge data is dropped instead of freezing the sensor
- No empty notification on a failed browser login; login-required hint stays while a vehicle still fails
- README/DOCS: unofficial community project and trademark notice; vehicle picture as `entity_picture`

## 0.1.0

First working version.

- Repository layout, `config.yaml`, Debian based Dockerfile with Playwright/Chromium
- Upstream code (stellantis.py, otp, const, utils) vendored unchanged, commit 69fddda
- `hass_shim`: stand-in for the `homeassistant` imports the upstream code uses
- Own coordinator (`base.py`): port of the upstream logic without HA (polling, command
  history, remote commands, charge limit, ABRP, last trip)
- MQTT discovery bridge (`bridge/`): all upstream entities (sensor, binary_sensor, button,
  number, switch, text, device_tracker) as one HA device per vehicle; command topics for
  buttons, numbers, switches, texts and the charge start time
- Ingress UI (`web/`): login (Chromium inside the add-on or manual via code/URL), OTP setup
  with SMS code + PIN, status page with token expiry, vehicles, notifications; disable or
  reconfigure remote commands, re-login
- Runtime (`runtime.py`): load vehicles, start coordinators, attach the bridge, retry on API
  errors, re-login after an expired session without restart
- MQTT broker from the options or from the Supervisor (`services: mqtt`)
- Clean-room Playwright login in `oauth_browser/`; real Chromium in the new headless mode
  (the headless shell is rejected by the Stellantis identity provider)
- Offline smoke tests `tests/smoke_bridge.py`, `tests/smoke_web.py`, `tests/smoke_runtime.py`
- `mobile_app` restricted to the apps supported upstream
- Real login and status retrieval verified; vehicle picture as `entity_picture`
- Docker image built and started locally (amd64, 1.9 GB); `.dockerignore`
- CI builds amd64 and aarch64 images to GHCR

Open: installation on the Pi.
