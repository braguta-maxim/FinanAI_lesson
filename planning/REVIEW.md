# Review of PLAN.md

Overall the plan is clear, well-scoped and mostly consistent with the market data code that already exists. The response shapes and Trade Rules are especially useful. Below are gaps and ambiguities that agents are likely to hit, in priority order. Each item includes a suggested minimal fix.

## High priority

### 1. Trade validation order has side effects
§8 Trade Rules says an untracked ticker is "first added to the watchlist", and only then checked for a price, cash and holdings. As a result:
- Selling a ticker you don't hold adds it to the watchlist, then fails.
- With Massive, every first trade in a new ticker fails ("try again shortly"), but the ticker stays in the watchlist. An invalid symbol stays there permanently, showing "—".
- A quantity that rounds to 0 at 4 decimal places (e.g. 0.00001) passes a `> 0` check done before rounding.

**Suggestion:** fix the order as: normalize ticker → validate ticker format → round quantity, then check `> 0` → for a sell, check holdings → add to the watchlist if untracked → check price → check cash → execute. State that the `> 0` check happens after rounding.

### 2. How to value a position with no price
`GET /api/portfolio`, portfolio snapshots, the LLM context and the frontend header all need `current_price`. With Massive, a price may be missing (right after startup, or if a poll fails). The simulator has the same gap after a ticker is removed and re-added.

**Suggestion:** state one rule. Fall back to `avg_cost` (so P&L = 0) and return `current_price: null` in the API.

### 3. Float drift in positions and cash
Quantities and cash are REAL. Selling "all" of a position bought in fractions can leave something like `1e-12` shares, so the row is never deleted.

**Suggestion:**
- Round the resulting quantity to 4 decimal places after each trade, and delete the row when it is `<= 0`.
- Round `cash_balance` to cents after each trade.
- Compare "quantity <= held" using the rounded values.

### 4. Trades must be atomic
Manual trades and chat trades can run at the same time. Each one reads cash and position, then writes the position, cash, trade row and snapshot.

**Suggestion:** say explicitly that each trade runs in one SQLite transaction, behind a single `asyncio.Lock` (or `BEGIN IMMEDIATE`) in the trade service.

### 5. Status codes: 400 vs 422 conflicts with Pydantic
The plan says 422 for malformed bodies and 400 for business rules. But the natural FastAPI model (`side: Literal["buy","sell"]`, `quantity: float = Field(gt=0)`) returns 422 for a bad side or quantity. Meanwhile the Trade Rules read as if these are 400s. LLM trades bypass the request model entirely, so the same checks must also exist in the service layer.

**Suggestion:** keep request models type-only (`ticker: str`, `quantity: float`, `side: str`). Do all rule validation in the shared trade/watchlist service and raise 400 with a readable `detail`. This gives one code path for manual and LLM actions. Add a sentence to §8.

### 6. The LLM call must not block the event loop
The cerebras-inference skill shows the synchronous `litellm.completion(...)`. Calling it directly in an `async def` route would freeze SSE and the simulator for the whole call.

**Suggestion:** state that the backend uses `litellm.acompletion` (same arguments), or wraps the call in `asyncio.to_thread`.

### 7. No local development workflow for the frontend
`output: 'export'` does not support Next.js `rewrites`, so `next dev` cannot proxy `/api/*` to FastAPI. The plan says "no CORS", which leaves the Frontend agent with no dev loop other than a full rebuild.

**Suggestion:** pick one approach and document it:
- (a) Add a dev-only `NEXT_PUBLIC_API_BASE=http://localhost:8000`, plus CORS enabled only when an env flag is set; or
- (b) always `npm run build` and let FastAPI serve `frontend/out` locally.

Option (b) is simplest.

## Medium priority

### 8. Paths for the database and static files
- `db/finally.db` means `/app/db` in the container. Locally the backend runs from `backend/`, so a relative path resolves to `backend/db/` — the schema directory, not the volume.
- "Copy frontend build output into a static/ directory" doesn't say where FastAPI looks for it.

**Suggestion:**
- Add a `DB_PATH` env var (default: `<project_root>/db/finally.db`), and `/app/db/finally.db` in Docker.
- Name the static directory explicitly (e.g. `backend/static`, mounted with `StaticFiles(html=True)` at `/` *after* all `/api` routers).

### 9. Startup order in the lifespan handler
The startup snapshot needs prices, so the order matters. Spell it out:
1. init DB
2. load watchlist ∪ positions
3. `create_market_data_source(cache)` then `await source.start(...)`
4. startup snapshot
5. start the 30 s snapshot task

