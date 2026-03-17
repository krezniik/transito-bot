# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the bot

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally (requires .env file with credentials)
python bot.py
```

## Environment variables required

| Variable | Description |
|---|---|
| `TELEGRAM_TOKEN` | Bot token from @BotFather |
| `ANTHROPIC_API_KEY` | For Claude Haiku (lote parsing) |
| `OPENAI_API_KEY` | For Whisper (voice transcription) |
| `CHAT_ID_GRUPO` | Telegram group chat ID for auto-reports |
| `ALLOWED_USERS` | Comma-separated Telegram user IDs (empty = all users allowed) |
| `TIMEZONE` | Default: `America/Guatemala` |

## Architecture

The bot is a **Telegram bot for a bean processing plant**. Workers register transit lots (batches of cans moving between machines), and the bot calculates how many boxes are in transit.

### Data flow for `/lote`

1. User sends text or voice note with lot data (machine, baskets, presentation, product, pin, market)
2. `lote_parser.py` sends text to **Claude Haiku** → receives structured JSON
3. `humanizar()` applies conversion tables (`TABLA`, `TABLA_ENTEROS`) to calculate `cajas_por_canasta`
4. `database.py` saves the lot under the user's active shift (`turno`)
5. Bot replies with confirmation

### Module responsibilities

- **`bot.py`** — All Telegram command handlers + APScheduler setup. The `post_init()` function registers automatic shift reports at 05:00, 15:00, and 22:00 (Guatemala time).
- **`database.py`** — SQLite wrapper. Each user has one active `turno` (shift). Lots belong to a turno. `cerrar_turno()` closes the current shift; the next `guardar_lote()` call opens a new one automatically.
- **`lote_parser.py`** — Claude Haiku extracts raw fields from free text; `humanizar()` maps them to canonical values and calculates box counts using hardcoded tables. Mespack 3 and Chub always use pin grande (`g`).
- **`recordatorio.py`** — APScheduler interval jobs, one per user, identified by `alerta_transito_{user_id}`.
- **`reporter.py`** — Text summary generation and two-sheet Excel export (Detail + Summary). Emojis are stripped from product/market fields in Excel output.

### Database schema

Two tables: `turnos` (one per shift, `abierto=1` while active) and `lotes` (many per turno). Queries use `SUBSTR(timestamp, 1, 10)` for date filtering — timestamps are stored as ISO 8601 strings in Guatemala local time.

### Box calculation logic

`calcular_cajas(producto, presentacion, pin)` in `lote_parser.py` — first checks `TABLA_ENTEROS` (for NE/RE products), then falls back to `TABLA`. If no match, returns `None` and the lot is rejected. The allowed combinations are the only valid pairings for this plant's production lines.

## Deployment

Deployed on **Render** as a worker service (not a web service). Config in `render.yaml`. Python 3.11.
