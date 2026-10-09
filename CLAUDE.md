# Projektkontext für Claude Code

Home-Assistant-Add-on (ein einziges), das die HACS-Integration
homeassistant-stellantis-vehicles und deren Playwright-OAuth-Worker in einem
Container zusammenfasst. Fahrzeuge kommen per MQTT Discovery nach HA.

## Regeln
- `stellantis_vehicles/app/stellantis_vehicles/` ist unveränderter Upstream-Code
  (siehe UPSTREAM.md dort). Nicht editieren — fehlende HA-Symbole in
  `app/hass_shim/` nachrüsten. Ausnahme: `base.py` ist eigen.
- Kein Code aus `homeassistant-stellantis-vehicles-worker-v2` übernehmen (keine Lizenz);
  `app/oauth_browser/` ist Clean-Room und bleibt es.
- Inline-Kommentare im Code auf Englisch. Nutzerseitige Doku (README.md, DOCS.md, CHANGELOG.md,
  config.yaml-Beschreibung) auf Englisch; interne Doku (CLAUDE.md, docs/PLANUNG.md) auf Deutsch.
- Base-Image Debian (Playwright braucht glibc), Images werden per CI vorgebaut.

## Weitere Doku
- `docs/PLANUNG.md`: vollständiges Planungsprotokoll mit Analyse, Entscheidungen, Risiken
- `stellantis_vehicles/DOCS.md`: Nutzerdoku (englisch) inkl. MQTT-Topic-Schema

## Aufbau der Bridge (Schritt 1, fertig)
- `app/stellantis_vehicles/base.py`: Port des Upstream-Coordinators (Polling, updatedAt-Vergleich,
  Leerantworten, Command-History, send_*-Kommandos, Ladelimit, ABRP, letzte Fahrt). Kein HA.
  `_sensors` ist der gemeinsame Zustand mit der Bridge — gleiche Ein-Zyklus-Verzögerung wie Upstream.
  Stand Upstream da32364: Wartungsabfrage (Fehler behalten die alten Werte, leere Antwort schaltet sie ab —
  bewusste Abweichung, Upstream lässt dann das ganze Update scheitern), Befehlssperre solange ein Befehl
  offen ist (`pending_action`, 60 s Timeout, Ablehnung als `ServiceValidationError`), History-Einträge mit
  `service`/`message`/`retried`/`sent_at` (braucht der MQTT-400-Retry in stellantis.py), max. 50 Einträge.
- `app/bridge/entities.py`: Ersatz für die Upstream-Plattformdateien. Jede Entity kennt Discovery-
  Felder, `update()` (Wert aus `coordinator.data`, schreibt `_sensors`) und `handle_command()`.
  Reihenfolge = Upstream-PLATFORMS (binary_sensor vor sensor!), sonst hinkt `last_charge` nach.
- `app/bridge/mqtt_bridge.py`: paho-Client, Discovery pro Entity, State/Attribute/Availability,
  Command-Dispatch (seriell). paho-Callbacks laufen im Netzwerk-Thread → `run_coroutine_threadsafe`.
- Abweichungen zu Upstream, durch MQTT erzwungen: keine `time`-Plattform → `battery_charging_start`
  ist ein `text` mit `HH:MM`; number/switch/text/last_charge persistieren in der Stored Config
  (`/data/config_entry.json`, Knoten `vehicles.<vin>`); Topics/unique_ids enthalten die Komponente,
  weil `battery_charging_limit` als number *und* switch existiert. `default_entity_id` braucht HA ≥ 2025.10.
- Offline-Test: `cd stellantis_vehicles && ../.venv/Scripts/python tests/smoke_bridge.py`
  (Fake-Client + Recorder statt paho, 52 Checks). Vor jeder Änderung an Bridge/Coordinator laufen lassen.

