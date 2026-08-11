# Static Control Room

`frontend/` contains the initial ATS Lab operator Control Room shell. It is
plain HTML, CSS, and browser JavaScript: no package manager, bundler, or
frontend dependency is required.

The shell is intentionally read-oriented. Command buttons display the command
placeholder only; they do not pause, resume, stop, claim, requeue, or otherwise
write ATS Lab state. Canonical state remains the ATS Lab SQLite projection.

## Run locally

Recommended: serve frontend and read-only API from one same-origin process:

```bash
ats-lab web --host 127.0.0.1 --port 8765
```

Open <http://127.0.0.1:8765>. API requests and static assets share one origin.

Frontend-only fallback:

```bash
python3 -m http.server 4173 --directory frontend
```

Set `window.ATS_LAB_API_BASE` before `app.js` loads when using that fallback.

With no API available, the page renders clearly labelled demo values and a
stale-data banner. This makes the layout inspectable without implying that
runtime data is current.

## API adapter

The browser adapter requests these same-origin JSON endpoints:

| View | Endpoint |
| --- | --- |
| Summary cards and attention | `/api/v1/summary` |
| Header health | `/api/v1/health` |
| Queue table | `/api/v1/queue` |
| HPO panel | `/api/v1/hpo/studies` |

The adapter accepts either a direct payload or common `data`, `summary`,
`items`, `queue`, `work_items`, `studies`, and `hpo_studies` wrappers. The
public hook is `window.ATS_LAB_CONTROL_ROOM`; `API_ROUTES` and
`createApiClient()` can be reused by a host application.

For a separately hosted API, set the base URL before `app.js` loads:

```html
<script>
  window.ATS_LAB_API_BASE = "http://127.0.0.1:8766";
</script>
<script src="app.js" defer></script>
```

The API host must permit the browser request through its normal CORS or
reverse-proxy policy. The existing Python dashboard routes are `/api/...`,
not `/api/v1/...`; this frontend does not alter or alias backend routes.

## Manual verification

No frontend test runner exists in this repository, so verification is manual:

1. Run the static server command above.
2. Confirm the page shows four summary cards, attention list, queue table, HPO
   cards, command placeholders, and the stale-data banner.
3. Click `Refresh`; confirm the button disables briefly and the stale banner
   remains visible when `/api/v1/*` is unavailable.
4. Activate each command button; confirm it only updates the status text and
   does not execute a shell command.
5. Resize to a narrow viewport and use keyboard `Tab`; confirm responsive
   layout, visible focus, skip link, table headers, and readable status text.
6. When API fixtures are available, serve the page from the API origin or set
   `window.ATS_LAB_API_BASE`, then confirm live/partial API labels and stale
   handling for an endpoint failure or old `updated_at` value.
