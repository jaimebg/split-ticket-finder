# Flight Finder Telegram Bot — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a Telegram bot that wraps the existing Google Flights scraper, providing a guided button-based UI to search flights, save favorites, view history, and get automatic price drop alerts.

**Architecture:** Single async Python process. Telegram bot (python-telegram-bot v21+) handles user interaction. Scraper uses asyncio.create_subprocess_exec to run curl non-blockingly. SQLite via aiosqlite stores history, favorites, and price checks. A background asyncio task re-checks favorites every 6 hours.

**Tech Stack:** Python 3.10+, python-telegram-bot[ext], aiosqlite, python-dotenv, curl (system)

---

### Task 1: Project scaffold and config

**Files:**
- Move: `flight_finder.py` -> `flight_finder/scraper.py` (strip CLI code, keep core, make async)
- Create: `flight_finder/config.py`
- Create: `flight_finder/requirements.txt`
- Create: `flight_finder/.env`
- Create: `flight_finder/.gitignore`
- Create: `flight_finder/bot.py` (stub)

**Step 1: Create .gitignore**

```
.env
__pycache__/
*.pyc
*.db
```

**Step 2: Create .env with bot token and owner id**

**Step 3: Create requirements.txt**

```
python-telegram-bot[ext]>=21.0
aiosqlite>=0.20.0
python-dotenv>=1.0.0
```

**Step 4: Create config.py** with all constants: BOT_TOKEN, OWNER_ID, ORIGIN, hub dicts, discount settings, SOCS_COOKIE, alert config. All loaded from os.getenv with defaults.

**Step 5: Create scraper.py** — extract from existing flight_finder.py:
- All protobuf helpers (_varint, _field, _str, _bytes, _var, encode_tfs, build_url)
- FlightResult dataclass, Route dataclass, fmt_dur, generate_dates
- _parse_offer, parse_flights (sync, pure functions — no change)
- fetch_html -> make async with asyncio.create_subprocess_exec instead of subprocess.run
- search -> make async (awaits fetch_html)
- Remove: argparse, interactive_config, parse_args, main, Tee, all CLI output code

**Step 6: Create bot.py stub** with logging setup and empty main()

**Step 7: Install deps and verify**

```bash
pip install -r requirements.txt
python -c "from scraper import search, FlightResult, Route; print('OK')"
```

**Step 8: Commit** "feat: project scaffold with async scraper and config"

---

### Task 2: Database layer

**Files:**
- Create: `flight_finder/db.py`

**Step 1: Write db.py** with:
- SCHEMA string creating 3 tables: searches, favorites, price_checks
- init_db() — runs CREATE TABLE IF NOT EXISTS
- save_search() — insert into searches with JSON-serialized destinations/dates/hubs/results
- get_searches(limit) — last N searches
- get_search_by_id(id)
- add_favorite(origin, hub, destination, adults, currency, price, check_dates)
- get_favorites()
- delete_favorite(fav_id)
- update_favorite_price(fav_id, price, is_record) — updates last_price + optionally record_price
- add_price_check(fav_id, price, route_detail)

All functions use `async with aiosqlite.connect(DB_PATH)`.

**Step 2: Verify**

```bash
python -c "import asyncio; from db import init_db; asyncio.run(init_db()); print('OK')"
```

**Step 3: Commit** "feat: SQLite database layer"

---

### Task 3: Search orchestrator

**Files:**
- Create: `flight_finder/search.py`

**Step 1: Write search.py** with:
- run_search(origin, destinations, dates, hubs, adults, currency, delay) -> list[Route]
  - Phase 1: async loop over hubs x dates, await search(), build dom_cache
  - Phase 2: async loop over hubs_found x destinations x dates, build intl_cache
  - Phase 3: combine, apply discount, sort by total
- routes_to_json(routes) — serialize top 25 to JSON string for DB
- format_results(routes, origin, currency) -> str — Telegram-friendly text with top 10, summaries by hub/date, Google Flights links for top 3. Split-friendly (no single line > 4096 chars).

**Step 2: Commit** "feat: async search orchestrator with result formatting"

---

### Task 4: Bot entry point with owner auth and main menu

**Files:**
- Create: `flight_finder/handlers/__init__.py` (empty)
- Create: `flight_finder/handlers/start.py`
- Modify: `flight_finder/bot.py`

**Step 1: Create handlers/start.py** with:
- owner_only decorator — checks update.effective_user.id == OWNER_ID, replies "Private bot." otherwise
- owner_only_callback — same but for callback queries
- MAIN_MENU_KEYBOARD — InlineKeyboardMarkup with 3 buttons: Search flights, My favorites, Search history
- start_command handler — sends welcome + main menu
- main_menu_callback — re-shows main menu (for "Back" buttons)

