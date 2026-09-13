"""Model & Serial Extractor — Daikin delivery-list photo scanner.

Flet desktop app. Load a photo of a Daikin parts/delivery list, send it to
Gemini vision, and get back a Model | Serial table ready for Stock In.

Run:  python extractor_app.py
Build: pyinstaller --noconsole --onefile --name ModelSerialExtractor extractor_app.py
"""

import csv
import datetime
import json
import re
import sys
from pathlib import Path

import flet as ft

GEMINI_MODEL = "gemini-3.5-flash"
CONFIG_PATH = Path.home() / ".model_serial_extractor.json"

PROMPT = """Extract model number and serial number from this Daikin equipment parts list.
No = the line item number in the leftmost No column.
Model = equipment model code from the Material column.
Serial = serial number after Serial No:.
If multiple serial numbers for same model, separate with comma.
Keep serial ranges as written, e.g. "K016634 - K016636".
Desc = the description text of the line item.
Ignore handwritten checkmarks next to serials.
Return ONLY JSON array, one object per row including rows without serials:
[{"no":"","model":"","serial":"","desc":""}]
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
        if model and (serial or desc):
            rows.append({"no": no, "model": model.upper(),
                         "serial": serial.upper(), "desc": desc})
    return rows


def load_saved_key() -> str:
    try:
        return json.loads(CONFIG_PATH.read_text()).get("api_key", "")
    except Exception:
        return ""


def save_key(key: str) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps({"api_key": key}))
    except Exception:
        pass


def card(content, padding=18):
    return ft.Container(
        content=content,
        bgcolor=CARD,
        border=ft.Border.all(1, LINE),
        border_radius=10,
        padding=padding,
    )


def main(page: ft.Page):
    page.title = "Model & Serial Extractor"
    page.bgcolor = BG
    page.padding = 20
    page.theme = ft.Theme(font_family="Segoe UI")
    if page.window:
        page.window.width = 1180
        page.window.height = 780

    # ------------------------------------------------------------- state
    state = {
        "api_key": load_saved_key(),
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
        height=420,
        border_radius=8,
        border=ft.Border.all(1, LINE),
        bgcolor="#FAFBFC",
        alignment=ft.Alignment(0, 0),
    )
    img_frame = ft.Container(
        content=ft.Stack([img_empty, img_preview]),
        height=420, border_radius=8,
    )

    badge = ft.Container(
        content=ft.Row(
            [ft.Icon(ft.Icons.CHECK_CIRCLE, size=14, color=TEAL),
             ft.Text("0 models found", color=TEAL, size=12, weight=ft.FontWeight.W_500)],
            spacing=4, tight=True),
        bgcolor=TEAL_BG, border_radius=20, padding=ft.Padding(10, 4, 10, 4),
        visible=False,
    )

    def col_label(text):
        return ft.Text(text, weight=ft.FontWeight.W_500, color=INK_SOFT, size=12)

    no_col = ft.DataColumn(label=col_label("No"))
    model_col = ft.DataColumn(label=col_label("Model"))
    serial_col = ft.DataColumn(label=col_label("Serial"))
    results_table = ft.DataTable(
        columns=[no_col, model_col, serial_col],
        rows=[],
        heading_row_color="#F5F7F8",
        border=ft.Border.all(1, LINE),
        border_radius=8,
        column_spacing=24,
        width=None,
    )
    results_scroll = ft.Column([results_table], scroll=ft.ScrollMode.AUTO, height=270)

    log_list = ft.ListView(spacing=2, height=140, auto_scroll=True)

    # ------------------------------------------------------------- helpers
    def log(msg: str, ok: bool = False):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        log_list.controls.append(
            ft.Text(f"[{ts}] {msg}", size=12.5,
                    color=TEAL if ok else INK, font_family="Consolas"))
        page.update()

    def set_results(rows: list[dict]):
        state["rows"] = rows
        results_table.rows = [
            ft.DataRow(cells=[
                ft.DataCell(ft.Text(r["no"], size=13, color=INK_SOFT)),
                ft.DataCell(ft.Text(r["model"], size=13)),
                ft.DataCell(
                    ft.Text(r["serial"] or r["desc"], size=13,
                            italic=not r["serial"],
                            color=INK if r["serial"] else INK_SOFT)
                ),
            ]) for r in rows
        ]
        serial_count = sum(len([s for s in r["serial"].split(",") if s.strip()])
                           for r in rows if r["serial"])
        model_col.label.value = f"Model ({len(rows)})"
        serial_col.label.value = f"Serial ({serial_count})"
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

    # ------------------------------------------------------------- API key dialog
    key_field = ft.TextField(password=True, can_reveal_password=True,
                             label="Gemini API key", hint_text="Paste your API key",
                             dense=True)
    remember_cb = ft.Checkbox(label="Remember on this PC", value=False)

    def close_key_dialog(e=None):
        key_dialog.open = False
        page.update()

    def save_key_click(e):
        state["api_key"] = key_field.value.strip()
        if remember_cb.value and state["api_key"]:
            save_key(state["api_key"])
        key_dialog.open = False
        if state["api_key"]:
            state["engine"] = "gemini"
            log("Gemini API key set. Engine: Gemini Flash ready.")
        else:
            state["engine"] = None
            log("No API key entered — Gemini Flash disabled.")
        style_engine_buttons()
        page.update()

    key_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("Gemini API key", size=15, weight=ft.FontWeight.W_600),
        content=ft.Column([key_field, remember_cb], tight=True, spacing=10),
        actions=[
            ft.TextButton("Cancel", on_click=close_key_dialog),
            ft.FilledButton("Save", on_click=save_key_click),
        ],
    )

    def pick_gemini(e):
        if state["engine"] == "gemini" and state["api_key"]:
            return
        key_field.value = state["api_key"] or ""
        key_dialog.open = True
        page.show_dialog(key_dialog)
        if state["api_key"]:
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
                w.writerow(["No", "Model", "Serial"])
                for r in state["rows"]:
                    w.writerow([r["no"], r["model"], r["serial"] or r["desc"]])
            log(f"Exported {len(state['rows'])} row(s) to {target}", ok=True)
        except Exception as ex:
            log(f"CSV export failed: {ex}")

    # ------------------------------------------------------------- scan
    def do_scan(e):
        if not state["image_path"]:
            log("Load an image before scanning.")
            return
        if state["engine"] != "gemini" or not state["api_key"]:
            log("Choose the Gemini Flash engine and enter your API key first.")
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
            client = genai.Client(api_key=state["api_key"])
            resp = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[
                    types.Part.from_bytes(data=img_bytes, mime_type=mime),
                    PROMPT,
                ],
            )
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
                        ),
                        ft.Container(
                            ft.Button(
                                "SCAN", icon=ft.Icons.QR_CODE_SCANNER,
                                bgcolor=TEAL, color="white", elevation=0,
                                style=ft.ButtonStyle(
                                    padding=ft.Padding(28, 14, 28, 14),
                                    text_style=ft.TextStyle(size=14, weight=ft.FontWeight.W_600),
                                ),
                                on_click=do_scan,
                            ),
                            alignment=ft.Alignment(0, 0),
                        ),
                        ft.OutlinedButton(
                            "Clear", icon=ft.Icons.DELETE_OUTLINE,
                            on_click=do_clear,
                        ),
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
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
        )
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
                ),
            ],
            spacing=10,
        )
    )

    page.add(
        ft.Column(
            [
                header,
                ft.Row(
                    [
                        ft.Column([left_card], expand=5),
                        ft.Column([results_card, log_card], expand=5, spacing=16),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.START,
                    spacing=16,
                    expand=True,
                ),
            ],
            expand=True,
        )
    )

    style_engine_buttons()
    if state["api_key"]:
        state["engine"] = "gemini"
        style_engine_buttons()
        log("App initialized. Engine: Gemini Flash (saved key loaded).")
    else:
        log("App initialized. Select 'Gemini Flash' and enter your API key.")
    log("Waiting for image payload...")


if __name__ == "__main__":
    ft.run(main)
