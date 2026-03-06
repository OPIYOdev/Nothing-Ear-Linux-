#!/usr/bin/env python3
"""
Nothing Ear Linux -- Unofficial companion app for Nothing Ear 3(a)
Auto-detects paired Nothing/CMF devices via BlueZ D-Bus (no bluetoothctl needed)
Communicates via RFCOMM channel 15 using the reverse-engineered Nothing protocol.

Install deps:
  sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \
                   python3-dbus bluez

Run:
  python3 nothing_ear_linux.py
"""

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gio, Gdk, Pango

import socket
import struct
import threading
import time
import subprocess
import re
import dbus


# --- Protocol (Nothing RFCOMM, channel 15) -----------------------------------

RFCOMM_CHANNEL = 15

CMD_GET_INFO    = bytes.fromhex("5560014240000003e0d1")
CMD_GET_SERIAL  = bytes.fromhex("556001064000000590dc")
CMD_GET_BATTERY = bytes.fromhex("556001014000000140e3")
CMD_GET_ANC     = bytes.fromhex("5560011ec001000c039819")

CMD_ANC = {
    "off":          bytes.fromhex("5560010ff003000cd101050004c4f7"),
    "transparency": bytes.fromhex("5560010ff003000cb101070000c5af"),
    "high":         bytes.fromhex("5560010ff003000cf101010000e66f"),
    "mid":          bytes.fromhex("5560010ff003000d5101020000e69f"),
    "low":          bytes.fromhex("5560010ff003000d7101030000e70f"),
    "adaptive":     bytes.fromhex("5560010ff003000dd101040000e53f"),
}

CMD_EQ = {
    "balanced":    bytes.fromhex("55600106400000060101009f3c"),
    "more_bass":   bytes.fromhex("55600106400000060101019e8c"),
    "more_treble": bytes.fromhex("55600106400000060101029e1c"),
    "voice":       bytes.fromhex("55600106400000060101039eac"),
}

CMD_IED_ON     = bytes.fromhex("55600104400000260101017310")
CMD_IED_OFF    = bytes.fromhex("5560010440000025010101b294")
CMD_LOWLAT_ON  = bytes.fromhex("5560014040000027010097f7")
CMD_LOWLAT_OFF = bytes.fromhex("5560014040000028020000a704")
CMD_RING_L     = bytes.fromhex("556001444000000b01010072f0")
CMD_RING_R     = bytes.fromhex("556001444000000b01020073a0")
CMD_RING_ALL   = bytes.fromhex("556001444000000b01030073b0")
CMD_RING_OFF   = bytes.fromhex("556001444000000b01000072c0")


# --- BlueZ D-Bus Scanner (auto-detect, no bluetoothctl needed) ---------------

NOTHING_KEYWORDS = [
    "nothing", "ear", "cmf", "buds", "tws",
    "ear(1)", "ear(2)", "ear(3)", "ear(a)", "ear(3a)",
]

def get_paired_bt_devices() -> list[dict]:
    """
    Query BlueZ via D-Bus for all paired Bluetooth devices.
    Returns list of {name, addr, paired, connected, icon}.
    Falls back to bluetoothctl subprocess if D-Bus fails.
    """
    devices = []

    # -- Method 1: D-Bus (preferred, no subprocess) --
    try:
        bus = dbus.SystemBus()
        manager = dbus.Interface(
            bus.get_object("org.bluez", "/"),
            "org.freedesktop.DBus.ObjectManager"
        )
        objects = manager.GetManagedObjects()
        for path, ifaces in objects.items():
            dev = ifaces.get("org.bluez.Device1")
            if dev is None:
                continue
            addr  = str(dev.get("Address", ""))
            name  = str(dev.get("Name", "") or dev.get("Alias", "") or addr)
            paired    = bool(dev.get("Paired", False))
            connected = bool(dev.get("Connected", False))
            icon  = str(dev.get("Icon", ""))
            if paired and addr:
                devices.append({
                    "addr": addr,
                    "name": name,
                    "paired": paired,
                    "connected": connected,
                    "icon": icon,
                    "source": "dbus",
                })
        return devices
    except Exception:
        pass

    # -- Method 2: bluetoothctl subprocess fallback --
    try:
        out = subprocess.check_output(
            ["bluetoothctl", "devices"],
            text=True, timeout=5,
            stderr=subprocess.DEVNULL
        )
        for line in out.splitlines():
            m = re.match(r"Device\s+([0-9A-Fa-f:]{17})\s+(.*)", line)
            if m:
                addr, name = m.group(1).upper(), m.group(2).strip()
                devices.append({
                    "addr": addr,
                    "name": name or addr,
                    "paired": True,
                    "connected": False,
                    "icon": "",
                    "source": "bluetoothctl",
                })
    except Exception:
        pass

    return devices


