"""AC Stock Tracker backend — FastAPI + Excel store + vision scan engine.

Serves the single-file frontend (ac-stock-tracker.html) and the REST API
it calls. Run:

    pip install -r requirements.txt
    python backend.py        # opens http://localhost:8000
"""

import datetime
import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # embeddable Python lacks script dir

from fastapi import FastAPI, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import scanner
from stock_store import StockStore

BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
HTML_PATH = BASE / "ac-stock-tracker.html"
if not HTML_PATH.exists():
    HTML_PATH = Path(__file__).parent / "ac-stock-tracker.html"

app = FastAPI(title="AC Stock Tracker")
store = StockStore()
_mtime = store.path.stat().st_mtime if store.path.exists() else 0


def _fresh():
    """Reload the workbook if it changed on disk (e.g. edited in Excel)."""
    global _mtime
    try:
        m = store.path.stat().st_mtime
    except FileNotFoundError:
        return
    if m != _mtime:
        store.load()
        _mtime = store.path.stat().st_mtime


def _saved():
    global _mtime
    try:
        _mtime = store.path.stat().st_mtime
    except FileNotFoundError:
        pass


@app.get("/")
def index():
    return FileResponse(HTML_PATH)


@app.get("/ac-stock-tracker.html")
def index_alias():
    return FileResponse(HTML_PATH)


@app.post("/api/reload")
def reload():
    store.load()
    _saved()
    return {"ok": True}


# ------------------------------------------------------------------ scan
@app.post("/api/scan")
def scan(file: UploadFile):
    logs: list[str] = []
    try:
        img = file.file.read()
        rows = scanner.scan_image(img, file.filename or "photo.jpg",
                                  logs.append)
        return {"ok": True, "rows": rows, "log": logs}
    except Exception as ex:
        logs.append(f"Scan failed: {ex}")
        return JSONResponse({"ok": False, "log": logs, "error": str(ex)},
                            status_code=200)


# ------------------------------------------------------------------ reads
@app.get("/api/inventory")
def inventory():
    _fresh()
    return {"inventory": store.inventory(), "brands": store.brands}


@app.get("/api/track")
def track():
    _fresh()
    return {"records": store.track_list()}


@app.get("/api/returns")
def returns():
    _fresh()
    return {"records": store.returns_list()}


@app.get("/api/brands")
def brands():
    _fresh()
    return {"brands": store.brands}


@app.get("/api/customers")
def customers():
    _fresh()
    return {"customers": store.customer_history()}


# ------------------------------------------------------------------ activity & backups
@app.get("/api/activity")
def get_activity(limit: int = 100):
    _fresh()
    return {"records": store.activity_log(limit=limit)}


@app.get("/api/backups")
def get_backups():
    return {"backups": store.list_backups()}


@app.post("/api/backup")
def trigger_backup():
    _fresh()
    bk = store.manual_backup()
    _saved()
    return {"ok": True, "backup": bk}


class RestoreBody(BaseModel):
    filename: str


@app.post("/api/restore")
def restore_backup(body: RestoreBody):
    ok, err = store.restore_backup(body.filename)
    if not ok:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    _saved()
    return {"ok": True}


