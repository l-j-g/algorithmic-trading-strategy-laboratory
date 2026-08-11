# Static Control Room

`frontend/` contains the initial ATS Lab operator Control Room shell. It is
plain HTML, CSS, and browser JavaScript: no package manager, bundler, or
frontend dependency is required.

The shell reads canonical SQLite state and exposes a narrow loopback supervisor
control panel. Controls start/resume, pause, or gracefully stop the existing
ATS Lab supervisor lifecycle. No claim, requeue, evidence, Jesse, or Memory
mutation is exposed.

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
| Supervisor state | `/api/v1/control` |

`POST /api/v1/control/start`, `/pause`, `/resume`, and `/stop` require the
matching `X-ATS-Lab-Confirm` header. Mutation routes are enabled only when
`ats-lab web` binds to loopback (`127.0.0.1`, `localhost`, or `::1`). Remote
bindings remain read-only.

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

1. Run the same-origin server command above.
2. Confirm the page shows four summary cards, attention list, queue table, HPO
   cards, supervisor controls, and CLI command placeholders.
3. Click `Refresh`; confirm the button disables briefly and the stale banner
   remains visible when `/api/v1/*` is unavailable.
4. Activate `Pause` or `Stop`; confirm browser confirmation, durable control
   state change, and audit event. Do not test against live research unless
   intentional.
5. Activate each CLI command button; confirm it only updates status text and
   does not execute a shell command.
6. Resize to a narrow viewport and use keyboard `Tab`; confirm responsive
   layout, visible focus, skip link, table headers, and readable status text.
7. When API fixtures are available, serve the page from the API origin or set
   `window.ATS_LAB_API_BASE`, then confirm live/partial API labels and stale
   handling for an endpoint failure or old `updated_at` value.
