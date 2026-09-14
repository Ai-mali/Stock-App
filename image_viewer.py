"""Standalone photo viewer window for the extractor app.

Launched as a subprocess: python image_viewer.py <image_path>
A separate OS window — freely movable/resizable like Telegram's viewer.
Drag to pan, double-click or +/- to zoom, mouse wheel zooms too.
"""
import sys

import flet as ft


def main(page: ft.Page):
    page.title = "Photo Viewer"
    page.bgcolor = "#111111"
    page.padding = 0
    page.window.width = 900
    page.window.height = 700
    page.window.min_width = 320
    page.window.min_height = 240

    img_path = sys.argv[1] if len(sys.argv) > 1 else ""
    state = {"scale": 1.0, "dx": 0.0, "dy": 0.0, "lx": 0.0, "ly": 0.0}
    img = ft.Image(src=img_path, fit=ft.BoxFit.CONTAIN, expand=True,
                   gapless_playback=True)

    def apply():
        w = float(page.width or 900)
        h = float(page.height or 600)
        limit = max(0.0, state["scale"] - 1.0) + 0.3
        fx = max(-limit, min(limit, state["dx"] / w))
        fy = max(-limit, min(limit, state["dy"] / h))
        img.scale = ft.Scale(state["scale"])
        img.offset = ft.Offset(fx, fy)
        img.update()

    def pan_start(e):
        state["lx"] = state["ly"] = 0.0

    def pan(e):
        d = e.global_delta or e.local_delta
        if not d:
            return
        state["dx"] += d.x - state["lx"]
        state["dy"] += d.y - state["ly"]
        state["lx"], state["ly"] = d.x, d.y
        apply()

    def zoom_by(factor):
        state["scale"] = min(8.0, max(0.3, state["scale"] * factor))
        apply()

    def dbl(e):
        if state["scale"] > 1.01:
            state.update(scale=1.0, dx=0.0, dy=0.0)
        else:
            state["scale"] = 2.0
        apply()

    def wheel(e):
        d = getattr(e, "scroll_delta", None)
        if d is None:
            return
        zoom_by(1.12 if d.y < 0 else 0.89)

    def fit(e=None):
        state.update(scale=1.0, dx=0.0, dy=0.0)
        apply()

    page.add(ft.Stack(
        expand=True,
        controls=[
            ft.GestureDetector(
                content=ft.Container(
                    img, expand=True, alignment=ft.Alignment(0, 0),
                    clip_behavior=ft.ClipBehavior.HARD_EDGE),
                on_pan_start=pan_start,
                on_pan_update=pan,
                on_double_tap=dbl,
                on_scroll=wheel,
            ),
            ft.Container(
                ft.Row([
                    ft.IconButton(ft.Icons.REMOVE, icon_color="white",
                                  bgcolor="#00000066",
                                  on_click=lambda e: zoom_by(0.75)),
                    ft.IconButton(ft.Icons.ADD, icon_color="white",
                                  bgcolor="#00000066",
                                  on_click=lambda e: zoom_by(1.33)),
                    ft.IconButton(ft.Icons.FIT_SCREEN, icon_color="white",
                                  bgcolor="#00000066", tooltip="Fit",
                                  on_click=fit),
                ], spacing=4, tight=True),
                bottom=12, right=12,
            ),
            ft.Container(
                ft.Text("Drag to move · double-click or wheel to zoom",
                        color="#FFFFFF99", size=11),
                bottom=14, left=14,
            ),
        ],
    ))


if __name__ == "__main__":
    ft.app(main)