@app.get("/api/backup/download")
def download_backup(filename: str = ""):
    if filename:
        from stock_store import BACKUP_DIR
        safe_name = Path(filename).name
        target = BACKUP_DIR / safe_name
        if not target.exists():
            return JSONResponse({"ok": False, "error": "File not found"}, status_code=404)
        return FileResponse(target, filename=safe_name, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    _fresh()
    if not store.path.exists():
        return JSONResponse({"ok": False, "error": "Workbook does not exist yet"}, status_code=404)
    return FileResponse(store.path, filename="daikin_stock.xlsx", media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ------------------------------------------------------------------ stock
class StockInBody(BaseModel):
    supplier: str
    model: str
    serials: list[str]
    date_in: str
    brand: str = ""


@app.post("/api/stock-in")
def stock_in(body: StockInBody):
    _fresh()
    serials = [s.strip().upper() for s in body.serials if s.strip()]
    if body.brand.strip():
        added, dupes = store.stock_in_with_brand(
            body.supplier, body.model, serials, body.date_in, body.brand)
        needs_brand = False
    else:
        added, dupes, ok = store.stock_in(
            body.supplier, body.model, serials, body.date_in)
        needs_brand = not ok
    _saved()
    return {"added": added, "dupes": dupes, "needs_brand": needs_brand}


class AssignBrandBody(BaseModel):
    model: str
    brand: str


@app.post("/api/models/brand")
def assign_brand(body: AssignBrandBody):
    store.assign_brand(body.model, body.brand.strip())
    _saved()
    return {"ok": True, "brands": store.brands}


class StockOutBody(BaseModel):
    serials: list[str]
    customer: str
    date_out: str


@app.post("/api/stock-out")
def stock_out(body: StockOutBody):
    _fresh()
    done = store.stock_out(body.serials, body.customer, body.date_out)
    _saved()
    return {"sold": done}


class ReturnBody(BaseModel):
    serial: str
    reason: str = ""
    condition: str = ""
    notes: str = ""
    action: str = "quarantine"  # 'restock' | 'quarantine'


@app.post("/api/returns")
def create_return(body: ReturnBody):
    _fresh()
    today = datetime.date.today().strftime("%m/%d/%Y")
    rec, err = store.create_return(body.serial, body.reason, body.condition,
                                   body.notes, body.action, today)
    _saved()
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=404)
    return {"ok": True}


# ------------------------------------------------------------------ config
def _config_payload() -> dict:
    providers = scanner.load_providers()
    active = scanner.load_active(providers)
    return {
        "active": active,
        "providers": {
            pid: {
                "label": scanner.PROVIDERS[pid]["label"],
                "models": scanner.PROVIDERS[pid]["models"],
                "model": cfg["model"],
                "keys": [{"masked": scanner.mask_key(k["key"]),
                          "remark": k.get("remark", ""),
                          "status": "ACTIVE"}
                         for k in cfg["keys"]],
            }
            for pid, cfg in providers.items()
        },
    }


@app.get("/api/config")
def get_config():
    return _config_payload()


@app.get("/api/config/models")
def list_models(provider: str):
    """Live model list for a provider (fetches via its API, else static)."""
    return {"models": scanner.list_models(provider)}


class ConfigBody(BaseModel):
    provider: str
    model: str


@app.post("/api/config")
def set_config(body: ConfigBody):
    providers = scanner.load_providers()
    if body.provider not in providers:
        return JSONResponse({"ok": False, "error": "unknown provider"},
                            status_code=400)
    providers[body.provider]["model"] = body.model
    scanner.save_providers(providers,
                           {"provider": body.provider, "model": body.model})
    return {"ok": True}


class KeyBody(BaseModel):
    provider: str
    key: str
    remark: str = ""


@app.post("/api/config/keys")
def add_key(body: KeyBody):
    providers = scanner.load_providers()
    if body.provider not in providers:
        return JSONResponse({"ok": False, "error": "unknown provider"},
                            status_code=400)
    keys = providers[body.provider]["keys"]
    if not any(k["key"] == body.key for k in keys):
        keys.append({"key": body.key.strip(), "remark": body.remark.strip()})
        scanner.save_providers(providers)
    return {"ok": True, "provider": _provider_payload(body.provider)}


def _provider_payload(pid: str) -> dict:
    cfg = scanner.load_providers()[pid]
    return {"id": pid, "label": scanner.PROVIDERS[pid]["label"],
            "models": scanner.PROVIDERS[pid]["models"],
            "model": cfg["model"],
            "keys": [{"masked": scanner.mask_key(k["key"]),
                      "remark": k.get("remark", ""), "status": "ACTIVE"}
                     for k in cfg["keys"]]}


class KeyReorderBody(BaseModel):
    provider: str
    order: list[int]


@app.post("/api/config/keys/reorder")
def reorder_keys(body: KeyReorderBody):
    providers = scanner.load_providers()
    if body.provider not in providers:
        return JSONResponse({"ok": False, "error": "unknown provider"},
                            status_code=400)
    keys = providers[body.provider]["keys"]
    if sorted(body.order) != list(range(len(keys))):
        return JSONResponse({"ok": False, "error": "order must be a "
                             "permutation of all key indices"},
                            status_code=400)
    providers[body.provider]["keys"] = [keys[i] for i in body.order]
    scanner.save_providers(providers)
    return {"ok": True, "provider": _provider_payload(body.provider)}


@app.delete("/api/config/keys")
def remove_key(provider: str, index: int):
    providers = scanner.load_providers()
    if provider not in providers:
        return JSONResponse({"ok": False, "error": "unknown provider"},
                            status_code=400)
    keys = providers[provider]["keys"]
    if 0 <= index < len(keys):
        keys.pop(index)
        scanner.save_providers(providers)
    return {"ok": True, "provider": _provider_payload(provider)}


# ------------------------------------------------------------------ run
if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    import uvicorn

    threading.Timer(1.0,
                    lambda: webbrowser.open("http://localhost:8000")).start()
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
