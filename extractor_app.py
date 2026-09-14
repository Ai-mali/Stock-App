"""Model & Serial Extractor — Daikin delivery-list photo scanner.

Flet desktop app. Load a photo of a Daikin parts/delivery list, send it to
Gemini vision, and get back a Model | Serial table ready for Stock In.

Run:  python extractor_app.py
Build: pyinstaller --noconsole --onefile --name ModelSerialExtractor extractor_app.py
"""

import asyncio
import csv
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

import flet as ft

CONFIG_PATH = Path.home() / ".model_serial_extractor.json"

# Real Stock In save path — the same store the manual form uses
# (duplicate-serial check + new-Type assignment + Excel write).
sys.path.insert(0, str(Path(__file__).parent))
try:
    from stock_store import StockStore
    stock_store = StockStore()
    STOCK_ERR = ""
except Exception as _ex:  # surfaced in the Status Log
    stock_store = None
    STOCK_ERR = str(_ex)

# Supported scan providers. Each holds its own key list and picked model.
PROVIDERS = {
    "gemini": {
        "label": "Gemini",
        "models": ["gemini-flash-latest", "gemini-2.5-flash",
                   "gemini-2.0-flash", "gemini-3.5-flash"],
        "default": "gemini-flash-latest",
        "url": None,  # uses the google-genai SDK
    },
    "alibaba": {
        "label": "Alibaba (Qwen-VL)",
        "models": ["qwen-vl-max", "qwen-vl-plus"],
        "default": "qwen-vl-max",
        "url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/"
               "chat/completions",
    },
    "openai": {
        "label": "ChatGPT",
        "models": ["gpt-4o-mini", "gpt-4o"],
        "default": "gpt-4o-mini",
        "url": "https://api.openai.com/v1/chat/completions",
    },
    "anthropic": {
        "label": "Claude",
        "models": ["claude-sonnet-4-5", "claude-haiku-4-5"],
        "default": "claude-sonnet-4-5",
        "url": "https://api.anthropic.com/v1/messages",
    },
}

PROMPT = """Extract model number and serial number from this Daikin equipment parts list.
No = the line item number in the leftmost No column.
Model = equipment model code from the Material column.
Serial = serial number after Serial No:.
If multiple serial numbers for same model, separate with comma.
Keep serial ranges as written, e.g. "K016634 - K016636".
Desc = the description text of the line item.
Qty = quantity number from the Quantity column.
Ignore handwritten checkmarks next to serials.
If a row's Material/model is cut off or belongs to a previous page, still
return the serials with model left empty.
Return ONLY JSON array, one object per row including rows without serials:
[{"no":"","model":"","serial":"","qty":"","desc":""}]
Leave serial empty when the row has no serial number.
"""

# ---------------------------------------------------------------- palette
TEAL = "#0F8A70"
TEAL_BG = "#E1F5EE"
INK = "#1C2024"
INK_SOFT = "#5B6470"
LINE = "#DFE3E7"
BG = "#F6F7F8"
CARD = "#FFFFFF"
ENGINE_GRAY = "#D9D9D9"
ENGINE_GRAY_ACTIVE = "#B9B9B9"


_RANGE_RE = re.compile(
    r"([A-Za-z]{0,4})(\d{3,})\s*[-–]\s*([A-Za-z]{0,4})(\d{3,})")


def expand_serial_range(text: str) -> str:
    """Expand inclusive ranges like 'K016634 - K016636' into individual serials.

    Applies to same-prefix ranges only; anything else is returned unchanged.
    """
    def _sub(m):
        pa, na, pb, nb = m.group(1), m.group(2), m.group(3), m.group(4)
        if pa != pb or len(na) != len(nb):
            return m.group(0)
        start, end = int(na), int(nb)
        if end < start or end - start > 500:
            return m.group(0)
        return ", ".join(f"{pa}{i:0{len(na)}d}" for i in range(start, end + 1))

    return _RANGE_RE.sub(_sub, text)


def _split_ranges(text: str) -> str:
    """Treat dashes as separators — 'A123 - A125' becomes 'A123, A125'."""
    return _RANGE_RE.sub(r"\1\2, \3\4", text)


def _serial_count(serial: str) -> int:
    return len([s for s in serial.split(",") if s.strip()])


def _qty_int(qty: str):
    try:
        return int(re.sub(r"[^0-9]", "", qty) or 0)
    except ValueError:
        return 0


def parse_gemini_json(raw: str) -> list[dict]:
    """Pull the JSON array out of a Gemini response (handles markdown fences)."""
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("no JSON array found in response")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, list):
        raise ValueError("response is not a JSON array")
    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model = str(item.get("model", "")).strip()
        raw_serial = str(item.get("serial", "")).strip()
        desc = str(item.get("desc", "")).strip()
        no = str(item.get("no", "")).strip()
        qty = str(item.get("qty", "")).strip()
        # keep rows that have a model OR serials — a model cut off from a
        # previous page still gets its serials, flagged for review
        if not (model or raw_serial or desc):
            continue
        serial, flag = _resolve_serials(raw_serial, qty)
        if not model:
            flag = flag or "no_model"
        rows.append({"no": no, "model": model.upper(), "qty": qty,
                     "serial": serial.upper(), "desc": desc,
                     "flag": flag or ""})
    return rows


def _resolve_serials(raw_serial: str, qty: str) -> tuple:
    """Pick the serial interpretation whose count matches Qty, else flag.

    A dash inside a serial list is ambiguous: a real range (K361-K368) or a
    separator the model read as a dash (K308 - K323 meaning two units).
    Expand it only when the expanded count equals the printed quantity.
    Returns (serial_text, flag).
    """
    if not raw_serial:
        return "", ""
    q = _qty_int(qty)
    expanded = expand_serial_range(raw_serial)
    split = _split_ranges(raw_serial) if _RANGE_RE.search(raw_serial) else None
    if not q:
        return expanded, ""
    if _serial_count(expanded) == q:
        return expanded, ""
    if split and _serial_count(split) == q:
        # dash was really a separator
        return split, "qty_fixed"
    # neither matches the printed quantity — keep expanded but flag for review
    return expanded, "qty_mismatch"


