# MCP server — the engine as tools for an AI assistant

Date: 2026-09-26. Branch: `feat/mcp-server` (from `main` @ `9a83997`).

The search engine has no Telegram dependency (`engine/`, `providers/`,
`results/`). This subproject exposes it as a local
[Model Context Protocol](https://modelcontextprotocol.io) server, so
Claude Code, Claude Desktop or any other MCP client can run split-ticket
searches on the user's own machine.

**Scope, as approved.**

- It runs locally over stdio only.
- The three tools are read-only.
- It uses no database: no favourites and no history.
- The bot on oracle1 neither installs nor runs it.

## 1. Packaging

- A new package, `split_ticket_mcp/`. It can't be named `mcp`, because that
  is the SDK's own name.
- `pyproject.toml`:
  - a new optional extra: `mcp = ["mcp>=2.2,<3"]`;
  - `dev` also gains `mcp>=2.2,<3`, so CI tests the server;
  - `[project.scripts] split-ticket-mcp = "split_ticket_mcp.server:main"`;
  - `split_ticket_mcp*` is added to the setuptools `find` include.
- The production install (`pip install -e .`, run by `deploy/update.sh`) does
  not pull `mcp`, and the bot never imports the package.
- Library: the official `mcp` SDK v2 (2.2.0, verified 2026-09-26; requires
  Python ≥ 3.10). The server is `mcp.server.MCPServer` with `@mcp.tool()`.
  Tests use `mcp.Client(server)` in memory. `main()` calls `mcp.run()`,
  whose transport defaults to stdio.
- **stdout carries the protocol.** `main()` sends logging to stderr before
  it does anything else, and nothing in the package prints.

## 2. Tools

Every tool validates its input, then calls exactly what the bot calls.
Failures are raised as `mcp.server.mcpserver.exceptions.ToolError` with a
sentence meant for a person.

### `search_split_tickets`

| Parameter | Type | Default |
|---|---|---|
| `destinations` | list of IATA codes (1–10) | required |
| `window_start`, `window_end` | `YYYY-MM-DD` | required |
| `origin` | IATA | `config.ORIGIN` |
| `hubs` | list of IATA | `config.DEFAULT_HUBS` |
| `trip_days` | int, 0 = one-way | 0 |
| `adults`, `children` | int | 1, 0 |
| `cabin` | `ECONOMY` / `PREMIUM_ECONOMY` / `BUSINESS` / `FIRST_CLASS` | `ECONOMY` |
| `currency` | 3 letters | `EUR` |
| `max_stops`, `min_layover_minutes` | int or null | null |
| `overnight` | bool (a night at the hub) | false |
| `hide_risky` | bool (drop HIGH/IMPOSSIBLE/UNKNOWN connections) | false |
| `limit` | 1–30 | 10 |

The tool description says plainly that one call sends roughly 90–190
requests to the flight sources and takes up to a minute. That tells an
assistant to call it deliberately, not in a loop.

**What it does.**

1. It validates the input:
   - codes: 3 letters, upper-cased;
   - dates: ISO, `start ≤ end`, not in the past;
   - the passenger bounds from 3c;
   - `window ≤ MAX_WINDOW_DAYS`.

   The date rules come from `models.bookable_onward_dates` and the rest
   mirrors the builder.
2. It builds `SearchOptions`, then checks
   `capabilities_of(primary_provider()).rejects(options)`.
3. It calls `engine.run_search`.
4. It applies `hide_risky` through `results.filters.Filters(hide_risky=True)`.
5. It sorts by total and maps the first `limit` itineraries.

**Output** (Pydantic):

```
SearchOutput
  strategy: "two-stage" | "grid"
  currency: str
  routes_found: int            # before hide_risky / limit
  hidden_as_risky: int
  failed_requests: int         # parse + fetch errors: an empty result with failures is not "no flights"
  itineraries: list[ItineraryOut]

ItineraryOut
  total: float                 # what the whole party pays for the split
  status: "confirmed" | "partial" | "estimate"
  date, return_date, dom_date, dom_return_date: str
  hub, hub_name, dest, dest_name: str
  overnight: bool
  domestic_discount_pct: int
  through_fare: float | null   # the airline's single ticket, when priced
  savings: float | null        # negative when the single ticket is cheaper
  risk: "low" | "medium" | "unknown" | "high" | "impossible" | null
  risk_reasons: list[str]
  tickets: list[TicketOut]     # in travel order, 2 one-way / 4 round trip

TicketOut
  role: "domestic out" | "onward out" | "onward return" | "domestic return"
  price: float                 # before the discount
  paid: float                  # after it (domestic tickets only differ)
  from_airport, to_airport: str | null
  stops: int | null
  duration_minutes: int | null
  flights: list[str]           # flight numbers
  departs, arrives: str | null # local ISO datetimes, first/last segment
  booking_url: str | null
  bags: str                    # the same wording as the results detail
```

A ticket that has no offer yet (partial or estimate) is still listed, with
`price` taken from the calendar estimate for its side and every flight field
`null`. That way an assistant can never present an unconfirmed ticket as
bookable.

The mapping is a pure function in `split_ticket_mcp/mapping.py`:
`itinerary_out(itin, currency) -> ItineraryOut`. The same rules the
Telegram view follows apply: savings keep their sign, and unknown bag
counts stay unknown.

### `price_calendar`

Input: `origin`, `dest`, `start`, `end` (≤ `MAX_WINDOW_DAYS`), and `adults`,
`children`, `cabin`, `currency`, `max_stops`.

Output: `{currency, days: [{date, price, rating}]}`, sorted by date. These
are the primary provider's cheapest-of-day **direct-route** prices, one
request per call.

A primary provider without a calendar (Google) returns
`ToolError("Your flight source has no price calendar.")`.

### `find_airports`

Input: `query` (text, 2–60 characters). Output: `list[{code, name, city,
country}]`. It uses `handlers.search.places.places_provider()`, the same
selection the builder makes, and calls `resolve_place` directly: the
bot's SQLite place cache is not touched.

If no provider can resolve names, it returns `ToolError("No configured
flight source can look up airport names; pass IATA codes instead.")`.

## 3. Configuration

`config.py` loads `.env` from the working directory, and an MCP client may
start the server anywhere. The server needs no secrets, since
`BOT_TOKEN`/`OWNER_ID` are bot-only and `config.validate()` is not called.
The defaults (`PROVIDERS=kiwi,google`, `ORIGIN=LPA`) work as they are.

The README documents both ways to override them: set `cwd` to the checkout,
or pass environment variables in the client config.

## 4. Testing

| File | Covers |
|---|---|
| `test_mcp_mapping.py` | confirmed / partial / estimate mapping; savings sign; unknown bags; round-trip ticket order; overnight dates |
| `test_mcp_server.py` | through `Client(server)` with `run_search`, `primary_provider` and `places_provider` patched: a search returns the structured output; `hide_risky` counts what it hid; `failed_requests`; each validation error (bad code, past dates, window too wide, bad passengers, capability refusal) is a tool error with its sentence; the calendar and airports tools, including the "no calendar" and "no places" errors; the server lists exactly the three tools |
| `test_mcp_stdio.py` | `main()` configures logging to stderr; importing the package prints nothing to stdout |
| `test_deploy_artifacts.py` | the console script is declared, and `mcp` is not a core dependency |

## 5. README

A "Use it from an AI assistant" section:

```bash
pip install -e ".[mcp]"
claude mcp add split-tickets -- /path/to/split-ticket-finder/.venv/bin/split-ticket-mcp
```

It also gives the Claude Desktop `mcpServers` JSON (`command`, `cwd`, `env`)
and an example prompt.

## 6. Done when

- `split-ticket-mcp` starts a stdio server exposing the three tools, and a
  real search through it returns the same itineraries the bot shows for
  the same query.
- The production install is unchanged.
- Full suite green; `ruff check .` clean.