On shutdown, cancel the snapshot task, then `await source.stop()`.

Note: `MassiveDataSource.start()` performs a blocking first poll, so startup waits on one network call.

### 10. How chat history is fed to the LLM
The plan says the last 20 messages are loaded and that "failed actions are stored in history, so the LLM sees them". But it doesn't say how `actions` reach the prompt.

**Suggestion:** append a compact summary of `actions` to each assistant message's content when building the prompt, e.g. `[actions: bought 10 AAPL @190.00; TSLA buy failed: Insufficient cash]`.

### 11. Result format for watchlist changes in chat
Trades have `executed` / `failed` + `error`, but the watchlist-change semantics are unspecified.

**Suggestion:**
- Removing a ticker that isn't in the watchlist -> `failed` with an error.
- Adding one that already exists -> `executed` (a no-op, matching the REST endpoint).
- An invalid symbol -> `failed`.

### 12. Details of the structured-output schema
State the Pydantic model explicitly so every agent builds the same one:
- `side: Literal["buy","sell"]`
- `action: Literal["add","remove"]`
- `quantity: float`
- all fields required, no extra properties

Also: when a trade validates but the LLM gives a quantity like `"10"`, treat it as a parse failure, not a coercion.

### 13. SSE / frontend flash semantics
The server checks the cache every 500 ms and the simulator also ticks every 500 ms, so ticks can coalesce or be skipped. That makes `previous_price`/`direction` in an event not necessarily match what the client showed last.

**Suggestion:** say the frontend decides the flash direction by comparing against its own last-rendered price for that ticker, not the `direction` field.

### 14. Watchlist DELETE normalization
Say the `{ticker}` path parameter is stripped and uppercased too, so `DELETE /api/watchlist/aapl` works.

### 15. Timestamps
Say all stored timestamps are UTC ISO-8601 with a `Z` suffix, as the examples show. Lightweight Charts needs Unix seconds, so the frontend converts them.

## Low priority / nice to have

16. **`OPENROUTER_API_KEY` is labeled "Required"**, but it isn't needed when `LLM_MOCK=true`. Reword it to "Required unless LLM_MOCK=true". Also say how `.env` is loaded for local runs (e.g. `python-dotenv`, or `uv run --env-file ../.env`). In Docker it is `--env-file`.
17. **Snapshot growth:** 2,880 rows per day, forever. That's fine for a demo, but `/api/portfolio/history` returning everything will slowly grow. Optionally cap it (e.g. the last 24 h, or the last 1,000 points).
18. **Mock mode precedence:** if a message contains both "buy" and "sell", the buy rule wins — say so. Also note that the E2E "sell via chat" scenario must buy first, because the mock sells AAPL, which isn't held by default.
19. **The E2E "SSE resilience" scenario is vague.** Suggest how: Playwright `page.route('**/api/stream/prices', r => r.abort())`, or `context.setOffline(true)`, then restore and assert the status dot turns yellow/red, then green.
20. **Header value vs backend `total_value`:** these will differ slightly (live SSE prices vs the fetch-time cache). That's acceptable, but worth one sentence so nobody "fixes" it.
21. **Ticker format "1-5 letters" excludes symbols like `BRK.B`.** Acceptable, but say it's intentional.
22. **Docker base images:** Node 24 LTS is fine. Python 3.12 matches `requires-python >=3.12`. Recommend installing uv via `COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/` and running `uv sync --frozen --no-dev`.
23. **HTTP codes for POST/DELETE:** say 200 (not 201/204), since all of them return the watchlist body.

## Observations about the existing market data code (FYI for the Backend agent)

- **`PriceCache.remove()` doesn't bump `version`.** A removal alone won't trigger an SSE push. That's harmless with the simulator (it ticks constantly), but note it if the frontend is expected to drop tickers based on SSE. The plan says the watchlist comes from REST, so it's fine as is.
- **`stream.py` defines `router` at module level** and registers the route inside `create_stream_router()`. Calling the factory twice (e.g. across test app instances) registers `/prices` twice. Create the `APIRouter` inside the factory.
- **`MassiveDataSource` normalizes tickers (upper/strip) but `SimulatorDataSource` doesn't.** That's fine as long as the API layer normalizes before calling `add_ticker`/`remove_ticker`, which §8 already requires.
- **Unknown simulator tickers start at a random price.** Removing a ticker and adding it back resets it to a new random price, which can make a held position's P&L jump. This can only happen if the position was closed first (the plan keeps held tickers tracked), so it's acceptable.
