"""
Checks that the system meets resilient-finch requirements.

Audio device setup is no longer needed. The app creates a CoreAudio process
tap at startup (macOS 14.2+) to capture system audio directly, with no
virtual devices or BlackHole required.
"""

from __future__ import annotations

import platform
import sys


def _macos_version() -> tuple[int, int]:
    parts = platform.mac_ver()[0].split(".")
    return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0


def setup() -> None:
    major, minor = _macos_version()
    if (major, minor) < (14, 2):
        sys.exit(
            f"Error: macOS 14.2 or later is required (you have {major}.{minor}).\n"
            "The process tap API used to capture system audio was introduced in macOS 14.2."
        )
    print(f"macOS {major}.{minor} — OK")
    print()
    print("No audio device setup required.")
    print("On first run, macOS will prompt for 'Screen & System Audio Recording' permission.")
    print()
    print("Run the app with:  uv run python main.py")


if __name__ == "__main__":
    print("resilient-finch system check")
    print("─" * 30)
    setup()
