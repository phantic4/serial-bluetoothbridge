from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import queue
import sys
import threading
from typing import Iterable

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData


COMMON_UART_CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

WRITE_PROPS = {"write", "write-without-response"}
NOTIFY_PROPS = {"notify", "indicate"}


class BridgeStop(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class UartCharacteristics:
    tx: BleakGATTCharacteristic
    rx: BleakGATTCharacteristic
    write_with_response: bool


def normalize_uuid(value: str) -> str:
    value = value.strip().lower()
    if len(value) == 4:
        return f"0000{value}-0000-1000-8000-00805f9b34fb"
    return value


def characteristic_props(char: BleakGATTCharacteristic) -> set[str]:
    return {prop.lower() for prop in (char.properties or [])}


def can_write(char: BleakGATTCharacteristic) -> bool:
    return bool(characteristic_props(char) & WRITE_PROPS)


def can_notify(char: BleakGATTCharacteristic) -> bool:
    return bool(characteristic_props(char) & NOTIFY_PROPS)


def choose_write_response(
    char: BleakGATTCharacteristic, write_with_response_arg: str
) -> bool:
    props = characteristic_props(char)
    if write_with_response_arg == "yes":
        return True
    if write_with_response_arg == "no":
        return False
    if "write-without-response" in props:
        return False
    return "write" in props


def find_char_by_uuid(
    chars: Iterable[BleakGATTCharacteristic], uuid: str
) -> BleakGATTCharacteristic | None:
    uuid = normalize_uuid(uuid)
    for char in chars:
        if normalize_uuid(char.uuid) == uuid:
            return char
    return None


def all_characteristics(client: BleakClient) -> list[BleakGATTCharacteristic]:
    chars: list[BleakGATTCharacteristic] = []
    for service in client.services:
        chars.extend(service.characteristics)
    return chars


def print_services(client: BleakClient) -> None:
    print(format_services(client))


def format_services(client: BleakClient) -> str:
    lines = ["", "Discovered GATT services and characteristics:"]
    for service in client.services:
        lines.append(f"  Service {service.uuid}  {service.description}")
        for char in service.characteristics:
            props = ", ".join(char.properties or [])
            lines.append(f"    Char {char.uuid}  handle={char.handle}  props=[{props}]")
    lines.append("")
    return "\n".join(lines)


def resolve_uart_characteristics(
    client: BleakClient,
    service_uuid: str | None,
    tx_uuid: str | None,
    rx_uuid: str | None,
    write_with_response_arg: str,
) -> UartCharacteristics:
    services = list(client.services)
    if service_uuid:
        service_uuid = normalize_uuid(service_uuid)
        services = [service for service in services if normalize_uuid(service.uuid) == service_uuid]
        if not services:
            print_services(client)
            raise RuntimeError(f"Service UUID was not found: {service_uuid}")

    chars: list[BleakGATTCharacteristic] = []
    for service in services:
        chars.extend(service.characteristics)

    tx = find_char_by_uuid(chars, tx_uuid) if tx_uuid else None
    rx = find_char_by_uuid(chars, rx_uuid) if rx_uuid else None

    if tx_uuid and tx is None:
        raise RuntimeError(f"TX characteristic was not found: {tx_uuid}")
    if rx_uuid and rx is None:
        raise RuntimeError(f"RX characteristic was not found: {rx_uuid}")

    if tx is None or rx is None:
        common_uart_char = find_char_by_uuid(chars, COMMON_UART_CHAR_UUID)
        if common_uart_char is not None:
            if tx is None and can_write(common_uart_char):
                tx = common_uart_char
            if rx is None and can_notify(common_uart_char):
                rx = common_uart_char

    if tx is None or rx is None:
        for service in services:
            service_chars = list(service.characteristics)
            writable = [char for char in service_chars if can_write(char)]
            notifying = [char for char in service_chars if can_notify(char)]
            if writable and notifying:
                tx = tx or writable[0]
                rx = rx or notifying[0]
                break

    if tx is None:
        writable = [char for char in chars if can_write(char)]
        if writable:
            tx = writable[0]

    if rx is None:
        notifying = [char for char in chars if can_notify(char)]
        if notifying:
            rx = notifying[0]

    if tx is None or rx is None:
        print_services(client)
        raise RuntimeError(
            "Could not find UART-like BLE characteristics. "
            "Pass --tx-uuid and --rx-uuid after checking --list-services."
        )

    if not can_write(tx):
        raise RuntimeError(f"Selected TX characteristic is not writable: {tx.uuid}")
    if not can_notify(rx):
        raise RuntimeError(f"Selected RX characteristic does not notify/indicate: {rx.uuid}")

    return UartCharacteristics(
        tx=tx,
        rx=rx,
        write_with_response=choose_write_response(tx, write_with_response_arg),
    )


def device_name(device: BLEDevice, adv: AdvertisementData | None = None) -> str:
    if adv is not None and adv.local_name:
        return adv.local_name
    return device.name or "(unnamed)"


async def scan_devices(timeout: float) -> list[tuple[BLEDevice, AdvertisementData]]:
    print(f"Scanning for BLE devices for {timeout:.1f} seconds...")
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    devices = list(found.values())
    devices.sort(key=lambda item: ((item[1].rssi if item[1].rssi is not None else -999)), reverse=True)
    if not devices:
        print("No BLE devices found.")
        return []

    print("\nNearby BLE devices:")
    for index, (device, adv) in enumerate(devices, start=1):
        name = device_name(device, adv)
        rssi = adv.rssi if adv.rssi is not None else "?"
        service_hint = ""
        if adv.service_uuids:
            service_hint = " services=" + ",".join(adv.service_uuids[:3])
        print(f"  {index:2d}. {name:24s} {device.address:20s} RSSI={rssi}{service_hint}")
    print()
    return devices


async def select_device(args: argparse.Namespace) -> BLEDevice:
    if args.address or args.name:
        wanted_address = args.address.lower() if args.address else None
        wanted_name = args.name.lower() if args.name else None

        def matches(device: BLEDevice, adv: AdvertisementData) -> bool:
            name = device_name(device, adv).lower()
            if wanted_address and device.address.lower() == wanted_address:
                return True
            if wanted_name and wanted_name in name:
                return True
            return False

        label = args.address or args.name
        print(f"Looking for BLE device: {label}")
        device = await BleakScanner.find_device_by_filter(
            matches, timeout=args.scan_timeout
        )
        if device is None:
            raise RuntimeError(f"Could not find BLE device matching {label!r}.")
        return device

    while True:
        devices = await scan_devices(args.scan_timeout)
        if not devices:
            choice = input("Press Enter to rescan, or q to quit: ").strip().lower()
            if choice in {"q", "quit", "exit"}:
                raise BridgeStop
            continue

        choice = input(
            "Select BLE module by number or address, r to rescan, q to quit: "
        ).strip()
        if not choice or choice.lower() == "r":
            continue
        if choice.lower() in {"q", "quit", "exit"}:
            raise BridgeStop

        if choice.isdigit():
            index = int(choice)
            if 1 <= index <= len(devices):
                return devices[index - 1][0]

        for device, _adv in devices:
            if device.address.lower() == choice.lower():
                return device

        print(f"Selection not recognized: {choice}")


class ConsoleLineReader:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        while True:
            line = sys.stdin.readline()
            if line == "":
                self.loop.call_soon_threadsafe(self.queue.put_nowait, None)
                return
            self.loop.call_soon_threadsafe(self.queue.put_nowait, line)


def line_ending_bytes(kind: str) -> bytes:
    return {
        "none": b"",
        "lf": b"\n",
        "cr": b"\r",
        "crlf": b"\r\n",
    }[kind]


async def write_ble_chunks(
    client: BleakClient,
    char: BleakGATTCharacteristic,
    data: bytes,
    response: bool,
    chunk_size: int,
    chunk_delay: float,
) -> None:
    if chunk_size <= 0:
        chunk_size = len(data) or 1

    for start in range(0, len(data), chunk_size):
        chunk = data[start : start + chunk_size]
        await client.write_gatt_char(char, chunk, response=response)
        if chunk_delay > 0:
            await asyncio.sleep(chunk_delay)


async def console_to_ble_loop(
    console: ConsoleLineReader,
    client: BleakClient,
    uart: UartCharacteristics,
    args: argparse.Namespace,
    stop_event: asyncio.Event,
) -> None:
    print("Type text and press Enter to send. Commands: /services, /quit")
    suffix = line_ending_bytes(args.line_ending)
    while not stop_event.is_set():
        line = await console.queue.get()
        if line is None:
            stop_event.set()
            raise BridgeStop

        text = line.rstrip("\r\n")
        command = text.strip().lower()
        if command in {"/quit", "/exit"}:
            stop_event.set()
            raise BridgeStop
        if command == "/services":
            print_services(client)
            continue
        if command == "/help":
            print("Commands: /services prints GATT UUIDs, /quit exits.")
            continue

        payload = text.encode(args.encoding, errors="replace") + suffix
        try:
            await write_ble_chunks(
                client,
                uart.tx,
                payload,
                uart.write_with_response,
                args.chunk_size,
                args.chunk_delay,
            )
        except Exception as exc:
            print(f"\n[BLE WRITE ERROR] {exc}")
            raise


async def connected_session(
    device: BLEDevice,
    args: argparse.Namespace,
    stop_event: asyncio.Event,
    console: ConsoleLineReader,
) -> None:
    disconnected_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_disconnect(_client: BleakClient) -> None:
        loop.call_soon_threadsafe(disconnected_event.set)

    print(f"Connecting to {device.name or '(unnamed)'} at {device.address}...")
    client = BleakClient(
        device,
        disconnected_callback=on_disconnect,
        timeout=args.connect_timeout,
    )

    try:
        await client.connect()
        print(f"Connected: {client.name} ({client.address})")

        if args.list_services:
            print_services(client)

        uart = resolve_uart_characteristics(
            client,
            args.service_uuid,
            args.tx_uuid,
            args.rx_uuid,
            args.write_with_response,
        )

        mode = "write-with-response" if uart.write_with_response else "write-without-response"
        print(f"TX characteristic: {uart.tx.uuid} [{mode}]")
        print(f"RX characteristic: {uart.rx.uuid} [notify/indicate]")

        def handle_rx(_sender: BleakGATTCharacteristic, data: bytearray) -> None:
            raw = bytes(data)
            text = raw.decode(args.encoding, errors="replace")
            print(text, end="", flush=True)

        await client.start_notify(uart.rx, handle_rx)
        print("Bridge is running.")

        tasks: list[asyncio.Task[object]] = [
            asyncio.create_task(disconnected_event.wait()),
            asyncio.create_task(stop_event.wait()),
        ]

        tasks.append(asyncio.create_task(console_to_ble_loop(console, client, uart, args, stop_event)))

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        for task in done:
            exc = task.exception()
            if isinstance(exc, BridgeStop):
                raise exc
            if exc:
                raise exc

        if disconnected_event.is_set() and not stop_event.is_set():
            print("\nDisconnected from BLE device.")
    finally:
        with contextlib.suppress(Exception):
            if client.is_connected:
                await client.stop_notify(uart.rx)
        with contextlib.suppress(Exception):
            if client.is_connected:
                await client.disconnect()


async def run(args: argparse.Namespace) -> None:
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    console = ConsoleLineReader(loop)
    console.start()

    device = await select_device(args)

    while not stop_event.is_set():
        try:
            await connected_session(device, args, stop_event, console)
            if args.no_reconnect or stop_event.is_set():
                break
        except BridgeStop:
            break
        except Exception as exc:
            if args.no_reconnect:
                raise
            print(f"\n[ERROR] {exc}")

        if stop_event.is_set() or args.no_reconnect:
            break

        print(f"Retrying in {args.reconnect_delay:.1f} seconds...")
        await asyncio.sleep(args.reconnect_delay)

        if args.address or args.name:
            with contextlib.suppress(Exception):
                device = await select_device(args)

    print("Bridge stopped.")


class GuiBleTerminal:
    def __init__(self, args: argparse.Namespace) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.args = args
        self.root = tk.Tk()
        self.root.title("Serial Bluetooth Bridge")
        self.root.geometry("820x560")
        self.root.minsize(620, 420)
        self.colors = {
            "bg": "#000000",
            "panel": "#050709",
            "terminal": "#000000",
            "blue": "#00c8ff",
            "orange": "#ff8a00",
            "green": "#39ff14",
            "red": "#ff2d55",
            "purple": "#c77dff",
            "white": "#f4f7fb",
            "black": "#000000",
        }

        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self.ui_queue: queue.Queue[tuple[object, tuple[object, ...], dict[str, object]]] = queue.Queue()

        self.devices: list[tuple[BLEDevice, AdvertisementData]] = []
        self.client: BleakClient | None = None
        self.uart: UartCharacteristics | None = None
        self.connect_task: asyncio.Future[object] | None = None
        self.tcp_server: asyncio.AbstractServer | None = None
        self.tcp_clients: set[asyncio.StreamWriter] = set()
        self.manual_disconnect = False
        self.auto_reconnect = not args.no_reconnect

        self.device_var = tk.StringVar()
        self.target_var = tk.StringVar(value=args.name or args.address or "")
        self.line_ending_var = tk.StringVar(value=args.line_ending)
        self.status_var = tk.StringVar(value="Ready")
        self.tcp_host_var = tk.StringVar(value=args.tcp_host)
        self.tcp_port_var = tk.StringVar(value=str(args.tcp_port))
        self.tcp_status_var = tk.StringVar(value="TCP stopped")
        self.auto_reconnect_var = tk.BooleanVar(value=self.auto_reconnect)

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _build_ui(self) -> None:
        tk = self.tk
        ttk = self.ttk
        colors = self.colors

        self.root.configure(bg=colors["bg"])
        style = ttk.Style(self.root)
        with contextlib.suppress(Exception):
            style.theme_use("clam")
        style.configure("Bridge.TFrame", background=colors["bg"])
        style.configure("BridgePanel.TFrame", background=colors["panel"])
        style.configure(
            "Bridge.TLabel",
            background=colors["bg"],
            foreground=colors["blue"],
        )
        style.configure(
            "Bridge.TCheckbutton",
            background=colors["bg"],
            foreground=colors["green"],
            focuscolor=colors["bg"],
        )
        style.map(
            "Bridge.TCheckbutton",
            background=[("active", colors["bg"])],
            foreground=[("active", colors["blue"])],
        )
        style.configure(
            "Bridge.TCombobox",
            fieldbackground=colors["panel"],
            background=colors["panel"],
            foreground=colors["green"],
            arrowcolor=colors["orange"],
        )
        style.configure(
            "Bridge.Vertical.TScrollbar",
            background=colors["blue"],
            troughcolor=colors["bg"],
            bordercolor=colors["bg"],
            arrowcolor=colors["black"],
        )

        def make_button(parent: object, text: str, command: object) -> object:
            return tk.Button(
                parent,
                text=text,
                command=command,
                bg=colors["orange"],
                fg=colors["black"],
                activebackground=colors["blue"],
                activeforeground=colors["black"],
                relief=tk.FLAT,
                borderwidth=0,
                padx=14,
                pady=7,
                font=("Segoe UI", 10, "bold"),
            )

        outer = ttk.Frame(self.root, padding=10, style="Bridge.TFrame")
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        top = ttk.Frame(outer, style="Bridge.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        make_button(top, "Scan", self.scan).grid(row=0, column=0, padx=(0, 6))
        self.device_combo = ttk.Combobox(top, textvariable=self.device_var, state="readonly", style="Bridge.TCombobox")
        self.device_combo.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        make_button(top, "Connect", self.connect).grid(row=0, column=2, padx=(0, 6))
        make_button(top, "Disconnect", self.disconnect).grid(row=0, column=3)

        target = ttk.Frame(outer, style="Bridge.TFrame")
        target.grid(row=1, column=0, sticky="ew", pady=(8, 8))
        target.columnconfigure(1, weight=1)
        ttk.Label(target, text="Name/address", style="Bridge.TLabel").grid(row=0, column=0, padx=(0, 6))
        tk.Entry(
            target,
            textvariable=self.target_var,
            bg=colors["panel"],
            fg=colors["green"],
            insertbackground=colors["blue"],
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground=colors["blue"],
            highlightcolor=colors["orange"],
        ).grid(row=0, column=1, sticky="ew", padx=(0, 10), ipady=5)
        ttk.Label(target, text="Line ending", style="Bridge.TLabel").grid(row=0, column=2, padx=(0, 6))
        ttk.Combobox(
            target,
            textvariable=self.line_ending_var,
            values=("none", "lf", "cr", "crlf"),
            width=7,
            state="readonly",
            style="Bridge.TCombobox",
        ).grid(row=0, column=3, padx=(0, 10), ipady=3)
        ttk.Checkbutton(
            target,
            text="Auto reconnect",
            variable=self.auto_reconnect_var,
            command=self._update_auto_reconnect,
            style="Bridge.TCheckbutton",
        ).grid(row=0, column=4)

        tcp_row = ttk.Frame(outer, style="Bridge.TFrame")
        tcp_row.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        tcp_row.columnconfigure(1, weight=1)
        ttk.Label(tcp_row, text="TCP listen IP", style="Bridge.TLabel").grid(row=0, column=0, padx=(0, 6))
        tk.Entry(
            tcp_row,
            textvariable=self.tcp_host_var,
            bg=colors["panel"],
            fg=colors["green"],
            insertbackground=colors["blue"],
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground=colors["blue"],
            highlightcolor=colors["orange"],
        ).grid(row=0, column=1, sticky="ew", padx=(0, 10), ipady=5)
        ttk.Label(tcp_row, text="Port", style="Bridge.TLabel").grid(row=0, column=2, padx=(0, 6))
        tk.Entry(
            tcp_row,
            textvariable=self.tcp_port_var,
            width=8,
            bg=colors["panel"],
            fg=colors["green"],
            insertbackground=colors["blue"],
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground=colors["blue"],
            highlightcolor=colors["orange"],
        ).grid(row=0, column=3, padx=(0, 10), ipady=5)
        make_button(tcp_row, "Start TCP", self.start_tcp_server).grid(row=0, column=4, padx=(0, 6))
        make_button(tcp_row, "Stop TCP", self.stop_tcp_server).grid(row=0, column=5, padx=(0, 10))
        ttk.Label(tcp_row, textvariable=self.tcp_status_var, style="Bridge.TLabel").grid(row=0, column=6, sticky="e")

        terminal_frame = ttk.Frame(outer, style="BridgePanel.TFrame")
        terminal_frame.grid(row=3, column=0, sticky="nsew")
        terminal_frame.columnconfigure(0, weight=1)
        terminal_frame.rowconfigure(0, weight=1)

        self.output = tk.Text(
            terminal_frame,
            wrap=tk.WORD,
            state=tk.DISABLED,
            bg=colors["terminal"],
            fg=colors["green"],
            insertbackground=colors["blue"],
            font=("Consolas", 10),
            relief=tk.FLAT,
            borderwidth=0,
            padx=10,
            pady=10,
        )
        self.output.tag_configure("rx", foreground=colors["green"])
        self.output.tag_configure("sent", foreground=colors["purple"])
        self.output.tag_configure("status", foreground=colors["blue"])
        self.output.tag_configure("blue", foreground=colors["blue"])
        self.output.tag_configure("error", foreground=colors["red"])
        self.output.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(terminal_frame, command=self.output.yview, style="Bridge.Vertical.TScrollbar")
        scroll.grid(row=0, column=1, sticky="ns")
        self.output.configure(yscrollcommand=scroll.set)

        send = ttk.Frame(outer, style="Bridge.TFrame")
        send.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        send.columnconfigure(0, weight=1)
        self.input_var = tk.StringVar()
        entry = tk.Entry(
            send,
            textvariable=self.input_var,
            bg=colors["panel"],
            fg=colors["purple"],
            insertbackground=colors["blue"],
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground=colors["blue"],
            highlightcolor=colors["orange"],
        )
        entry.grid(row=0, column=0, sticky="ew", padx=(0, 6), ipady=7)
        entry.bind("<Return>", lambda _event: self.send_line())
        make_button(send, "Send", self.send_line).grid(row=0, column=1, padx=(0, 6))
        make_button(send, "Clear", self.clear_output).grid(row=0, column=2)

        status = tk.Label(
            outer,
            textvariable=self.status_var,
            anchor="w",
            bg=colors["blue"],
            fg=colors["black"],
            padx=10,
            pady=7,
            font=("Segoe UI", 10, "bold"),
        )
        status.grid(row=5, column=0, sticky="ew", pady=(8, 0))

        self.append_output(
            "Serial Bluetooth bridge mode.\n"
            "Scan, connect to the BLE module, then type serial data here or start the TCP listener.\n\n",
            "status",
        )

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self) -> None:
        self.loop_thread.start()
        self.root.after(50, self._drain_ui_queue)
        if self.args.name or self.args.address:
            self.scan()
        self.root.mainloop()

    def post_ui(self, func: object, *args: object, **kwargs: object) -> None:
        self.ui_queue.put((func, args, kwargs))

    def _drain_ui_queue(self) -> None:
        while True:
            try:
                func, args, kwargs = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                func(*args, **kwargs)
            except Exception as exc:
                self.append_output(f"\n[GUI ERROR] {exc}\n", "error")
        self.root.after(50, self._drain_ui_queue)

    def _update_auto_reconnect(self) -> None:
        self.auto_reconnect = bool(self.auto_reconnect_var.get())

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def set_tcp_status(self) -> None:
        if self.tcp_server is None:
            self.tcp_status_var.set("TCP stopped")
        else:
            self.tcp_status_var.set(f"TCP clients: {len(self.tcp_clients)}")

    def append_output(self, text: str, tag: str = "rx") -> None:
        self.output.configure(state=self.tk.NORMAL)
        self.output.insert(self.tk.END, text, tag)
        self.output.see(self.tk.END)
        self.output.configure(state=self.tk.DISABLED)

    def clear_output(self) -> None:
        self.output.configure(state=self.tk.NORMAL)
        self.output.delete("1.0", self.tk.END)
        self.output.configure(state=self.tk.DISABLED)

    def run_coro(self, coro: object) -> asyncio.Future[object]:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def scan(self) -> None:
        self.set_status(f"Scanning for {self.args.scan_timeout:.1f} seconds...")
        self.append_output(f"Scanning for BLE devices for {self.args.scan_timeout:.1f} seconds...\n", "status")
        self.run_coro(self._scan())

    async def _scan(self) -> None:
        try:
            found = await BleakScanner.discover(
                timeout=self.args.scan_timeout,
                return_adv=True,
            )
            devices = list(found.values())
            devices.sort(
                key=lambda item: item[1].rssi if item[1].rssi is not None else -999,
                reverse=True,
            )
            self.post_ui(self._show_scan_results, devices)
        except Exception as exc:
            self.post_ui(self.set_status, "Scan failed")
            self.post_ui(self.append_output, f"[SCAN ERROR] {exc}\n", "error")

    def _show_scan_results(self, devices: list[tuple[BLEDevice, AdvertisementData]]) -> None:
        self.devices = devices
        values: list[str] = []
        for index, (device, adv) in enumerate(devices, start=1):
            name = device_name(device, adv)
            rssi = adv.rssi if adv.rssi is not None else "?"
            values.append(f"{index}. {name} | {device.address} | RSSI={rssi}")

        self.device_combo["values"] = values
        if values:
            self.device_combo.current(0)
            self.set_status(f"Found {len(values)} BLE device(s)")
            self.append_output("\nNearby BLE devices:\n", "status")
            for value in values:
                self.append_output(f"  {value}\n", "blue")
            self.append_output("\n", "status")
        else:
            self.set_status("No BLE devices found")
            self.append_output("No BLE devices found.\n\n", "error")

    def selected_device(self) -> BLEDevice | None:
        selection = self.device_combo.current()
        if 0 <= selection < len(self.devices):
            return self.devices[selection][0]
        return None

    def connect(self) -> None:
        if self.connect_task and not self.connect_task.done():
            self.append_output("A connection attempt is already running.\n", "status")
            return

        target_text = self.target_var.get().strip()
        device = self.selected_device()
        if not device and not target_text:
            self.append_output("Scan and select a device, or type a name/address first.\n", "error")
            return

        self.manual_disconnect = False
        self.connect_task = self.run_coro(self._connect_loop(device, target_text))

    def start_tcp_server(self) -> None:
        self.run_coro(self._start_tcp_server())

    def stop_tcp_server(self) -> None:
        self.run_coro(self._stop_tcp_server())

    async def _start_tcp_server(self) -> None:
        if self.tcp_server is not None:
            self.post_ui(self.append_output, "TCP listener is already running.\n", "status")
            return

        host = self.tcp_host_var.get().strip() or "127.0.0.1"
        try:
            port = int(self.tcp_port_var.get().strip())
            if port < 1 or port > 65535:
                raise ValueError
        except ValueError:
            self.post_ui(self.append_output, "TCP port must be 1-65535.\n", "error")
            return

        try:
            self.tcp_server = await asyncio.start_server(self._handle_tcp_client, host, port)
        except Exception as exc:
            self.tcp_server = None
            self.post_ui(self.append_output, f"[TCP ERROR] {exc}\n", "error")
            self.post_ui(self.set_tcp_status)
            return

        self.post_ui(self.append_output, f"TCP listener started on {host}:{port}\n", "status")
        self.post_ui(self.set_tcp_status)

    async def _stop_tcp_server(self) -> None:
        if self.tcp_server is not None:
            self.tcp_server.close()
            await self.tcp_server.wait_closed()
            self.tcp_server = None

        clients = list(self.tcp_clients)
        self.tcp_clients.clear()
        for writer in clients:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

        self.post_ui(self.append_output, "TCP listener stopped.\n", "status")
        self.post_ui(self.set_tcp_status)

    async def _handle_tcp_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        self.tcp_clients.add(writer)
        self.post_ui(self.append_output, f"TCP client connected: {peer}\n", "status")
        self.post_ui(self.set_tcp_status)

        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break

                text = data.decode(self.args.encoding, errors="replace")
                display_text = text if text.endswith(("\n", "\r")) else text + "\n"
                self.post_ui(self.append_output, f"TCP> {display_text}", "sent")
                ok = await self._send_payload(data, show_error=False)
                if not ok:
                    self.post_ui(self.append_output, "[TCP DROP] BLE module is not connected.\n", "error")
        except Exception as exc:
            self.post_ui(self.append_output, f"[TCP CLIENT ERROR] {exc}\n", "error")
        finally:
            self.tcp_clients.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            self.post_ui(self.append_output, f"TCP client disconnected: {peer}\n", "status")
            self.post_ui(self.set_tcp_status)

    async def _broadcast_tcp(self, data: bytes) -> None:
        if not self.tcp_clients:
            return

        disconnected: list[asyncio.StreamWriter] = []
        for writer in list(self.tcp_clients):
            try:
                writer.write(data)
                await writer.drain()
            except Exception:
                disconnected.append(writer)

        for writer in disconnected:
            self.tcp_clients.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

        if disconnected:
            self.post_ui(self.set_tcp_status)

    async def _connect_loop(
        self,
        device: BLEDevice | None,
        target_text: str,
    ) -> None:
        while not self.manual_disconnect:
            try:
                selected = device
                if selected is None and target_text:
                    selected = await self._find_target(target_text)
                if selected is None:
                    self.post_ui(self.set_status, "No target device selected")
                    return

                await self._connected_session(selected)
            except Exception as exc:
                self.post_ui(self.set_status, "Disconnected")
                self.post_ui(self.append_output, f"\n[ERROR] {exc}\n", "error")

            if self.manual_disconnect or not self.auto_reconnect:
                break

            self.post_ui(
                self.set_status,
                f"Retrying in {self.args.reconnect_delay:.1f} seconds...",
            )
            await asyncio.sleep(self.args.reconnect_delay)

        self.post_ui(self.set_status, "Disconnected")

    async def _find_target(self, target_text: str) -> BLEDevice | None:
        wanted = target_text.lower()

        def matches(device: BLEDevice, adv: AdvertisementData) -> bool:
            name = device_name(device, adv).lower()
            return device.address.lower() == wanted or wanted in name

        self.post_ui(self.set_status, f"Looking for {target_text}...")
        return await BleakScanner.find_device_by_filter(
            matches,
            timeout=self.args.scan_timeout,
        )

    async def _connected_session(self, device: BLEDevice) -> None:
        disconnected_event = asyncio.Event()

        def on_disconnect(_client: BleakClient) -> None:
            self.loop.call_soon_threadsafe(disconnected_event.set)

        self.post_ui(
            self.set_status,
            f"Connecting to {device.name or '(unnamed)'}...",
        )
        self.post_ui(
            self.append_output,
            f"Connecting to {device.name or '(unnamed)'} at {device.address}...\n",
            "status",
        )

        client = BleakClient(
            device,
            disconnected_callback=on_disconnect,
            timeout=self.args.connect_timeout,
        )
        self.client = client
        self.uart = None

        try:
            await client.connect()
            uart = resolve_uart_characteristics(
                client,
                self.args.service_uuid,
                self.args.tx_uuid,
                self.args.rx_uuid,
                self.args.write_with_response,
            )
            self.uart = uart

            if self.args.list_services:
                self.post_ui(self.append_output, format_services(client), "blue")

            mode = "write-with-response" if uart.write_with_response else "write-without-response"

            def handle_rx(_sender: BleakGATTCharacteristic, data: bytearray) -> None:
                raw = bytes(data)
                text = raw.decode(self.args.encoding, errors="replace")
                self.post_ui(self.append_output, text, "rx")
                self.loop.call_soon_threadsafe(
                    lambda payload=raw: self.loop.create_task(self._broadcast_tcp(payload))
                )

            await client.start_notify(uart.rx, handle_rx)
            self.post_ui(self.set_status, f"Connected to {client.name} ({client.address})")
            self.post_ui(
                self.append_output,
                "Connected.\n"
                f"TX characteristic: {uart.tx.uuid} [{mode}]\n"
                f"RX characteristic: {uart.rx.uuid} [notify/indicate]\n\n",
                "status",
            )
            await disconnected_event.wait()
            if not self.manual_disconnect:
                self.post_ui(self.append_output, "\nDisconnected from BLE device.\n", "error")
        finally:
            with contextlib.suppress(Exception):
                if self.client and self.client.is_connected and self.uart:
                    await self.client.stop_notify(self.uart.rx)
            with contextlib.suppress(Exception):
                if self.client and self.client.is_connected:
                    await self.client.disconnect()
            self.client = None
            self.uart = None

    def send_line(self) -> None:
        text = self.input_var.get()
        self.input_var.set("")
        payload = text.encode(self.args.encoding, errors="replace")
        payload += line_ending_bytes(self.line_ending_var.get())
        self.append_output(f"> {text}\n", "sent")
        self.run_coro(self._send_payload(payload))

    async def _send_payload(self, payload: bytes, show_error: bool = True) -> bool:
        if not self.client or not self.client.is_connected or not self.uart:
            if show_error:
                self.post_ui(self.append_output, "[NOT CONNECTED] Cannot send yet.\n", "error")
            return False
        try:
            await write_ble_chunks(
                self.client,
                self.uart.tx,
                payload,
                self.uart.write_with_response,
                self.args.chunk_size,
                self.args.chunk_delay,
            )
        except Exception as exc:
            if show_error:
                self.post_ui(self.append_output, f"[BLE WRITE ERROR] {exc}\n", "error")
            return False
        return True

    def disconnect(self) -> None:
        self.manual_disconnect = True
        self.run_coro(self._disconnect())

    async def _disconnect(self) -> None:
        if self.client and self.client.is_connected:
            await self.client.disconnect()

    def close(self) -> None:
        self.manual_disconnect = True
        future = self.run_coro(self._shutdown())
        with contextlib.suppress(Exception):
            future.result(timeout=2)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.root.destroy()

    async def _shutdown(self) -> None:
        await self._stop_tcp_server()
        await self._disconnect()


def run_gui(args: argparse.Namespace) -> None:
    GuiBleTerminal(args).start()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bluetooth LE serial bridge for UART-style Bluetooth modules."
    )
    parser.add_argument("--address", help="BLE device address/MAC as shown by the scan.")
    parser.add_argument("--name", help="Advertised BLE device name or name substring.")
    parser.add_argument("--scan-timeout", type=float, default=8.0, help="BLE scan timeout in seconds.")
    parser.add_argument("--connect-timeout", type=float, default=20.0, help="BLE connection timeout in seconds.")
    parser.add_argument("--service-uuid", help="UART-like BLE service UUID.", default=None)
    parser.add_argument("--tx-uuid", help="Writable characteristic UUID.")
    parser.add_argument("--rx-uuid", help="Notify characteristic UUID.")
    parser.add_argument(
        "--write-with-response",
        choices=("auto", "yes", "no"),
        default="auto",
        help="BLE write mode. Auto prefers write-without-response when available.",
    )
    parser.add_argument(
        "--line-ending",
        choices=("none", "lf", "cr", "crlf"),
        default="lf",
        help="Line ending appended in built-in terminal mode.",
    )
    parser.add_argument("--encoding", default="utf-8", help="Text encoding for terminal mode.")
    parser.add_argument("--chunk-size", type=int, default=20, help="Max bytes per BLE write.")
    parser.add_argument("--chunk-delay", type=float, default=0.02, help="Delay between BLE chunks in seconds.")
    parser.add_argument("--reconnect-delay", type=float, default=3.0, help="Delay before reconnect attempts.")
    parser.add_argument("--no-reconnect", action="store_true", help="Exit instead of reconnecting after a drop.")
    parser.add_argument("--list-services", action="store_true", help="Print GATT services after connecting.")
    parser.add_argument("--gui", action="store_true", help="Use a graphical BLE control terminal.")
    parser.add_argument("--scan-only", action="store_true", help="Scan for BLE devices and exit without connecting.")
    parser.add_argument("--tcp-host", default="127.0.0.1", help="Default GUI TCP listener host.")
    parser.add_argument("--tcp-port", type=int, default=65432, help="Default GUI TCP listener port.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.gui and args.scan_only:
        parser.error("Use either --gui or --scan-only, not both.")

    try:
        if args.scan_only:
            asyncio.run(scan_devices(args.scan_timeout))
        elif args.gui:
            run_gui(args)
        else:
            asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except BridgeStop:
        return 0
    except Exception as exc:
        print(f"\nFatal error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
