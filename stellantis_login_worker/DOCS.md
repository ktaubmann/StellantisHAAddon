# Stellantis Login Worker

A local replacement for the shared login service that the
[homeassistant-stellantis-vehicles](https://github.com/andreadegiovine/homeassistant-stellantis-vehicles)
integration uses to obtain its OAuth authorization code.

Use this add-on if you want to keep the HACS integration but not send your
Stellantis credentials through a third-party server. If you would rather have
integration and login in one package, install the **Stellantis Vehicles**
add-on from this repository instead — you do not need both.

## How it works

The integration cannot log in by itself: Stellantis has no public B2C API, so
the login runs through the brand's normal web form, and the authorization code
arrives as a redirect to an app scheme (`mym…://oauth2redirect?code=…`) that a
browser cannot follow. This add-on drives a headless Chromium through that
flow and returns the code.

Your e-mail address and password are passed straight to the browser. They are
not stored, not written to the log and not sent anywhere except the Stellantis
identity provider.

## Installation

1. Install and start the add-on.
2. On start the add-on announces itself to Home Assistant (Supervisor
   discovery). Integration versions that support this show a **Discovered**
   card under Settings → Devices & services. Confirming it fills in the
   **Login service URL** for you — for a new account as well as for accounts
   that still use the default login service. Accounts that point to a custom
   login service on purpose stay unchanged.
3. With an integration version that does not support discovery yet, enter the
   URL by hand at the **remote login** step. The add-on log prints it on
   start:

   ```
   Announced to Home Assistant as http://0e0578fd-stellantis-login-worker:3000
   ```

   The hostname is the add-on's name on the internal Supervisor network; the
   prefix depends on the repository URL and may differ on your system.
4. Complete the login. Afterwards you can stop the add-on again — it is only
   needed for the initial login and for re-authentication, which is why it is
   set to start manually.

The port is not published on the host by default, because Home Assistant does
not need it. If something outside Home Assistant should call the worker, set a
host port under the add-on's **Network** settings and use
`http://<home-assistant-ip>:<port>`; `curl http://<home-assistant-ip>:<port>/health`
then answers `{"status": "ok"}`. Note that the worker has no authentication of
its own.

## Options

| Option      | Default | Meaning                                                              |
| ----------- | ------- | -------------------------------------------------------------------- |
| `log_level` | `info`  | Set to `debug` to see every URL the browser visits (query values are masked). |
| `timeout`   | `60`    | Seconds to wait for each page step of the login. The whole login is limited to 240 seconds. The integration may send its own value. |

## API

The add-on speaks the same wire format as the community `worker-v2` service,
so it is a drop-in replacement:

```
POST /        {"url": "<oauth authorize url>", "email": "…", "password": "…"}
              → 200 {"code": "<authorization code>"}
              → 400 {"message": "<reason>", "code": 400}
GET  /health  → {"status": "ok"}
```

Only one login runs at a time; further requests queue. Chromium is started per
login and closed afterwards, so the add-on idles at a few MB.

## Troubleshooting

**"Stellantis IdP rejected the login"** — wrong credentials, or the account is
locked. The message contains the text of the page the login stopped on.

**"No authorization code captured (timeout)"** — the flow got past the login
but the redirect never arrived. Set `log_level` to `debug` and try again; the
log then lists every URL that was seen.

**`IntegrationNotFound: Integration 'stellantis_vehicles' not found` in the
Home Assistant log** — the add-on announced itself, but the integration is not
installed. Home Assistant logs this on every add-on start and every restart.
Install the integration via HACS, or stop the add-on if you do not need it.

**The add-on does not start on a Raspberry Pi** — Chromium needs a few hundred
MB while a login is running. Stop other memory-hungry add-ons, or run the
login once from a stronger machine.

## Security

The worker is not authenticated. Since 0.2.0 its port is only reachable on the
internal Supervisor network (Home Assistant and other add-ons), not from your
LAN. If you publish a host port under **Network**, anyone on your network
could send it a login request of their own (with their own credentials; it
will not reveal yours). Keeping the add-on stopped except during logins
avoids this entirely.