def load_providers() -> dict:
    """Return {provider: {"keys": [...], "model": str}}; migrates old formats."""
    out = {p: {"keys": [], "model": PROVIDERS[p]["default"]}
           for p in PROVIDERS}
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except Exception:
        return out
    saved = data.get("providers")
    if isinstance(saved, dict):
        for p, cfg in saved.items():
            if p in out and isinstance(cfg, dict):
                if isinstance(cfg.get("keys"), list):
                    out[p]["keys"] = [k for k in cfg["keys"] if k]
                if cfg.get("model"):
                    out[p]["model"] = cfg["model"]
    # migrate legacy key fields into the gemini/alibaba slots
    for eng in ("gemini", "alibaba"):
        legacy = data.get(f"{eng}_keys")
        if isinstance(legacy, list):
            out[eng]["keys"] += [k for k in legacy if k]
    flat = data.get("api_keys")
    if isinstance(flat, list):
        out["gemini"]["keys"] += [k for k in flat if k]
    old = data.get("api_key", "")
    if old:
        out["gemini"]["keys"].append(old)
    for p in out:
        # keys are {"key": str, "remark": str} dicts; migrate plain strings
        seen = []
        for k in out[p]["keys"]:
            seen.append(k if isinstance(k, dict) else {"key": k, "remark": ""})
        dedup = {}
        for entry in seen:
            if entry.get("key"):
                dedup[entry["key"]] = entry
        out[p]["keys"] = list(dedup.values())
    return out


def save_providers(providers: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps({"providers": providers}))
    except Exception:
        pass


def card(content, padding=18, expand=None):
    return ft.Container(
        content=content,
        bgcolor=CARD,
        border=ft.Border.all(1, LINE),
        border_radius=10,
        padding=padding,
        expand=expand,
    )


