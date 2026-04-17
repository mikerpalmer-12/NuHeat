# NuHeat Thermostat Control

A self-hosted control server for NuHeat floor heating thermostats. Provides a
web dashboard, REST API, query string API, and push notifications for
monitoring and controlling multiple thermostats from anywhere on your network.

## Features

- **Web dashboard** at `/` - live temperature control for all thermostats
- **REST API** at `/api/*` - JSON-based endpoints for integrations
- **Query String API** at `/qs/*` - simple GET-only endpoints for webhooks, IoT devices, and curl one-liners
- **Weekly schedule viewer** - see the 7-day schedule for each thermostat
- **Activity log** at `/logs` - track auth, polls, writes, errors, rate limits, and API requests
- **Settings page** at `/settings` - manage account, serial numbers, poll interval, rate limits, and notifications
- **Pushover notifications** for errors (auth failures, write failures, offline thermostats, etc.)
- **Persistent configuration** - settings changes survive container restarts
- **Cache-first architecture** - protects NuHeat from being overwhelmed by client polling

## Which NuHeat API Does This Use?

**Important context for future development:** NuHeat has two separate APIs, and
this app currently uses the **Legacy API**.

### Legacy API (currently in use)

- Base URL: `https://mynuheat.com/api`
- Authentication: Email/password login returning a `SessionId`
- **Unofficial** - reverse-engineered from the MyNuHeat app and used by
  Home Assistant, SmartThings, and the `python-nuheat` PyPI package for years
- Requires only your MyNuHeat account credentials - no developer registration
- Risk: NuHeat could change or disable this API without notice

Code lives at `nuheat/api/legacy.py`.

### OAuth2 API (official, supported but not in use by default)

- Base URL: `https://api.mynuheat.com/api/v1`
- Authentication: OAuth2 authorization code flow with ClientId + ClientSecret
- **Official** and documented via Swagger at `api.mynuheat.com/swagger`
- Requires a developer registration request through NuHeat support to obtain
  credentials (contact is via `https://go.nvent.com/connected-home`)
- More stable, provides additional endpoints (account info, energy logs)
- Harder initial setup: requires a browser-based authorization flow

Code lives at `nuheat/api/oauth2.py`.

### Why Legacy?

We chose the Legacy API initially because it works immediately with just an
email and password. OAuth2 requires contacting NuHeat support and waiting for
developer credentials. The two clients share a common interface
(`nuheat/api/base.py::NuHeatAPI`), so swapping is as simple as changing
`NUHEAT_API_TYPE` in the config.

### To switch to OAuth2 later

Once you have ClientId and ClientSecret from NuHeat:

```env
NUHEAT_API_TYPE=oauth2
NUHEAT_CLIENT_ID=your-client-id
NUHEAT_CLIENT_SECRET=your-client-secret
NUHEAT_REDIRECT_URI=http://localhost:8787/callback
```

Everything else (dashboard, REST API, CLI) works identically with either backend.

## Architecture Overview

```
       [NuHeat API (Legacy or OAuth2)]
                  ^
                  | (only called by background poller, every 5 min)
                  |
          [Background Poller]
                  |
                  v
          [In-Memory Cache]  <--- 1MB JSONL log (persistent)
                  ^
                  | (all reads come from cache, never hit NuHeat directly)
                  |
   +--------------+--------------+
   |              |              |
[Dashboard]  [REST API]    [QS API]
```

Client read requests never trigger NuHeat API calls. Writes are throttled
(default 60s minimum between set-temperature commands) and always followed
by one targeted refresh to keep the cache accurate.

## Running with Docker

```bash
cp .env.example .env
# Edit .env with your NuHeat credentials and serial numbers
docker compose up -d --build
```

Then open `http://localhost:8777` in your browser.

## Configuration

Initial configuration comes from `.env` (see `.env.example`). Runtime changes
made through the Settings page are persisted to `config.json` in the Docker
volume and override `.env` defaults on startup.

| Variable | Description | Default |
|---|---|---|
| `NUHEAT_API_TYPE` | `legacy` or `oauth2` | `legacy` |
| `NUHEAT_EMAIL` | MyNuHeat account email (legacy) | required |
| `NUHEAT_PASSWORD` | MyNuHeat password (legacy) | required |
| `NUHEAT_SERIAL_NUMBERS` | Comma-separated thermostat serials | required |
| `NUHEAT_POLL_INTERVAL` | Seconds between NuHeat polls | 300 |
| `NUHEAT_RATE_LIMIT_READS` | Read requests per minute per IP | 60 |
| `NUHEAT_RATE_LIMIT_WRITES` | Write requests per minute per IP | 10 |
| `NUHEAT_WRITE_THROTTLE` | Minimum seconds between writes | 60 |
| `NUHEAT_DEBUG_LOG` | Write logs to disk immediately | false |
| `NUHEAT_LOG_DIR` | Log and config directory | `/app/logs` |

## Project Layout

```
nuheat/
  api/
    base.py          # Abstract API interface
    legacy.py        # Legacy mynuheat.com client (currently used)
    oauth2.py        # Official OAuth2 client (swappable)
  static/            # Dashboard, settings, logs, API reference pages
  activity_log.py    # In-memory log with persistent JSONL writes
  cli.py             # Command-line interface
  config.py          # Constants and temperature conversions
  manager.py         # High-level thermostat manager with caching
  notifications.py   # Pushover push notifications
  persistent_config.py  # Persistent runtime settings
  server.py          # FastAPI REST and query string server
  thermostat.py      # Data model
```

## Tech Stack

- **FastAPI** - the web framework (REST API, middleware, static file serving)
- **Uvicorn** - ASGI server
- **Pydantic** - request/response validation
- **aiohttp** - async HTTP client for talking to NuHeat
- **Pillow** - app icon generation
- All async, so the server can handle many requests while waiting on NuHeat without blocking.

## Community References

If the Legacy API ever breaks, these projects may have updated solutions:

- [broox/python-nuheat](https://github.com/broox/python-nuheat) - Python library using Legacy API
- [Home Assistant NuHeat integration](https://www.home-assistant.io/integrations/nuheat/) - uses python-nuheat
- [simplextech/udi-poly-nuheat](https://github.com/simplextech/udi-poly-nuheat) - Polyglot nodeserver using OAuth2 API

## License

MIT (or add your preferred license here).
