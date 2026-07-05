# stash scraper worker

Private FastAPI service that renders retailer pages with Playwright Stealth and
returns rendered HTML to `stash-bff`. The shared browser launches during worker
startup and stays resident, while every fetch uses a new isolated context. The worker does
not authenticate Stash users,
select retailer integrations, parse product data, or persist watchlist items.

## Local development

Python 3.12 is recommended.

```powershell
cd services/scraper-worker
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
$env:SCRAPER_SERVICE_TOKEN="dev-secret-change-me"
$env:SCRAPER_MAX_CONCURRENCY="2"
$env:SCRAPER_BACKEND="playwright-stealth"
python run.py
```

`run.py` enables reload mode, limits file watching to the application directory,
and reads `.env` when present. The worker runs its browser on a dedicated Proactor
event loop on Windows, so Playwright subprocesses also work when Uvicorn reload
mode uses a Selector loop. Set `SCRAPER_RELOAD=false` for a non-reloading local
process.

Direct Uvicorn startup is also supported:

```powershell
python -m uvicorn app.main:app --reload --reload-dir app --port 8000 --env-file .env
```

Stop the reloader before installing or upgrading dependencies; otherwise
WatchFiles will treat changes inside `.venv` as application changes. If a fetch
reports that the browser is not installed, run
`python -m playwright install chromium`.

Run the browser-independent worker tests with:

```powershell
python -m unittest discover
```

`GET /health` is public and returns `{ "data": { "status": "ok" } }`.
Keep the service on a private network in production; `/fetch` is intended only
for the BFF and requires a shared bearer token.

## Fetch contract

```http
POST /fetch
Authorization: Bearer <SCRAPER_SERVICE_TOKEN>
Content-Type: application/json
```

```json
{
  "url": "https://www2.hm.com/en_ca/productpage.1234567890.html",
  "allowedHosts": ["www2.hm.com"],
  "timeoutMs": 20000,
  "waitAfterDomMs": 1500,
  "maxHtmlBytes": 10000000,
  "locale": "en-CA",
  "timezone": "America/Toronto",
  "waitUntil": "domcontentloaded"
}
```

The response contains `requestedUrl`, `finalUrl`, and rendered `html`. The worker
only handles safe browser navigation and transport; the BFF extractor determines
whether the HTML contains usable product data. The worker rejects disallowed
navigation hosts, non-HTTP(S) navigation, private-network URL forms, oversized
HTML, and definitive upstream HTTP failures.
Errors use `{ "error": { "code", "message" } }` with one of
`SOURCE_BLOCKED`, `UPSTREAM_FAILURE`, `INTEGRATION_TIMEOUT`, `HTML_TOO_LARGE`,
`INVALID_FINAL_URL`, or `UNAUTHORIZED`.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `SCRAPER_SERVICE_TOKEN` | `dev-secret-change-me` | Bearer token shared with the BFF. Set a strong secret outside local development. |
| `SCRAPER_MAX_CONCURRENCY` | `2` | Maximum simultaneous isolated browser contexts. |
| `SCRAPER_BACKEND` | `playwright-stealth` | Browser backend registry key; currently only `playwright-stealth` is available. |
| `SCRAPER_PROXY_URL` | unset | Optional HTTP/SOCKS proxy passed to the selected browser backend. |
| `SCRAPER_LOG_LEVEL` | `INFO` | Worker diagnostic log level. |

The worker logs one structured line when each scrape starts and finishes. Failures
include a request ID, sanitized target URL, duration, public error code, HTTP status,
and the underlying browser exception. Supply `X-Request-ID` to correlate BFF and
worker logs; otherwise the worker generates one. Query strings and proxy credentials
are never written to these request logs. Successful navigation also logs the final
URL and HTML byte count; full HTML is not logged.

## Docker

The infrastructure repository at [c:\Dev\Projects\stash-infra](c:\Dev\Projects\stash-infra) is the recommended deployment path. It builds
the browser into the image, shares the service token with the BFF, and keeps
the worker on the private service network.

```powershell
docker build -t stash-scraper-worker services/scraper-worker
docker run --init --env-file services/scraper-worker/.env -p 127.0.0.1:8000:8000 stash-scraper-worker
```

Binding to loopback is suitable for local development. In production, expose
the worker only to the BFF over a private service network.
