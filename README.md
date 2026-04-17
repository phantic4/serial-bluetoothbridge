# BLE Robot Controller

This program controls an Arduino robot through a Bluetooth Low Energy serial-style module. The computer connects to the BLE module, sends typed robot commands, and shows data that the Arduino sends back.

The GUI version is meant for school or lab computers where opening a terminal is not allowed. Build the app on a computer where Python is available, then copy the finished program to the robot-control computer.

## Windows Build

```powershell
cd "c:\Users\shri.000\Desktop\robotics\bluetooth script"
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
.\.venv\Scripts\python.exe build_exe.py
```

Windows output:

```text
dist\BLERobotController.exe
```

## Linux Build

```bash
cd "/path/to/bluetooth script"
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt -r requirements-build.txt
./.venv/bin/python build_exe.py
```

Linux output:

```text
dist/BLERobotController
```

## Running From Source

Terminal mode:

```bash
python ble_robot_controller.py --name DEVICE_NAME
```

GUI mode:

```bash
python ble_robot_controller.py --gui
```

Scan only:

```bash
python ble_robot_controller.py --scan-only --scan-timeout 15
```
