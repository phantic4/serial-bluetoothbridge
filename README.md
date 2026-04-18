# bluetooth bridge

A serial Bluetooth Bridge python script

## Windows Build

```powershell
cd 

py -3 -m venv .venv

.\.venv\Scripts\python.exe -m pip install --upgrade pip

.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt

.\.venv\Scripts\python.exe build_exe.py
```



```text
dist\SerialBluetoothBridge.exe
```

## Linux Build

```bash
cd 

python3 -m venv .venv

./.venv/bin/python -m pip install --upgrade pip

./.venv/bin/python -m pip install -r requirements.txt -r requirements-build.txt

./.venv/bin/python build_exe.py
```


```text
dist/SerialBluetoothBridge
```

## Running From Source

Terminal mode:

```bash
python serial_bluetooth_bridge.py --name DEVICE_NAME
```

GUI mode:

```bash
python serial_bluetooth_bridge.py --gui
```

Scan only:

```bash
python serial_bluetooth_bridge.py --scan-only --scan-timeout 15
```