## Ingress-UI (Schritt 2, fertig)
- `app/web/setup.py`: `SetupFlow` = Ersatz für die Upstream-`config_flow.py`, ohne HTML. Schritte
  `login` → `otp` → `done`, abgeleitet aus der Stored Config (`_derive_step`). Lange Aktionen
  (Chromium-Login, Token-Requests) laufen als Task, die Seite pollt (`meta refresh`, solange `busy`).
  Speichert exakt die Upstream-Entry-Felder (`oauth`, `mqtt`, `customer_id`, `remote_commands`,
  `notifications`, `anonymize_logs`, `mobile_app`, `country_code`) über `_store()` = `save_config` +
  `update_stored_config`. Fehlertexte aus `translations/<lang>.json` (`config.error.*`).
- `app/web/server.py`: eine Seite `/`, rendert je nach Schritt; POST-Endpunkte `login`, `otp`, `sms`,
  `remote/disable`, `remote/reconfigure`, `reauth`, `notifications/dismiss`; `GET /api/state`, `/health`.
  Nur relative URLs (Ingress-Proxy). `state["vehicles"]` erwartet Dicts `{vin, type, ok, updated_at}`.
- Stolpersteine: `hass_notify()` schweigt ohne `notifications: true` in der Stored Config → Flow persistiert
  die Defaults im Konstruktor; `get_otp_code()` macht `os.mkdir` auf `<config>/.storage/<domain>` →
  `.storage` wird vor dem OTP-Schritt angelegt; `reconfigure` setzt `remote_commands=False`, daher `_force_otp`.
- Offline-Test: `tests/smoke_web.py` (aiohttp-TestClient, Fake-Client, 44 Checks).

## Runtime (Schritt 3, fertig)
- `app/runtime.py`: `Runtime.start()` = Rest von Upstream-`async_setup_entry`: `scheduled_tokens_refresh` →
  `get_user_vehicles` → `prune_stored_vehicle_configs` → Coordinator je Fahrzeug → `bridge.attach` →
  `stagger_first_poll` → `coordinator.start()`. Idempotent; API-Fehler → Retry nach 60 s; Auth-Fehler
  (`coordinator.on_auth_failed`) → `flow.reauth()` + Notification, nach Neu-Login startet `on_configured`
  die gestoppten Coordinators wieder (`coordinator.start()` startet beendete Loops neu).
- `app/main.py`: MQTT-Zugang aus `options.mqtt` oder `STELLANTIS_MQTT_*` (Supervisor-Service via `run`);
  ohne Broker läuft alles, nur ohne Publishing. `ADDON_VERSION` kommt aus `BUILD_VERSION` (Dockerfile).
- Offline-Test: `tests/smoke_runtime.py`.

## Erkenntnisse aus dem echten Login (05.09.2026)
- Die Playwright-Headless-Shell wird vom Stellantis-IdP erkannt: Gigya-Login klappt, aber der
  ForgeRock-Schritt `POST /am/json/authenticate` endet nach 30 s auf `#failedLogin`. Mit echtem
  Chromium im neuen Headless-Modus (`channel="chromium"`, kein gefälschter UA) kommt der Code.
- `browser.close()` hängt unter Windows/Python 3.14 grundsätzlich → alle Teardown-Schritte sind auf
  10 s begrenzt, `pw.stop()` räumt auf. Im Linux-Container nicht beobachtet.
- Diagnose ohne Add-on: `python app/oauth_browser/login.py --email …` (fragt Passwort ab, listet URLs).

## Docker (Schritt 4, lokal verifiziert 05.09.2026)
- Lokaler Build (Docker Desktop, amd64): `docker build --build-arg BUILD_VERSION=0.1.0 -t stellantis-vehicles:dev stellantis_vehicles`
  (`BUILD_FROM` hat einen amd64-Default; der HA-Builder überschreibt ihn). Build ~2 min, Image **1,92 GB**
  (volles Chromium + Headless-Shell + X11/GTK-Libs; die 600 MB aus der Planung waren zu optimistisch).
