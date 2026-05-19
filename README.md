# Voice-First Agentic Co-Presenter

> **v1.0** · macOS · MIT-licensed scaffolding · domain-extensible

A real-time, voice-first co-presenter that works alongside you on stage. Two collaborating Amazon Nova Sonic voice agents conduct a live conversation that the audience hears: a generalist co-presenter who drives your slides, and a domain specialist that's spawned on demand, calls your data sources, narrates each step, and lays a finished report on a second screen.

The platform ships with **one reference specialist** ("Carlos", a financial analyst that calls a stock-market data API), but the entire architecture is domain-agnostic. Adding a new specialist for **legal**, **medical**, **engineering**, **weather analytics**, **sports**, **code review**, or any other domain is **four drop-in files** with no edits to the core. See [§ Customization](#customization-build-your-own-specialist) for a complete walkthrough.

```text
                    ┌──────────────────────────────────────┐
                    │ Audience hears two voices in dialog:  │
                    │                                       │
                    │  Presenter ─── Co-presenter (Nova)    │
                    │       │                ▲              │
                    │       │  "Pull up …"   │ "ok, getting │
                    │       ▼                │  Carlos for  │
                    │  Specialist (Carlos) ──┘  the numbers"│
                    │       │                               │
                    │       ▼                               │
                    │  Live narration in es-419,            │
                    │  chart + summary report on a second   │
                    │  screen, return control to Nova.      │
                    └──────────────────────────────────────┘
```

---

## Table of contents

- [Highlights](#highlights)
- [What's in v1.0](#whats-in-v10)
- [Architecture](#architecture)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running a session](#running-a-session)
- [Stage ergonomics](#stage-ergonomics)
- [Customization: build your own specialist](#customization-build-your-own-specialist)
- [Project structure](#project-structure)
- [Testing](#testing)
- [Operations & troubleshooting](#operations--troubleshooting)
- [Roadmap](#roadmap)
- [License](#license)

---

## Highlights

| Feature | What it means on stage |
|---|---|
| **Two collaborating voice agents** | Audience hears a conversation, not a monologue. The handoff is itself a UX feature ("ok, let me get Carlos for the numbers"). |
| **Spoken pipeline narration** | The specialist narrates each step of its work — querying data, transforming, charting, summarizing — in real-time. No silent loaders. |
| **Visual chart + executive summary** | The specialist writes a polished two-slide report (chart + bullet summary) into a Chrome visor on a second screen as it finishes. |
| **Slide-deck integration** | The co-presenter drives PowerPoint in real-time: navigate, analyze the current slide with vision, advance/return, restore fullscreen on handback. |
| **System-wide spacebar mute** | A floating cross-Space "Live AI" pill plus a global spacebar hotkey lets you mute the agent from any app — including PowerPoint slideshow — without touching the trackpad. |
| **Modular by construction** | One declarative `SpecialistAgent` config + one `SpecialistToolkit` subclass + one prompt + one report template per domain. The session manager, dispatcher, visor, and Nova's prompt all read from this contract — never edited. |
| **End-to-end testable** | 723 Python tests + 75 Node tests + an offline e2e smoke that drives every Session B tool with mocked externals. |

## What's in v1.0

**v1.0 ships:**

- The full two-agent voice runtime (Nova Sonic A + B with a deterministic handoff/handback)
- The Session A toolkit: `analyze_slide`, `navigate_slide`, `control_slideshow`, `switch_window`, `handoff_to_specialist`
- The shared Session B toolkit base: `fetch_data`, `transform_data`, `compute_stats`, `generate_chart`, `compose_summary`, `render_report`, `end_session`
- The reference financial specialist (Carlos + Finalysis API + AntV charts + Sonnet executive summary)
- A live visor (Express + SSE + chokidar) that watches `reports/` for HTML drops
- macOS-only spacebar mute hotkey + cross-Space floating indicator
- A registry that auto-discovers specialists at FastAPI startup
- The full installer + start/stop scripts + diagnostic endpoint + 800+ tests

**Not in v1.0** (planned for v1.1+):

- Linux / Windows host support — the spacebar hotkey, AppleScript PowerPoint driver, and Chrome CDP launcher are macOS-specific today
- Multi-locale per specialist — each specialist outputs one locale today (`es-419` for Carlos)
- Concurrent specialist instances — `concurrency_limit` is informational; v1 enforces 1 active handoff
- A no-code/low-code authoring UI — adding a specialist is currently a Python package contribution

See [§ Roadmap](#roadmap).

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────────────────┐
│  Audience-facing surface                                                         │
│  ┌─────────────────┐   CDP 9222         ┌──────────────────────────────┐       │
│  │  Google Chrome  │ ◄──────────────────┤ src/platform/chrome.py       │       │
│  │  Tab A :3000    │   (Playwright)     │  bring_tab_to_front, nav     │       │
│  │  Tab B :3333    │                    └──────────────────────────────┘       │
│  └────────┬────────┘                              ▲                             │
│           │ WebSocket (mic in, audio out)         │ HTTP tool_call               │
│           ▼                                        │                             │
│  ┌──────────────────────────────────────────────────────────────────────────┐  │
│  │ Node WS server :3000  (server.js + session-manager.js)                    │  │
│  │   ┌────────────────────────────────────────────────────────────────────┐│  │
│  │   │ NovaSonicSessionManager                                             ││  │
│  │   │   Session A (presenter)        Session B (specialist, on demand)    ││  │
│  │   │   audio in: yes (mic)          audio in: NONE (text-only input)     ││  │
│  │   │   audio out: when active       audio out: when active               ││  │
│  │   │   lifecycle: full session      lifecycle: 15-30s per handoff        ││  │
│  │   │                                                                      ││  │
│  │   │   activeSession → browser speaker (audio mux)                        ││  │
│  │   │   browser mic → A.audioInput only                                    ││  │
│  │   │   barge-in   → terminate B, restore A                                ││  │
│  │   └────────────────────────────────────────────────────────────────────┘│  │
│  └────┬─────────────────────────────────────────────────────────────────────┘  │
│       │ POST /tool_call                                                          │
│       ▼                                                                          │
│  ┌──────────────────────────────────────────────────────────────────────────┐  │
│  │ Python FastAPI backend :8000  (src/api_server.py)                         │  │
│  │   Tool dispatcher routes by (session_id, tool_name)                       │  │
│  │     Session A tools:  analyze_slide, navigate_slide, control_slideshow,   │  │
│  │                       switch_window, handoff_to_specialist                │  │
│  │     Session B tools:  fetch_data, transform_data, generate_chart,         │  │
│  │                       compose_summary, render_report, end_session         │  │
│  │   Specialist registry: auto-discovers src/specialists/agents/*.py         │  │
│  │     v1 ships: financial   ← reference implementation                      │  │
│  │     v1.1-ready slot for: legal, medical, engineering, weather, …          │  │
│  └──────────────────────────────────────────────────────────────────────────┘  │
│                                                                                  │
│  Visor (Express + SSE + chokidar) — visor/server.mjs :3333                       │
│      Streams /events to the visor tab; chokidar watches reports/ for the         │
│      finished HTML and swaps the iframe in atomically.                           │
│                                                                                  │
│  AntV chart MCP (@antv/mcp-server-chart, streamable-HTTP) :1122/mcp              │
│      Renders the specialist's chart spec → hosted PNG → embedded in report.      │
│                                                                                  │
│  macOS Mute helper (src/platform/mute_helper.py, optional)                       │
│      CGEventTap captures spacebar globally → POST /toggle_mute → browser flips.  │
│      NSWindow at NSPopUpMenuWindowLevel + FullScreenAuxiliary draws a            │
│      "Live AI" / "Muted" pill above every fullscreen Space.                      │
│                                                                                  │
│  External: AWS Bedrock (Nova Sonic ×2, Claude Haiku, Claude Sonnet, Nova Lite)   │
│            Domain-specific data API   (Finalysis for the financial specialist)   │
│            Microsoft PowerPoint (macOS, AppleScript)                              │
└────────────────────────────────────────────────────────────────────────────────┘
```

Key architectural properties:

- **One concurrent Nova Sonic stream most of the time, two during a handoff.** Session B is opened only when the user (via Session A) calls `handoff_to_specialist`, and is torn down on terminator detection or barge-in.
- **Session B is audio-OUT only.** The microphone never reaches Bedrock through Session B — this avoids dual-VAD ambiguity in big-room PA setups.
- **The specialist registry is the only extension point.** The session manager, the visor, the Chrome adapter, and Session A's prompt do NOT know the registered specialist set at compile time. Session A's catalog is rebuilt on every server boot from `/registry/*`.
- **Reports are HTML files on disk.** A specialist completes by atomically writing `reports/<slug>-<date>.html`, which chokidar picks up and the visor swaps the iframe to. No real-time message bus needed for reports — the file system is the queue.

---

## Installation

### One-command install (macOS, recommended)

```bash
git clone <repo-url> agentic-copresenter
cd agentic-copresenter
./scripts/install.sh
```

The installer is idempotent and portable across both Apple Silicon and Intel macOS:

- Installs Homebrew formulas: `python@3.12`, `node`, `poppler`, `portaudio`, `expat`
- Installs Homebrew casks: `google-chrome`, `libreoffice` (skipped if `.app` is already present)
- Creates a Python 3.12 venv, runs `pip install -r requirements.txt`, runs `playwright install chromium`
- Installs Node deps for `websocket-server/` and `visor/`
- Copies `.env.example` → `.env` on first run
- Runs the offline test suites (~440 Python + ~75 Node) as a smoke check
- Probes AWS credentials and surfaces anything still needing manual action

> **Why an installer rather than just `brew + pip + npm`:** Homebrew's `python@3.12` is linked against a newer `libexpat` than `/usr/lib/libexpat.1.dylib` ships, which makes `xml.parsers.expat` (and therefore `plistlib`, `xmlrpc`, and any XML-heavy library) fail at import with `Symbol not found: _XML_SetAllocTrackerActivationThreshold`. The installer fixes this by installing Homebrew `expat` and baking `DYLD_LIBRARY_PATH="$(brew --prefix expat)/lib"` into the venv's `activate` script, so every entrypoint that sources `.venv/bin/activate` is immune. `start.sh` defensively re-applies the same fix.

Flags:

```bash
VERBOSE=1 ./scripts/install.sh        # show full brew/pip/npm output
SKIP_BREW=1 ./scripts/install.sh      # skip Homebrew steps (CI, locked-down machines)
```

After install, three manual steps that only you can do:

1. **Grant macOS Automation permission for PowerPoint** — System Settings → Privacy & Security → Automation → enable PowerPoint for your terminal. Required for slide navigation and slideshow control.
2. **Enable Bedrock model access** in your region — AWS Console → Bedrock → Model access → enable Nova Sonic, Claude Haiku 4.5, Claude Sonnet 4.6, Nova 2 Lite.
3. **Provide your domain's data credentials.** For the bundled financial specialist that means filling `FINALYSIS_API_KEY` in `.env`; for your own specialist it means whatever your data source needs.

If you'd prefer to install step-by-step, the manual procedure is documented in [docs/install-manual.md](docs/install-manual.md).

### macOS extras: granting Accessibility for the spacebar hotkey

The optional global spacebar mute hotkey (see [§ Stage ergonomics](#stage-ergonomics)) requires Accessibility permission for whatever terminal is launching the helper:

- System Settings → Privacy & Security → Accessibility → enable your terminal (Terminal, iTerm, Warp, etc.)
- Then `./stop.sh && ./start.sh <deck.pptx>` — phase 7/7 prints `ready (pid …)` on success or a clear instruction on failure

If you skip this, everything else still works — the mute helper just won't intercept spacebar, and the floating "Live AI" pill won't appear. Mute via the in-page button in `localhost:3000` continues working.

---

## Configuration

All runtime configuration is in `.env`. Copy `.env.example` and fill in:

| Variable | Required? | Purpose |
|---|---|---|
| `AWS_REGION` | yes | Bedrock region. `us-east-1` is the default (where Nova Sonic + Claude 4.x are available). |
| `AWS_PROFILE` | optional | If set, `start.sh` calls `scripts/refresh-credentials.sh` before launch and unsets `AWS_*_KEY_ID` so the profile's `credential_process` is the effective source. Recommended for Amazonian Isengard / federated SSO setups. |
| `FINALYSIS_API_KEY` | only if running the bundled financial specialist | Auth header for the Finalysis API. Skip if you've removed `financial` from the registry. |
| `NOVA_VOICE_A` | optional | Nova Sonic voice for the always-on co-presenter. Default: `tiffany`. |
| `NOVA_VOICE_B` | optional | Voice for Session B specialists. **Must differ from `NOVA_VOICE_A`.** Default: `carlos`. |
| `NOVA_HANDOFF_*` | optional | Rate-limit + cap settings for `handoff_to_specialist`. Defaults are tuned for ~12-15s pipelines. |
| `NOVA_DATA_HANDLE_TTL_S` | optional | TTL for opaque tool-result handles (the `fn-…` and `td-…` IDs Session B passes between tools). Default: 120s. |
| `NOVA_USE_SPACES_SWIPE` | optional | `1` (default) uses dual-fullscreen + Ctrl+←/→ Spaces swipes for window switching. `0` falls back to legacy slideshow exit/restart. |
| `BEDROCK_*_MODEL_ID` | optional | Override the default Bedrock model IDs. Useful for trying alpha models without code changes. |

For the complete annotated list with rationale, see `.env.example`.

### AWS IAM minimum

Whatever credentials you use must be able to:

- `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` for the four model IDs you've enabled
- `bedrock:Converse` for the same set
- (No other AWS services are required — there's no S3, no DynamoDB, no Lambda)

---

## Running a session

```bash
./start.sh path/to/your-deck.pptx
```

Brings up all seven services in order, polls each for readiness, and prints a summary. Failure at any phase prints the offending log path:

| Phase | Service | Port | Health check |
|---|---|---|---|
| 1/7 | Python FastAPI | 8000 | `GET /diagnose` JSON returns 200 |
| 1b/7 | Bedrock pre-flight | — | `/diagnose` reports `bedrock.checks.{haiku,nova_lite,sonnet}.ok = true` |
| 2/7 | Node WS server | 3000 | TCP connect + `200 OK` on `/healthz` |
| 3/7 | Visor (Express + SSE) | 3333 | TCP connect + `200 OK` on `/` |
| 4/7 | AntV chart MCP | 1122 | TCP connect + MCP `initialize` round-trip |
| 5/7 | Chrome with CDP | 9222 | `/json/version` reachable on the CDP socket; voice-UI + visor tabs opened |
| 6/7 | PowerPoint open | — | AppleScript verifies the active presentation matches the path |
| 7/7 | Mute helper (macOS) | — | Process alive, Accessibility granted (otherwise prints a tip) |

Then open the voice UI (it auto-opened too):

```bash
open http://localhost:3000
```

Click **Start Session** and try:

```
"Nova, next slide"
"Nova, what's on this slide?"
"Nova, pull up Tesla's 50-day SMA for the last six months"
   → Nova: "ok, let me bring in Carlos for the numbers"
   → Chrome visor comes to front
   → Carlos narrates the pipeline in Spanish:
       "Consultando Finalysis... Tesla."
       "Datos recibidos... cien puntos."
       "Armando la gráfica."
       "Redactando el resumen."
       "Reporte en pantalla."
   → Two-slide report fades in
"Nova, back to the slides"
   → PowerPoint returns to front (fullscreen restored if it was on)
```

To stop:

```bash
./stop.sh                    # gracefully closes Bedrock streams first
CHROME_STOP=1 ./stop.sh      # also kills the CDP Chrome instance
```

The full pre-flight check is also exposed as a wrapper script:

```bash
./scripts/demo-go-live.sh path/to/your-deck.pptx
```

This runs eight phases (credential refresh → service liveness → `/diagnose` deep-check → optional fullscreen-Space layout) and gives a green/red final verdict before stage time.

---

## Stage ergonomics

Two features make the system viable in front of a live audience:

### Global spacebar mute (macOS)

While a session is active, **pressing spacebar from any app** — including PowerPoint slideshow and Chrome fullscreen — toggles Nova's mute state. A 2-second macOS notification banner confirms each toggle ("🎤 Live" / "🔇 Muted"), and a small pill in the top-right of your main display continuously shows the current state.

| State | Pill | Notification |
|---|---|---|
| Live (mic open, Nova listening) | green pill, text "Live AI" | "🎤 Live — Nova is listening. Press Space again to mute." |
| Muted (mic gated, Nova ignoring) | red pill, text "Muted" | "🔇 Muted — Nova won't hear you. Press Space again to unmute." |
| No active session | pill hidden | (no banner) |

**Trade-off:** while a session is active, spacebar no longer advances PowerPoint slides. Use **→ / N / Page Down** to advance instead. Most professional presenters already prefer those keys because spacebar also triggers animation steps within a slide and is therefore behaviorally ambiguous. The override is fully reversible — when the helper exits, PowerPoint instantly regains its default spacebar=advance behavior.

### Cross-Space layout for fullscreen flow

`scripts/demo-setup-fullscreen.sh` arranges your displays so that:

- Space 1: regular desktop / voice UI
- Space 2: PowerPoint in slideshow fullscreen
- Space 3: Chrome with the visor tab in fullscreen

Then `switch_window` (a Session A tool) emits a single `Ctrl+→` / `Ctrl+←` keystroke to swap between the slideshow and the visor — instead of exiting/restarting slideshow each time. Faster, smoother, and the audience never sees a desktop.

Required: System Settings → Keyboard → Keyboard Shortcuts → Mission Control → enable "Move left/right a space".

---

## Customization: build your own specialist

The whole point of v1.0 is that **you don't fork the platform to add a new domain** — you drop in four files and the registry picks them up at the next FastAPI restart.

### The four-file contract

| File | What it declares |
|---|---|
| `src/specialists/agents/<id>.py` | A `SpecialistAgent` instance + `TOOLKIT_FACTORY` callable |
| `src/specialists/toolkits/<id>.py` | A `SpecialistToolkit` subclass (your domain logic) |
| `src/prompts/specialists/<id>.md` | The Nova Sonic system prompt for Session B |
| `reports/templates/<id>.html` | The HTML template for the visor's two-slide report |

The `SpecialistAgent` is declarative; everything else is just code/text/HTML you'd write anyway. **Zero edits to `NovaSonicSessionManager`, the dispatcher, the visor server, the Chrome adapter, or Session A's prompt.**

### Worked example: a "weather" specialist

Let's add `weather` — a specialist that pulls historical temperature/precipitation from a public weather API, charts it, and writes an executive summary. We'll show the full set of files; copy and adapt for your real domain.

#### 1. `src/specialists/agents/weather.py` (the registration)

```python
"""Weather specialist — historical climate trends.

Triggered by phrases like:
   "Nova, show me last summer's temperature in Madrid"
   "Nova, compare rainfall in São Paulo vs Buenos Aires this year"
"""
from pathlib import Path

from src.specialists.base import SpecialistAgent
from src.specialists.toolkits.weather import WeatherToolkit


ROOT = Path(__file__).resolve().parents[3]


AGENT = SpecialistAgent(
    id="weather",
    display_name="Casey",                                       # the persona name
    description="weather analyst (temperature, precipitation, climate trends)",
    voice_id="amy",                                             # different from NOVA_VOICE_A!
    locale="en-US",                                             # this specialist speaks English
    system_prompt_path=ROOT / "src/prompts/specialists/weather.md",
    report_template_path=ROOT / "reports/templates/weather.html",
    toolkit_class_path="src.specialists.toolkits.weather.WeatherToolkit",
    visor_phases=[                                              # 5 labels in this specialist's locale
        "Querying weather data",
        "Transforming time series",
        "Building chart",
        "Composing executive summary",
        "Auditing with reviewer",
        "Assembling report",
    ],
    terminator_phrases=[                                        # lowercase substrings → handback
        "report on screen",
        "report is on screen",
    ],
    tool_defs=[                                                 # see § "tool definitions" below
        # … see weather.py example for the full Nova Sonic toolConfiguration list
    ],
    trigger_examples={                                          # for Session A's catalog (per locale)
        "en": [
            "show me last summer's temperature in Madrid",
            "compare rainfall in São Paulo vs Buenos Aires this year",
        ],
        "es": [
            "muéstrame la temperatura del verano pasado en Madrid",
        ],
    },
    handoff_lines={                                             # how Nova hands off (per locale × tone)
        "en": {
            "warm_brief": "ok, let me get Casey for the climate numbers",
            "concise":    "Casey, take it",
            "professional": "passing to Casey, our climate analyst",
        },
        "es": {
            "warm_brief": "ok, vamos con Casey para los datos del clima",
        },
    },
    typical_duration_seconds=20,
    concurrency_limit=1,
    supported_locales=frozenset({"en-US"}),
)


def TOOLKIT_FACTORY(clients: dict) -> WeatherToolkit:
    """Build the toolkit with the app's shared clients.

    The framework calls this exactly once, after auto-discovery, with
    a dict containing every client `app.state` knows about. Your
    specialist takes whichever ones it needs.
    """
    return WeatherToolkit(
        bedrock_router=clients["bedrock_router"],
        antv_chart=clients["antv_chart"],
        report_renderer=clients["report_renderer"],
        # If you need a domain-specific HTTP client, build it yourself:
        # weather_api=httpx.AsyncClient(base_url=os.getenv("WEATHER_API_BASE_URL")),
    )
```

That's the **only** edit to wire your specialist into the platform. The registry's `auto_discover()` walks `src/specialists/agents/*.py`, finds anything that exports a top-level `AGENT` and `TOOLKIT_FACTORY`, and adds it to the catalog.

#### 2. `src/specialists/toolkits/weather.py` (the domain logic)

The toolkit must implement three abstract methods (`fetch_data`, `transform_data`, `compute_stats`); the rest come from `SharedToolkitMixin`.

```python
"""Weather domain logic — implements the SpecialistToolkit contract."""
from __future__ import annotations

from typing import Any

from src.specialists.base import (
    SpecialistToolkit, ToolContext, FetchResult, TransformResult,
)
from src.specialists.toolkits.shared import SharedToolkitMixin


class WeatherToolkit(SharedToolkitMixin, SpecialistToolkit):
    def __init__(self, *, bedrock_router, antv_chart, report_renderer, weather_api=None):
        self.bedrock_router = bedrock_router
        self.antv_chart = antv_chart
        self.report_renderer = report_renderer
        # In real life, build an httpx.AsyncClient against your data source:
        self.weather_api = weather_api  # or None if you'll lazy-init

    async def fetch_data(self, *, params: dict[str, Any], ctx: ToolContext) -> FetchResult:
        """Pull historical climate series from the weather API.

        Required steps:
          1. POST phase(0) so the visor shows "Querying weather data"
          2. Validate params (location, start_date, end_date, metric)
          3. Call the upstream API
          4. Store the raw response in the data-handle store
          5. Return a COMPACT FetchResult — never inline the raw data.
        """
        await ctx.phase(0, substep=f"location={params['location']}")

        # 1. Validate params (Pydantic model recommended; see src/models/financial.py
        #    for the financial specialist's model layer.)
        location = params["location"]
        start = params["start_date"]
        end = params["end_date"]
        metric = params.get("metric", "temperature")

        # 2. Call the API (pseudo-code; replace with your real client)
        try:
            response = await self.weather_api.get(
                "/historical",
                params={"location": location, "from": start, "to": end, "metric": metric},
            )
            response.raise_for_status()
            raw = response.json()
        except Exception as e:
            return FetchResult(
                ok=False,
                code="WEATHER_API_ERROR",
                message=f"Weather API failed for {location}: {e}",
            )

        if not raw.get("series"):
            return FetchResult(
                ok=False,
                code="EMPTY_SERIES",
                message=f"No data for {location} in {start}..{end}",
            )

        # 3. Stash the raw payload, return a compact summary
        handle = await ctx.put_handle("fn", raw)
        return FetchResult(
            ok=True,
            handle=handle,
            count=len(raw["series"]),
            first_value=raw["series"][0]["value"],
            last_value=raw["series"][-1]["value"],
            metadata={"location": location, "metric": metric},
        )

    async def transform_data(
        self, *, handle: str, target: str, ctx: ToolContext,
    ) -> TransformResult:
        """Shape the raw series for AntV. Most domains can use Haiku for this:
        the SharedToolkitMixin has helpers for the line_single / line_multi
        targets that the financial specialist uses too — see
        src/specialists/toolkits/shared.py.
        """
        await ctx.phase(1)
        raw = await ctx.get_handle(handle)
        if raw is None:
            return TransformResult(ok=False, code="HANDLE_NOT_FOUND", message="lost handle")

        # Domain-specific shaping — for weather we just map (date → value).
        series = [
            {"date": p["date"], "value": float(p["value"])}
            for p in raw["series"]
        ]
        td_handle = await ctx.put_handle("td", series)
        return TransformResult(ok=True, handle=td_handle, target=target, count=len(series))

    async def compute_stats(self, *, handle: str, ctx: ToolContext) -> dict[str, Any]:
        """Compute the numeric facts compose_summary will ground on.
        SharedToolkitMixin.compose_summary calls this BEFORE Sonnet, then
        passes the dict into the prompt so the executive summary is
        always grounded on real numbers.
        """
        raw = await ctx.get_handle(handle)
        values = [p["value"] for p in raw["series"]]
        return {
            "count": len(values),
            "min":   min(values),
            "max":   max(values),
            "avg":   sum(values) / len(values),
            "first": values[0],
            "last":  values[-1],
            "delta_pct": (values[-1] - values[0]) / values[0] * 100,
        }
```

The remaining four methods — `generate_chart`, `compose_summary`, `render_report`, `end_session` — come from `SharedToolkitMixin` for free. Override them only if your domain needs custom behavior.

#### 3. `src/prompts/specialists/weather.md` (the Session B system prompt)

This is the file Nova Sonic loads when Session B opens. The financial specialist's prompt (`src/prompts/specialists/financial.md`) is ~620 lines because financial has many edge cases (sector ETFs, indicator naming, weekend padding); your domain may need fewer.

The MUST-HAVE sections, with the financial specialist as the reference:

1. **Opening**: persona statement + locale lock ("Eres Casey, climate analyst. Habla en…")
2. **REGLA DURA / HARD RULE**: what to do on `ok:false` from any tool — **always one short sentence + `end_session`**, never retry
3. **REGLA DE NARRACIÓN OBLIGATORIA**: forces the model to speak before each tool call. This is the single most important section — it's what makes the audience hear the pipeline. Copy the structure verbatim from `financial.md` § "REGLA DE NARRACIÓN OBLIGATORIA".
4. **Tool order**: `fetch_data → transform_data → generate_chart → compose_summary → render_report → end_session`. Always in this order, each tool exactly once per analysis.
5. **Phase narration templates**: the 5 fixed sentences the model MUST speak at each phase boundary (`"Consultando Finalysis..."`, `"Datos recibidos..."`, `"Armando la gráfica."`, `"Redactando el resumen."`, `"Reporte en pantalla."`). The terminator detector watches for the last one.
6. **Domain-specific knowledge** — symbol/location mapping, common edge cases, expected ambiguities.
7. **Anti-patterns** — what the model MUST NOT do (greetings, goodbyes, repeating the user's question, narrating raw data values, …).

> **Tip:** Nova Sonic is more compliant with **structural instructions** (first-token directives, exact phrase templates, explicit consequences) than abstract ones (`"DEBES HABLAR"`). When designing your prompt, prefer "LA PRIMERA PALABRA QUE GENERAS DEBE SER 'X'" over "you should speak first". Empirically reduces phase-skip rate by 60-80%.

#### 4. `reports/templates/<id>.html` (the visor report)

Two-slide HTML using the project's design tokens. Easiest path: copy `reports/templates/financial.html`, then change:

- The page title and `eyebrow` text
- The chart placeholder to whatever AntV produces for your domain
- The bullet-list styling if you want a different visual signature

The renderer (`src/render/report.py`) substitutes `{{customer_name}}`, `{{description}}`, `{{chart_url}}`, `{{summary_bullets}}`, `{{footer_left}}`, `{{footer_right}}`. Add your own placeholders if you need them — `report.py` is straightforward Jinja-style replacement.

### Tool definitions (the `tool_defs` list)

Each specialist defines exactly 6 tools that Nova Sonic can call: `fetch_data`, `transform_data`, `generate_chart`, `compose_summary`, `render_report`, `end_session`. The tool **names** are stable across specialists (so the dispatcher routing stays domain-agnostic), but the **JSON schema** for each tool's `inputSchema` is fully customizable per domain.

For example, the financial specialist's `fetch_data` schema accepts `kind`, `indicator`, `symbol`, `symbols[]`, `windows[]`, `start_date`, `end_date`, etc. The weather specialist would accept `location`, `start_date`, `end_date`, `metric`. Same tool name, different schema, different dispatcher behavior. See `src/specialists/agents/financial.py` for the canonical reference.

### Common pitfalls (from real domain ports)

1. **Wrong voice ID.** Session B's `voice_id` MUST differ from `NOVA_VOICE_A`. The platform won't catch this at startup; the audience just hears one voice doing both halves of the conversation. Double-check before stage time.
2. **Missing `terminator_phrases`.** Without at least one lowercase substring that the model is prompted to say at the end of its work, Session B never hands back and the watchdog kills it at ~30s. The financial specialist uses `["reporte en pantalla", …]`; pick something equivalent in your locale.
3. **Visor phase count mismatch.** If your prompt narrates 5 phases (0–4) but `visor_phases` only has 3 entries, the visor stays stuck on the third phase. The validator requires `len(visor_phases) >= 3`; aim for 5–6 for visual fidelity.
4. **Toolkit MRO.** When subclassing both `SharedToolkitMixin` and `SpecialistToolkit`, list `SharedToolkitMixin` FIRST (`class WeatherToolkit(SharedToolkitMixin, SpecialistToolkit):`). This way Python's MRO picks the mixin's `generate_chart` / `compose_summary` / `render_report` / `end_session` over the abstract base's missing definitions.
5. **`fetch_data` returning the raw payload.** The `FetchResult` model is intentionally compact (handle + count + first/last + tiny metadata). Don't inline the raw API response — Session B's context window is shared with the prompt + history, and bloated tool returns blow the budget fast.
6. **Forgetting `await ctx.phase(N)`.** Visor stays on the previous phase. Always emit the phase update at the START of each tool method, before any expensive work.

### Verifying your specialist

```bash
# 1. Restart the Python backend so the registry rediscovers
./stop.sh && ./start.sh path/to/deck.pptx

# 2. Confirm the registry sees your specialist
curl -s http://127.0.0.1:8000/registry/ids | jq
# → {"ids": ["financial", "weather"]}

curl -s http://127.0.0.1:8000/registry/weather | jq
# → full SpecialistAgent serialization

# 3. Trigger from the voice UI
# In localhost:3000:
#   "Nova, show me last summer's temperature in Madrid"
# Watch Carlos's voice swap to Casey's, watch the visor flip phases.
```

If the registry doesn't pick up your file: check `logs/python.log` for an `ImportError` at startup. Most often it's a circular import from your toolkit pulling in something that pulls in `app.state` — the toolkit must only import via `ctx.*` and the clients you receive in the factory.

---

## Project structure

```
.
├── src/
│   ├── api_server.py              # FastAPI app, tool dispatcher, registry plumbing
│   ├── clients/
│   │   ├── antv_chart.py          # @antv/mcp-server-chart HTTP client
│   │   ├── bedrock_router.py      # Bedrock Converse + Streaming router (Haiku/Sonnet/Nova Lite)
│   │   ├── finalysis.py           # Reference data-source client (financial)
│   │   └── visor.py               # POST /api/start /api/phase /api/done
│   ├── models/                    # Pydantic models (FetchResult, TransformResult, …)
│   ├── platform/
│   │   ├── chrome.py              # Chrome CDP via Playwright (bring_tab_to_front, …)
│   │   ├── powerpoint.py          # AppleScript driver (navigate, slideshow, …)
│   │   ├── spaces.py              # Ctrl+←/→ Mission Control swipes
│   │   ├── window_manager.py      # Higher-level "move PPT to Space 2" orchestration
│   │   ├── mute_helper.py         # CGEventTap + NSWindow + osascript notification
│   │   └── keyboard_hook.py       # Legacy (pre-CGEventTap) hook
│   ├── prompts/specialists/       # System prompts (one .md per specialist)
│   ├── render/report.py           # HTML template renderer
│   ├── specialists/
│   │   ├── base.py                # SpecialistAgent / SpecialistToolkit / ToolContext
│   │   ├── registry.py            # AgentRegistry.auto_discover()
│   │   ├── agents/                # Per-specialist registration files
│   │   └── toolkits/              # Per-specialist toolkits + shared mixin
│   ├── state/                     # data_handles, slide_checkpoint, handoff_rate
│   └── tools/                     # Session A tools (analyze_slide, navigate_slide, …)
│
├── websocket-server/              # Node.js WS server + session manager
│   ├── server.js                  # WS + HTTP + mute endpoints + browser file serving
│   ├── session-manager.js         # NovaSonicSessionManager (A+B lifecycle, audio mux, handoff)
│   ├── nova-sonic-client.js       # Bedrock bidirectional streaming client
│   ├── mute-state.js              # Pure helpers for mute state machine (unit-tested)
│   └── tests/                     # 75 Node tests (lifecycle, handoff, watchdog, …)
│
├── visor/                         # Express + SSE + chokidar
│   └── server.mjs                 # Inline visor HTML; watches reports/ and broadcasts
│
├── browser/                       # Static voice-UI files served by websocket-server
│   ├── app.js                     # Mic capture, audio playback, mute helpers, WS client
│   ├── audio-worklet.js           # PCM resampler 48k → 16k for Bedrock
│   └── barge-in.js                # Client-side VAD for early interruption signal
│
├── reports/templates/             # HTML templates (one .html per specialist)
├── tests/                         # 723 Python tests (offline by default)
├── scripts/
│   ├── install.sh                 # One-command idempotent installer
│   ├── refresh-credentials.sh     # AWS profile refresh wrapper
│   ├── demo-setup-fullscreen.sh   # Arrange Spaces + fullscreen for stage
│   ├── demo-go-live.sh            # 8-phase pre-flight wrapper
│   └── ensure-{chart,chrome,visor}.sh
├── start.sh                       # Bring up all 7 services
├── stop.sh                        # Graceful shutdown
├── requirements.txt               # Python deps (pinned where it matters)
└── .env.example                   # Annotated environment template
```

---

## Testing

```bash
# Python — 723 tests, ~3-4 s
PYTHONPATH=. .venv/bin/pytest tests/ -q --ignore=tests/_smoke_analyze_slide.py

# Node — 75 tests, ~3 s, in priority order so a broken core fails fast
cd websocket-server && node tests/run.js

# E2E smoke (Python-side, offline; mocks Bedrock + Finalysis)
PYTHONPATH=. .venv/bin/pytest tests/test_e2e_smoke.py -v
```

The **e2e smoke** drives every Session B tool in sequence with mocked externals and verifies:

- The visor saw phases 0→1→2→3→4 in order
- A real HTML report lands on disk
- The rate limiter records and releases correctly
- The Finalysis-error path surfaces a Spanish error code that Session B can narrate

**Real-service smoke** before stage time:

```bash
curl http://127.0.0.1:8000/diagnose | jq
```

Confirms Bedrock, Chrome CDP, visor, AntV MCP, PowerPoint, and (your data source) are all reachable.

When porting a new specialist, add at least:

- One unit test per `SpecialistToolkit` method (mock the upstream API)
- One integration test that drives the whole `fetch → transform → chart → summary → render` chain through your specialist
- An entry in `tests/test_specialists.py::test_registry_loads` confirming the new agent appears

The financial specialist has ~150 tests across `test_finalysis_client.py`, `test_financial_toolkit.py`, `test_integration_api_financial.py`, `test_e2e_smoke.py` — use them as a model.

---

## Operations & troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ImportError ... pyexpat ... Symbol not found: _XML_SetAllocTrackerActivationThreshold` | Homebrew `python@3.12` linked against newer libexpat than `/usr/lib/libexpat.1.dylib` | `./scripts/install.sh` bakes the fix into `.venv/bin/activate`. Manual: `brew install expat && export DYLD_LIBRARY_PATH="$(brew --prefix expat)/lib"`. |
| `chrome: CDP did not become ready` | Chrome already running outside CDP mode | Quit Chrome entirely, or set a different `CHROME_USER_DATA_DIR` |
| `navigate_slide` returns `NO_PERMISSION` | macOS Automation not granted | System Settings → Privacy & Security → Automation → enable PowerPoint for your terminal |
| `handoff_to_specialist` returns `UNKNOWN_SPECIALIST` | Backend couldn't import the agent module | `curl /registry/ids` → expected to include your `<id>`. Otherwise check `logs/python.log` for `ImportError`. |
| Session B opens but says `"Reporte en pantalla"` instantly | Empty `visor_phases` or missing `system_prompt_path` in your `SpecialistAgent` | Validate the file exists; `len(visor_phases) >= 3`; `system_prompt_path.exists()` |
| Voice UI shows "Server unreachable" | Node WS server didn't start | `logs/node.log` for `EADDRINUSE` or missing deps; `cd websocket-server && npm install` |
| `Bedrock pre-flight not green` | AWS creds missing or model not enabled in region | `aws sts get-caller-identity`; AWS Console → Bedrock → Model access |
| Report lands in `reports/` but visor doesn't update | chokidar missed the write | Visor logs; verify atomic-write tempfile cleanup |
| Session B's voice is the same as Session A's | `NOVA_VOICE_B` collides with `NOVA_VOICE_A` | Set distinct voices in `.env` |
| Spacebar doesn't toggle mute and pill doesn't appear | macOS Accessibility permission missing for the helper | System Settings → Privacy & Security → Accessibility → enable your terminal; `./stop.sh && ./start.sh` |
| `[ fail ] pre-flight checks failed` | Partial stack (some services down) | `./start.sh <deck>` — idempotency means alive services skip and dead ones start. Check `/diagnose` JSON to see which services |
| `"... is not a function"` at visor startup | A backtick or `${...}` in your `visor/server.mjs` HTML literal | The visor's HTML is one giant template literal — never use double-backticks for emphasis inside it; use single quotes |

Logs:

| Service | Log path |
|---|---|
| Python backend | `logs/python.log` |
| Node WS server | `logs/node.log` |
| Visor | `logs/visor.log` |
| AntV chart MCP | `logs/chart-mcp.log` |
| Chrome | `logs/chrome.log` |
| Mute helper | `logs/mute_helper.log` |

---

## Roadmap

**v1.1 — domain-extension polish**

- Cross-platform host (Linux: replace AppleScript with `xdotool`; Windows: PowerShell; replace mute-helper CGEventTap with `pynput` global hook)
- Multi-locale per specialist (one `SpecialistAgent` serving en + es + pt-BR)
- Browser SpeechSynthesis fallback narration for the visor (gated by `isAssistantPlaying`) so silent moments during model phase-skip get a backup voice cue

**v1.2 — operational scale**

- Concurrent specialist instances (`concurrency_limit > 1`)
- Pre-recorded voice-clip fallback for phase narration (using a one-shot Polly run in the specialist's voice for perfect continuity)
- Specialist-specific telemetry dashboards

**v2.0 — authoring**

- A no-code specialist authoring UI (declarative YAML → `SpecialistAgent`, visual prompt editor, schema builder)
- Specialist marketplace (signed packages, discoverability, audit)
- Multi-presenter / multi-co-presenter sessions

PRs welcome on any of these — the architectural seams are already cut.

---

## License

Project scaffolding: **MIT**. Upstream dependencies retain their original licenses (AWS SDKs, AntV `mcp-server-chart`, Playwright, etc.). The financial specialist's data source (Finalysis) is a third-party API with its own terms — using it requires your own credentials.

The bundled financial specialist is provided as a **reference implementation** for the `SpecialistAgent` contract. It is not financial advice. Outputs from the platform are for demonstration purposes; verify any numbers against authoritative sources before making real decisions.

---

## Acknowledgments

Built on top of:

- **Amazon Nova Sonic** — bidirectional streaming voice models for both the co-presenter and specialist sessions.
- **Anthropic Claude Haiku 4.5 + Sonnet 4.6** — slide vision and executive-summary composition, accessed via Bedrock.
- **Amazon Nova Lite** — intent classification.
- **AntV `mcp-server-chart`** — chart rendering as a streamable-HTTP MCP server.
- **Playwright (Chromium CDP)** — second-screen Chrome control on macOS.

This project is a community release — issues and PRs welcome.
