# FinAlly — AI Trading Workstation

## Project Specification

## 1. Vision

FinAlly (Finance Ally) is a visually stunning AI-powered trading workstation for the **Russian stock market (MOEX)** that streams live market data, lets users trade a simulated portfolio, and integrates an LLM chat assistant that can analyze positions and execute trades on the user's behalf. It looks and feels like a modern Bloomberg terminal with an AI copilot.

This is the capstone project for an agentic AI coding course. It is built entirely by Coding Agents demonstrating how orchestrated AI agents can produce a production-quality full-stack application. Agents interact through files in `planning/`.

## 2. User Experience

### First Launch

The user runs a single Docker command (or a provided start script). A browser opens to `http://localhost:8000`. No login, no signup. They immediately see:

- A watchlist of 10 default MOEX tickers with live-updating prices in a grid
- ₽10,000 in virtual cash
- A dark, data-rich trading terminal aesthetic
- An AI chat panel ready to assist

### What the User Can Do

- **Watch prices stream** — prices flash green (uptick) or red (downtick) with subtle CSS animations that fade
- **View sparkline mini-charts** — price action beside each ticker in the watchlist, accumulated on the frontend from the SSE stream since page load (sparklines fill in progressively)
- **Click a ticker** to see a larger detailed chart in the main chart area
- **Buy and sell shares** — market orders only, instant fill at current price, no fees, no confirmation dialog
- **Monitor their portfolio** — a heatmap (treemap) showing positions sized by weight and colored by P&L, plus a P&L chart tracking total portfolio value over time
- **View a positions table** — ticker, quantity, average cost, current price, unrealized P&L, % change
- **Chat with the AI assistant** — ask about their portfolio, get analysis, and have the AI execute trades and manage the watchlist through natural language
- **Manage the watchlist** — add/remove tickers manually or via the AI chat

### Visual Design

- **Dark theme**: backgrounds around `#0d1117` or `#1a1a2e`, muted gray borders, no pure black
- **Price flash animations**: brief green/red background highlight on price change, fading over ~500ms via CSS transitions
- **Connection status indicator**: a small colored dot (green = connected, yellow = reconnecting, red = disconnected) visible in the header
- **Professional, data-dense layout**: inspired by Bloomberg/trading terminals — every pixel earns its place
- **Responsive but desktop-first**: optimized for wide screens, functional on tablet

### Color Scheme
- Accent Yellow: `#ecad0a`
- Blue Primary: `#209dd7`
- Purple Secondary: `#753991` (submit buttons)

## 3. Architecture Overview

### Single Container, Single Port

```
┌─────────────────────────────────────────────────┐
│  Docker Container (port 8000)                   │
│                                                 │
│  FastAPI (Python/uv)                            │
│  ├── /api/*          REST endpoints             │
│  ├── /api/stream/*   SSE streaming              │
│  └── /*              Static file serving         │
│                      (Next.js export)            │
│                                                 │
│  SQLite database (volume-mounted)               │
│  Background task: market data polling/sim        │
└─────────────────────────────────────────────────┘
```

- **Frontend**: Next.js with TypeScript, built as a static export (`output: 'export'`), served by FastAPI as static files
- **Backend**: FastAPI (Python), managed as a `uv` project
- **Database**: SQLite, single file at `db/finally.db`, volume-mounted for persistence
- **Real-time data**: Server-Sent Events (SSE) — simpler than WebSockets, one-way server→client push, works everywhere
- **AI integration**: LiteLLM → OpenRouter (Cerebras for fast inference), with structured outputs for trade execution
- **Market data**: Environment-variable driven — simulator by default, real MOEX data if enabled

### Why These Choices

| Decision | Rationale |
|---|---|
| SSE over WebSockets | One-way push is all we need; simpler, no bidirectional complexity, universal browser support |
| Static Next.js export | Single origin, no CORS issues, one port, one container, simple deployment |
| SQLite over Postgres | No auth = no multi-user = no need for a database server; self-contained, zero config |
| Single Docker container | Students run one command; no docker-compose for production, no service orchestration |
| uv for Python | Fast, modern Python project management; reproducible lockfile; what students should learn |
| Market orders only | Eliminates order book, limit order logic, partial fills — dramatically simpler portfolio math |

---

## 4. Directory Structure