- Im Container: `channel="chromium"` startet (151.0.7922.34), s6-Start liefert nach ~7 s `/health`.
  Ohne Supervisor meckert bashio im `run`-Skript („Could not resolve host: supervisor") — harmlos.
- Test-Setup: temporäres `/data` mit `options.json` mounten, `docker run -d -p 8099:8099 -v <dir>:/data …`;
  Chromium-Check per `docker run --rm --entrypoint python3 … /data/chromium_check.py`.
- `.dockerignore` hält `data/` (Tokens, OTP-Pickle) und `tests/` aus dem Build-Kontext.

## CI / GHCR (Schritt 5, 05.09.2026)
- Repo `ktaubmann/StellantisHAAddon`, `origin` gesetzt, GitHub-CLI (`C:\Program Files\GitHub CLI\gh.exe`)
  ist als Repo-Owner angemeldet (Scopes repo/workflow, **kein** read:packages → Paketliste per API geht nicht;
  Sichtbarkeit stattdessen per anonymem `docker manifest inspect ghcr.io/ktaubmann/stellantis-vehicles-<arch>:0.1.0` prüfen).
- Erster Workflow-Lauf (Run 33984930360) grün: amd64 2 min, aarch64 7 min. Images:
  `ghcr.io/ktaubmann/stellantis-vehicles-{amd64,aarch64}:0.1.0` (+ `latest`).
- Repo und beide Pakete sind seit 05.09.2026 **public** (anonymer Manifest-Zugriff verifiziert, ~540/573 MB
  komprimiert). Paket-Sichtbarkeit ist eine eigene Einstellung je Paket:
  `https://github.com/users/ktaubmann/packages/container/<paket>/settings` → Change visibility.
- Der GitHub-Account hieß früher `taubenhorst`. GHCR leitet nicht um: die alten Pakete unter
  `ghcr.io/taubenhorst/…` liefern anonym 403, `image:` in den config.yaml zeigt deshalb auf `ghcr.io/ktaubmann/…`
  (0.2.0 dort anonym abrufbar, geprüft 08.10.2026).

## Mehrsprachigkeit
- Alle 15 Upstream-Übersetzungen sind vendored (seit da32364 inkl. `sl`); der Shim-Loader (`hass_shim/.../translation.py`) legt
  die gewählte Sprache über die englische Basis (fehlende Schlüssel → Englisch, nicht → Schlüsselname),
  kennt Aliase (`cs`→`cz`, `nb-NO`, `pt-BR`) und Regionalcodes (`fr-FR`→`fr`). `config.yaml` bietet alle 15 an.
- Der Chromium-Login nutzt das Locale der App-Konfiguration (`stellantis.get_config("locale")`, z. B. `de-DE`).
- Eigene UI-Texte (`web/server.py`, `web/setup.py`) sind weiterhin nur de/en; andere Sprachen sehen Englisch.
- Test: `tests/smoke_i18n.py`.

## Zweites Add-on: stellantis_login_worker
Schlanker Login-Dienst für alle, die die HACS-Integration behalten und nur deren
"Login service URL" auf die eigene Box zeigen lassen wollen. Bewusst ein zweites Add-on im
selben Repository (eine Store-URL für beides), kein eigenes Repo.
- `app/server.py`: aiohttp, spricht das Wire-Format des Community-Dienstes `worker-v2`
  (`POST /` → `{"code": …}`, `GET /health`) und ist damit ein Drop-in-Ersatz. Vertrag verifiziert
  gegen `stellantis.py:get_oauth_code` der Integration: Body `{url, email, password}`, Timeout 300 s,
  die Antwort muss `code` enthalten.
- `app/login.py`: **Kopie** von `stellantis_vehicles/app/oauth_browser/login.py` ohne Diagnose-CLI.
  Kein gemeinsames Modul, weil der HA-Builder je Add-on-Ordner einen eigenen Build-Context nutzt.
  Bei Änderungen am Login-Flow beide Kopien pflegen.
- Aus `worker-v2` stammt **kein** Code — das Repo hat keine Lizenzdatei, also alle Rechte vorbehalten.
- `boot: manual`, Port 3000, ein Login zur Zeit (Lock), Chromium pro Request auf und wieder zu.
- Ab 0.2.0 Supervisor-Discovery (`app/discovery.py`): beim Start `GET /addons/self/info` → `hostname`,
  dann `POST /discovery` mit `service: stellantis_vehicles`, `config: {host, port: 3000}`. Braucht nur
  `discovery:` in der config.yaml (kein `hassio_api`, `/discovery` und `/addons/self/*` sind freigegeben;
  der Supervisor prüft Service-Namen nicht mehr gegen eine Liste). HA macht daraus einen Config-Flow
  `source=hassio` für die Domain; die Upstream-Integration braucht dafür `async_step_hassio` (eigener PR).
  Der Supervisor listet Meldungen nur für gestartete Add-ons. Host-Port 3000 ist seit 0.2.0 standardmäßig
  aus (`null`), HA erreicht den Worker intern.
  Live auf dem Pi verifiziert (24.09.2026): Meldung kommt an, Flow `hassio_confirm` erscheint, Bestätigen setzt
  `oauth_code_url` im Entry, erneutes Melden nach Worker-Neustart wird vom Supervisor dedupliziert (gleiche UUID).
  Ohne installierte Integration loggt HA `IntegrationNotFound` als ERROR (HA-Verhalten, in DOCS dokumentiert).
  Review MoellerDi zu PR #645 (25.09.2026): `flow_title` ist `{name}` (gilt für alle Flow-Karten der Domain, Reauth
  liefert nur `name` → `{addon}` gab „Translation Error“); Bestätigen stellt nur Accounts mit Standard-Dienst
  (`oauth_code_url` fehlt oder = `OAUTH_CODE_URL`) um, eigene Worker bleiben. Kein Account mehr umzustellen →
  `already_configured`, damit die Karte beim Discovery-Replay nach jedem Neustart nicht wiederkommt.
