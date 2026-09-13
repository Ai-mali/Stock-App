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
import sys
from pathlib import Path

import flet as ft

CONFIG_PATH = Path.home() / ".model_serial_extractor.json"

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

    return re.sub(
        r"([A-Za-z]{0,4})(\d{3,})\s*[-–]\s*([A-Za-z]{0,4})(\d{3,})", _sub, text
    )


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
        serial = expand_serial_range(str(item.get("serial", "")).strip())
        desc = str(item.get("desc", "")).strip()
        no = str(item.get("no", "")).strip()
        qty = str(item.get("qty", "")).strip()
        if model and (serial or desc):
            rows.append({"no": no, "model": model.upper(), "qty": qty,
                         "serial": serial.upper(), "desc": desc})
    return rows


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
        out[p]["keys"] = list(dict.fromkeys(out[p]["keys"]))
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
        "rows": [],
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
        zoom_img.scale = ft.Scale(zoom_state["scale"])
        zoom_img.offset = ft.Offset(zoom_state["dx"], zoom_state["dy"])
        page.update()

    def _pan(e):
        zoom_state["dx"] += e.delta_x
        zoom_state["dy"] += e.delta_y
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

    badge = ft.Container(
        content=ft.Row(
            [ft.Icon(ft.Icons.CHECK_CIRCLE, size=14, color=TEAL),
             ft.Text("0 models found", color=TEAL, size=12, weight=ft.FontWeight.W_500)],
            spacing=4, tight=True),
        bgcolor=TEAL_BG, border_radius=20, padding=ft.Padding(10, 4, 10, 4),
        visible=False,
    )

    # Custom table: proportional columns (flex weights) so the grid stays
    # evenly spaced at any window size, with a frozen header and scrolling body.
    FLEX = {"no": 1, "model": 3, "serial": 6, "qty": 2}
    EDIT_W = 44

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

        result_rows = []
        for i, r in enumerate(rows):
            result_rows.append(ft.Container(
                ft.Row([
                    cell(r["no"], FLEX["no"], color=INK_SOFT,
                         no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
                    cell(r["model"], FLEX["model"],
                         no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
                    cell(r["serial"] or r["desc"], FLEX["serial"],
                         italic=not r["serial"],
                         color=INK if r["serial"] else INK_SOFT),
                    cell(qty_text(r.get("qty", "")), FLEX["qty"], no_wrap=True),
                    ft.Container(
                        ft.IconButton(ft.Icons.EDIT_OUTLINED, icon_size=16,
                                      tooltip="Edit row", icon_color=INK_SOFT,
                                      on_click=lambda e, i=i: open_edit(i)),
                        width=EDIT_W, alignment=ft.Alignment(0, 0),
                    ),
                ], spacing=0),
                bgcolor="#FFFFFF" if i % 2 == 0 else "#FAFBFC",
                border=ft.Border.only(bottom=ft.BorderSide(1, "#EEF1F3")),
            ))
        results_body.controls = result_rows
        serial_count = sum(len([s for s in r["serial"].split(",") if s.strip()])
                           for r in rows if r["serial"])
        head_model.content.value = f"Model ({len(rows)})"
        head_serial.content.value = f"Serial ({serial_count})"
        badge.content.controls[1].value = f"{len(rows)} models found"
        badge.visible = bool(rows)
        page.update()
        return serial_count

    # ------------------------------------------------------------- Scan Model
    # One button opens a manager window: provider list on top, each provider
    # has its own model dropdown + multi-key list. The button label collapses
    # to the chosen provider/model afterwards.
    engine_btn = ft.Button("Scan Model", icon=ft.Icons.MEMORY,
                           bgcolor=ENGINE_GRAY, color=INK, elevation=0)

    def engine_caption() -> str:
        eng = state["engine"]
        if not eng:
            return "Scan Model"
        p = PROVIDERS[eng]
        return f"Scan Model ({p['label']} · {state['providers'][eng]['model']})"

    # ------------------------------------------------------------- engine manager
    # One window listing all providers; each has a model dropdown and its own
    # multi-key list. Selecting a provider (with at least one key) sets it as
    # the active engine and the header button collapses to show the choice.
    key_field = ft.TextField(password=True, can_reveal_password=True,
                             label="API key", hint_text="Paste a new API key",
                             dense=True)
    keys_list = ft.Column(spacing=4)
    model_dd = ft.Dropdown(label="Model", dense=True, expand=True)
    provider_row = ft.Row(spacing=6)

    def _mask(k: str) -> str:
        return k[:6] + "..." + k[-4:] if len(k) > 12 else "****"

    def _cfg() -> dict:
        return state["providers"][state["key_engine"]]

    def refresh_engine_btn():
        engine_btn.content = ft.Row(
            [ft.Icon(ft.Icons.MEMORY, size=16, color=INK),
             ft.Text(engine_caption(), color=INK, size=13,
                     weight=ft.FontWeight.W_500, no_wrap=True)],
            spacing=6, tight=True)
        page.update()

    def refresh_provider_panel():
        eng = state["key_engine"]
        provider_row.controls = [
            ft.Button(
                PROVIDERS[p]["label"], expand=True,
                bgcolor=(TEAL if p == eng else ENGINE_GRAY),
                color=("white" if p == eng else INK), elevation=0,
                style=ft.ButtonStyle(padding=ft.Padding(4, 8, 4, 8),
                                     text_style=ft.TextStyle(size=12)),
                on_click=lambda e, p=p: pick_engine(p))
            for p in PROVIDERS
        ]
        model_dd.options = [ft.dropdown.Option(m)
                            for m in PROVIDERS[eng]["models"]]
        model_dd.value = _cfg()["model"]
        key_field.label = f"{PROVIDERS[eng]['label']} API key"
        keys_list.controls = [
            ft.Row([
                ft.Icon(ft.Icons.KEY, size=14, color=TEAL),
                ft.Text(f"Key {i+1}: {_mask(k)}", size=12,
                        font_family="Consolas", expand=True),
                ft.IconButton(ft.Icons.CLOSE, icon_size=14, tooltip="Remove key",
                              icon_color=INK_SOFT,
                              on_click=lambda e, i=i: remove_key(i)),
            ], spacing=6)
            for i, k in enumerate(_cfg()["keys"])
        ]
        page.update()

    def pick_engine(engine: str):
        """Provider selected in the manager — confirm it if keys exist."""
        state["key_engine"] = engine
        if _cfg()["keys"]:
            state["engine"] = engine
        refresh_provider_panel()
        refresh_engine_btn()

    def on_model_change(e):
        _cfg()["model"] = model_dd.value
        save_providers(state["providers"])
        refresh_engine_btn()

    model_dd.on_select = on_model_change

    def remove_key(i: int):
        _cfg()["keys"].pop(i)
        save_providers(state["providers"])
        refresh_provider_panel()
        if not _cfg()["keys"] and state["engine"] == state["key_engine"]:
            state["engine"] = None
            refresh_engine_btn()
        log(f"Key {i+1} removed.")

    def add_key(e=None):
        k = key_field.value.strip()
        if not k:
            return
        if k in _cfg()["keys"]:
            log("That key is already saved.")
        else:
            _cfg()["keys"].append(k)
            save_providers(state["providers"])
            log(f"{PROVIDERS[state['key_engine']]['label']} key "
                f"{len(_cfg()['keys'])} added ({_mask(k)}).", ok=True)
        key_field.value = ""
        refresh_provider_panel()
        if _cfg()["keys"]:
            state["engine"] = state["key_engine"]
            refresh_engine_btn()

    def close_key_dialog(e=None):
        # Done also commits any key still sitting in the input field.
        if key_field.value and key_field.value.strip():
            add_key()
        key_dialog.open = False
        if _cfg()["keys"]:
            state["engine"] = state["key_engine"]
            refresh_engine_btn()
            log(f"Engine: {engine_caption()} ready "
                f"({len(_cfg()['keys'])} key(s) saved).")
        page.update()

    key_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Scan Model", size=15, weight=ft.FontWeight.W_600),
        content=ft.Container(
            ft.Column([
                provider_row,
                model_dd,
                keys_list,
                ft.Row([key_field,
                        ft.IconButton(ft.Icons.ADD_CIRCLE_OUTLINE,
                                      icon_color=TEAL, tooltip="Add key",
                                      on_click=add_key)], spacing=6),
                ft.Text("Pick a provider, choose its model, then add key(s). "
                        "If one key hits its limit, the next is used "
                        "automatically.", size=11, color=INK_SOFT),
            ], tight=True, spacing=10),
            width=460,
        ),
        actions=[
            ft.FilledButton("Done", on_click=close_key_dialog),
        ],
    )

    def open_key_manager(e=None):
        state["key_engine"] = state["engine"] or "gemini"
        refresh_provider_panel()
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
        state["image_path"] = path
        img_preview.src = path
        img_preview.visible = True
        img_empty.visible = False
        page.update()
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
    def set_busy(busy: bool):
        """Give the buttons a live/loading feel while a scan runs."""
        scan_btn.disabled = busy
        load_btn.disabled = busy
        clear_btn.disabled = busy
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
        page.update()

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
            for ki, key in enumerate(keys):
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
            for ki, key in enumerate(keys):
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
            for ki, key in enumerate(keys):
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

    SCANNERS = {
        "gemini": run_gemini,
        "alibaba": lambda: run_openai_compatible("alibaba"),
        "openai": lambda: run_openai_compatible("openai"),
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
        else:
            log("Extraction returned no model/serial pairs.")

    def do_clear(e):
        state["image_path"] = None
        img_preview.visible = False
        img_preview.src = ""
        img_empty.visible = True
        set_results([])
        badge.visible = False
        log_list.controls.clear()
        log("Cleared. Waiting for image payload...")

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
        edit_dialog.open = True
        page.show_dialog(edit_dialog)
        page.update()

    def close_edit():
        edit_dialog.open = False
        page.update()

    def save_edit():
        i = edit_idx["i"]
        if i < 0:
            close_edit()
            return
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
        set_results(state["rows"])
        close_edit()
        log(f"Row {r['no'] or i+1} updated.")

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
                     badge,
                     ft.IconButton(ft.Icons.DOWNLOAD, icon_size=18,
                                   tooltip="Export CSV", on_click=export_csv)],
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

    page.add(
        ft.Stack(
            [
                ft.Column(
                    [
                        header,
                        ft.Row(
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
                        ),
                    ],
                    expand=True,
                ),
                zoom_overlay,
            ],
            expand=True,
        )
    )

    refresh_engine_btn()
    ready = [p for p in PROVIDERS if state["providers"][p]["keys"]]
    if ready:
        state["engine"] = ready[0]
        refresh_engine_btn()
        log(f"App initialized. {engine_caption()} "
            f"({len(state['providers'][state['engine']]['keys'])} saved key(s)).")
    else:
        log("App initialized. Click 'Scan Model' and add your API key.")
    log("Waiting for image payload...")


if __name__ == "__main__":
    ft.run(main)
