# AC Stock Tracker

Photo-scan parts lists (vision AI) into an Excel-backed stock system:
Stock In → Available Stock → Stock Out → Track List → Return.

## Run

```
pip install -r requirements.txt
python backend.py
```

Then open http://localhost:8000 — the backend serves the UI and the REST API
from one process, so nothing else to start.

## Architecture

- `ac-stock-tracker.html` — single-file vanilla HTML/CSS/JS frontend (all 5 screens + config modal).
- `backend.py` — FastAPI REST server. Also serves the HTML page.
- `scanner.py` — vision scan engine (Gemini / Alibaba Qwen-VL / OpenAI / Anthropic / DeepSeek) with per-provider key lists and automatic failover.
- `stock_store.py` — Excel data layer. `daikin_stock.xlsx` (gitignored) holds MasterRecord, Brands, ModelBrands, Returns. The old Type column is migrated to Brand automatically on first run.
- `~/.model_serial_extractor.json` — API keys + active scan engine config (shared with the old desktop app).

## REST endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/inventory` | in-stock units grouped Brand → Model → serials |
| GET | `/api/track` | sold units (Track List) |
| GET | `/api/returns` | return records |
| GET | `/api/brands` | brand list |
| POST | `/api/scan` | multipart image → parsed rows `{no, model, qty, serials[], desc, flag}` |
| POST | `/api/stock-in` | `{supplier, model, serials, date_in, brand?}` → `{added, dupes, needs_brand}` |
| POST | `/api/models/brand` | assign a Brand to a new Model (exact match, remembered) |
| POST | `/api/stock-out` | `{serials, customer, date_out}` |
| POST | `/api/returns` | `{serial, reason, condition, notes, action: restock\|quarantine}` |
| GET/POST | `/api/config` | scan engine: active provider + model |
| POST/DELETE | `/api/config/keys` | add/remove API keys |
