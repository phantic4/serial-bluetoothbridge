from __future__ import annotations

import sys

from ble_robot_controller import build_parser, run_gui


def main() -> int:
    parser = build_parser()
    args = parser.parse_args(["--gui", *sys.argv[1:]])
    run_gui(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
