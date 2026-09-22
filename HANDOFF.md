# HANDOFF — AC Stock Tracker (Daikin)

Everything a new AI/developer needs to continue this project. Read this
whole file before changing code — it encodes decisions made with the
owner over multiple sessions.

## 1. What this app is

Daikin AC parts-list photo → AI vision scan → Excel-backed stock system.
Flow: **Stock In (scan) → Available Stock → Stock Out → Track List →
Return (RMA)**. Single-file HTML frontend + FastAPI backend; one process
serves both at `http://localhost:8000`.

- Repo: `github.com/Ai-mali/Stock-App`, work directly on `main`.
- Owner runs: `D:\AI-Project\Stock New UI\Stock-App` on Windows, uses
  `git-pull.bat` + `run.bat` (both in repo).
- **Deliver commits to `main` — the owner git-pulls and tests himself.**
- Final packaging goal: a Python `.exe` (PyInstaller) — deferred to the
  very end on purpose.

## 2. File map

| File | Role |
|---|---|
| `ac-stock-tracker.html` | The ENTIRE frontend: ~3700 lines, vanilla HTML/CSS/JS, no frameworks. All screens, modals, and logic live here. |
| `backend.py` | FastAPI app: serves the HTML at `/` + `/ac-stock-tracker.html`, plus all `/api/*` endpoints. Auto-opens the browser. |
| `scanner.py` | Vision engine: `PROVIDERS` dict (gemini/alibaba/openai/anthropic/deepseek), `PROMPT` sent to the vision model, `parse_scan_json`, per-provider scanners with **multi-key failover**, `list_models` (live model fetch with static fallback). |
| `stock_store.py` | `StockStore` — openpyxl layer over `daikin_stock.xlsx`. Sheets: `MasterRecord` `Supplier\|Brand\|Model\|Serial\|Date In\|Status\|Customer\|Date Out` (one row per serial), `Brands`, `ModelBrands` (exact Model→Brand), `Returns` `Serial\|Model\|Customer\|Reason\|Condition\|Notes\|Action\|Date`, and `ActivityLog` `Timestamp\|Action\|Model\|Serials Count\|Details\|Status`. Automatic rotating backups in `backups/`, customer history ranking, and warranty tracking. |
| `requirements.txt` | `fastapi, uvicorn, python-multipart, google-genai, openpyxl` |
| `git-pull.bat`, `run.bat` | Owner's helpers (`git pull` / `python backend.py`). |
| `extractor_flowchart.html` | Old flowchart doc — informational only. |

## 3. NOT in git (must know)

- `daikin_stock.xlsx` — **gitignored on purpose** (real business data). A
  fresh clone starts with an empty workbook; auto-created on first run.
- `~/.model_serial_extractor.json` — scan engine config + ALL API keys,
  stored per-user, shared with the old desktop app. Shape:
  `{"providers": {pid: {"keys": [{"key","remark"}], "model"}},
   "active": {"provider","model"}}`. NEVER commit keys or this file.

## 4. Run / develop

```
pip install -r requirements.txt
python backend.py          # uvicorn on 127.0.0.1:8000 + opens browser
```

Windows note: the owner's Python is an embeddable distro — `backend.py`
starts with `sys.path.insert(0, script_dir)`; keep that line.

**JS check before committing** `ac-stock-tracker.html` edits (there is no
linter): extract `<script>` blocks to a temp file, run `node --check` on
it. Also `python -c "import ast; ast.parse(open('scanner.py').read())"`.

## 5. Business rules & conventions (decided with owner — do not regress)

### Scan rows
Row shape: `{no, model, qty, serial, serials[], desc, flag, sev, warn}`.
- Each `Serial No:` block belongs ONLY to its row — never merge serials
  across rows. Model-less blocks keep `model=""` (UI shows `—`).
- Dash ranges (`K361 - K368`) expand ONLY when the expanded count equals
  declared Qty; if splitting dashes as separators matches Qty instead,
  use that (flag `qty_fixed`); else expand + `qty_mismatch`.
- Tokens without digits are description text, not serials — parse moves
  them to `desc` (catches models reading "DAIKIN REFNET JOINT" as serials).

