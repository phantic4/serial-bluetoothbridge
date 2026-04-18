from __future__ import annotations

import platform
from pathlib import Path

import PyInstaller.__main__


APP_NAME = "SerialBluetoothBridge"
SCRIPT = "serial_bluetooth_bridge.pyw"


def main() -> int:
    system = platform.system()
    args = [
        SCRIPT,
        "--name",
        APP_NAME,
        "--onefile",
        "--clean",
        "--noconfirm",
    ]

    if system in {"Windows", "Darwin"}:
        args.append("--windowed")

    PyInstaller.__main__.run(args)

    suffix = ".exe" if system == "Windows" else ""
    print(f"Built {Path('dist') / (APP_NAME + suffix)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
