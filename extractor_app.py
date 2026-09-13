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

# "gemini-flash-latest" tracks the newest flash model — avoids model retirement
# (gemini-1.5/2.5/3.5 flash get deprecated for new keys over time).
GEMINI_MODEL = "gemini-flash-latest"
CONFIG_PATH = Path.home() / ".model_serial_extractor.json"

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


def load_saved_keys() -> list[str]:
    """Return the saved API-key list; migrates the old single-key format."""
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except Exception:
        return []
    keys = data.get("api_keys")
    if isinstance(keys, list):
        return [k for k in keys if k]
    old = data.get("api_key", "")
    return [old] if old else []


def save_keys(keys: list[str]) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps({"api_keys": keys}))
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
        "api_keys": load_saved_keys(),
        "engine": None,          # None | 'gemini' | 'model'
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

    # ------------------------------------------------------------- engine buttons
    model_btn = ft.Button("Model")
    gemini_btn = ft.Button("Gemini Flash")

    def style_engine_buttons():
        model_btn.bgcolor = ENGINE_GRAY_ACTIVE if state["engine"] == "model" else ENGINE_GRAY
        gemini_btn.bgcolor = ENGINE_GRAY_ACTIVE if state["engine"] == "gemini" else ENGINE_GRAY
        model_btn.color = gemini_btn.color = INK
        model_btn.elevation = gemini_btn.elevation = 0
        page.update()

    # ------------------------------------------------------------- API key manager
    # Multiple keys are supported: if one key hits a quota/error mid-scan the
    # next key in the list takes over automatically.
    key_field = ft.TextField(password=True, can_reveal_password=True,
                             label="Gemini API key", hint_text="Paste a new API key",
                             dense=True)
    keys_list = ft.Column(spacing=4)

    def _mask(k: str) -> str:
        return k[:6] + "..." + k[-4:] if len(k) > 12 else "****"

    def refresh_keys_list():
        keys_list.controls = [
            ft.Row([
                ft.Icon(ft.Icons.KEY, size=14, color=TEAL),
                ft.Text(f"Key {i+1}: {_mask(k)}", size=12,
                        font_family="Consolas", expand=True),
                ft.IconButton(ft.Icons.CLOSE, icon_size=14, tooltip="Remove key",
                              icon_color=INK_SOFT,
                              on_click=lambda e, i=i: remove_key(i)),
            ], spacing=6)
            for i, k in enumerate(state["api_keys"])
        ]
        page.update()

    def remove_key(i: int):
        state["api_keys"].pop(i)
        save_keys(state["api_keys"])
        refresh_keys_list()
        if not state["api_keys"]:
            state["engine"] = None
            style_engine_buttons()
        log(f"Key {i+1} removed.")

    def add_key(e=None):
        k = key_field.value.strip()
        if not k:
            return
        if k in state["api_keys"]:
            log("That key is already saved.")
        else:
            state["api_keys"].append(k)
            save_keys(state["api_keys"])
            log(f"Key {len(state['api_keys'])} added ({_mask(k)}).", ok=True)
        key_field.value = ""
        refresh_keys_list()
        state["engine"] = "gemini"
        style_engine_buttons()

    def close_key_dialog(e=None):
        key_dialog.open = False
        if state["api_keys"]:
            state["engine"] = "gemini"
            style_engine_buttons()
            log(f"Engine: Gemini Flash ready ({len(state['api_keys'])} key(s) saved).")
        page.update()

    key_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Gemini API keys", size=15, weight=ft.FontWeight.W_600),
        content=ft.Container(
            ft.Column([
                keys_list,
                ft.Row([key_field,
                        ft.IconButton(ft.Icons.ADD_CIRCLE_OUTLINE,
                                      icon_color=TEAL, tooltip="Add key",
                                      on_click=add_key)], spacing=6),
                ft.Text("If one key hits its daily limit, the next key is "
                        "used automatically.", size=11, color=INK_SOFT),
            ], tight=True, spacing=10),
            width=380,
        ),
        actions=[
            ft.FilledButton("Done", on_click=close_key_dialog),
        ],
    )

    def pick_gemini(e):
        # Always open the manager so more keys can be added later.
        refresh_keys_list()
        key_dialog.open = True
        page.show_dialog(key_dialog)
        if state["api_keys"]:
            state["engine"] = "gemini"
            style_engine_buttons()
        page.update()

    def pick_model(e):
        state["engine"] = "model"
        style_engine_buttons()
        log("Local 'Model' engine selected — backend not implemented yet.")

    model_btn.on_click = pick_model
    gemini_btn.on_click = pick_gemini

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
    def do_scan(e):
        if not state["image_path"]:
            log("Load an image before scanning.")
            return
        if state["engine"] != "gemini" or not state["api_keys"]:
            log("Choose the Gemini Flash engine and add an API key first.")
            pick_gemini(None)
            return
        page.run_task(run_gemini)

    async def run_gemini():
        from google import genai
        from google.genai import types

        log("Gemini multimodal parser active: scanning tabular data...")
        page.update()
        try:
            img_bytes = Path(state["image_path"]).read_bytes()
            suffix = Path(state["image_path"]).suffix.lower().lstrip(".")
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                    "webp": "image/webp", "bmp": "image/bmp"}.get(suffix, "image/jpeg")
            contents = [types.Part.from_bytes(data=img_bytes, mime_type=mime),
                        PROMPT]

            def _retryable(err: Exception) -> bool:
                s = str(err)
                return any(t in s for t in
                           ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
                            "quota"))

            # Try each saved key in turn; on a retryable error (busy / quota)
            # wait briefly, then move on to the next key.
            resp = None
            last_err = None
            for ki, key in enumerate(state["api_keys"]):
                if len(state["api_keys"]) > 1:
                    log(f"Using key {ki + 1} of {len(state['api_keys'])}...")
                client = genai.Client(api_key=key)
                for attempt in range(3):
                    try:
                        resp = client.models.generate_content(
                            model=GEMINI_MODEL, contents=contents)
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
                if ki + 1 < len(state["api_keys"]):
                    log(f"Key {ki + 1} exhausted — switching to key {ki + 2}.")
            if resp is None:
                raise last_err
        except Exception as ex:
            log(f"Gemini request failed: {ex}")
            return
        try:
            rows = parse_gemini_json(resp.text or "")
        except Exception as ex:
            log(f"Could not parse Gemini response ({ex}). Raw reply logged below.")
            log((resp.text or "")[:300])
            return
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
                model_btn, gemini_btn,
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=10,
        ),
        bgcolor=CARD, padding=ft.Padding(16, 10, 16, 10),
        border=ft.Border.only(bottom=ft.BorderSide(1, LINE)),
        margin=ft.Margin(-20, -20, -20, 14),
    )

    left_card = card(
        ft.Column(
            [
                ft.Text("Image Preview", size=15, weight=ft.FontWeight.W_600),
                img_frame,
                ft.Row(
                    [
                        ft.OutlinedButton(
                            "Load Image",
                            icon=ft.Icons.UPLOAD_FILE_OUTLINED,
                            on_click=load_image,
                            expand=True,
                            style=ft.ButtonStyle(padding=ft.Padding(8, 10, 8, 10)),
                        ),
                        ft.Button(
                            "SCAN", icon=ft.Icons.QR_CODE_SCANNER,
                            bgcolor=TEAL, color="white", elevation=0,
                            expand=True,
                            style=ft.ButtonStyle(
                                padding=ft.Padding(8, 10, 8, 10),
                                text_style=ft.TextStyle(size=14, weight=ft.FontWeight.W_600),
                            ),
                            on_click=do_scan,
                        ),
                        ft.OutlinedButton(
                            "Clear", icon=ft.Icons.DELETE_OUTLINE,
                            on_click=do_clear,
                            expand=True,
                            style=ft.ButtonStyle(padding=ft.Padding(8, 10, 8, 10)),
                        ),
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

    style_engine_buttons()
    if state["api_keys"]:
        state["engine"] = "gemini"
        style_engine_buttons()
        log(f"App initialized. Engine: Gemini Flash "
            f"({len(state['api_keys'])} saved key(s)).")
    else:
        log("App initialized. Select 'Gemini Flash' and add your API key.")
    log("Waiting for image payload...")


if __name__ == "__main__":
    ft.run(main)