### Warnings (`flag` code → `sev` + `warn` human message)
- `no_model` — **red** (`sev:"error"`): model is REQUIRED to commit.
- `qty_mismatch` — **red**: "N serials captured but Qty declares M…".
- `qty_fixed` — **yellow** (`sev:"warn"`): verify dash interpretation.
- `no_serial` — **yellow**: description shown instead; still usable.
- UI: warning ⚠ icon sits in the actions column BEFORE the zoom icon;
  whole row tinted red/yellow. Editing + saving a row CLEARS warnings
  and re-flags only if the problem still exists.

### Desc-only rows are committable
`commitSerialsOf(row)` in the frontend: real serials if present, else the
description as the unit ID — expanded `desc #1..#N` when Qty > 1 so each
unit is counted and dedup still works. Rows with neither serials nor
desc, or no model, are skipped/blocked.

### Commit flow
- Per-row ▶ commits individually; "Commit All" bulk button exists too.
- `/api/stock-in` returns `needs_brand` when the model is unmapped →
  frontend `askBrand()` modal → `POST /api/models/brand` → retry.
- Duplicate serials are skipped server-side (case-insensitive `_serial_set`).

### API keys
- Order in the config table IS priority: index 0 = PRIMARY, rest FAILOVER;
  scanner tries top→down on quota/5xx errors. UI rows are drag-to-reorder
  (`POST /api/config/keys/reorder` accepts a permutation of indices).

### UI specifics
- Serial cell scrolls internally (`max-height ~82px`) so 100+ serial rows
  stay compact; buttons pinned in a fixed-width `.actions-cell`.
- Zoom 🔍 button = read-only modal showing No|Model|Qty + all serial chips.
- Pencil ✏️ = Edit Scanned Item modal: No/Model/Qty/Serials, live serial
  count, auto-format, delete row. Esc + backdrop close for all modals.
- Stitch-designed CSS — match existing classes/variables (`--green-bright`,
  `--surface2`, `btn-action-icon`, `scan-serial-badge`, …), dark + light
  themes (`body.theme-light` overrides).

## 6. Roadmap status & features implemented

1. Serial-less parts policy — partly done (desc commits); confirm desired
   stock-out UX for desc-identified units.
2. [DONE] **Backup + activity log for daikin_stock.xlsx**:
   - Automatic rotating backups saved to `backups/daikin_stock_YYYYMMDD_HHMMSS.xlsx` before every mutation (keeps latest 30 snapshots, gitignored).
   - Dedicated `ActivityLog` sheet inside `daikin_stock.xlsx` documenting Timestamp, Action, Model, Count, Details, and Status.
   - UI Activity Log & Backups modal with live history search, instant manual backup trigger, active file download, and one-click restore.
3. [DONE] **Customer autocomplete in Stock Out**:
   - History query ranking customers by order frequency and recency.
   - Stitch-styled autocomplete dropdown supporting keyboard navigation (↑/↓/Enter/Esc).
   - Quick-select "Recent Customers" chip bar for 1-click selection.
   - Dynamic tag indicating "Known Customer" vs "✦ New Customer".
4. [DONE] **Low-stock alert (≤2 units per model)**:
   - Amber alert banner at the top of Available Stock highlighting low-stock models across all brands, with 1-click filter toggle (`⚡ View Low Stock Only`).
   - Warning badge on model rows: `⚠️ Low Stock (N)` when ≤2 units, `Out of Stock (0)` when 0 units.
   - Warning badges also displayed in Stock Out view.
5. [DONE] **Warranty tracking**:
   - Auto-computed from `Date Out` + duration (standard 12 months default).
   - Track List displays `Active (Xd left)`, `Expiring Soon (Xd left)`, or `Expired` badges.
   - Warranty filter pills in Track List (`All Warranty`, `🛡️ Active`, `⚠️ Expiring Soon`, `❌ Expired`) and CSV export inclusion.
   - Return (RMA) screen shows live warranty status banner for selected serial.
6. **Final step: PyInstaller `.exe` packaging** — deferred until all upgrades and new features are finished as requested by the owner. Keep repo and folder clean until that final step.


Owner instruction that still applies: *"the most important is you have
to make the improvement on my idea — do not agree with me everytime,
and suggest me good ideas."* Push back constructively.

## 7. Workflow with the owner

- Commit to `main`, tell him to `git pull` (or run `git-pull.bat`).
- He tests on his own machine and reports screenshots — diagnose from
  those; root-cause fixes, not workarounds.
- He sometimes sends `CHANGES.md` / HTML snippets from Google Stitch —
  apply them while keeping the backend wiring intact.
- Khmer phrases appear occasionally; e.g. "ថោរ​មេ" = "for me".