```
finally/
├── frontend/                 # Next.js TypeScript project (static export)
├── backend/                  # FastAPI uv project (Python)
│   └── db/                   # Schema definitions and seed logic
├── planning/                 # Project-wide documentation for agents
│   ├── PLAN.md               # This document
│   └── ...                   # Additional agent reference docs
├── scripts/
│   ├── start.sh              # Launch Docker container (macOS/Linux)
│   └── stop.sh               # Stop Docker container (macOS/Linux)
├── test/                     # Playwright E2E tests + docker-compose.test.yml
├── db/                       # Volume mount target (SQLite file lives here at runtime)
│   └── .gitkeep              # Directory exists in repo; finally.db is gitignored
├── Dockerfile                # Multi-stage build (Node → Python)
├── .env                      # Environment variables (gitignored, .env.example committed)
└── .gitignore
```

### Key Boundaries

- **`frontend/`** is a self-contained Next.js project. It knows nothing about Python. It talks to the backend via `/api/*` endpoints and `/api/stream/*` SSE endpoints. Internal structure is up to the Frontend Engineer agent.
- **`backend/`** is a self-contained uv project with its own `pyproject.toml`. It owns all server logic including database initialization, schema, seed data, API routes, SSE streaming, market data, and LLM integration. Internal structure is up to the Backend/Market Data agents.
- **`backend/db/`** contains schema SQL definitions and seed logic. The backend initializes the database at startup — creating tables and seeding default data if the SQLite file doesn't exist or is empty.
- **`db/`** at the top level is the runtime volume mount point. The SQLite file (`db/finally.db`) is created here by the backend and persists across container restarts via Docker volume.
- **`planning/`** contains project-wide documentation, including this plan. All agents reference files here as the shared contract.
- **`test/`** contains Playwright E2E tests and supporting infrastructure (e.g., `docker-compose.test.yml`). Unit tests live within `frontend/` and `backend/` respectively, following each framework's conventions.
- **`scripts/`** contains start/stop scripts that wrap Docker commands.

---

## 5. Environment Variables

```bash
# Required unless LLM_MOCK=true: OpenRouter API key for LLM chat functionality
OPENROUTER_API_KEY=your-openrouter-api-key-here

# Optional: set to "true" to fetch real prices from the MOEX ISS API
# If not set (or "false"), the built-in market simulator is used (recommended for most users)
# No API key is needed for MOEX — its free/anonymous tier requires no registration (see MOEX_API.md)
MOEX_ENABLED=false

# Optional: Set to "true" for deterministic mock LLM responses (testing)
LLM_MOCK=false

# Optional: SQLite file path (default: <project_root>/db/finally.db; the Docker image sets /app/db/finally.db)
DB_PATH=
```

### Behavior

- If `MOEX_ENABLED=true` → backend uses the MOEX ISS API for market data (real Russian stock prices, ~15 minutes delayed, no key required)
- If `MOEX_ENABLED` is absent, empty, or anything other than `"true"` → backend uses the built-in market simulator
- If `LLM_MOCK=true` → backend returns deterministic mock LLM responses (for E2E tests)
- The backend reads `.env` from the project root. Locally it is loaded with `python-dotenv`; in Docker it is passed via `--env-file`

---

## 6. Market Data

### Two Implementations, One Interface

Both the simulator and the MOEX client implement the same abstract interface. The backend selects which to use based on the `MOEX_ENABLED` environment variable. All downstream code (SSE streaming, price cache, frontend) is agnostic to the source.

### Simulator (Default)

