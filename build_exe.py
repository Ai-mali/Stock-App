"""Build standalone Windows executable for AC Stock Tracker using PyInstaller."""

import sys
from pathlib import Path
import PyInstaller.__main__

BASE = Path(__file__).parent


def build():
    html_file = BASE / "ac-stock-tracker.html"
    if not html_file.exists():
        print(f"Error: {html_file} not found!")
        sys.exit(1)

    args = [
        str(BASE / "backend.py"),
        "--name=DaikinStockTracker",
        "--onefile",
        "--clean",
        f"--add-data={html_file};.",
        "--hidden-import=uvicorn.logging",
        "--hidden-import=uvicorn.loops",
        "--hidden-import=uvicorn.loops.auto",
        "--hidden-import=uvicorn.protocols",
        "--hidden-import=uvicorn.protocols.http",
        "--hidden-import=uvicorn.protocols.http.auto",
        "--hidden-import=uvicorn.protocols.websockets",
        "--hidden-import=uvicorn.protocols.websockets.auto",
        "--hidden-import=uvicorn.lifespans",
        "--hidden-import=uvicorn.lifespans.on",
        "--hidden-import=openpyxl",
        "--hidden-import=fastapi",
        "--hidden-import=pydantic",
        "--collect-submodules=uvicorn",
        "--collect-submodules=openpyxl",
    ]
    print("Building DaikinStockTracker.exe...")
    PyInstaller.__main__.run(args)
    print("\nBuild complete! Executable is in: dist/DaikinStockTracker.exe")


if __name__ == "__main__":
    build()