- Test: `stellantis_login_worker/tests/smoke_worker.py` (28 Checks, ohne Netz, inkl. Fake-Supervisor).
- CI-Matrix ist jetzt zweidimensional (arch × addon), Image `stellantis-login-worker-{arch}`.

## Stand
Schritte 1–5 umgesetzt: Bridge, UI, Runtime, lokaler Build, CI-Build für beide Architekturen, Review-Fixes.
Echter Login + Statusabruf des e-Rifters verifiziert. Repo/Pakete public.
Add-on 0.2.0 (25.09.2026): Upstream auf develop da32364 gehoben (Schritte 1–4: Vendor+Shim, base.py, Bridge,
Release). Offline-Tests grün, live auf dem Pi noch nicht gelaufen.
Zweites Add-on `stellantis_login_worker` 0.2.0 (mit Supervisor-Discovery) — HTTP-Vertrag getestet, live noch nicht gelaufen.
0.2.1 (08.10.2026, beide Add-ons): nur Image-Pfad/Links auf `ktaubmann` (Nutzer bekam 403 von `ghcr.io/taubenhorst`).
0.2.2 (09.10.2026, beide Add-ons): PRs #1/#2 von FiraSenax plus eigene Review-Fixes. Abhängigkeiten mit Hashes
(`requirements.in` → `requirements.txt`, `docs/DEPENDENCIES.md`), Playwright 1.63, Build per `build-image`-Action
(liest `build.yaml`, `latest` nur von main/Tags). Login: `timeout` je Seitenschritt, Gesamtfrist 240 s
(`LOGIN_DEADLINE_S`), Notbremse 255 s beim Aufrufer; nur `OauthBrowserError(public=True)` geht an Nutzer;
nur dokumentierte Gigya-Codes brechen ab; Worker antwortet bei parallelem Login mit 429.
Echter Login mit Playwright 1.63 auf aarch64 noch nicht verifiziert.

## Nächste Schritte
6. Auf dem Pi: Add-on-Store → Repositories →
   `https://github.com/ktaubmann/StellantisHAAddon` → installieren, Optionen setzen, Ingress-Login.
   Vorher die HACS-Integration `stellantis_vehicles` deaktivieren (doppeltes Polling, OTP-Gerätelimit).
   Erster Lauf mit Supervisor-MQTT prüfen (`services: mqtt:want`, `run`-Skript setzt `STELLANTIS_MQTT_*`).
7. Neue Version = `version:` in `config.yaml` anheben + Tag `vX.Y.Z` pushen (Workflow baut auch bei Tags).
8. Optional Image verkleinern (`playwright install chromium --no-shell` spart ~150 MB, dann greift der
   Fallback in `_launch` nicht mehr).

## Lokal starten
    cd stellantis_vehicles && pip install -r requirements.txt
    DATA_DIR=./data python app/main.py
