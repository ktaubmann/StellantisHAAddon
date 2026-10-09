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

**"Identity provider rejected the login"** or **"Login endpoint returned error
<code>; manual sign-in may be required"** (codes 401021, 401022, 403041, 403042,
403044, 403120) — wrong credentials, or the account is locked or needs attention. Sign in once in the official app or on the brand's
website, then retry.

**"Login or consent form not found (…)"** — a page step did not appear within
`timeout` seconds. Usually a slow host or a changed login page. Raise `timeout`
and retry; if it persists, the page layout may have changed.

**"Login deadline reached"** or **"Login timed out"** (HTTP 504) — the whole
login took longer than 240 seconds. The add-on log names the phase the login
stopped in and the last URL (query values redacted).

**"Login did not complete. Check the official app or sign in manually."** — an
unexpected browser error. Set `log_level` to `debug` and try again; the log then
lists every URL that was seen.

**HTTP 429 "A login is already running"** — another login is in progress. Wait
for it to finish, then retry (see below).

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

## Concurrent logins and diagnostics

Only one browser login runs at a time. A second HTTP request receives **429**
with `Retry-After: 10`; it no longer queues credentials behind the running login.
When several accounts need reauthentication, finish one before retrying the next.
The header is a retry hint, not a promise that the browser will be free after ten
seconds. The worker does not automatically retry or switch to a hosted helper.

Known failures return a numeric login endpoint error or a fixed explanation such
as "Identity provider rejected the login". Unexpected exceptions remain generic.
Logs include the final phase and a URL with query values and fragments redacted,
even without a debug directory. With `log_level: debug`, ForgeRock authentication
responses report HTTP/numeric status and known callback types, and browser console
warnings/errors report their level. Raw console messages, response bodies, callback
values and page text are not logged. These structured diagnostics cannot preserve
every detail of a raw provider response.

A nonzero Gigya `errorCode` does not always mean final rejection. SAP documents
pending registration (206001), verification (206002) and further authentication
steps; screen-sets may continue handling these in the page. The worker terminates
early only for the documented rejection codes 401021, 401022, 403041, 403042,
403044 and 403120. Pending and unknown codes continue within the existing deadline;
this does not implement additional interactive verification or prove that the
provider will complete it automatically.

References: [SAP account error handling](https://help.sap.com/docs/SAP_CUSTOMER_DATA_CLOUD/8b8d6fffe113457094a17701f63e3d6a/9849bde7aff74b1f8074a6275a6bf7b3.html)
and [SAP response codes](https://help.sap.com/docs/SAP_CUSTOMER_DATA_CLOUD/8b8d6fffe113457094a17701f63e3d6a/416d41b170b21014bbc5a10ce4041860.html).