def is_nothing_device(dev: dict) -> bool:
    name_lower = dev["name"].lower()
    icon = dev.get("icon", "").lower()
    if any(k in name_lower for k in NOTHING_KEYWORDS):
        return True
    if icon in ("audio-headset", "audio-headphones", "audio-card"):
        return True
    return False


# --- Bluetooth Connection Manager --------------------------------------------

class BTManager:
    def __init__(self):
        self.sock = None
        self.connected = False
        self.addr = None
        self._running = False
        self.on_data    = None   # fn(bytes)
        self.on_status  = None   # fn(bool, str)

    def connect(self, addr: str):
        """Connect in background thread."""
        self.addr = addr.upper()
        t = threading.Thread(target=self._connect_thread, args=(self.addr,), daemon=True)
        t.start()

    def _connect_thread(self, addr):
        try:
            if self.sock:
                try: self.sock.close()
                except: pass
            s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
            s.settimeout(10)
            s.connect((addr, RFCOMM_CHANNEL))
            s.settimeout(None)
            self.sock = s
            self.connected = True
            self._running = True
            GLib.idle_add(self.on_status, True, f"Connected to {addr}")
            self._recv_loop()
        except OSError as e:
            self.connected = False
            msg = self._friendly_error(e, addr)
            GLib.idle_add(self.on_status, False, msg)

    def _friendly_error(self, e: OSError, addr: str) -> str:
        code = e.errno
        if code == 13:
            return "Permission denied -- add yourself to the 'bluetooth' group"
        if code == 111:
            return "Refused -- are earbuds out of the case and awake?"
        if code == 112:
            return "Host is down -- earbuds may be asleep or out of range"
        if code == 115:
            return "Timed out -- try putting earbuds back in case then out again"
        return f"Failed: {e}"

    def _recv_loop(self):
        buf = b""
        while self._running and self.sock:
            try:
                chunk = self.sock.recv(512)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= 7:
                    if buf[0] != 0x55:
                        buf = buf[1:]
                        continue
                    try:
                        plen = struct.unpack_from("<H", buf, 5)[0]
                        total = 7 + plen + 2
                    except struct.error:
                        break
                    if len(buf) < total:
                        break
                    pkt, buf = buf[:total], buf[total:]
                    if self.on_data:
                        GLib.idle_add(self.on_data, bytes(pkt))
            except OSError:
                break
        self.connected = False
        self._running = False
        if self.on_status:
            GLib.idle_add(self.on_status, False, "Disconnected")

    def send(self, data: bytes) -> bool:
        if not self.connected or not self.sock:
            return False
        try:
            self.sock.sendall(data)
            return True
        except OSError:
            self.connected = False
            if self.on_status:
                GLib.idle_add(self.on_status, False, "Connection lost")
            return False

    def disconnect(self):
        self._running = False
        self.connected = False
        if self.sock:
            try: self.sock.close()
            except: pass
            self.sock = None


# --- Packet Parser ------------------------------------------------------------

def parse_packet(data: bytes) -> dict:
    if len(data) < 7:
        return {}
    rtype = data[3]
    result = {}

    if rtype == 0x01 and len(data) >= 13:
        try:
            result["battery"] = {
                "left":  int(data[8]),
                "right": int(data[9]),
                "case":  int(data[10]),
                "left_charging":  bool(data[11] & 0x01),
                "right_charging": bool(data[11] & 0x02),
                "case_charging":  bool(data[11] & 0x04),
            }
        except (IndexError, ValueError):
            pass

    elif rtype == 0x42 and len(data) > 9:
        try:
            vlen = int(data[6])
            fw = data[8:8+vlen].decode("ascii", errors="ignore")
            result["firmware"] = fw
        except Exception:
            pass

    elif rtype == 0x06 and len(data) > 9:
        try:
            result["serial"] = data[8:-2].decode("ascii", errors="ignore").strip("\x00")
        except Exception:
            pass

    elif rtype == 0x1e and len(data) >= 11:
        anc_map = {0x05:"off", 0x07:"transparency", 0x01:"high",
                   0x02:"mid", 0x03:"low", 0x04:"adaptive"}
        result["anc"] = anc_map.get(int(data[10]), None)

    return result


# --- CSS ---------------------------------------------------------------------

