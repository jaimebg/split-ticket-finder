# Flight Finder Telegram Bot — Design Document

**Date:** 2026-02-25
**Status:** Approved

## Problem

Finding cheap flights from the Canary Islands means exploiting the 85% resident
discount on Spanish domestic flights. The optimal strategy is splitting the trip:
cheap domestic hop to a peninsula hub, then international from there. The existing
CLI script (`flight_finder.py`) automates this but requires terminal access and
manual invocation. A Telegram bot makes this accessible from a phone at any time.

## Decisions

| Decision | Choice |
|---|---|
| Audience | Private — single user (owner ID locked) |
| Interaction | Guided flow with inline keyboard buttons |
| Long searches | Fire and forget — bot notifies on completion |
| Results format | Text summary in chat with Google Flights links |
| Persistence | SQLite — history, favorites, price tracking |
| Alerts | Auto-threshold: 10%+ below all-time record triggers alert |
| Architecture | Async monolith — single process, asyncio scraper |
| Dependencies | python-telegram-bot, aiosqlite, curl (system) |

## Architecture

Single Python process running:
- Telegram bot (python-telegram-bot v21+, native asyncio)
- Async scraper (curl via asyncio.create_subprocess_exec)
- Scheduler (asyncio background task for periodic favorite re-checks)
- SQLite via aiosqlite for all persistence

No workers, no queue, no Docker, no external services.

## Project Structure

```
flight_finder/
├── bot.py                  # Entry point — starts bot + scheduler
├── scraper.py              # Async Google Flights scraper
├── search.py               # Search orchestrator — combines domestic + intl legs
├── db.py                   # SQLite persistence
├── scheduler.py            # Periodic re-check of favorites for price drops
├── handlers/
│   ├── start.py            # /start, welcome, main menu
│   ├── search_flow.py      # Guided search conversation
│   ├── history.py          # Search history, rerun
│   └── favorites.py        # Save/delete favorites, alert management
├── config.py               # Bot token, defaults, constants
├── requirements.txt        # python-telegram-bot[ext], aiosqlite
└── .env                    # BOT_TOKEN, OWNER_ID (gitignored)
```

## Conversation Flows

### Main Menu (/start)

Four inline buttons: Search flights, My favorites, Search history, Settings.

### Search Flow (guided)

1. "Where to?" — user types airport code(s)
2. "Dates?" — button choice: Fixed dates / Date range
3. "Hubs?" — button choice: All defaults / Big two (MAD,BCN) / Custom
4. "Confirm?" — summary + Start search / Cancel buttons
5. Bot says "on it", runs search in background, sends results when done

### History

Shows last 10 searches with best price. Buttons to view full results or rerun.

### Favorites + Alerts

From any search result, user can save a specific origin->hub->destination combo.
Favorites list shows current price vs record. Can delete individual favorites.
Bot re-checks every 6 hours and alerts on 10%+ price drops.

## Database Schema

```sql
searches (id, created_at, origin, destinations, dates, hubs, adults, currency,
          best_price, best_route, results)

favorites (id, created_at, origin, hub, destination, adults, currency,
           record_price, record_date, last_price, last_checked, check_dates)

price_checks (id, favorite_id, checked_at, best_price, route_detail)
```

## Scheduler

- Asyncio background task, loops every 6 hours
- For each favorite: searches 3-5 dates, single hub+destination combo
- If new_price < record_price * 0.90: updates record, sends alert
- Otherwise: silently updates last_price/last_checked

## Config

Via .env file: BOT_TOKEN, OWNER_ID, ORIGIN, ALERT_INTERVAL_HOURS,
PRICE_DROP_THRESHOLD, REQUEST_DELAY.

## Tech Stack

- Python 3.10+
- python-telegram-bot[ext] (v21+)
- aiosqlite
- curl (system binary, no pip dependency)
- stdlib: asyncio, json, re, base64, sqlite3
