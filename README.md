# stash scraper worker

Private FastAPI service that loads retailer pages with Camoufox and returns
rendered HTML to `stash-bff`. The shared browser launches lazily on the first
authorized fetch, and every fetch uses a new isolated context. The worker does
not authenticate Stash users,
select retailer integrations, parse product data, or persist watchlist items.

## Local development

Python 3.12 is recommended.

```powershell
cd services/scraper-worker
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m camoufox fetch
$env:SCRAPER_SERVICE_TOKEN="dev-secret-change-me"
$env:SCRAPER_MAX_CONCURRENCY="2"
python run.py
```

`run.py` enables reload mode, limits file watching to the application directory,
and reads `.env` when present. The worker runs Camoufox on a dedicated Proactor
event loop on Windows, so Playwright subprocesses also work when Uvicorn reload
mode uses a Selector loop. Set `SCRAPER_RELOAD=false` for a non-reloading local
process.

Direct Uvicorn startup is also supported:

```powershell
python -m uvicorn app.main:app --reload --reload-dir app --port 8000 --env-file .env
```

Stop the reloader before installing or upgrading dependencies; otherwise
WatchFiles will treat changes inside `.venv` as application changes. If a fetch
reports that the browser is not installed, run `python -m camoufox fetch` once.

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
  "timezone": "America/Toronto"
}
```

The response contains `requestedUrl`, `finalUrl`, `title`, rendered `html`, and
visible `bodyText`. The worker rejects disallowed navigation hosts, non-HTTP(S)
navigation, private-network URL forms, oversized HTML, and access challenges.
Errors use `{ "error": { "code", "message" } }` with one of
`SOURCE_BLOCKED`, `UPSTREAM_FAILURE`, `INTEGRATION_TIMEOUT`, `HTML_TOO_LARGE`,
`INVALID_FINAL_URL`, or `UNAUTHORIZED`.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `SCRAPER_SERVICE_TOKEN` | `dev-secret-change-me` | Bearer token shared with the BFF. Set a strong secret outside local development. |
| `SCRAPER_MAX_CONCURRENCY` | `2` | Maximum simultaneous isolated browser contexts. |

## Docker

The repository-level `compose.yaml` is the recommended deployment. It builds
the browser into the image, shares the service token with the BFF, and does not
publish the worker port.

```powershell
docker build -t stash-scraper-worker services/scraper-worker
docker run --init --env-file services/scraper-worker/.env -p 127.0.0.1:8000:8000 stash-scraper-worker
```

Binding to loopback is suitable for local development. In production, expose
the worker only to the BFF over a private service network.

## Docker development with production parity

Start `stash-db` first so the shared Docker network exists, then use:

```bash
docker compose up -d --watch
```

Editing a tracked project file rebuilds and recreates the production image.
There are no source-code or `.env` bind mounts in the container.