**Step 2: Update bot.py** — import from handlers.start, build Application with BOT_TOKEN, register /start and menu_main callback, add post_init that calls init_db, run_polling.

**Step 3: Test** — /start shows buttons, non-owner gets rejected

**Step 4: Commit** "feat: bot entry point with /start, main menu, owner auth"

---

### Task 5: Search flow handler (guided conversation)

**Files:**
- Create: `flight_finder/handlers/search_flow.py`
- Modify: `flight_finder/bot.py`

**Step 1: Create handlers/search_flow.py** using ConversationHandler with states:
- DEST: entry from menu_search callback. Asks "Where to?". User types codes.
- DATE_MODE: buttons "Fixed dates" / "Date range"
- FIXED_DATES: user types comma-separated dates
- RANGE_START, RANGE_END, RANGE_EVERY: step-by-step range input, every N via buttons (3/5/7/10)
- HUBS: buttons for All/Top2(MAD,BCN)/Top3(+LIS)/Custom
- CONFIRM: shows summary (origin, dests, N dates, hubs, ~N queries), buttons Start/Cancel

On "Start search":
- Sends "On it, I'll message you when done."
- Launches _run_search_task as background via application.create_task
- Returns ConversationHandler.END

_run_search_task:
- Calls run_search from search.py
- Calls format_results
- Sends result text (split into 4000-char chunks for Telegram limit)
- Saves to DB via save_search
- Sends "Save best as favorite" button if routes found

**Step 2: Wire into bot.py** — add_handler(build_search_conversation())

**Step 3: End-to-end test** in Telegram

**Step 4: Commit** "feat: guided search flow with conversation handler"

---

### Task 6: History handler

**Files:**
- Create: `flight_finder/handlers/history.py`
- Modify: `flight_finder/bot.py`

**Step 1: Create handlers/history.py** with:
- history_menu — fetches last 10 searches, shows list with results/rerun buttons per row
- history_view — loads search by id, shows top 10 results formatted
- history_rerun — reconstructs search params from DB row, launches _run_search_task
- get_history_handlers() — returns list of CallbackQueryHandlers

**Step 2: Wire into bot.py**

**Step 3: Test** — run search, check history, view results, rerun

**Step 4: Commit** "feat: search history with view and rerun"

---

### Task 7: Favorites handler

**Files:**
- Create: `flight_finder/handlers/favorites.py`
- Modify: `flight_finder/bot.py`

**Step 1: Create handlers/favorites.py** with:
- favorites_menu — fetches all favorites, shows list with delete buttons
- save_favorite — called from savefav_ callback, parses hub+dest from callback data, extracts price from message text, saves to DB
- delete_fav — deletes and refreshes list
- get_favorites_handlers() — returns list of CallbackQueryHandlers

**Step 2: Wire into bot.py**

**Step 3: Test** — save from search, list, delete

**Step 4: Commit** "feat: favorites management"

---

### Task 8: Scheduler for automatic price alerts

**Files:**
- Create: `flight_finder/scheduler.py`
- Modify: `flight_finder/bot.py`

**Step 1: Create scheduler.py** with:
- check_favorites(bot, owner_chat_id) — iterates all favorites, for each:
  - Loads check_dates, samples max 5
  - For each date: search domestic + international, compute discounted total
  - Tracks best price across sample dates
  - Saves price_check record
  - If best_price < record_price * (1 - PRICE_DROP_THRESHOLD): send alert message, update record
  - Otherwise: just update last_price/last_checked
- scheduler_loop(bot, owner_chat_id) — infinite loop, sleeps ALERT_INTERVAL_HOURS, calls check_favorites

**Step 2: Wire into bot.py** — in post_init, asyncio.create_task(scheduler_loop(...))

**Step 3: Test** — save a favorite, verify scheduler logs checks

**Step 4: Commit** "feat: background scheduler for price drop alerts"

---

### Task 9: Final wiring and end-to-end test

**Files:**
- Modify: `flight_finder/bot.py` (final clean version)

**Step 1: Write final bot.py** ensuring correct handler registration order:
1. ConversationHandler (search_flow) — must be first for priority
2. History callback handlers
3. Favorites callback handlers
4. main_menu_callback (fallback for menu_main)
5. /start CommandHandler

**Step 2: Full end-to-end test checklist:**
- /start -> 3 buttons visible
- Search -> NRT -> Fixed dates -> 2026-03-15 -> MAD,BCN -> Start -> wait -> results arrive
- Save best as favorite
- /start -> Search history -> see search -> view results -> rerun
- /start -> My favorites -> see favorite -> delete
- Verify scheduler logs in terminal

**Step 3: Final commit** "feat: final bot wiring, ready to run"