APP_CSS = """
* { box-sizing: border-box; }

window, .main-window {
  background-color: #0a0a0a;
  color: #e0e0e0;
}

/* -- Scrolled area -- */
.scroll-area {
  background: transparent;
}

/* -- Top bar -- */
.topbar {
  background: #111111;
  border-bottom: 1px solid #1e1e1e;
  padding: 10px 20px;
  min-height: 52px;
}
.app-title {
  font-family: 'DM Mono', 'Courier New', monospace;
  font-size: 16px;
  font-weight: 600;
  letter-spacing: 3px;
  color: #ffffff;
}
.app-sub {
  font-family: 'DM Mono', monospace;
  font-size: 10px;
  color: #444;
  letter-spacing: 2px;
}
.conn-badge {
  font-family: 'DM Mono', monospace;
  font-size: 10px;
  padding: 4px 10px;
  border-radius: 20px;
  letter-spacing: 1px;
  font-weight: bold;
}
.conn-badge.connected {
  background: #0d2a1a;
  color: #00e676;
  border: 1px solid #00e676;
}
.conn-badge.disconnected {
  background: #1a1a1a;
  color: #555;
  border: 1px solid #2a2a2a;
}
.conn-badge.connecting {
  background: #1a1a0a;
  color: #ffd600;
  border: 1px solid #ffd600;
}

/* -- Section labels -- */
.section-label {
  font-family: 'DM Mono', monospace;
  font-size: 9px;
  letter-spacing: 3px;
  color: #444;
  margin-bottom: 6px;
  margin-top: 4px;
}

/* -- Cards -- */
.card {
  background: #131313;
  border: 1px solid #1e1e1e;
  border-radius: 14px;
  padding: 14px 16px;
  margin-bottom: 10px;
}
.card-inner {
  background: #0f0f0f;
  border: 1px solid #1a1a1a;
  border-radius: 10px;
  padding: 10px 14px;
  margin: 4px 0;
}

/* -- Device list -- */
.device-row {
  background: #131313;
  border: 1px solid #1e1e1e;
  border-radius: 10px;
  padding: 12px 14px;
  margin: 4px 0;
  transition: all 120ms;
}
.device-row:hover {
  background: #191919;
  border-color: #2e2e2e;
}
.device-row.highlighted {
  border-color: #00e676;
  background: #0d1f14;
}
.device-name-label {
  font-family: 'DM Mono', monospace;
  font-size: 13px;
  font-weight: 600;
  color: #e8e8e8;
}
.device-addr-label {
  font-family: 'DM Mono', monospace;
  font-size: 10px;
  color: #444;
  letter-spacing: 1px;
}
.device-status-dot {
  font-size: 8px;
  color: #00e676;
}

/* -- Connect button -- */
.btn-connect {
  font-family: 'DM Mono', monospace;
  font-size: 11px;
  font-weight: bold;
  letter-spacing: 2px;
  background: linear-gradient(135deg, #00e676 0%, #00b248 100%);
  color: #000000;
  border: none;
  border-radius: 8px;
  padding: 8px 18px;
  min-width: 90px;
}
.btn-connect:hover {
  background: linear-gradient(135deg, #33eb91 0%, #00c957 100%);
}
.btn-connect:disabled {
  background: #1e1e1e;
  color: #333;
}
.btn-disconnect {
  font-family: 'DM Mono', monospace;
  font-size: 11px;
  font-weight: bold;
  letter-spacing: 2px;
  background: transparent;
  color: #ff5252;
  border: 1px solid #ff5252;
  border-radius: 8px;
  padding: 8px 16px;
}
.btn-scan {
  font-family: 'DM Mono', monospace;
  font-size: 11px;
  background: #1a1a1a;
  color: #888;
  border: 1px solid #2a2a2a;
  border-radius: 8px;
  padding: 8px 14px;
}
.btn-scan:hover {
  background: #222;
  color: #ccc;
}

/* -- MAC entry -- */
.mac-entry {
  font-family: 'DM Mono', monospace;
  font-size: 12px;
  background: #0f0f0f;
  color: #00e676;
  border: 1px solid #222;
  border-radius: 8px;
  padding: 8px 12px;
  caret-color: #00e676;
}
.mac-entry:focus {
  border-color: #00e676;
}

/* -- Battery -- */
.batt-label-top {
  font-family: 'DM Mono', monospace;
  font-size: 9px;
  color: #555;
  letter-spacing: 2px;
}
.batt-pct {
  font-family: 'DM Mono', monospace;
  font-size: 20px;
  font-weight: bold;
  color: #e0e0e0;
}
.batt-pct.charging {
  color: #ffd600;
}
.batt-pct.low {
  color: #ff5252;
}
progressbar trough {
  background: #1a1a1a;
  border-radius: 3px;
  min-height: 5px;
}
progressbar progress {
  background: linear-gradient(90deg, #00b248, #00e676);
  border-radius: 3px;
}
progressbar.low progress {
  background: #ff5252;
}
progressbar.charging progress {
  background: #ffd600;
}

/* -- ANC buttons -- */
.anc-btn {
  font-family: 'DM Mono', monospace;
  font-size: 10px;
  font-weight: bold;
  letter-spacing: 1px;
  border-radius: 10px;
  padding: 12px 6px;
  border: 1px solid #222;
  background: #111;
  color: #555;
  transition: all 150ms;
}
.anc-btn:hover {
  background: #1a1a1a;
  color: #aaa;
  border-color: #333;
}
.anc-btn.active {
  background: linear-gradient(135deg, #0d2a1a, #0a1f12);
  color: #00e676;
  border-color: #00e676;
}

/* -- EQ -- */
.eq-btn {
  font-family: 'DM Mono', monospace;
  font-size: 10px;
  font-weight: bold;
  letter-spacing: 1px;
  border-radius: 10px;
  padding: 10px 4px;
  border: 1px solid #1e1e1e;
  background: #111;
  color: #555;
}
.eq-btn:hover {
  background: #181818;
  color: #888;
}
.eq-btn.active {
  background: linear-gradient(135deg, #0a1a2a, #071525);
  color: #64b5f6;
  border-color: #64b5f6;
}

/* -- Toggles -- */
.toggle-label {
  font-family: 'DM Mono', monospace;
  font-size: 12px;
  color: #ccc;
}
.toggle-sublabel {
  font-family: 'DM Mono', monospace;
  font-size: 10px;
  color: #444;
}
switch {
  background: #1a1a1a;
  border: 1px solid #2a2a2a;
  border-radius: 20px;
}
switch:checked {
  background: #00b248;
  border-color: #00e676;
}

/* -- Find buds -- */
.ring-btn {
  font-family: 'DM Mono', monospace;
  font-size: 10px;
  font-weight: bold;
  letter-spacing: 1px;
  border-radius: 10px;
  padding: 12px 4px;
  border: 1px solid #2a1a00;
  background: #130e00;
  color: #ffb74d;
}
.ring-btn:hover {
  background: #1e1500;
  border-color: #ffb74d;
}
.ring-btn.stop {
  border-color: #222;
  background: #111;
  color: #555;
}
.ring-btn.stop:hover {
  border-color: #ff5252;
  color: #ff5252;
  background: #1a0a0a;
}

/* -- Info grid -- */
.info-key {
  font-family: 'DM Mono', monospace;
  font-size: 10px;
  color: #444;
  letter-spacing: 1px;
}
.info-val {
  font-family: 'DM Mono', monospace;
  font-size: 11px;
  color: #00e676;
}

/* -- Toast / status bar -- */
.statusbar {
  background: #0d0d0d;
  border-top: 1px solid #1a1a1a;
  padding: 6px 18px;
  min-height: 28px;
}
.status-msg {
  font-family: 'DM Mono', monospace;
  font-size: 10px;
  color: #555;
  letter-spacing: 1px;
}
.status-msg.error { color: #ff5252; }
.status-msg.success { color: #00e676; }
.status-msg.warning { color: #ffd600; }
"""