- Generates prices using geometric Brownian motion (GBM) with configurable drift and volatility per ticker
- Updates at ~500ms intervals
- Correlated moves across tickers (e.g., oil & gas tickers move together)
- Occasional random "events" — sudden 2-5% moves on a ticker for drama
- Starts from realistic seed prices (e.g., SBER ~₽273, GAZP ~₽97, LKOH ~₽5,315, etc. — see §7's default watchlist)
- Runs as an in-process background task — no external dependencies

### MOEX ISS API (Optional)

See `planning/MOEX_API.md` for the full research behind this section (endpoints, verified example responses, the 15-minute delay measurement).

- REST API polling (not WebSocket) against `iss.moex.com` — simpler, and MOEX's own real-time feed requires a paid subscription anyway
- No API key or registration required. The free/anonymous tier returns prices delayed by ~15 minutes, updating continuously (not frozen end-of-day)
- Polls all tracked tickers every 15 seconds in a single request (`GET .../boards/TQBR/securities.json?securities=SBER,GAZP,...`) — MOEX publishes no official rate limit for anonymous access, so this interval is a conservative, good-citizen default rather than a measured ceiling
- Parses the REST response into the same format as the simulator; tickers not found on the TQBR board (invalid symbol) come back with an empty result rather than an error — treated the same as "no price yet"

### Tracked Tickers

The market data source tracks the **watchlist ∪ tickers with open positions**, so held positions keep getting prices after their ticker is removed from the watchlist.

- At startup: `source.start(watchlist ∪ positions)`
- Watchlist add (manual, via chat, or automatically when trading an untracked ticker) → `source.add_ticker(ticker)`
- Watchlist remove → `source.remove_ticker(ticker)` only if no open position in it
- Position closed (sold to zero) → `source.remove_ticker(ticker)` only if it is not in the watchlist

**Unknown tickers**: any symbol of 1–5 letters is accepted (symbols such as `BRK.B` are intentionally unsupported; MOEX preferred-share tickers like `SBERP` or `TATNP` fit this rule naturally). The simulator starts unknown tickers at a random price between ₽50 and ₽2,000 with default volatility — wider than a single-country-in-dollars range would need, since real MOEX tickers already span from ~₽50 (VTBR) to over ₽5,000 (LKOH). With MOEX, an invalid symbol simply never receives a price (shown as "—"; trades in it are rejected).

### Shared Price Cache

- A single background task (simulator or MOEX poller) writes to an in-memory price cache
- The cache holds the latest price, previous price, and timestamp for each ticker
- SSE streams read from this cache and push updates to connected clients
- This architecture supports future multi-user scenarios without changes to the data layer

### SSE Streaming

- Endpoint: `GET /api/stream/prices`
- Long-lived SSE connection; client uses native `EventSource` API
- Server checks the price cache every ~500ms and pushes one event with **all tracked tickers** whenever anything changed
- Client handles reconnection automatically (EventSource has built-in retry; the server sends `retry: 1000`)

Event format (implemented in `backend/app/market/stream.py`) — one `data:` message containing a dict keyed by ticker:

```
data: {"SBER": {"ticker": "SBER", "price": 273.5, "previous_price": 273.32, "timestamp": 1790000000.123, "change": 0.18, "change_percent": 0.0658, "direction": "up"}, "GAZP": {...}}
```

- `timestamp` — Unix seconds (float)
- `change` / `change_percent` — relative to the previous tick (not daily)
- `direction` — `"up"`, `"down"` or `"flat"`

---

## 7. Database

### SQLite with Initialization at Startup

The backend checks the SQLite database at startup (in the FastAPI lifespan handler). If the file doesn't exist or tables are missing, it creates the schema and seeds default data. This means:

- No separate migration step
- No manual database setup
- Fresh Docker volumes start with a clean, seeded database automatically

### Startup and Shutdown Order

The FastAPI lifespan handler runs, in order:

1. Initialize the database (schema + seed)
2. Load watchlist ∪ positions
3. Create the market data source and `await source.start(...)` (the MOEX source performs one blocking poll here)
4. Record the startup portfolio snapshot
5. Start the 30-second snapshot task

On shutdown: cancel the snapshot task, then `await source.stop()`.

### Conventions

- All stored timestamps are UTC ISO-8601 with a `Z` suffix. The frontend converts them to Unix seconds for Lightweight Charts.
- The database path comes from `DB_PATH` (§5).

### Schema

All tables except `users_profile` include a `user_id` column defaulting to `"default"` (`users_profile` is keyed by the user id itself in its `id` column). This is hardcoded for now (single-user) but enables future multi-user support without schema migration.

**users_profile** — User state (cash balance)
- `id` TEXT PRIMARY KEY (default: `"default"`)
- `cash_balance` REAL (default: `10000.0`)
- `created_at` TEXT (ISO timestamp)

**watchlist** — Tickers the user is watching
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `added_at` TEXT (ISO timestamp)
- UNIQUE constraint on `(user_id, ticker)`

**positions** — Current holdings (one row per ticker per user)
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `quantity` REAL (fractional shares supported)
- `avg_cost` REAL
- `updated_at` TEXT (ISO timestamp)
- UNIQUE constraint on `(user_id, ticker)`
- The row is deleted when a sell brings the rounded `quantity` to 0 (see Trade Rules)

**trades** — Trade history (append-only log)
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `side` TEXT (`"buy"` or `"sell"`)
- `quantity` REAL (fractional shares supported)
- `price` REAL
- `executed_at` TEXT (ISO timestamp)

**portfolio_snapshots** — Portfolio value over time (for P&L chart). Recorded once at startup (so the chart is never empty), every 30 seconds by a background task, and immediately after each trade execution.
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `total_value` REAL
- `recorded_at` TEXT (ISO timestamp)

**chat_messages** — Conversation history with LLM
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `role` TEXT (`"user"` or `"assistant"`)
- `content` TEXT
- `actions` TEXT (JSON — trades executed, watchlist changes made; null for user messages)
- `created_at` TEXT (ISO timestamp)

### Default Seed Data

- One user profile: `id="default"`, `cash_balance=10000.0` (displayed as ₽10,000)
- Ten watchlist entries (all on the MOEX TQBR board — regular equities, ordinary shares): SBER, GAZP, LKOH, GMKN, ROSN, NVTK, MTSS, TATN, PLZL, VTBR — Sberbank, Gazprom, Lukoil, Norilsk Nickel, Rosneft, Novatek, MTS, Tatneft, Polyus and VTB, spanning banking, oil & gas, metals/mining and telecom

---

## 8. API Endpoints

### Market Data
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/stream/prices` | SSE stream of live price updates |

### Portfolio
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/portfolio` | Current positions, cash balance, total value, unrealized P&L |
| POST | `/api/portfolio/trade` | Execute a trade: `{ticker, quantity, side}` |
| GET | `/api/portfolio/history` | Portfolio value snapshots over time (for P&L chart) |

### Watchlist
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/watchlist` | Current watchlist tickers (prices come from SSE) |
| POST | `/api/watchlist` | Add a ticker: `{ticker}` |
| DELETE | `/api/watchlist/{ticker}` | Remove a ticker |

### Chat
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/chat` | Send a message: `{message}`; receive complete JSON response (message + executed actions) |
| GET | `/api/chat/history` | Stored conversation, so the chat panel survives a page reload |

### System
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check (for Docker/deployment) |

### Response Shapes

Errors use FastAPI's default shape `{"detail": "<human-readable message>"}`: 400 for business-rule violations, 404 for unknown resources, 422 for malformed request bodies (missing fields or wrong JSON types).

Request models are type-only (`ticker: str`, `quantity: float`, `side: str`). All rule validation (ticker format, side, quantity) lives in the shared trade/watchlist service and raises 400, so manual and LLM actions use one code path. POST and DELETE endpoints return 200 with a body (never 201/204).

**`GET /api/portfolio`**
```json
{
  "cash_balance": 8100.0,
  "total_value": 10050.25,
  "unrealized_pnl": 50.25,
  "positions": [
    {"ticker": "SBER", "quantity": 10, "avg_cost": 270.0, "current_price": 275.03,
     "market_value": 2750.3, "unrealized_pnl": 50.3, "pnl_percent": 1.86}
  ]
}
```
- `total_value` = `cash_balance` + Σ `market_value`; `pnl_percent` = (`current_price` − `avg_cost`) / `avg_cost` × 100
- If a held ticker has no price yet (e.g. right after startup with MOEX), `current_price` is `null` and the position is valued at `avg_cost` (`market_value` = quantity × `avg_cost`, P&L = 0). The same rule applies to snapshots and the LLM context.
- The frontend header uses live SSE prices, so it may differ slightly from the backend `total_value`. This is expected.
- Monetary fields (`cash_balance`, `total_value`, prices, etc.) are plain numbers, in rubles; the frontend renders them with a `₽` symbol.

**`POST /api/portfolio/trade`** — request `{"ticker": "SBER", "quantity": 10, "side": "buy"}`; response 200:
```json
{
  "trade": {"id": "uuid", "ticker": "SBER", "side": "buy", "quantity": 10, "price": 270.0,
            "executed_at": "2026-09-28T10:00:00Z"},
  "portfolio": { "...same shape as GET /api/portfolio..." }
}
```
Error example (400): `{"detail": "Insufficient cash: need ₽53,150.00, have ₽10,000.00"}`

**`GET /api/portfolio/history`** — oldest first:
```json
[{"total_value": 10000.0, "recorded_at": "2026-09-28T10:00:00Z"}]
```

**`GET /api/watchlist`**, **`POST /api/watchlist`**, **`DELETE /api/watchlist/{ticker}`** — all return the current watchlist, ordered by `added_at`:
```json
["SBER", "GAZP", "LKOH"]
```
- POST: 400 if the ticker is not 1–5 letters; adding an existing ticker is a no-op
- DELETE: the `{ticker}` path parameter is stripped and uppercased; 404 if the ticker is not in the watchlist

**`POST /api/chat`** — request `{"message": "Buy 10 SBER"}`; response 200:
```json
{
  "message": "Done — bought 10 SBER at ₽270.00.",
  "actions": {
    "trades": [
      {"ticker": "SBER", "side": "buy", "quantity": 10, "status": "executed", "price": 270.0},
      {"ticker": "LKOH", "side": "buy", "quantity": 10, "status": "failed", "error": "Insufficient cash: ..."}
    ],
    "watchlist_changes": [
      {"ticker": "YDEX", "action": "add", "status": "executed"}
    ]
  }
}
```
The same `actions` JSON is stored in `chat_messages.actions`.

Watchlist change results: adding an existing ticker is `executed` (a no-op, like the REST endpoint); removing a ticker that is not in the watchlist, or adding an invalid symbol, is `failed` with an `error`.

**`GET /api/chat/history`** — oldest first; `actions` is `null` for user messages:
```json
[
  {"role": "user", "content": "Buy 10 SBER", "actions": null, "created_at": "2026-09-28T10:00:00Z"},
  {"role": "assistant", "content": "Done — bought 10 SBER at ₽270.00.", "actions": {"trades": [], "watchlist_changes": []}, "created_at": "2026-09-28T10:00:01Z"}
]
```

**`GET /api/health`** → `{"status": "ok"}`

### Trade Rules

Applied identically to manual trades and LLM trades:

Checks run in this order, so a failed trade has no side effects:

1. `ticker` is stripped and uppercased and must be 1–5 letters; `side` must be `"buy"` or `"sell"`
2. `quantity` is rounded to 4 decimal places (fractional shares allowed), then must be > 0
3. Sell only: `quantity` ≤ held quantity (compared after rounding)
4. If the ticker is not tracked, it is added to the watchlist (same as `POST /api/watchlist`). The simulator prices it immediately; with MOEX the price arrives on the next poll (up to 15s)
5. The ticker must have a current price in the price cache — otherwise 400 (`"No price available for XYZ yet, try again shortly"`)
6. Buy only: `quantity × price` ≤ `cash_balance`

Execution:

- Buy: new `avg_cost` = (old_qty × old_avg + qty × price) / (old_qty + qty)
- Sell: `avg_cost` is unchanged; the position row is deleted when the resulting quantity is ≤ 0
- The resulting quantity is rounded to 4 decimal places and `cash_balance` to cents after each trade, to avoid float drift
- Each trade runs in one SQLite transaction behind a single `asyncio.Lock` in the trade service, so concurrent manual and chat trades cannot interleave
- Every executed trade appends to `trades` and records a portfolio snapshot

---

## 9. LLM Integration

When writing code to make calls to LLMs, use cerebras-inference skill to use LiteLLM via OpenRouter to the `openrouter/openai/gpt-oss-120b` model with Cerebras as the inference provider. Structured Outputs should be used to interpret the results.

There is an OPENROUTER_API_KEY in the .env file in the project root.

### How It Works

When the user sends a chat message, the backend:

1. Loads the user's current portfolio context (cash, positions with P&L, watchlist with live prices, total portfolio value)
2. Loads the last 20 messages of conversation history from the `chat_messages` table. Each assistant message gets a compact summary of its `actions` appended, e.g. `[actions: bought 10 SBER @270.00; LKOH buy failed: Insufficient cash]`, so the LLM sees past failures
3. Constructs a prompt with a system message, portfolio context, conversation history, and the user's new message
4. Calls the LLM via LiteLLM → OpenRouter, requesting structured output, using the cerebras-inference skill. The call must not block the event loop: use `litellm.acompletion` (or wrap the sync call in `asyncio.to_thread`), otherwise SSE and the simulator stall while the model answers
5. Parses the complete structured JSON response
6. Auto-executes any trades or watchlist changes specified in the response
7. Stores the user message, and the assistant message with the execution results (`actions`), in `chat_messages`
8. Returns the complete JSON response to the frontend (shape in §8; no token-by-token streaming — Cerebras inference is fast enough that a loading indicator is sufficient)

### Structured Output Schema

The LLM is instructed to respond with JSON matching this schema:

```json
{
  "message": "Your conversational response to the user",
  "trades": [
    {"ticker": "SBER", "side": "buy", "quantity": 10}
  ],
  "watchlist_changes": [
    {"ticker": "YDEX", "action": "add"}
  ]
}
```

All three fields are required (strict structured output, no extra properties); the arrays are empty when unused. Define it as one Pydantic model: `side: Literal["buy", "sell"]`, `action: Literal["add", "remove"]`, `quantity: float`. A wrongly typed value (e.g. `"10"` for `quantity`) is a parse failure, not coerced.

- `message`: The conversational text shown to the user
- `trades`: Array of trades to auto-execute. `side` is `"buy"` or `"sell"`. Each trade goes through the same Trade Rules as manual trades (§8)
- `watchlist_changes`: Array of watchlist modifications. `action` is `"add"` or `"remove"`

If the LLM call fails or returns unparsable output, the endpoint returns a short apology as `message`, with empty `actions` and nothing executed.

### Auto-Execution

Trades specified by the LLM execute automatically — no confirmation dialog. This is a deliberate design choice:
- It's a simulated environment with fake money, so the stakes are zero
- It creates an impressive, fluid demo experience
- It demonstrates agentic AI capabilities — the core theme of the course

There is a single LLM call per message, so the LLM's `message` is written before execution. If a trade fails validation (e.g., insufficient cash), the backend marks it `"status": "failed"` with an `error` in `actions`, and the frontend renders it inline next to the message. There is no second LLM call. Failed actions are stored in history, so the LLM sees them in later turns.

### System Prompt Guidance

The LLM should be prompted as "FinAlly, an AI trading assistant for the Russian stock market (MOEX)" with instructions to:
- Analyze portfolio composition, risk concentration, and P&L
- Suggest trades with reasoning
- Execute trades when the user asks or agrees
- Manage the watchlist proactively
- Be concise and data-driven in responses
- Always respond with valid structured JSON

### LLM Mock Mode

When `LLM_MOCK=true`, the backend returns deterministic mock responses instead of calling OpenRouter. The mock response then goes through the normal execution flow. Rules (case-insensitive match on the user message):

- Contains `"buy"` → `message: "Mock: buying 1 share of SBER."`, `trades: [{"ticker": "SBER", "side": "buy", "quantity": 1}]`
- Contains `"sell"` → `message: "Mock: selling 1 share of SBER."`, `trades: [{"ticker": "SBER", "side": "sell", "quantity": 1}]`
- If both match, `"buy"` wins
- Otherwise → `message: "Mock: this is a test response."`, no actions

This enables:
- Fast, free, reproducible E2E tests
- Development without an API key
- CI/CD pipelines

---

## 10. Frontend Design

### Layout

The frontend is a single-page application with a dense, terminal-inspired layout. The specific component architecture and layout system is up to the Frontend Engineer, but the UI should include these elements:

- **Watchlist panel** — grid/table of watched tickers (from `GET /api/watchlist`, joined with SSE prices) with: ticker symbol, current price (flashing green/red on change), session change %, and a sparkline mini-chart (accumulated from SSE since page load). Session change % = change vs. the first price received for that ticker since page load (the simulator has no daily open).
- **Main chart area** — larger chart for the currently selected ticker, with at minimum price over time. Clicking a ticker in the watchlist selects it here.
- **Portfolio heatmap** — treemap visualization where each rectangle is a position, sized by portfolio weight, colored by P&L (green = profit, red = loss)
- **P&L chart** — line chart showing total portfolio value over time, using data from `portfolio_snapshots`
- **Positions table** — tabular view of all positions: ticker, quantity, avg cost, current price, unrealized P&L, % change
- **Trade bar** — simple input area: ticker field, quantity field, buy button, sell button. Market orders, instant fill.
- **AI chat panel** — docked/collapsible sidebar. Loads past messages from `GET /api/chat/history` on page load. Message input, scrolling conversation history, loading indicator while waiting for LLM response. Trade executions and watchlist changes shown inline as confirmations.
- **Header** — portfolio total value (updating live), connection status indicator, cash balance

### Technical Notes

- Use `EventSource` for SSE connection to `/api/stream/prices`
- Live portfolio values are computed on the frontend: header total value = cash + Σ quantity × latest SSE price; positions table and heatmap use the same live prices. The portfolio is fetched from `GET /api/portfolio` on load and refetched after every trade and chat response
- Charting:
  - **Lightweight Charts** (canvas) for the main price chart and the P&L chart
  - **Recharts `Treemap`** for the portfolio heatmap
  - **Inline SVG polyline** for sparklines (no library)
- Price flash effect: on receiving a new price, briefly apply a CSS class with background color transition, then remove it. The direction is decided by comparing with the frontend's own last-rendered price for that ticker, not the event's `direction` field (server ticks can coalesce or be skipped)
- All API calls go to the same origin (`/api/*`) — no CORS configuration needed
- Currency formatting: all monetary values (cash, prices, P&L) are plain numbers from the API, in rubles. The frontend formats them with a `₽` prefix, e.g. `₽10,000.00` — there is no locale-driven currency formatting requirement beyond this
- Local development: `output: 'export'` does not support `next dev` rewrites, so the workflow is `npm run build` and FastAPI serves `frontend/out` (from `backend/static` in Docker, see §11)
- Tailwind CSS for styling with a custom dark theme

---

## 11. Docker & Deployment

### Multi-Stage Dockerfile

```
Stage 1: Node 24 slim (LTS)
  - Copy frontend/
  - npm install && npm run build (produces static export)

Stage 2: Python 3.12 slim
  - Install uv (COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/)
  - Copy backend/
  - uv sync --frozen --no-dev (install Python dependencies from lockfile)
  - Copy frontend build output into backend/static/
  - Expose port 8000
  - CMD: uvicorn serving FastAPI app
```

FastAPI serves all API routes on port 8000 and mounts `backend/static` with `StaticFiles(html=True)` at `/`, registered after all `/api` routers.

### Docker Volume

The SQLite database persists via a named Docker volume:

```bash
docker run -v finally-data:/app/db -p 8000:8000 --env-file .env finally
```

The `db/` directory in the project root maps to `/app/db` in the container. The backend writes `finally.db` to this path via `DB_PATH=/app/db/finally.db` (set in the Dockerfile). Locally the default is `<project_root>/db/finally.db`, not `backend/db/`, which holds the schema code.

### Start/Stop Scripts

**`scripts/start.sh`** (macOS/Linux):
- Builds the Docker image if not already built (or if `--build` flag passed)
- Runs the container with the volume mount, port mapping, and `.env` file
- Prints the URL to access the app
- Optionally opens the browser

**`scripts/stop.sh`** (macOS/Linux):
- Stops and removes the running container
- Does NOT remove the volume (data persists)

All scripts should be idempotent — safe to run multiple times.

### Optional Cloud Deployment

The container is designed to deploy to AWS App Runner, Render, or any container platform. A Terraform configuration for App Runner may be provided in a `deploy/` directory as a stretch goal, but is not part of the core build.

---

## 12. Testing Strategy

### Unit Tests (within `frontend/` and `backend/`)

**Backend (pytest)**:
- Market data: simulator generates valid prices, GBM math is correct, MOEX ISS response parsing works, both implementations conform to the abstract interface
- Portfolio: trade execution logic, P&L calculations, edge cases (selling more than owned, buying with insufficient cash, selling at a loss)
- LLM: structured output parsing handles all valid schemas, graceful handling of malformed responses, trade validation within chat flow
- API routes: correct status codes, response shapes, error handling

**Frontend (React Testing Library or similar)**:
- Component rendering with mock data
- Price flash animation triggers correctly on price changes
- Watchlist CRUD operations
- Portfolio display calculations
- Chat message rendering and loading state

### E2E Tests (in `test/`)

**Infrastructure**: A separate `docker-compose.test.yml` in `test/` that spins up the app container plus a Playwright container. This keeps browser dependencies out of the production image.

**Environment**: Tests run with `LLM_MOCK=true` by default for speed and determinism.

**Key Scenarios**:
- Fresh start: default watchlist appears, ₽10k balance shown, prices are streaming
- Add and remove a ticker from the watchlist
- Buy shares: cash decreases, position appears, portfolio updates
- Sell shares: cash increases, position updates or disappears
- Portfolio visualization: heatmap renders with correct colors, P&L chart has data points
- AI chat (mocked): send a message, receive a response, trade execution appears inline. The mock sells SBER, which is not held by default, so a "sell" scenario must buy first
- SSE resilience: block the stream (`page.route('**/api/stream/prices', r => r.abort())` or `context.setOffline(true)`), assert the status dot turns yellow/red, restore, assert it turns green