def main(page: ft.Page):
    page.title = "Model & Serial Extractor"
    page.bgcolor = BG
    page.padding = 20
    page.theme = ft.Theme(font_family="Segoe UI")
    if page.window:
        page.window.width = 1040
        page.window.height = 720
        page.window.min_width = 720
        page.window.min_height = 560

    # ------------------------------------------------------------- state
    state = {
        "providers": load_providers(),  # {name: {"keys": [...], "model": str}}
        "engine": None,          # None | provider key
        "key_engine": "gemini",  # provider currently shown in the manager
        "image_path": None,
        "busy": False,
        "rows": [],
        "added": set(),  # row indexes already sent to Stock In
    }

    # ------------------------------------------------------------- widgets
    img_preview = ft.Image(
        src="",
        fit=ft.BoxFit.CONTAIN,
        border_radius=8,
        visible=False,
    )
    img_empty = ft.Container(
        content=ft.Column(
            [
                ft.Icon(ft.Icons.IMAGE_OUTLINED, size=44, color=INK_SOFT),
                ft.Text("Load a delivery-list photo to preview it here",
                        color=INK_SOFT, size=13),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=8,
        ),
        height=260,
        border_radius=8,
        border=ft.Border.all(1, LINE),
        bgcolor="#FAFBFC",
        alignment=ft.Alignment(0, 0),
    )
    # ---- Telegram-style zoom overlay: drag to pan, double-click or
    # +/- buttons to zoom, X or background click to close.
    zoom_state = {"scale": 1.0, "dx": 0.0, "dy": 0.0}
    zoom_img = ft.Image(src="", fit=ft.BoxFit.CONTAIN, expand=True)

    def _apply_zoom():
        # ft.Offset units are fractions of the control size, so pixel drag
        # distances are converted before being applied.
        w = float(page.width or 900)
        h = float(page.height or 600)
        limit = max(0.0, zoom_state["scale"] - 1.0) + 0.25
        fx = max(-limit, min(limit, zoom_state["dx"] / w))
        fy = max(-limit, min(limit, zoom_state["dy"] / h))
        zoom_img.scale = ft.Scale(zoom_state["scale"])
        zoom_img.offset = ft.Offset(fx, fy)
        page.update()

    def _pan_start(e):
        zoom_state["lx"] = zoom_state["ly"] = 0.0

    def _pan(e):
        # Flet 0.86 reports movement cumulative since drag start, so diff
        # against the previous value to get this event's delta in pixels.
        d = e.global_delta or e.local_delta
        if not d:
            return
        zoom_state["dx"] += d.x - zoom_state.get("lx", 0.0)
        zoom_state["dy"] += d.y - zoom_state.get("ly", 0.0)
        zoom_state["lx"], zoom_state["ly"] = d.x, d.y
        _apply_zoom()

    def _zoom_by(factor):
        zoom_state["scale"] = min(6.0, max(0.5, zoom_state["scale"] * factor))
        _apply_zoom()

    def _dbl(e):
        # double-click toggles between fit and 2x zoom
        if zoom_state["scale"] > 1.01:
            zoom_state.update(scale=1.0, dx=0.0, dy=0.0)
        else:
            zoom_state["scale"] = 2.0
        _apply_zoom()

    def close_zoom(e=None):
        zoom_overlay.visible = False
        page.update()

    zoom_overlay = ft.Container(
        visible=False,
        expand=True,
        bgcolor="#000000CC",
        content=ft.Stack([
            ft.GestureDetector(
                content=ft.Container(zoom_img, expand=True,
                                     alignment=ft.Alignment(0, 0),
                                     clip_behavior=ft.ClipBehavior.HARD_EDGE),
                on_pan_start=_pan_start,
                on_pan_update=_pan,
                on_double_tap=_dbl,
            ),
            ft.Container(
                ft.IconButton(ft.Icons.CLOSE, icon_size=20, icon_color="white",
                              bgcolor="#00000066", on_click=close_zoom,
                              tooltip="Close"),
                right=12, top=12,
            ),
            ft.Container(
                ft.Row([
                    ft.IconButton(ft.Icons.REMOVE, icon_color="white",
                                  bgcolor="#00000066",
                                  on_click=lambda e: _zoom_by(0.75)),
                    ft.IconButton(ft.Icons.ADD, icon_color="white",
                                  bgcolor="#00000066",
                                  on_click=lambda e: _zoom_by(1.33)),
                    ft.IconButton(ft.Icons.FIT_SCREEN, icon_color="white",
                                  bgcolor="#00000066", tooltip="Fit",
                                  on_click=lambda e: (
                                      zoom_state.update(scale=1.0, dx=0.0, dy=0.0),
                                      _apply_zoom())),
                ], spacing=4),
                bottom=16, alignment=ft.Alignment(0, 1),
            ),
        ]),
    )

    def open_zoom(e):
        if not state["image_path"]:
            return
        # Separate OS window (Telegram-style): movable, resizable, free pan.
        viewer = Path(__file__).with_name("image_viewer.py")
        if viewer.exists():
            subprocess.Popen([sys.executable, str(viewer),
                              state["image_path"]])
        else:  # fallback: in-app overlay
            zoom_state.update(scale=1.0, dx=0.0, dy=0.0)
            zoom_img.src = state["image_path"]
            zoom_img.scale = ft.Scale(1.0)
            zoom_img.offset = ft.Offset(0, 0)
            zoom_overlay.visible = True
            page.update()

    img_frame = ft.Container(
        content=ft.Stack([img_empty, img_preview]),
        height=260, border_radius=8,
        on_click=open_zoom,
        tooltip="Click to zoom",
    )

    # ---- batch-level Stock In details: one Supplier + one Date In shared
    # by every scanned row, same rule as the manual Stock In form.
    supplier_field = ft.TextField(
        label="SUPPLIER", hint_text="e.g. Daikin distributor", dense=True,
        expand=3)
    date_in_field = ft.TextField(
        label="DATE IN", hint_text="mm/dd/yyyy",
        value=datetime.date.today().strftime("%m/%d/%Y"),
        dense=True, read_only=True, expand=2)
    date_picker = ft.DatePicker(value=datetime.date.today())
    page.overlay.append(date_picker)

    def _on_date_picked(e):
        if date_picker.value:
            d = date_picker.value
            date_in_field.value = f"{d.month:02d}/{d.day:02d}/{d.year}"
            date_in_field.update()

    date_picker.on_change = _on_date_picked

    # Custom table: proportional columns (flex weights) so the grid stays
    # evenly spaced at any window size, with a frozen header and scrolling body.
    FLEX = {"no": 1, "model": 3, "serial": 6, "qty": 2}
    EDIT_W = 76  # pencil + send arrow

    def _head(text, flex=None, center=True):
        return ft.Container(
            ft.Text(text, weight=ft.FontWeight.W_500, color=INK_SOFT, size=12,
                    text_align=ft.TextAlign.CENTER if center else ft.TextAlign.LEFT,
                    no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
            width=EDIT_W if flex == "fixed" else None,
            expand=flex if isinstance(flex, int) else None,
            alignment=ft.Alignment(0 if center else -1, 0),
            padding=ft.Padding(8, 8, 8, 8),
        )

    head_no = _head("No", FLEX["no"])
    head_model = _head("Model", FLEX["model"])
    head_serial = _head("Serial", FLEX["serial"])
    head_qty = _head("Qty", FLEX["qty"])
    head_edit = _head("", "fixed")

    table_header = ft.Container(
        ft.Row([head_no, head_model, head_serial, head_qty, head_edit],
               spacing=0),
        bgcolor="#F5F7F8",
        border=ft.Border.only(bottom=ft.BorderSide(1, LINE)),
    )
    results_body = ft.ListView(spacing=0, expand=True)
    results_scroll = ft.Container(
        ft.Column([table_header, results_body], spacing=0, expand=True),
        border=ft.Border.all(1, LINE),
        border_radius=8,
        expand=True,
    )

    log_list = ft.ListView(spacing=2, expand=True, auto_scroll=True)

    # ------------------------------------------------------------- helpers
    def log(msg: str, ok: bool = False):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        log_list.controls.append(
            ft.Text(f"[{ts}] {msg}", size=12.5,
                    color=TEAL if ok else INK, font_family="Consolas"))
        page.update()

    def set_results(rows: list[dict]):
        state["rows"] = rows

        def cell(text, flex, center=True, **kw):
            return ft.Container(
                ft.Text(text, size=13,
                        text_align=ft.TextAlign.CENTER if center else ft.TextAlign.LEFT,
                        **kw),
                expand=flex,
                alignment=ft.Alignment(0 if center else -1, 0),
                padding=ft.Padding(8, 8, 8, 8),
            )

        def qty_text(q):
            if not q:
                return ""
            return f"{q} PC" if q == "1" else f"{q} PCS"

        FLAG_TIPS = {
            "qty_mismatch": "Serial count doesn't match Qty — check this row",
            "qty_fixed": "Dash was read as separate serials to match Qty — "
                         "double-check",
            "no_model": "No model on this line (may be cut off from the "
                        "previous page)",
        }
        result_rows = []
        for i, r in enumerate(rows):
            flagged = bool(r.get("flag"))
            added = i in state["added"]
            sendable = bool(r["model"] and r["serial"])
            result_rows.append(ft.Container(
                ft.Row([
                    cell(r["no"], FLEX["no"], color=INK_SOFT,
                         no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Container(
                        ft.Row([
                            ft.Icon(ft.Icons.WARNING_AMBER, size=14,
                                    color="#D97706",
                                    tooltip=FLAG_TIPS.get(r["flag"], ""))
                            if flagged else ft.Container(width=0),
                            ft.Text(r["model"], size=13, no_wrap=True,
                                    overflow=ft.TextOverflow.ELLIPSIS),
                        ], spacing=4, tight=True),
                        expand=FLEX["model"], alignment=ft.Alignment(0, 0),
                        padding=ft.Padding(8, 8, 8, 8),
                    ),
                    cell(r["serial"] or r["desc"], FLEX["serial"],
                         italic=not r["serial"],
                         color=INK if r["serial"] else INK_SOFT),
                    cell(qty_text(r.get("qty", "")), FLEX["qty"], no_wrap=True),
                    ft.Container(
                        ft.Row([
                            ft.IconButton(ft.Icons.EDIT_OUTLINED, icon_size=16,
                                          tooltip="Edit row",
                                          icon_color=INK_SOFT,
                                          on_click=lambda e, i=i: open_edit(i)),
                            ft.Icon(ft.Icons.CHECK_CIRCLE, size=18,
                                    color=TEAL, tooltip="Added to stock")
                            if added else
                            ft.IconButton(ft.Icons.SEND, icon_size=16,
                                          tooltip="Add this row to Stock In"
                                          if sendable else
                                          "Needs a Model and at least one "
                                          "Serial before it can be added",
                                          icon_color=TEAL if sendable
                                          else "#B9BEC5",
                                          disabled=not sendable,
                                          on_click=(
                                              lambda e, i=i: send_row(i))),
                        ], spacing=0, tight=True,
                            alignment=ft.MainAxisAlignment.CENTER),
                        width=EDIT_W, alignment=ft.Alignment(0, 0),
                    ),
                ], spacing=0),
                bgcolor="#E6F4EC" if added else
                        "#FDECEA" if flagged else
                        ("#FFFFFF" if i % 2 == 0 else "#FAFBFC"),
                border=ft.Border.only(bottom=ft.BorderSide(1, "#EEF1F3")),
                opacity=0.6 if added else 1.0,
            ))
        results_body.controls = result_rows
        serial_count = sum(len([s for s in r["serial"].split(",") if s.strip()])
                           for r in rows if r["serial"])
        qty_total = sum(_qty_int(r.get("qty", "")) for r in rows)
        head_model.content.value = f"Model ({len(rows)})"
        head_serial.content.value = f"Serial ({serial_count})"
        head_qty.content.value = f"Qty ({qty_total})" if rows else "Qty"
        page.update()
        return serial_count

    # ------------------------------------------------------------- Scan Model
    # Figma design: header button opens "Model & API Configuration" —
    # engine router (provider + model), credentials table with remarks,
    # add-key panel, and a Cancel / Apply footer.
    engine_btn = ft.Button("Scan Model", bgcolor=ENGINE_GRAY, color=INK,
                           elevation=0)

    def engine_caption() -> str:
        eng = state["engine"]
        if not eng:
            return "Scan Model"
        return (f"Scan Model ({PROVIDERS[eng]['label']} · "
                f"{state['providers'][eng]['model']})")

    def _mask(k: str) -> str:
        return k[:6] + "..." + k[-4:] if len(k) > 12 else "****"

    def _cfg() -> dict:
        return state["providers"][state["key_engine"]]

    def refresh_engine_btn():
        engine_btn.content = ft.Text(engine_caption(), color=INK, size=13,
                                     weight=ft.FontWeight.W_500, no_wrap=True)
        try:
            engine_btn.update()
        except RuntimeError:
            pass

    # ---- engine router (01)
    provider_dd = ft.Dropdown(label="AI Provider", dense=True, expand=True)
    model_dd = ft.Dropdown(label="Model Selection", dense=True, expand=True)

    # ---- credentials table (02)
    keys_table = ft.Column(spacing=0)
    keys_count = ft.Text("0 keys registered", size=11, color=INK_SOFT)
    new_key_field = ft.TextField(hint_text="Paste a new API key",
                                 password=True, can_reveal_password=True,
                                 dense=True, expand=3)
    new_remark_field = ft.TextField(hint_text="e.g. Rachel (Field Tech)",
                                    dense=True, expand=2)
    footer_status = ft.Text("", size=12, color=INK_SOFT)
    revealed = set()  # indexes whose key is shown in full

    def _section_title(txt):
        return ft.Text(txt, size=11, weight=ft.FontWeight.W_800, color=TEAL)

    def _key_row(i: int, entry: dict):
        shown = i in revealed
        return ft.Container(
            ft.Row([
                ft.Container(ft.Text(PROVIDERS[state["key_engine"]]["label"],
                                     size=10, weight=ft.FontWeight.W_700,
                                     color="#17635C", no_wrap=True),
                             bgcolor="#EEFAF7", border_radius=6,
                             padding=ft.Padding(6, 4, 6, 4), width=110),
                ft.Text(entry["key"] if shown else _mask(entry["key"]),
                        size=11, font_family="Consolas", color="#58647A",
                        width=130, no_wrap=True, expand=True),
                ft.Container(ft.Text("ACTIVE", size=9,
                                     weight=ft.FontWeight.W_800,
                                     color="#087F5B"),
                             bgcolor="#D9FBE9", border_radius=99,
                             padding=ft.Padding(7, 4, 7, 4), width=62,
                             alignment=ft.Alignment(0, 0)),
                ft.TextField(value=entry.get("remark", ""),
                             hint_text="Owner / Remark", dense=True,
                             text_size=12, width=150,
                             on_change=lambda e, i=i: save_remark(i, e)),
                ft.Row([
                    ft.IconButton(
                        ft.Icons.VISIBILITY_OFF if shown
                        else ft.Icons.VISIBILITY,
                        icon_size=15, tooltip="Reveal / hide key",
                        on_click=lambda e, i=i: toggle_reveal(i)),
                    ft.IconButton(ft.Icons.DELETE_OUTLINE, icon_size=15,
                                  icon_color="#EF4444", tooltip="Delete key",
                                  on_click=lambda e, i=i: remove_key(i)),
                ], spacing=2),
            ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding(8, 6, 8, 6),
            border=ft.Border.only(bottom=ft.BorderSide(1, "#EDF0F5")),
            bgcolor="#FFFFFF" if i % 2 == 0 else "#F9FAFB",
        )

    def save_remark(i: int, e):
        _cfg()["keys"][i]["remark"] = e.control.value
        save_providers(state["providers"])

    def toggle_reveal(i: int):
        if i in revealed:
            revealed.discard(i)
        else:
            revealed.add(i)
        refresh_key_table()

    def refresh_key_table():
        keys = _cfg()["keys"]
        n = len(keys)
        keys_count.value = f"{n} key{'s' if n != 1 else ''} registered"
        head = ft.Container(
            ft.Row([
                ft.Text("PROVIDER", size=9, weight=ft.FontWeight.W_800,
                        color="#738096", width=110),
                ft.Text("KEY SIGNATURE", size=9, weight=ft.FontWeight.W_800,
                        color="#738096", expand=True),
                ft.Text("STATUS", size=9, weight=ft.FontWeight.W_800,
                        color="#738096", width=62),
                ft.Text("OWNER / REMARK", size=9, weight=ft.FontWeight.W_800,
                        color="#738096", width=150),
                ft.Text("ACTIONS", size=9, weight=ft.FontWeight.W_800,
                        color="#738096", width=80),
            ], spacing=8),
            padding=ft.Padding(8, 6, 8, 6), bgcolor="#F7F9FC",
            border=ft.Border.only(bottom=ft.BorderSide(1, "#EDF0F5")),
        )
        keys_table.controls = [head] + [_key_row(i, k)
                                        for i, k in enumerate(keys)]
        try:
            keys_count.update()
            keys_table.update()
        except RuntimeError:
            pass  # controls not mounted yet — they'll render on dialog open

    def refresh_router():
        eng = state["key_engine"]
        provider_dd.value = eng
        model_dd.options = [ft.dropdown.Option(m)
                            for m in PROVIDERS[eng]["models"]]
        model_dd.value = _cfg()["model"]
        new_key_field.hint_text = f"Enter {PROVIDERS[eng]['label']} API key"
        refresh_key_table()
        footer_status.value = (f"Local routing active on "
                               f"{PROVIDERS[eng]['label']} · "
                               f"{_cfg()['model']}"
                               if _cfg()["keys"] else
                               f"{PROVIDERS[eng]['label']}: no key saved yet")
        # repaint the router controls — they're already mounted by the time
        # the user picks a provider
        for c in (provider_dd, model_dd, new_key_field, footer_status):
            try:
                c.update()
            except RuntimeError:
                pass

    def on_provider_change(e):
        state["key_engine"] = provider_dd.value
        revealed.clear()
        refresh_router()

    def on_model_change(e):
        _cfg()["model"] = model_dd.value
        save_providers(state["providers"])
        refresh_router()
        refresh_engine_btn()

    provider_dd.options = [ft.dropdown.Option(k, PROVIDERS[k]["label"])
                           for k in PROVIDERS]
    provider_dd.on_select = on_provider_change
    model_dd.on_select = on_model_change

    def remove_key(i: int):
        _cfg()["keys"].pop(i)
        revealed.clear()
        save_providers(state["providers"])
        refresh_router()
        if not _cfg()["keys"] and state["engine"] == state["key_engine"]:
            state["engine"] = None
        refresh_engine_btn()
        log(f"Key {i + 1} removed.")

    def register_key(e=None):
        k = new_key_field.value.strip()
        if not k:
            return
        existing = [x["key"] for x in _cfg()["keys"]]
        if k in existing:
            log("That key is already saved.")
        else:
            _cfg()["keys"].append(
                {"key": k, "remark": new_remark_field.value.strip()})
            save_providers(state["providers"])
            log(f"{PROVIDERS[state['key_engine']]['label']} key "
                f"{len(_cfg()['keys'])} added ({_mask(k)}).", ok=True)
        new_key_field.value = ""
        new_remark_field.value = ""
        refresh_router()

    def apply_config(e=None):
        if new_key_field.value and new_key_field.value.strip():
            register_key()
        if _cfg()["keys"]:
            state["engine"] = state["key_engine"]
            refresh_engine_btn()
            log(f"Engine: {engine_caption()} ready "
                f"({len(_cfg()['keys'])} key(s) saved).")
        key_dialog.open = False
        page.update()

    def cancel_config(e=None):
        key_dialog.open = False
        page.update()

    key_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Row([
            ft.Column([
                ft.Text("Model & API Configuration", size=16,
                        weight=ft.FontWeight.W_700),
                ft.Text("Configure engine routing and active secure "
                        "credentials.", size=12, color=INK_SOFT),
            ], spacing=2, expand=True),
            ft.IconButton(ft.Icons.CLOSE, icon_color=INK_SOFT,
                          on_click=cancel_config),
        ]),
        content=ft.Container(
            ft.Column([
                _section_title("01 / ENGINE ROUTER"),
                ft.Row([provider_dd, model_dd], spacing=10),
                ft.Row([_section_title("02 / API KEY CREDENTIALS"),
                        keys_count],
                       alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                       vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Container(keys_table, border_radius=10,
                             border=ft.Border.all(1, "#E2E7EF"),
                             clip_behavior=ft.ClipBehavior.HARD_EDGE),
                ft.Container(
                    ft.Column([
                        ft.Text("ADD NEW CONFIGURATION KEY", size=10,
                                weight=ft.FontWeight.W_800),
                        ft.Row([
                            new_key_field, new_remark_field,
                            ft.Button("Register", icon=ft.Icons.ADD,
                                      bgcolor=TEAL, color="white",
                                      on_click=register_key),
                        ], spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    ], spacing=8),
                    bgcolor="#F7F9FC", border_radius=10,
                    padding=ft.Padding(12, 10, 12, 12),
                    border=ft.Border.all(1, "#E5E9F0")),
            ], tight=True, spacing=10, scroll=ft.ScrollMode.AUTO),
            width=620, height=480,
        ),
        actions=[
            footer_status,
            ft.Button("Cancel", on_click=cancel_config,
                      style=ft.ButtonStyle(
                          side=ft.BorderSide(1, "#DCE2EA"),
                          bgcolor="#FFFFFF", color=INK)),
            ft.FilledButton("Apply Configuration", icon=ft.Icons.CHECK,
                            bgcolor=TEAL, color="white",
                            on_click=apply_config),
        ],
        actions_alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
    )

    def open_key_manager(e=None):
        state["key_engine"] = state["engine"] or "gemini"
        revealed.clear()
        refresh_router()
        key_dialog.open = True
        page.show_dialog(key_dialog)
        page.update()

    engine_btn.on_click = open_key_manager


    # ------------------------------------------------------------- file pickers
    img_picker = ft.FilePicker()
    save_picker = ft.FilePicker()
    page.services.append(img_picker)
    page.services.append(save_picker)

    async def load_image(e):
        files = await img_picker.pick_files(
            dialog_title="Choose a parts-list photo",
            file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=["png", "jpg", "jpeg", "webp", "bmp"],
        )
        if not files:
            return
        path = files[0].path
        if not path:
            log("Could not get file path for the selected image.")
            return
        # lock buttons while the image decodes/previews (large photos take a moment)
        state["busy"] = True
        _update_buttons()
        state["image_path"] = path
        img_preview.src = path
        img_preview.visible = True
        img_empty.visible = False
        state["busy"] = False
        _update_buttons()
        log(f"Parts list image loaded successfully. ({files[0].name})")

    async def export_csv(e):
        if not state["rows"]:
            log("Nothing to export — run a scan first.")
            return
        target = await save_picker.save_file(
            dialog_title="Export results as CSV",
            file_name="model_serial_results.csv",
            allowed_extensions=["csv"],
        )
        if not target:
            return
        try:
            with open(target, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["No", "Model", "Serial", "Qty"])
                for r in state["rows"]:
                    w.writerow([r["no"], r["model"], r["serial"] or r["desc"],
                                r.get("qty", "")])
            log(f"Exported {len(state['rows'])} row(s) to {target}", ok=True)
        except Exception as ex:
            log(f"CSV export failed: {ex}")

    # ------------------------------------------------------------- scan
    def _update_buttons():
        busy = state["busy"]
        # SCAN stays locked until an image is loaded; everything locks during a scan.
        locked = busy or not state["image_path"]
        scan_btn.disabled = locked
        scan_btn.bgcolor = "#8A8F94" if locked else TEAL
        load_btn.disabled = busy
        clear_btn.disabled = busy
        page.update()

    def set_busy(busy: bool):
        """Give the buttons a live/loading feel while a scan runs."""
        state["busy"] = busy
        if busy:
            scan_btn.content = ft.Row(
                [ft.ProgressRing(width=16, height=16, stroke_width=2,
                                 color="white"),
                 ft.Text("SCANNING...", size=14, weight=ft.FontWeight.W_600)],
                spacing=8, alignment=ft.MainAxisAlignment.CENTER, tight=True)
            scan_btn.icon = None
        else:
            scan_btn.content = ft.Text("SCAN", size=14, weight=ft.FontWeight.W_600)
            scan_btn.icon = ft.Icons.QR_CODE_SCANNER
        _update_buttons()

    def do_scan(e):
        if not state["image_path"]:
            log("Load an image before scanning.")
            return
        eng = state["engine"]
        if not eng:
            log("Pick a Scan Model first.")
            open_key_manager()
            return
        if not state["providers"][eng]["keys"]:
            log(f"Add a {PROVIDERS[eng]['label']} API key first.")
            open_key_manager()
            return
        # instant feedback — confirm the click before network work starts
        cfg = state["providers"][eng]
        log(f"Scan button clicked — sending image to {PROVIDERS[eng]['label']} "
            f"({cfg['model']}), waiting for response...")
        set_busy(True)
        page.run_task(SCANNERS[eng])

    async def run_gemini():
        from google import genai
        from google.genai import types

        set_busy(True)
        log("Gemini multimodal parser active: scanning tabular data...")
        page.update()
        try:
            img_bytes = Path(state["image_path"]).read_bytes()
            suffix = Path(state["image_path"]).suffix.lower().lstrip(".")
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                    "webp": "image/webp", "bmp": "image/bmp"}.get(suffix, "image/jpeg")
            contents = [types.Part.from_bytes(data=img_bytes, mime_type=mime),
                        PROMPT]
            model = state["providers"]["gemini"]["model"]

            def _retryable(err: Exception) -> bool:
                s = str(err)
                return any(t in s for t in
                           ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
                            "quota"))

            # Try each saved key in turn; on a retryable error (busy / quota)
            # wait briefly, then move on to the next key.
            resp = None
            last_err = None
            keys = state["providers"]["gemini"]["keys"]
            for ki, entry in enumerate(keys):
                key = entry["key"]
                if len(keys) > 1:
                    log(f"Using Gemini key {ki + 1} of {len(keys)}...")
                client = genai.Client(api_key=key)
                for attempt in range(3):
                    try:
                        resp = client.models.generate_content(
                            model=model, contents=contents)
                        break
                    except Exception as ex:
                        last_err = ex
                        if not _retryable(ex):
                            raise
                        if attempt < 2:
                            log(f"Gemini busy — retrying in "
                                f"{2 * (attempt + 1)}s...")
                            await asyncio.sleep(2 * (attempt + 1))
                if resp is not None:
                    break
                if ki + 1 < len(keys):
                    log(f"Key {ki + 1} exhausted — switching to key {ki + 2}.")
            if resp is None:
                raise last_err
        except Exception as ex:
            log(f"Gemini request failed: {ex}")
            set_busy(False)
            return
        finish_scan(resp.text or "")

    def _retryable_http(err: Exception) -> bool:
        s = str(err)
        return any(t in s for t in ("500", "502", "503", "429",
                                    "Throttling", "quota", "Timeout",
                                    "overloaded"))

    async def run_openai_compatible(engine: str):
        """Scan via an OpenAI-compatible endpoint (Alibaba / ChatGPT).

        Image is sent as a base64 data URL inside a chat message.
        """
        import base64
        import urllib.request

        cfg = state["providers"][engine]
        label = PROVIDERS[engine]["label"]
        url = PROVIDERS[engine]["url"]
        set_busy(True)
        log(f"{label} ({cfg['model']}) parser active: scanning tabular data...")
        page.update()
        try:
            img_bytes = Path(state["image_path"]).read_bytes()
            suffix = Path(state["image_path"]).suffix.lower().lstrip(".")
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                    "webp": "image/webp", "bmp": "image/bmp"}.get(suffix,
                                                                  "image/jpeg")
            data_url = (f"data:{mime};base64,"
                        + base64.b64encode(img_bytes).decode())
            payload = json.dumps({
                "model": cfg["model"],
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url",
                         "image_url": {"url": data_url}},
                        {"type": "text", "text": PROMPT},
                    ],
                }],
            }).encode()

            def _post(key: str) -> str:
                req = urllib.request.Request(
                    url, data=payload,
                    headers={"Authorization": f"Bearer {key}",
                             "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    body = json.loads(r.read().decode())
                return body["choices"][0]["message"]["content"]

            text = None
            last_err = None
            keys = cfg["keys"]
            for ki, entry in enumerate(keys):
                key = entry["key"]
                if len(keys) > 1:
                    log(f"Using {label} key {ki + 1} of {len(keys)}...")
                for attempt in range(3):
                    try:
                        text = await asyncio.to_thread(_post, key)
                        break
                    except Exception as ex:
                        last_err = ex
                        if not _retryable_http(ex):
                            raise
                        if attempt < 2:
                            log(f"{label} busy — retrying in "
                                f"{2 * (attempt + 1)}s...")
                            await asyncio.sleep(2 * (attempt + 1))
                if text is not None:
                    break
                if ki + 1 < len(keys):
                    log(f"Key {ki + 1} failed — switching to key {ki + 2}.")
            if text is None:
                raise last_err
        except Exception as ex:
            log(f"{label} request failed: {ex}")
            set_busy(False)
            return
        finish_scan(text)

    async def run_anthropic():
        """Scan via Anthropic Messages API (Claude vision)."""
        import base64
        import urllib.request

        cfg = state["providers"]["anthropic"]
        set_busy(True)
        log(f"Claude ({cfg['model']}) parser active: scanning tabular data...")
        page.update()
        try:
            img_bytes = Path(state["image_path"]).read_bytes()
            suffix = Path(state["image_path"]).suffix.lower().lstrip(".")
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                    "webp": "image/webp", "bmp": "image/bmp"}.get(suffix,
                                                                  "image/jpeg")
            payload = json.dumps({
                "model": cfg["model"],
                "max_tokens": 4096,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image",
                         "source": {"type": "base64", "media_type": mime,
                                    "data": base64.b64encode(img_bytes).decode()}},
                        {"type": "text", "text": PROMPT},
                    ],
                }],
            }).encode()

            def _post(key: str) -> str:
                req = urllib.request.Request(
                    PROVIDERS["anthropic"]["url"], data=payload,
                    headers={"x-api-key": key,
                             "anthropic-version": "2023-06-01",
                             "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    body = json.loads(r.read().decode())
                return "".join(b.get("text", "")
                               for b in body.get("content", []))

            text = None
            last_err = None
            keys = cfg["keys"]
            for ki, entry in enumerate(keys):
                key = entry["key"]
                if len(keys) > 1:
                    log(f"Using Claude key {ki + 1} of {len(keys)}...")
                for attempt in range(3):
                    try:
                        text = await asyncio.to_thread(_post, key)
                        break
                    except Exception as ex:
                        last_err = ex
                        if not _retryable_http(ex):
                            raise
                        if attempt < 2:
                            log(f"Claude busy — retrying in "
                                f"{2 * (attempt + 1)}s...")
                            await asyncio.sleep(2 * (attempt + 1))
                if text is not None:
                    break
                if ki + 1 < len(keys):
                    log(f"Key {ki + 1} failed — switching to key {ki + 2}.")
            if text is None:
                raise last_err
        except Exception as ex:
            log(f"Claude request failed: {ex}")
            set_busy(False)
            return
        finish_scan(text)

    async def run_alibaba():
        await run_openai_compatible("alibaba")

    async def run_openai():
        await run_openai_compatible("openai")

    # page.run_task requires real coroutine functions — not plain lambdas.
    SCANNERS = {
        "gemini": run_gemini,
        "alibaba": run_alibaba,
        "openai": run_openai,
        "anthropic": run_anthropic,
    }

    def finish_scan(raw: str):
        try:
            rows = parse_gemini_json(raw)
        except Exception as ex:
            log(f"Could not parse response ({ex}). Raw reply logged below.")
            log(raw[:300])
            set_busy(False)
            return
        set_busy(False)
        n_serial = set_results(rows)
        if rows:
            log(f"Extract complete. {len(rows)} models and {n_serial} serial numbers found.", ok=True)
            flagged = [(r["no"], r["flag"]) for r in rows if r.get("flag")]
            if flagged:
                names = ", ".join(f"No {n or '?'}" for n, _ in flagged)
                log(f"⚠ {len(flagged)} row(s) need review: {names} — "
                    f"hover the warning icon or open the row to fix.")
        else:
            log("Extraction returned no model/serial pairs.")

    def do_clear(e):
        state["image_path"] = None
        state["added"] = set()
        img_preview.visible = False
        img_preview.src = ""
        img_empty.visible = True
        _update_buttons()  # re-lock SCAN once the image is cleared
        set_results([])
        log_list.controls.clear()
        log("Cleared. Waiting for image payload...")

    def send_row(i: int):
        """Send one scanned row to Stock In — same validation + save path
        as manual entry (duplicate check + new-Type modal)."""
        r = state["rows"][i]
        supplier = supplier_field.value.strip()
        if not supplier:
            log("Fill in SUPPLIER before adding a row to stock.")
            return
        if not r["model"] or not r["serial"]:
            log("This row needs a Model and Serial before it can be added.")
            return
        date_in = date_in_field.value.strip()
        if stock_store is None:
            log("⚠ daikin_stock.xlsx not available — nothing was saved.")
            return
        pending_send["i"] = i
        _commit_send(supplier, r, date_in)

    def _commit_send(supplier: str, r: dict, date_in: str):
        i = pending_send["i"]
        serials = [s.strip() for s in r["serial"].split(",") if s.strip()]
        added, dupes, ok_type = stock_store.stock_in(
            supplier=supplier, model=r["model"],
            serials=serials, date_in=date_in)
        if not ok_type:
            open_type_modal(r["model"])  # batch held until Type is chosen
            return
        _finish_send(r, added, dupes)

    def _finish_send(r: dict, added: list, dupes: list):
        if added:
            state["added"].add(pending_send["i"])
            log(f"ADDED {len(added)} UNIT(S) OF {r['model'].upper()}.", ok=True)
            set_results(state["rows"])
        if dupes:
            log(f"DUPLICATED SERIAL NUMBER: {', '.join(dupes).upper()}")

    # ---- new-Model Type assignment (same modal as Stock In spec)
    pending_send = {"i": -1, "supplier": "", "date_in": ""}
    type_dd = ft.Dropdown(label="Existing Type", dense=True)
    type_new = ft.TextField(label="Or new Type name", dense=True)
    type_err = ft.Text("", color="#DC2626", size=12, visible=False)
    type_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("", size=15, weight=ft.FontWeight.W_600),
        content=ft.Container(
            ft.Column([type_dd, ft.Text("— OR —", size=11, color=INK_SOFT),
                       type_new, type_err],
                      tight=True, spacing=10,
                      alignment=ft.MainAxisAlignment.START),
            width=360, height=190),
        actions=[
            ft.TextButton("Cancel", on_click=lambda e: close_type_modal()),
            ft.FilledButton("Confirm", on_click=lambda e: confirm_type_modal()),
        ],
    )
    page.overlay.append(type_dialog)

    def open_type_modal(model: str):
        pending_send["supplier"] = supplier_field.value.strip()
        pending_send["date_in"] = date_in_field.value.strip()
        type_dd.options = [ft.dropdown.Option(t) for t in stock_store.types]
        type_dd.value = None
        type_new.value = ""
        type_err.visible = False
        type_dialog.title.value = (
            f'New model "{model.upper()}" needs a Type. '
            f"Pick one, or add a new one below.")
        type_dialog.open = True
        page.update()

    def close_type_modal():
        # Cancel discards the pending row — nothing saves, model not remembered
        type_dialog.open = False
        pending_send["i"] = -1
        page.update()

    def confirm_type_modal():
        chosen = type_new.value.strip() or (type_dd.value or "")
        if not chosen:
            type_err.value = "Pick an existing Type, or enter a new one."
            type_err.visible = True
            page.update()
            return
        i = pending_send["i"]
        type_dialog.open = False
        if i >= 0:
            r = state["rows"][i]
            serials = [s.strip() for s in r["serial"].split(",") if s.strip()]
            added, dupes = stock_store.stock_in_with_type(
                supplier=pending_send["supplier"], model=r["model"],
                serials=serials, date_in=pending_send["date_in"],
                type_name=chosen)
            _finish_send(r, added, dupes)
        pending_send["i"] = -1
        page.update()

    # ------------------------------------------------------------- row edit dialog
    edit_no = ft.TextField(label="No", dense=True)
    edit_model = ft.TextField(label="Model", dense=True)
    edit_serial = ft.TextField(label="Serial (comma-separated)", dense=True)
    edit_qty = ft.TextField(label="Qty", dense=True)
    edit_idx = {"i": -1}

    edit_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Edit row", size=15, weight=ft.FontWeight.W_600),
        content=ft.Column([edit_no, edit_model, edit_serial, edit_qty],
                          tight=True, spacing=10),
        actions=[
            ft.TextButton("Cancel", on_click=lambda e: close_edit()),
            ft.FilledButton("Save", on_click=lambda e: save_edit()),
        ],
    )

    def open_edit(i: int):
        edit_idx["i"] = i
        r = state["rows"][i]
        edit_no.value = r["no"]
        edit_model.value = r["model"]
        edit_serial.value = r["serial"] or r["desc"]
        edit_qty.value = r.get("qty", "")
        edit_dialog.title.value = "Edit row"
        edit_dialog.open = True
        page.show_dialog(edit_dialog)
        page.update()

    def open_add_row(e=None):
        edit_idx["i"] = -1
        edit_no.value = ""
        edit_model.value = ""
        edit_serial.value = ""
        edit_qty.value = ""
        edit_dialog.title.value = "Add row"
        edit_dialog.open = True
        page.show_dialog(edit_dialog)
        page.update()

    def close_edit():
        edit_dialog.open = False
        page.update()

    def save_edit():
        i = edit_idx["i"]
        new_row = i < 0
        if new_row:
            r = {"no": "", "model": "", "serial": "", "qty": "",
                 "desc": "", "flag": ""}
        else:
            r = state["rows"][i]
        r["no"] = edit_no.value.strip()
        r["model"] = edit_model.value.strip().upper()
        val = edit_serial.value.strip()
        # If the value looks like serials (letters+digits), store as serial;
        # otherwise keep it as a description fallback.
        if re.match(r"^[A-Z0-9,\s\-–]+$", val.upper()) and re.search(r"\d", val):
            r["serial"] = expand_serial_range(val.upper())
            r["desc"] = r.get("desc", "")
        else:
            r["desc"] = val
            r["serial"] = ""
        r["qty"] = edit_qty.value.strip()
        # re-evaluate the warning flag against the corrected values
        q = _qty_int(r["qty"])
        if not r["model"]:
            r["flag"] = "no_model"
        elif r["serial"] and q and _serial_count(r["serial"]) != q:
            r["flag"] = "qty_mismatch"
        else:
            r["flag"] = ""
        if new_row:
            state["rows"].append(r)
        set_results(state["rows"])
        close_edit()
        log((f"Row {r['no'] or len(state['rows'])} added manually."
             if new_row else f"Row {r['no'] or i+1} updated."))

    # ------------------------------------------------------------- layout
    header = ft.Container(
        content=ft.Row(
            [
                ft.Container(
                    ft.Icon(ft.Icons.DOCUMENT_SCANNER, color=TEAL, size=20),
                    bgcolor=TEAL_BG, border_radius=8, padding=6,
                ),
                ft.Text("Model & Serial Extractor", size=16,
                        weight=ft.FontWeight.W_600),
                ft.Container(expand=True),
                engine_btn,
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=10,
        ),
        bgcolor=CARD, padding=ft.Padding(16, 10, 16, 10),
        border=ft.Border.only(bottom=ft.BorderSide(1, LINE)),
        margin=ft.Margin(-20, -20, -20, 14),
    )

    load_btn = ft.OutlinedButton(
        "Load Image", icon=ft.Icons.UPLOAD_FILE_OUTLINED,
        on_click=load_image, expand=True,
        style=ft.ButtonStyle(padding=ft.Padding(8, 10, 8, 10)),
    )
    scan_btn = ft.Button(
        "SCAN", icon=ft.Icons.QR_CODE_SCANNER,
        bgcolor=TEAL, color="white", elevation=0, expand=True,
        style=ft.ButtonStyle(
            padding=ft.Padding(8, 10, 8, 10),
            text_style=ft.TextStyle(size=14, weight=ft.FontWeight.W_600),
        ),
        on_click=do_scan,
    )
    clear_btn = ft.OutlinedButton(
        "Clear", icon=ft.Icons.DELETE_OUTLINE,
        on_click=do_clear, expand=True,
        style=ft.ButtonStyle(padding=ft.Padding(8, 10, 8, 10)),
    )

    left_card = card(
        ft.Column(
            [
                ft.Text("Image Preview", size=15, weight=ft.FontWeight.W_600),
                img_frame,
                ft.Row(
                    [
                        load_btn,
                        scan_btn,
                        clear_btn,
                    ],
                    spacing=8,
                ),
            ],
            spacing=12,
        )
    )

    results_card = card(
        ft.Column(
            [
                ft.Row(
                    [ft.Text("Scan Results", size=15, weight=ft.FontWeight.W_600),
                     ft.Container(expand=True),
                     ft.IconButton(ft.Icons.ADD_CIRCLE_OUTLINE, icon_size=20,
                                   icon_color=TEAL,
                                   tooltip="Add a row manually",
                                   on_click=open_add_row),
                     ft.IconButton(ft.Icons.DOWNLOAD, icon_size=18,
                                   tooltip="Export CSV", on_click=export_csv)],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Row(
                    [supplier_field, date_in_field,
                     ft.IconButton(ft.Icons.CALENDAR_MONTH, icon_size=18,
                                   tooltip="Pick Date In",
                                   on_click=lambda e: page.show_dialog(
                                       date_picker))],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                results_scroll,
            ],
            spacing=10,
            expand=True,
        ),
        expand=True,
    )

    log_card = card(
        ft.Column(
            [
                ft.Row(
                    [ft.Text("Status Log", size=15, weight=ft.FontWeight.W_600),
                     ft.Container(expand=True),
                     ft.Text("Operational", size=12, color=INK_SOFT)],
                ),
                ft.Container(
                    log_list,
                    border=ft.Border.all(1, LINE),
                    border_radius=8,
                    padding=10,
                    bgcolor="#FAFBFC",
                    expand=True,
                ),
            ],
            spacing=10,
            expand=True,
        ),
        expand=True,
    )

    stock_in_row = ft.Row(
        [
            ft.Column(
                [left_card, log_card],
                expand=5, spacing=16,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
            ft.Column([results_card], expand=6,
                      horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
        ],
        vertical_alignment=ft.CrossAxisAlignment.START,
        spacing=16,
        expand=True,
    )

    # ---- Stock Tracker nav: the extractor IS screen 1 (Stock In);
    # screens 3/4/5 are ported one at a time into these tabs.
    TABS = ["1  Stock In", "3  Available Stock", "4  Stock Out",
            "5  Track List"]
    nav_btns = {}
    placeholder_text = ft.Text("", color=INK_SOFT, size=13)
    placeholder = ft.Container(
        content=ft.Column(
            [ft.Icon(ft.Icons.CONSTRUCTION, size=44, color=INK_SOFT),
             placeholder_text],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER, spacing=8),
        bgcolor=CARD, border_radius=12, border=ft.Border.all(1, LINE),
        expand=True, visible=False,
    )

    def switch_tab(name):
        is_in = name == TABS[0]
        stock_in_row.visible = is_in
        placeholder.visible = not is_in
        if not is_in:
            placeholder_text.value = f"{name.strip()} — coming next"
        for n, b in nav_btns.items():
            active = n == name
            b.bgcolor = TEAL if active else "#EDF0F2"
            b.content.color = "white" if active else INK_SOFT
        page.update()

    for t in TABS:
        b = ft.Button(content=ft.Text(t, size=12.5,
                                      weight=ft.FontWeight.W_600),
                      on_click=lambda e, t=t: switch_tab(t), elevation=0)
        nav_btns[t] = b
    nav_bar = ft.Row(list(nav_btns.values()), spacing=8)

    page.add(
        ft.Stack(
            [
                ft.Column(
                    [
                        header,
                        nav_bar,
                        ft.Stack([stock_in_row, placeholder], expand=True),
                    ],
                    expand=True,
                ),
                zoom_overlay,
            ],
            expand=True,
        )
    )
    switch_tab(TABS[0])

    refresh_engine_btn()
    _update_buttons()  # SCAN locked until an image is loaded
    ready = [p for p in PROVIDERS if state["providers"][p]["keys"]]
    if ready:
        state["engine"] = ready[0]
        refresh_engine_btn()
        log(f"App initialized. {engine_caption()} "
            f"({len(state['providers'][state['engine']]['keys'])} saved key(s)).")
    else:
        log("App initialized. Click 'Scan Model' and add your API key.")
    if stock_store is None:
        log(f"⚠ daikin_stock.xlsx could not be opened ({STOCK_ERR}) — "
            f"send arrows will not save to stock.")
    log("Waiting for image payload...")


if __name__ == "__main__":
    ft.run(main)