# --- Main Application ---------------------------------------------------------

class NothingEarApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="com.nothing.ear.linux",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.bt = BTManager()
        self.bt.on_data   = self._on_bt_data
        self.bt.on_status = self._on_bt_status

        self.current_anc = None
        self.current_eq  = "balanced"
        self.all_devices = []     # all paired devices
        self.nothing_devices = [] # filtered Nothing/audio devices
        self._ied_state = False
        self._ll_state  = False

        self.connect("activate", self._activate)

    # -- Activate --------------------------------------------------------------

    def _activate(self, app):
        css = Gtk.CssProvider()
        css.load_from_data(APP_CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self.win = Adw.ApplicationWindow(application=app, title="Nothing Ear")
        self.win.set_default_size(420, 780)
        self.win.set_resizable(True)

        self._build_ui()
        self.win.present()

        # Auto-scan on launch
        GLib.idle_add(self._do_scan)

    # -- Build UI --------------------------------------------------------------

    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Top bar
        root.append(self._make_topbar())

        # Scrollable content
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add_css_class("scroll-area")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.set_margin_start(16)
        content.set_margin_end(16)
        content.set_margin_top(10)
        content.set_margin_bottom(16)

        content.append(self._make_section("DEVICE",        self._make_device_panel()))
        content.append(self._make_section("BATTERY",       self._make_battery_panel()))
        content.append(self._make_section("NOISE CONTROL", self._make_anc_panel()))
        content.append(self._make_section("EQUALIZER",     self._make_eq_panel()))
        content.append(self._make_section("SETTINGS",      self._make_toggles_panel()))
        content.append(self._make_section("FIND MY BUDS",  self._make_find_panel()))
        content.append(self._make_section("DEVICE INFO",   self._make_info_panel()))

        scroll.set_child(content)
        root.append(scroll)

        # Status bar
        root.append(self._make_statusbar())

        self.win.set_content(root)

    # -- Top bar ---------------------------------------------------------------

    def _make_topbar(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        bar.add_css_class("topbar")

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        left.set_hexpand(True)
        title = Gtk.Label(label="NOTHING EAR")
        title.add_css_class("app-title")
        title.set_halign(Gtk.Align.START)
        sub = Gtk.Label(label="LINUX COMPANION")
        sub.add_css_class("app-sub")
        sub.set_halign(Gtk.Align.START)
        left.append(title)
        left.append(sub)
        bar.append(left)

        self.conn_badge = Gtk.Label(label="● DISCONNECTED")
        self.conn_badge.add_css_class("conn-badge")
        self.conn_badge.add_css_class("disconnected")
        bar.append(self.conn_badge)

        return bar

    # -- Section wrapper -------------------------------------------------------

    def _make_section(self, title: str, child: Gtk.Widget) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        lbl = Gtk.Label(label=title)
        lbl.add_css_class("section-label")
        lbl.set_halign(Gtk.Align.START)
        box.append(lbl)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("card")
        card.append(child)
        box.append(card)
        return box

    # -- Device panel ----------------------------------------------------------

    def _make_device_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        # Scan button row
        scan_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        scan_lbl = Gtk.Label(label="Paired Bluetooth Devices")
        scan_lbl.add_css_class("toggle-label")
        scan_lbl.set_hexpand(True)
        scan_lbl.set_halign(Gtk.Align.START)
        scan_row.append(scan_lbl)

        self.scan_btn = Gtk.Button(label="⟳  SCAN")
        self.scan_btn.add_css_class("btn-scan")
        self.scan_btn.connect("clicked", lambda _: self._do_scan())
        scan_row.append(self.scan_btn)
        box.append(scan_row)

        # Device list container
        self.device_list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(self.device_list_box)

        # Separator
        sep = Gtk.Separator()
        sep.set_margin_top(4)
        sep.set_margin_bottom(4)
        box.append(sep)

        # Manual MAC entry
        mac_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mac_hint = Gtk.Label(label="Manual MAC")
        mac_hint.add_css_class("toggle-sublabel")
        mac_hint.set_halign(Gtk.Align.START)
        mac_row.append(mac_hint)
        box.append(mac_row)

        entry_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.mac_entry = Gtk.Entry()
        self.mac_entry.set_placeholder_text("XX:XX:XX:XX:XX:XX")
        self.mac_entry.add_css_class("mac-entry")
        self.mac_entry.set_hexpand(True)
        self.mac_entry.connect("activate", self._on_mac_entry_activate)
        entry_row.append(self.mac_entry)

        mac_conn_btn = Gtk.Button(label="GO")
        mac_conn_btn.add_css_class("btn-connect")
        mac_conn_btn.connect("clicked", self._on_mac_entry_activate)
        entry_row.append(mac_conn_btn)
        box.append(entry_row)

        # Disconnect button
        self.disc_btn = Gtk.Button(label="DISCONNECT")
        self.disc_btn.add_css_class("btn-disconnect")
        self.disc_btn.set_sensitive(False)
        self.disc_btn.connect("clicked", self._on_disconnect)
        self.disc_btn.set_margin_top(4)
        box.append(self.disc_btn)

        return box

    def _make_device_row(self, dev: dict) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.add_css_class("device-row")
        if is_nothing_device(dev):
            row.add_css_class("highlighted")

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        left.set_hexpand(True)

        name_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        if dev.get("connected"):
            dot = Gtk.Label(label="●")
            dot.add_css_class("device-status-dot")
            name_row.append(dot)

        name_lbl = Gtk.Label(label=dev["name"])
        name_lbl.add_css_class("device-name-label")
        name_lbl.set_halign(Gtk.Align.START)
        name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        name_row.append(name_lbl)
        left.append(name_row)

        addr_lbl = Gtk.Label(label=dev["addr"])
        addr_lbl.add_css_class("device-addr-label")
        addr_lbl.set_halign(Gtk.Align.START)
        left.append(addr_lbl)
        row.append(left)

        btn = Gtk.Button(label="CONNECT")
        btn.add_css_class("btn-connect")
        btn.connect("clicked", lambda _, d=dev: self._connect_to(d["addr"]))
        row.append(btn)

        return row

    # -- Battery panel ---------------------------------------------------------

    def _make_battery_panel(self):
        self.batt_widgets = {}
        grid = Gtk.Grid()
        grid.set_column_spacing(12)
        grid.set_row_spacing(10)
        grid.set_column_homogeneous(True)

        for col, (key, label) in enumerate([("left","LEFT"),("right","RIGHT"),("case","CASE")]):
            top = Gtk.Label(label=label)
            top.add_css_class("batt-label-top")
            top.set_halign(Gtk.Align.CENTER)
            grid.attach(top, col, 0, 1, 1)

            pct = Gtk.Label(label="--")
            pct.add_css_class("batt-pct")
            pct.set_halign(Gtk.Align.CENTER)
            grid.attach(pct, col, 1, 1, 1)

            bar = Gtk.ProgressBar()
            bar.set_fraction(0)
            grid.attach(bar, col, 2, 1, 1)

            self.batt_widgets[key] = {"pct": pct, "bar": bar}

        ref_btn = Gtk.Button(label="↻  REFRESH")
        ref_btn.add_css_class("btn-scan")
        ref_btn.set_margin_top(6)
        ref_btn.set_halign(Gtk.Align.CENTER)
        ref_btn.connect("clicked", lambda _: self.bt.send(CMD_GET_BATTERY))
        grid.attach(ref_btn, 0, 3, 3, 1)

        return grid

    # -- ANC panel -------------------------------------------------------------

    def _make_anc_panel(self):
        self.anc_btns = {}
        anc_modes = [
            ("off",          "OFF",          "No noise control"),
            ("transparency", "TRANSPARENT",  "Hear surroundings"),
            ("low",          "ANC LOW",      "Light cancellation"),
            ("mid",          "ANC MID",      "Balanced"),
            ("high",         "ANC HIGH",     "Max cancellation"),
            ("adaptive",     "ADAPTIVE",     "Auto adjust"),
        ]
        grid = Gtk.Grid()
        grid.set_column_spacing(8)
        grid.set_row_spacing(8)
        grid.set_column_homogeneous(True)

        for i, (key, label, _tip) in enumerate(anc_modes):
            btn = Gtk.Button(label=label)
            btn.add_css_class("anc-btn")
            btn.set_tooltip_text(_tip)
            btn.connect("clicked", self._on_anc_click, key)
            self.anc_btns[key] = btn
            grid.attach(btn, i % 3, i // 3, 1, 1)

        return grid

    # -- EQ panel --------------------------------------------------------------

    def _make_eq_panel(self):
        self.eq_btns = {}
        eq_presets = [
            ("balanced",    "BALANCED"),
            ("more_bass",   "BASS +"),
            ("more_treble", "TREBLE +"),
            ("voice",       "VOICE"),
        ]
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_homogeneous(True)

        for key, label in eq_presets:
            btn = Gtk.Button(label=label)
            btn.add_css_class("eq-btn")
            btn.connect("clicked", self._on_eq_click, key)
            self.eq_btns[key] = btn
            box.append(btn)

        self._refresh_eq_ui()
        return box

    # -- Toggles panel ---------------------------------------------------------

    def _make_toggles_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        def row(label, sublabel, on_cmd, off_cmd):
            r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            r.add_css_class("card-inner")
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            labels.set_hexpand(True)
            lbl = Gtk.Label(label=label)
            lbl.add_css_class("toggle-label")
            lbl.set_halign(Gtk.Align.START)
            sub = Gtk.Label(label=sublabel)
            sub.add_css_class("toggle-sublabel")
            sub.set_halign(Gtk.Align.START)
            labels.append(lbl)
            labels.append(sub)
            r.append(labels)
            sw = Gtk.Switch()
            sw.set_valign(Gtk.Align.CENTER)
            sw.connect("state-set", lambda s, v, oc=on_cmd, fc=off_cmd:
                       self.bt.send(oc if v else fc) or False)
            r.append(sw)
            return r

        box.append(row(
            "In-Ear Detection",
            "Auto-pause when earbuds removed",
            CMD_IED_ON, CMD_IED_OFF
        ))
        box.append(row(
            "Low Latency Mode",
            "Reduce audio delay for gaming",
            CMD_LOWLAT_ON, CMD_LOWLAT_OFF
        ))
        return box

    # -- Find My Buds panel ----------------------------------------------------

    def _make_find_panel(self):
        grid = Gtk.Grid()
        grid.set_column_spacing(8)
        grid.set_row_spacing(0)
        grid.set_column_homogeneous(True)

        items = [
            ("◉ LEFT",  CMD_RING_L,   False),
            ("◉ RIGHT", CMD_RING_R,   False),
            ("◉ BOTH",  CMD_RING_ALL, False),
            ("✕ STOP",  CMD_RING_OFF, True),
        ]
        for i, (label, cmd, is_stop) in enumerate(items):
            btn = Gtk.Button(label=label)
            btn.add_css_class("ring-btn")
            if is_stop:
                btn.add_css_class("stop")
            btn.connect("clicked", lambda _, c=cmd: self.bt.send(c))
            grid.attach(btn, i, 0, 1, 1)

        return grid

    # -- Info panel ------------------------------------------------------------

    def _make_info_panel(self):
        self.info_vals = {}
        grid = Gtk.Grid()
        grid.set_column_spacing(16)
        grid.set_row_spacing(8)

        for row, (key, label) in enumerate([
            ("model",    "MODEL"),
            ("firmware", "FIRMWARE"),
            ("serial",   "SERIAL"),
            ("address",  "ADDRESS"),
        ]):
            k = Gtk.Label(label=label)
            k.add_css_class("info-key")
            k.set_halign(Gtk.Align.START)
            grid.attach(k, 0, row, 1, 1)

            v = Gtk.Label(label="--")
            v.add_css_class("info-val")
            v.set_halign(Gtk.Align.START)
            v.set_selectable(True)
            self.info_vals[key] = v
            grid.attach(v, 1, row, 1, 1)

        return grid

    # -- Status bar ------------------------------------------------------------

    def _make_statusbar(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        bar.add_css_class("statusbar")
        self.status_lbl = Gtk.Label(label="Ready -- scan or enter MAC address to connect")
        self.status_lbl.add_css_class("status-msg")
        self.status_lbl.set_halign(Gtk.Align.START)
        self.status_lbl.set_hexpand(True)
        self.status_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        bar.append(self.status_lbl)
        return bar

    # -- Scan logic ------------------------------------------------------------

    def _do_scan(self):
        self._set_status("Scanning for paired devices…", "warning")
        self.scan_btn.set_sensitive(False)

        def worker():
            devices = get_paired_bt_devices()
            GLib.idle_add(self._populate_devices, devices)

        threading.Thread(target=worker, daemon=True).start()

    def _populate_devices(self, devices: list[dict]):
        self.scan_btn.set_sensitive(True)

        # Clear list
        while True:
            child = self.device_list_box.get_first_child()
            if child is None:
                break
            self.device_list_box.remove(child)

        self.all_devices = devices
        # Sort: Nothing devices first, then by name
        sorted_devs = sorted(
            devices,
            key=lambda d: (0 if is_nothing_device(d) else 1, d["name"].lower())
        )

        if not sorted_devs:
            lbl = Gtk.Label(label="No paired devices found.\nPair your earbuds first via system Bluetooth settings.")
            lbl.add_css_class("toggle-sublabel")
            lbl.set_justify(Gtk.Justification.CENTER)
            lbl.set_margin_top(8)
            lbl.set_margin_bottom(8)
            self.device_list_box.append(lbl)
            self._set_status("No paired devices found -- pair earbuds in system settings first", "error")
        else:
            for dev in sorted_devs:
                self.device_list_box.append(self._make_device_row(dev))
            n_nothing = sum(1 for d in sorted_devs if is_nothing_device(d))
            self._set_status(
                f"Found {len(sorted_devs)} device(s)"
                + (f" -- {n_nothing} Nothing/audio device(s) highlighted" if n_nothing else ""),
                "success" if n_nothing else None
            )

            # Auto-connect to first Nothing device if only one found
            nothing_devs = [d for d in sorted_devs if is_nothing_device(d)]
            if len(nothing_devs) == 1 and not self.bt.connected:
                GLib.timeout_add(600, lambda: self._connect_to(nothing_devs[0]["addr"]) or False)

    # -- Connect logic ---------------------------------------------------------

    def _connect_to(self, addr: str):
        if self.bt.connected:
            self.bt.disconnect()

        self._set_badge("connecting")
        self._set_status(f"Connecting to {addr}…", "warning")
        self.disc_btn.set_sensitive(False)
        self.bt.connect(addr)

    def _on_mac_entry_activate(self, *_):
        addr = self.mac_entry.get_text().strip().upper()
        if re.match(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$", addr):
            self._connect_to(addr)
        else:
            self._set_status("Invalid MAC -- format must be XX:XX:XX:XX:XX:XX", "error")

    def _on_disconnect(self, *_):
        self.bt.disconnect()

    # -- BT callbacks ----------------------------------------------------------

    def _on_bt_status(self, connected: bool, msg: str):
        if connected:
            self._set_badge("connected")
            self._set_status(msg, "success")
            self.disc_btn.set_sensitive(True)
            self.info_vals["address"].set_label(self.bt.addr or "--")
            self.info_vals["model"].set_label("Nothing Ear 3(a)")
            # Request device info
            GLib.timeout_add(300,  lambda: self.bt.send(CMD_GET_INFO) or False)
            GLib.timeout_add(600,  lambda: self.bt.send(CMD_GET_SERIAL) or False)
            GLib.timeout_add(900,  lambda: self.bt.send(CMD_GET_BATTERY) or False)
            GLib.timeout_add(1200, lambda: self.bt.send(CMD_GET_ANC) or False)
            # Keep polling battery every 60s
            GLib.timeout_add(60000, self._battery_poll)
        else:
            self._set_badge("disconnected")
            self._set_status(msg, "error" if "fail" in msg.lower() or "denied" in msg.lower() else None)
            self.disc_btn.set_sensitive(False)
            self._reset_ui()

    def _on_bt_data(self, data: bytes):
        parsed = parse_packet(data)

        if "battery" in parsed:
            b = parsed["battery"]
            for key in ("left", "right", "case"):
                pct = b.get(key)
                charging = b.get(f"{key}_charging", False)
                w = self.batt_widgets.get(key)
                if w and isinstance(pct, int) and 0 <= pct <= 100:
                    txt = f"{pct}%"
                    if charging: txt += " ⚡"
                    w["pct"].set_label(txt)
                    w["bar"].set_fraction(pct / 100)
                    # Style
                    for cls in ("charging", "low"):
                        w["pct"].remove_css_class(cls)
                        w["bar"].remove_css_class(cls)
                    if charging:
                        w["pct"].add_css_class("charging")
                        w["bar"].add_css_class("charging")
                    elif pct <= 20:
                        w["pct"].add_css_class("low")
                        w["bar"].add_css_class("low")

        if "firmware" in parsed:
            self.info_vals["firmware"].set_label(parsed["firmware"])

        if "serial" in parsed:
            self.info_vals["serial"].set_label(parsed["serial"])

        if "anc" in parsed and parsed["anc"]:
            self.current_anc = parsed["anc"]
            self._refresh_anc_ui()

    def _battery_poll(self):
        if self.bt.connected:
            self.bt.send(CMD_GET_BATTERY)
            return True  # repeat
        return False

    # -- ANC / EQ --------------------------------------------------------------

    def _on_anc_click(self, _, key: str):
        if not self.bt.connected:
            self._set_status("Not connected", "error")
            return
        if self.bt.send(CMD_ANC[key]):
            self.current_anc = key
            self._refresh_anc_ui()

    def _on_eq_click(self, _, key: str):
        if not self.bt.connected:
            self._set_status("Not connected", "error")
            return
        if self.bt.send(CMD_EQ[key]):
            self.current_eq = key
            self._refresh_eq_ui()

    def _refresh_anc_ui(self):
        for key, btn in self.anc_btns.items():
            if key == self.current_anc:
                btn.add_css_class("active")
            else:
                btn.remove_css_class("active")

    def _refresh_eq_ui(self):
        for key, btn in self.eq_btns.items():
            if key == self.current_eq:
                btn.add_css_class("active")
            else:
                btn.remove_css_class("active")

    # -- UI helpers ------------------------------------------------------------

    def _set_badge(self, state: str):
        labels = {"connected": "● CONNECTED", "disconnected": "● DISCONNECTED", "connecting": "◌ CONNECTING…"}
        self.conn_badge.set_label(labels.get(state, state))
        for cls in ("connected", "disconnected", "connecting"):
            self.conn_badge.remove_css_class(cls)
        self.conn_badge.add_css_class(state)

    def _set_status(self, msg: str, kind: str = None):
        self.status_lbl.set_label(msg)
        for cls in ("error", "success", "warning"):
            self.status_lbl.remove_css_class(cls)
        if kind:
            self.status_lbl.add_css_class(kind)

    def _reset_ui(self):
        for w in self.batt_widgets.values():
            w["pct"].set_label("--")
            w["bar"].set_fraction(0)
        for v in self.info_vals.values():
            v.set_label("--")
        self.current_anc = None
        self._refresh_anc_ui()


# --- Entry point -------------------------------------------------------------

if __name__ == "__main__":
    app = NothingEarApp()
    app.run(None)
