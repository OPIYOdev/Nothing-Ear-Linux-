#!/usr/bin/env python3
"""
Nothing Ear Linux -- Unofficial companion app for Nothing Ear 3(a)
Features: ANC, EQ (presets + 5-band custom), gestures, ear tip fit test,
          battery, find my buds, device info, auto-connect background service.

Install:
  sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 python3-dbus bluez

Run:
  python3 nothing_ear_linux.py
"""

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gio, Gdk, Pango

import socket, struct, threading, subprocess, re, os, json, time, signal, sys

try:
    import dbus, dbus.mainloop.glib
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    HAS_DBUS = True
except ImportError:
    HAS_DBUS = False

# ---------------------------------------------------------------------------
# Config file  (~/.config/nothing-ear-linux/config.json)
# ---------------------------------------------------------------------------
CONFIG_DIR  = os.path.expanduser("~/.config/nothing-ear-linux")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------
RFCOMM_CHANNEL  = 15
CMD_GET_INFO    = bytes.fromhex("5560014240000003e0d1")
CMD_GET_SERIAL  = bytes.fromhex("556001064000000590dc")
CMD_GET_BATTERY = bytes.fromhex("556001014000000140e3")
CMD_GET_ANC     = bytes.fromhex("5560011ec001000c039819")
CMD_EAR_FIT     = bytes.fromhex("556001144000000a2a014316")

CMD_ANC = {
    "off":          bytes.fromhex("5560010ff003000cd101050004c4f7"),
    "transparency": bytes.fromhex("5560010ff003000cb101070000c5af"),
    "high":         bytes.fromhex("5560010ff003000cf101010000e66f"),
    "mid":          bytes.fromhex("5560010ff003000d5101020000e69f"),
    "low":          bytes.fromhex("5560010ff003000d7101030000e70f"),
    "adaptive":     bytes.fromhex("5560010ff003000dd101040000e53f"),
}

CMD_EQ_PRESET = {
    "balanced":    bytes.fromhex("55600106400000060101009f3c"),
    "more_bass":   bytes.fromhex("55600106400000060101019e8c"),
    "more_treble": bytes.fromhex("55600106400000060101029e1c"),
    "voice":       bytes.fromhex("55600106400000060101039eac"),
}

# Custom EQ: 5 bands, each -6..+6 dB, sent as signed bytes offset by 0x00
# Packet: 55 60 01 06 40 00 00 07 02 [b1 b2 b3 b4 b5] [csum1 csum2]
def build_custom_eq(bands):
    """bands: list of 5 ints, range -6..+6"""
    clamped = [max(-6, min(6, int(b))) for b in bands]
    # encode as unsigned: 0 = 0dB, positive = boost, negative = cut
    encoded = [(v + 6) for v in clamped]  # 0..12 range
    payload = bytearray([0x55,0x60,0x01,0x06,0x40,0x00,0x00,0x07,0x02])
    payload.extend(encoded)
    # 2-byte checksum placeholder (device accepts without valid checksum for EQ)
    payload.extend([0x00, 0x00])
    return bytes(payload)

# Gesture command builder
# Byte layout: 55 60 01 [side_byte] 40 00 00 [action_id] 01 [gesture_byte] 00 00
# side_byte: 0x11=left, 0x12=right
# gesture actions: double_tap, triple_tap, long_press
# action ids and gesture values from Ear (2) protocol
GESTURE_ACTIONS = {
    "double_tap":   0x01,
    "triple_tap":   0x02,
    "long_press":   0x03,
}
GESTURE_FUNCTIONS = {
    "none":             0x00,
    "play_pause":       0x01,
    "next_track":       0x02,
    "prev_track":       0x03,
    "volume_up":        0x04,
    "volume_down":      0x05,
    "anc_toggle":       0x06,
    "voice_assistant":  0x07,
    "ambient_sound":    0x08,
}
GESTURE_FUNCTION_LABELS = {
    "none":            "None",
    "play_pause":      "Play / Pause",
    "next_track":      "Next Track",
    "prev_track":      "Previous Track",
    "volume_up":       "Volume Up",
    "volume_down":     "Volume Down",
    "anc_toggle":      "Toggle ANC",
    "voice_assistant": "Voice Assistant",
    "ambient_sound":   "Ambient Sound",
}

def build_gesture_cmd(side, action, function):
    side_byte  = 0x11 if side == "left" else 0x12
    action_id  = GESTURE_ACTIONS.get(action, 0x01)
    func_byte  = GESTURE_FUNCTIONS.get(function, 0x00)
    pkt = bytearray([0x55,0x60,0x01,side_byte,0x40,0x00,0x00,
                     action_id,0x01,func_byte,0x00,0x00])
    return bytes(pkt)

CMD_IED_ON     = bytes.fromhex("55600104400000260101017310")
CMD_IED_OFF    = bytes.fromhex("5560010440000025010101b294")
CMD_LOWLAT_ON  = bytes.fromhex("5560014040000027010097f7")
CMD_LOWLAT_OFF = bytes.fromhex("5560014040000028020000a704")
CMD_RING_L     = bytes.fromhex("556001444000000b01010072f0")
CMD_RING_R     = bytes.fromhex("556001444000000b01020073a0")
CMD_RING_ALL   = bytes.fromhex("556001444000000b01030073b0")
CMD_RING_OFF   = bytes.fromhex("556001444000000b01000072c0")

NOTHING_KEYWORDS = ["nothing", "ear", "cmf", "buds", "tws"]

# ---------------------------------------------------------------------------
# Device scanner
# ---------------------------------------------------------------------------

def get_paired_devices():
    devices = []
    if HAS_DBUS:
        try:
            bus = dbus.SystemBus()
            mgr = dbus.Interface(
                bus.get_object("org.bluez", "/"),
                "org.freedesktop.DBus.ObjectManager"
            )
            for path, ifaces in mgr.GetManagedObjects().items():
                dev = ifaces.get("org.bluez.Device1")
                if not dev:
                    continue
                addr = str(dev.get("Address", ""))
                name = str(dev.get("Name", "") or dev.get("Alias", "") or addr)
                if bool(dev.get("Paired", False)) and addr:
                    devices.append({
                        "addr": addr, "name": name,
                        "connected": bool(dev.get("Connected", False)),
                        "icon": str(dev.get("Icon", "")),
                    })
            return devices
        except Exception:
            pass
    try:
        out = subprocess.check_output(
            ["bluetoothctl", "devices"], text=True, timeout=5,
            stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            m = re.match(r"Device\s+([0-9A-Fa-f:]{17})\s+(.*)", line)
            if m:
                devices.append({"addr": m.group(1).upper(),
                                 "name": m.group(2).strip() or m.group(1),
                                 "connected": False, "icon": ""})
    except Exception:
        pass
    return devices

def is_audio_device(dev):
    nl = dev["name"].lower()
    return (any(k in nl for k in NOTHING_KEYWORDS)
            or dev["icon"] in ("audio-headset","audio-headphones","audio-card"))

# ---------------------------------------------------------------------------
# Bluetooth manager
# ---------------------------------------------------------------------------

class BTManager:
    def __init__(self):
        self.sock = None
        self.connected = False
        self.addr = None
        self._running = False
        self.on_data   = None
        self.on_status = None

    def connect(self, addr):
        self.addr = addr.upper()
        threading.Thread(target=self._connect_thread, args=(self.addr,), daemon=True).start()

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
            GLib.idle_add(self.on_status, True, "Connected to " + addr)
            self._recv_loop()
        except OSError as e:
            self.connected = False
            GLib.idle_add(self.on_status, False, self._err(e))

    def _err(self, e):
        return {
            13:  "Permission denied -- run: sudo usermod -aG bluetooth $USER  then re-login",
            111: "Connection refused -- are earbuds out of the case?",
            112: "Host down -- earbuds may be sleeping or out of range",
            115: "Timed out -- try putting earbuds back in case then taking them out",
        }.get(e.errno, "Connection failed: " + str(e))

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
                        buf = buf[1:]; continue
                    try:
                        plen  = struct.unpack_from("<H", buf, 5)[0]
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
        self._running  = False
        if self.on_status:
            GLib.idle_add(self.on_status, False, "Disconnected")

    def send(self, data):
        if not self.connected or not self.sock:
            return False
        try:
            self.sock.sendall(data); return True
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

# ---------------------------------------------------------------------------
# Auto-connect background watcher
# ---------------------------------------------------------------------------

class AutoConnectWatcher:
    """
    Watches BlueZ D-Bus for the target device appearing (coming out of case).
    Calls on_device_appeared(addr) when it shows up as Connected=True.
    """
    def __init__(self, target_addr, on_appeared):
        self.target  = target_addr.upper() if target_addr else None
        self.cb      = on_appeared
        self._active = False
        self._thread = None

    def start(self, addr):
        self.target  = addr.upper()
        self._active = True
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def stop(self):
        self._active = False

    def _watch(self):
        if not HAS_DBUS:
            return
        while self._active:
            try:
                bus = dbus.SystemBus()
                mgr = dbus.Interface(
                    bus.get_object("org.bluez", "/"),
                    "org.freedesktop.DBus.ObjectManager"
                )
                for path, ifaces in mgr.GetManagedObjects().items():
                    dev = ifaces.get("org.bluez.Device1")
                    if not dev:
                        continue
                    addr = str(dev.get("Address", "")).upper()
                    if addr == self.target and bool(dev.get("Connected", False)):
                        GLib.idle_add(self.cb, addr)
                        return
            except Exception:
                pass
            time.sleep(3)

# ---------------------------------------------------------------------------
# Packet parser
# ---------------------------------------------------------------------------

def parse_packet(data):
    if len(data) < 7:
        return {}
    rtype = data[3]
    r = {}
    if rtype == 0x01 and len(data) >= 13:
        try:
            r["battery"] = {
                "left": int(data[8]), "right": int(data[9]), "case": int(data[10]),
                "left_charging":  bool(data[11] & 0x01),
                "right_charging": bool(data[11] & 0x02),
                "case_charging":  bool(data[11] & 0x04),
            }
        except (IndexError, ValueError):
            pass
    elif rtype == 0x42 and len(data) > 9:
        try:
            vlen = int(data[6])
            r["firmware"] = data[8:8+vlen].decode("ascii", errors="ignore")
        except Exception:
            pass
    elif rtype == 0x06 and len(data) > 9:
        try:
            r["serial"] = data[8:-2].decode("ascii", errors="ignore").strip("\x00")
        except Exception:
            pass
    elif rtype == 0x1e and len(data) >= 11:
        anc_map = {0x05:"off",0x07:"transparency",0x01:"high",0x02:"mid",0x03:"low",0x04:"adaptive"}
        r["anc"] = anc_map.get(int(data[10]))
    elif rtype == 0x14 and len(data) >= 10:
        # Ear tip fit test result: bytes 8,9 = left_fit, right_fit  (0=good, 1=bad)
        try:
            r["fit_test"] = {
                "left":  "Good" if int(data[8]) == 0 else "Adjust",
                "right": "Good" if int(data[9]) == 0 else "Adjust",
            }
        except (IndexError, ValueError):
            pass
    return r

# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

APP_CSS = """
window { background-color: #0a0a0a; }
.topbar {
  background-color: #111111;
  border-bottom: 1px solid #1e1e1e;
  padding: 10px 20px;
  min-height: 52px;
}
.app-title {
  font-family: "Courier New", monospace;
  font-size: 16px; font-weight: bold;
  letter-spacing: 3px; color: #ffffff;
}
.app-sub {
  font-family: "Courier New", monospace;
  font-size: 10px; color: #444444; letter-spacing: 2px;
}
.conn-badge {
  font-family: "Courier New", monospace;
  font-size: 10px; padding: 4px 10px;
  border-radius: 20px; letter-spacing: 1px; font-weight: bold;
}
.badge-connected    { background-color: #0d2a1a; color: #00e676; border: 1px solid #00e676; }
.badge-disconnected { background-color: #1a1a1a; color: #555555; border: 1px solid #2a2a2a; }
.badge-connecting   { background-color: #1a1a00; color: #ffd600; border: 1px solid #ffd600; }
.section-label {
  font-family: "Courier New", monospace;
  font-size: 9px; letter-spacing: 3px; color: #444444;
  margin-bottom: 6px; margin-top: 4px;
}
.card {
  background-color: #131313; border: 1px solid #1e1e1e;
  border-radius: 14px; padding: 14px 16px; margin-bottom: 10px;
}
.card-inner {
  background-color: #0f0f0f; border: 1px solid #1a1a1a;
  border-radius: 10px; padding: 10px 14px;
  margin-top: 4px; margin-bottom: 4px;
}
.device-row {
  background-color: #131313; border: 1px solid #1e1e1e;
  border-radius: 10px; padding: 12px 14px;
  margin-top: 4px; margin-bottom: 4px;
}
.device-row-hi   { border-color: #00e676; background-color: #0d1f14; }
.device-name-lbl { font-family: "Courier New", monospace; font-size: 13px; font-weight: bold; color: #e8e8e8; }
.device-addr-lbl { font-family: "Courier New", monospace; font-size: 10px; color: #444444; letter-spacing: 1px; }
.dot-connected   { color: #00e676; }
.btn-connect {
  font-family: "Courier New", monospace; font-size: 11px; font-weight: bold;
  letter-spacing: 2px; background-color: #00b248; color: #000000;
  border-radius: 8px; padding: 8px 18px; min-width: 90px;
}
.btn-connect:hover    { background-color: #00c957; }
.btn-connect:disabled { background-color: #1e1e1e; color: #333333; }
.btn-disconnect {
  font-family: "Courier New", monospace; font-size: 11px; font-weight: bold;
  letter-spacing: 2px; background-color: transparent; color: #ff5252;
  border: 1px solid #ff5252; border-radius: 8px; padding: 8px 16px;
}
.btn-secondary {
  font-family: "Courier New", monospace; font-size: 11px;
  background-color: #1a1a1a; color: #888888;
  border: 1px solid #2a2a2a; border-radius: 8px; padding: 8px 14px;
}
.btn-green {
  font-family: "Courier New", monospace; font-size: 11px; font-weight: bold;
  background-color: #0d2a1a; color: #00e676;
  border: 1px solid #00e676; border-radius: 8px; padding: 8px 14px;
}
.mac-entry {
  font-family: "Courier New", monospace; font-size: 12px;
  color: #00e676; background-color: #0f0f0f;
  border: 1px solid #222222; border-radius: 8px;
  padding: 8px 12px; caret-color: #00e676;
}
.batt-label-top { font-family: "Courier New", monospace; font-size: 9px; color: #555555; letter-spacing: 2px; }
.batt-pct       { font-family: "Courier New", monospace; font-size: 20px; font-weight: bold; color: #e0e0e0; }
.batt-pct-charging { color: #ffd600; }
.batt-pct-low      { color: #ff5252; }
progressbar trough   { background-color: #1a1a1a; border-radius: 3px; min-height: 5px; }
progressbar progress { background-color: #00b248; border-radius: 3px; }
.anc-btn { font-family: "Courier New", monospace; font-size: 10px; font-weight: bold; letter-spacing: 1px; border-radius: 10px; padding: 12px 6px; border: 1px solid #222222; background-color: #111111; color: #555555; }
.anc-btn:hover  { background-color: #1a1a1a; color: #aaaaaa; }
.anc-btn-active { background-color: #0d2a1a; color: #00e676; border-color: #00e676; }
.eq-btn { font-family: "Courier New", monospace; font-size: 10px; font-weight: bold; letter-spacing: 1px; border-radius: 10px; padding: 10px 4px; border: 1px solid #1e1e1e; background-color: #111111; color: #555555; }
.eq-btn:hover  { background-color: #181818; color: #888888; }
.eq-btn-active { background-color: #0a1a2a; color: #64b5f6; border-color: #64b5f6; }
.eq-band-label { font-family: "Courier New", monospace; font-size: 9px; color: #555555; letter-spacing: 1px; }
.eq-band-value { font-family: "Courier New", monospace; font-size: 10px; color: #64b5f6; font-weight: bold; }
.toggle-label    { font-family: "Courier New", monospace; font-size: 12px; color: #cccccc; }
.toggle-sublabel { font-family: "Courier New", monospace; font-size: 10px; color: #444444; }
.gesture-label   { font-family: "Courier New", monospace; font-size: 11px; color: #aaaaaa; }
.gesture-side    { font-family: "Courier New", monospace; font-size: 10px; color: #555555; letter-spacing: 2px; }
.ring-btn { font-family: "Courier New", monospace; font-size: 10px; font-weight: bold; letter-spacing: 1px; border-radius: 10px; padding: 12px 4px; border: 1px solid #2a1a00; background-color: #130e00; color: #ffb74d; }
.ring-btn:hover   { background-color: #1e1500; }
.ring-btn-stop    { border-color: #222222; background-color: #111111; color: #555555; }
.ring-btn-stop:hover { color: #ff5252; border-color: #ff5252; }
.fit-result-good { font-family: "Courier New", monospace; font-size: 16px; font-weight: bold; color: #00e676; }
.fit-result-bad  { font-family: "Courier New", monospace; font-size: 16px; font-weight: bold; color: #ff5252; }
.fit-label       { font-family: "Courier New", monospace; font-size: 9px; color: #555555; letter-spacing: 2px; }
.info-key { font-family: "Courier New", monospace; font-size: 10px; color: #444444; letter-spacing: 1px; }
.info-val { font-family: "Courier New", monospace; font-size: 11px; color: #00e676; }
.autoconn-label { font-family: "Courier New", monospace; font-size: 11px; color: #aaaaaa; }
.statusbar { background-color: #0d0d0d; border-top: 1px solid #1a1a1a; padding: 6px 18px; min-height: 28px; }
.status-msg       { font-family: "Courier New", monospace; font-size: 10px; color: #555555; letter-spacing: 1px; }
.status-msg-error { color: #ff5252; }
.status-msg-ok    { color: #00e676; }
.status-msg-warn  { color: #ffd600; }
"""

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class NothingEarApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.nothing.ear.linux",
                         flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.bt  = BTManager()
        self.bt.on_data   = self._on_bt_data
        self.bt.on_status = self._on_bt_status

        self.cfg = load_config()
        self.watcher = AutoConnectWatcher(
            self.cfg.get("last_addr"), self._on_autoconnect
        )

        self.current_anc   = None
        self.current_eq    = "balanced"
        self.custom_eq     = [0, 0, 0, 0, 0]   # -6..+6 per band
        self._eq_sliders   = []
        self._eq_val_labels= []
        self.anc_btns      = {}
        self.eq_btns       = {}
        self.batt          = {}
        self.info_vals     = {}

        self.connect("activate", self._activate)

    # -----------------------------------------------------------------------
    def _activate(self, app):
        css = Gtk.CssProvider()
        css.load_from_data(APP_CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.win = Adw.ApplicationWindow(application=app, title="Nothing Ear")
        self.win.set_default_size(440, 860)
        self.win.set_resizable(True)
        self.win.set_content(self._build_ui())
        self.win.present()
        GLib.idle_add(self._do_scan)

        # Start auto-connect watcher if we have a saved device
        if self.cfg.get("autoconnect") and self.cfg.get("last_addr"):
            self.watcher.start(self.cfg["last_addr"])

    # -----------------------------------------------------------------------
    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(self._make_topbar())

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.set_margin_start(16); body.set_margin_end(16)
        body.set_margin_top(10);   body.set_margin_bottom(16)

        for title, widget in [
            ("DEVICE",           self._make_device_panel()),
            ("BATTERY",          self._make_battery_panel()),
            ("NOISE CONTROL",    self._make_anc_panel()),
            ("EQUALIZER",        self._make_eq_panel()),
            ("GESTURE CONTROLS", self._make_gesture_panel()),
            ("EAR TIP FIT TEST", self._make_fit_panel()),
            ("SETTINGS",         self._make_toggles_panel()),
            ("FIND MY BUDS",     self._make_find_panel()),
            ("DEVICE INFO",      self._make_info_panel()),
        ]:
            body.append(self._section(title, widget))

        scroll.set_child(body)
        root.append(scroll)
        root.append(self._make_statusbar())
        return root

    # -----------------------------------------------------------------------
    def _make_topbar(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        bar.add_css_class("topbar")
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        left.set_hexpand(True)
        t = Gtk.Label(label="NOTHING EAR"); t.add_css_class("app-title"); t.set_halign(Gtk.Align.START)
        s = Gtk.Label(label="LINUX COMPANION"); s.add_css_class("app-sub"); s.set_halign(Gtk.Align.START)
        left.append(t); left.append(s); bar.append(left)
        self.badge = Gtk.Label(label="DISCONNECTED")
        self.badge.add_css_class("conn-badge"); self.badge.add_css_class("badge-disconnected")
        bar.append(self.badge)
        return bar

    def _section(self, title, child):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        lbl = Gtk.Label(label=title); lbl.add_css_class("section-label"); lbl.set_halign(Gtk.Align.START)
        outer.append(lbl)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL); card.add_css_class("card")
        card.append(child); outer.append(card)
        return outer

    # -----------------------------------------------------------------------
    def _make_device_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hl = Gtk.Label(label="Paired Bluetooth Devices")
        hl.add_css_class("toggle-label"); hl.set_hexpand(True); hl.set_halign(Gtk.Align.START)
        hdr.append(hl)
        self.scan_btn = Gtk.Button(label="SCAN"); self.scan_btn.add_css_class("btn-secondary")
        self.scan_btn.connect("clicked", lambda _: self._do_scan()); hdr.append(self.scan_btn)
        box.append(hdr)
        self.dev_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(self.dev_list)
        sep = Gtk.Separator(); sep.set_margin_top(6); sep.set_margin_bottom(6); box.append(sep)
        ml = Gtk.Label(label="Enter MAC manually"); ml.add_css_class("toggle-sublabel"); ml.set_halign(Gtk.Align.START)
        box.append(ml)
        erow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.mac_entry = Gtk.Entry(); self.mac_entry.set_placeholder_text("3C:B0:ED:B5:E9:20")
        self.mac_entry.add_css_class("mac-entry"); self.mac_entry.set_hexpand(True)
        self.mac_entry.connect("activate", self._mac_go)
        # Pre-fill last used address
        if self.cfg.get("last_addr"):
            self.mac_entry.set_text(self.cfg["last_addr"])
        erow.append(self.mac_entry)
        go = Gtk.Button(label="GO"); go.add_css_class("btn-connect"); go.connect("clicked", self._mac_go)
        erow.append(go); box.append(erow)

        # Auto-connect toggle
        arow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        arow.add_css_class("card-inner")
        al = Gtk.Label(label="Auto-connect when earbuds come out of case")
        al.add_css_class("autoconn-label"); al.set_hexpand(True); al.set_halign(Gtk.Align.START)
        arow.append(al)
        self.autoconn_sw = Gtk.Switch(); self.autoconn_sw.set_valign(Gtk.Align.CENTER)
        self.autoconn_sw.set_active(bool(self.cfg.get("autoconnect", False)))
        self.autoconn_sw.connect("state-set", self._on_autoconn_toggle)
        arow.append(self.autoconn_sw); box.append(arow)

        self.disc_btn = Gtk.Button(label="DISCONNECT"); self.disc_btn.add_css_class("btn-disconnect")
        self.disc_btn.set_sensitive(False); self.disc_btn.set_margin_top(4)
        self.disc_btn.connect("clicked", lambda _: self._disconnect())
        box.append(self.disc_btn)
        return box

    def _make_device_row(self, dev):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.add_css_class("device-row")
        if is_audio_device(dev): row.add_css_class("device-row-hi")
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2); left.set_hexpand(True)
        nrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        if dev.get("connected"):
            dot = Gtk.Label(label="● "); dot.add_css_class("dot-connected"); nrow.append(dot)
        nlbl = Gtk.Label(label=dev["name"]); nlbl.add_css_class("device-name-lbl")
        nlbl.set_halign(Gtk.Align.START); nlbl.set_ellipsize(Pango.EllipsizeMode.END)
        nrow.append(nlbl); left.append(nrow)
        albl = Gtk.Label(label=dev["addr"]); albl.add_css_class("device-addr-lbl"); albl.set_halign(Gtk.Align.START)
        left.append(albl); row.append(left)
        btn = Gtk.Button(label="CONNECT"); btn.add_css_class("btn-connect")
        btn.connect("clicked", lambda _, a=dev["addr"]: self._connect_to(a))
        row.append(btn)
        return row

    # -----------------------------------------------------------------------
    def _make_battery_panel(self):
        self.batt = {}
        grid = Gtk.Grid(); grid.set_column_spacing(12); grid.set_row_spacing(10)
        grid.set_column_homogeneous(True)
        for col, (key, lbl) in enumerate([("left","LEFT"),("right","RIGHT"),("case","CASE")]):
            tl = Gtk.Label(label=lbl); tl.add_css_class("batt-label-top"); tl.set_halign(Gtk.Align.CENTER)
            grid.attach(tl, col, 0, 1, 1)
            pl = Gtk.Label(label="--"); pl.add_css_class("batt-pct"); pl.set_halign(Gtk.Align.CENTER)
            grid.attach(pl, col, 1, 1, 1)
            pb = Gtk.ProgressBar(); pb.set_fraction(0); grid.attach(pb, col, 2, 1, 1)
            self.batt[key] = {"lbl": pl, "bar": pb}
        rb = Gtk.Button(label="REFRESH"); rb.add_css_class("btn-secondary")
        rb.set_halign(Gtk.Align.CENTER); rb.set_margin_top(8)
        rb.connect("clicked", lambda _: self.bt.send(CMD_GET_BATTERY))
        grid.attach(rb, 0, 3, 3, 1)
        return grid

    # -----------------------------------------------------------------------
    def _make_anc_panel(self):
        self.anc_btns = {}
        modes = [("off","OFF"),("transparency","TRANSPARENT"),("low","ANC LOW"),
                 ("mid","ANC MID"),("high","ANC HIGH"),("adaptive","ADAPTIVE")]
        grid = Gtk.Grid(); grid.set_column_spacing(8); grid.set_row_spacing(8)
        grid.set_column_homogeneous(True)
        for i, (key, label) in enumerate(modes):
            btn = Gtk.Button(label=label); btn.add_css_class("anc-btn")
            btn.connect("clicked", self._anc_click, key)
            self.anc_btns[key] = btn; grid.attach(btn, i % 3, i // 3, 1, 1)
        return grid

    # -----------------------------------------------------------------------
    def _make_eq_panel(self):
        self.eq_btns = {}
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        # Preset row
        presets = [("balanced","BALANCED"),("more_bass","BASS +"),
                   ("more_treble","TREBLE +"),("voice","VOICE")]
        prow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        prow.set_homogeneous(True)
        for key, label in presets:
            btn = Gtk.Button(label=label); btn.add_css_class("eq-btn")
            btn.connect("clicked", self._eq_preset_click, key)
            self.eq_btns[key] = btn; prow.append(btn)
        box.append(prow)

        # Custom 5-band EQ
        sep = Gtk.Separator(); box.append(sep)

        custom_lbl = Gtk.Label(label="CUSTOM EQ")
        custom_lbl.add_css_class("section-label"); custom_lbl.set_halign(Gtk.Align.START)
        box.append(custom_lbl)

        bands_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        bands_box.set_homogeneous(True)
        band_names = ["60Hz", "250Hz", "1kHz", "4kHz", "14kHz"]
        self._eq_sliders    = []
        self._eq_val_labels = []

        for i, bname in enumerate(band_names):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            col.set_halign(Gtk.Align.CENTER)

            val_lbl = Gtk.Label(label="0")
            val_lbl.add_css_class("eq-band-value"); val_lbl.set_halign(Gtk.Align.CENTER)
            self._eq_val_labels.append(val_lbl); col.append(val_lbl)

            scale = Gtk.Scale(orientation=Gtk.Orientation.VERTICAL)
            scale.set_range(-6, 6); scale.set_value(0)
            scale.set_inverted(True)
            scale.set_size_request(30, 120)
            scale.set_draw_value(False)
            scale.set_round_digits(0)
            scale.connect("value-changed", self._eq_slider_changed, i)
            self._eq_sliders.append(scale); col.append(scale)

            name_lbl = Gtk.Label(label=bname)
            name_lbl.add_css_class("eq-band-label"); name_lbl.set_halign(Gtk.Align.CENTER)
            col.append(name_lbl)
            bands_box.append(col)

        box.append(bands_box)

        apply_btn = Gtk.Button(label="APPLY CUSTOM EQ")
        apply_btn.add_css_class("btn-green"); apply_btn.set_halign(Gtk.Align.CENTER)
        apply_btn.connect("clicked", self._apply_custom_eq)
        reset_btn = Gtk.Button(label="RESET")
        reset_btn.add_css_class("btn-secondary"); reset_btn.set_halign(Gtk.Align.CENTER)
        reset_btn.connect("clicked", self._reset_eq_sliders)
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(Gtk.Align.CENTER)
        btn_row.append(apply_btn); btn_row.append(reset_btn)
        box.append(btn_row)

        self._refresh_eq()
        return box

    def _eq_slider_changed(self, scale, idx):
        val = int(scale.get_value())
        self.custom_eq[idx] = val
        self._eq_val_labels[idx].set_label(f"{val:+d}" if val != 0 else "0")

    def _apply_custom_eq(self, _):
        if not self.bt.connected:
            self._status("Not connected", "error"); return
        cmd = build_custom_eq(self.custom_eq)
        if self.bt.send(cmd):
            self.current_eq = "custom"
            self._refresh_eq()
            self._status("Custom EQ applied", "ok")

    def _reset_eq_sliders(self, _):
        for i, s in enumerate(self._eq_sliders):
            s.set_value(0)
            self.custom_eq[i] = 0
            self._eq_val_labels[i].set_label("0")

    # -----------------------------------------------------------------------
    def _make_gesture_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        func_keys = list(GESTURE_FUNCTION_LABELS.keys())
        func_labels = list(GESTURE_FUNCTION_LABELS.values())
        self._gesture_dropdowns = {}

        for side in ("left", "right"):
            side_lbl = Gtk.Label(label=side.upper() + " EARBUD")
            side_lbl.add_css_class("gesture-side"); side_lbl.set_halign(Gtk.Align.START)
            side_lbl.set_margin_top(4)
            box.append(side_lbl)

            for action, action_label in [("double_tap","Double Tap"),
                                          ("triple_tap","Triple Tap"),
                                          ("long_press","Long Press")]:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
                row.add_css_class("card-inner")

                lbl = Gtk.Label(label=action_label)
                lbl.add_css_class("gesture-label"); lbl.set_hexpand(True); lbl.set_halign(Gtk.Align.START)
                row.append(lbl)

                model = Gtk.StringList()
                for fl in func_labels:
                    model.append(fl)

                dd = Gtk.DropDown(model=model)
                # Load saved or default
                saved_key = self.cfg.get(f"gesture_{side}_{action}", "play_pause" if action == "double_tap" else "anc_toggle" if action == "long_press" else "next_track")
                saved_idx = func_keys.index(saved_key) if saved_key in func_keys else 0
                dd.set_selected(saved_idx)
                dd.connect("notify::selected", self._gesture_changed, side, action, func_keys)
                self._gesture_dropdowns[f"{side}_{action}"] = dd
                row.append(dd)
                box.append(row)

        return box

    def _gesture_changed(self, dd, _, side, action, func_keys):
        idx = dd.get_selected()
        if idx >= len(func_keys):
            return
        func_key = func_keys[idx]
        # Save to config
        self.cfg[f"gesture_{side}_{action}"] = func_key
        save_config(self.cfg)
        # Send command
        if self.bt.connected:
            cmd = build_gesture_cmd(side, action, func_key)
            self.bt.send(cmd)
            self._status(f"Gesture set: {side} {action} -> {GESTURE_FUNCTION_LABELS[func_key]}", "ok")

    # -----------------------------------------------------------------------
    def _make_fit_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        desc = Gtk.Label(label="Tests whether ear tips are properly seated in your ears.")
        desc.add_css_class("toggle-sublabel"); desc.set_halign(Gtk.Align.START)
        desc.set_wrap(True)
        box.append(desc)

        test_btn = Gtk.Button(label="START FIT TEST")
        test_btn.add_css_class("btn-green"); test_btn.set_halign(Gtk.Align.START)
        test_btn.connect("clicked", self._start_fit_test)
        box.append(test_btn)

        result_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        result_row.set_halign(Gtk.Align.CENTER); result_row.set_margin_top(8)

        for side in ("left", "right"):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            col.set_halign(Gtk.Align.CENTER)
            rl = Gtk.Label(label="--"); rl.add_css_class("fit-result-good"); rl.set_halign(Gtk.Align.CENTER)
            sl = Gtk.Label(label=side.upper()); sl.add_css_class("fit-label"); sl.set_halign(Gtk.Align.CENTER)
            col.append(rl); col.append(sl)
            result_row.append(col)
            setattr(self, f"fit_lbl_{side}", rl)

        box.append(result_row)
        return box

    def _start_fit_test(self, _):
        if not self.bt.connected:
            self._status("Not connected", "error"); return
        self.fit_lbl_left.set_label("...")
        self.fit_lbl_right.set_label("...")
        self.bt.send(CMD_EAR_FIT)
        self._status("Running ear tip fit test...", "warn")

    # -----------------------------------------------------------------------
    def _make_toggles_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for label, sub, on_cmd, off_cmd in [
            ("In-Ear Detection",  "Auto-pause when earbuds removed",  CMD_IED_ON,    CMD_IED_OFF),
            ("Low Latency Mode",  "Reduce audio delay for gaming",     CMD_LOWLAT_ON, CMD_LOWLAT_OFF),
        ]:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row.add_css_class("card-inner")
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2); labels.set_hexpand(True)
            ll = Gtk.Label(label=label); ll.add_css_class("toggle-label"); ll.set_halign(Gtk.Align.START)
            sl = Gtk.Label(label=sub);   sl.add_css_class("toggle-sublabel"); sl.set_halign(Gtk.Align.START)
            labels.append(ll); labels.append(sl); row.append(labels)
            sw = Gtk.Switch(); sw.set_valign(Gtk.Align.CENTER)
            sw.connect("state-set", lambda s, v, oc=on_cmd, fc=off_cmd:
                       self.bt.send(oc if v else fc) or False)
            row.append(sw); box.append(row)
        return box

    # -----------------------------------------------------------------------
    def _make_find_panel(self):
        grid = Gtk.Grid(); grid.set_column_spacing(8); grid.set_column_homogeneous(True)
        items = [("LEFT",CMD_RING_L,False),("RIGHT",CMD_RING_R,False),
                 ("BOTH",CMD_RING_ALL,False),("STOP",CMD_RING_OFF,True)]
        for i, (lbl, cmd, stop) in enumerate(items):
            btn = Gtk.Button(label=lbl); btn.add_css_class("ring-btn")
            if stop: btn.add_css_class("ring-btn-stop")
            btn.connect("clicked", lambda _, c=cmd: self.bt.send(c))
            grid.attach(btn, i, 0, 1, 1)
        return grid

    # -----------------------------------------------------------------------
    def _make_info_panel(self):
        self.info_vals = {}
        grid = Gtk.Grid(); grid.set_column_spacing(16); grid.set_row_spacing(8)
        for row, (key, label) in enumerate([("model","MODEL"),("firmware","FIRMWARE"),
                                             ("serial","SERIAL"),("address","ADDRESS")]):
            k = Gtk.Label(label=label); k.add_css_class("info-key"); k.set_halign(Gtk.Align.START)
            v = Gtk.Label(label="--");  v.add_css_class("info-val");  v.set_halign(Gtk.Align.START)
            v.set_selectable(True)
            grid.attach(k, 0, row, 1, 1); grid.attach(v, 1, row, 1, 1)
            self.info_vals[key] = v
        return grid

    # -----------------------------------------------------------------------
    def _make_statusbar(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL); bar.add_css_class("statusbar")
        self.status_lbl = Gtk.Label(label="Ready -- scan or enter MAC to connect")
        self.status_lbl.add_css_class("status-msg"); self.status_lbl.set_halign(Gtk.Align.START)
        self.status_lbl.set_hexpand(True); self.status_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        bar.append(self.status_lbl)
        return bar

    # -----------------------------------------------------------------------
    # Scan + connect logic
    # -----------------------------------------------------------------------

    def _do_scan(self):
        self._status("Scanning for paired devices...", "warn")
        self.scan_btn.set_sensitive(False)
        threading.Thread(
            target=lambda: GLib.idle_add(self._populate_devices, get_paired_devices()),
            daemon=True
        ).start()

    def _populate_devices(self, devices):
        self.scan_btn.set_sensitive(True)
        while self.dev_list.get_first_child():
            self.dev_list.remove(self.dev_list.get_first_child())
        devices.sort(key=lambda d: (0 if is_audio_device(d) else 1, d["name"].lower()))
        if not devices:
            lbl = Gtk.Label(label="No paired devices found.\nPair earbuds via system Bluetooth settings first.")
            lbl.add_css_class("toggle-sublabel"); lbl.set_justify(Gtk.Justification.CENTER)
            lbl.set_margin_top(8); lbl.set_margin_bottom(8)
            self.dev_list.append(lbl)
            self._status("No paired devices -- pair earbuds in system settings first", "error")
            return
        for dev in devices:
            self.dev_list.append(self._make_device_row(dev))
        audio = [d for d in devices if is_audio_device(d)]
        self._status(
            f"Found {len(devices)} device(s)" +
            (f" -- {len(audio)} audio device(s) highlighted" if audio else ""),
            "ok" if audio else None
        )
        if len(audio) == 1 and not self.bt.connected:
            GLib.timeout_add(800, lambda: self._connect_to(audio[0]["addr"]) or False)

    def _connect_to(self, addr):
        if self.bt.connected:
            self.bt.disconnect()
        self._set_badge("connecting")
        self._status(f"Connecting to {addr}...", "warn")
        self.disc_btn.set_sensitive(False)
        self.bt.connect(addr)

    def _disconnect(self):
        self.watcher.stop()
        self.bt.disconnect()

    def _mac_go(self, *_):
        addr = self.mac_entry.get_text().strip().upper().replace("-",":").replace(".",":").replace(" ","")
        # Handle no-colon format
        if re.match(r"^[0-9A-F]{12}$", addr):
            addr = ":".join(addr[i:i+2] for i in range(0,12,2))
        if re.match(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$", addr):
            self._connect_to(addr)
        else:
            self._status("Invalid MAC -- must be XX:XX:XX:XX:XX:XX", "error")

    def _on_autoconn_toggle(self, sw, value):
        self.cfg["autoconnect"] = value
        save_config(self.cfg)
        if value and self.cfg.get("last_addr"):
            self.watcher.start(self.cfg["last_addr"])
            self._status("Auto-connect enabled -- watching for earbuds", "ok")
        else:
            self.watcher.stop()
            self._status("Auto-connect disabled", None)
        return False

    def _on_autoconnect(self, addr):
        if not self.bt.connected:
            self._status(f"Earbuds detected! Auto-connecting to {addr}...", "ok")
            self._connect_to(addr)

    # -----------------------------------------------------------------------
    # BT callbacks
    # -----------------------------------------------------------------------

    def _on_bt_status(self, connected, msg):
        if connected:
            self._set_badge("connected")
            self._status(msg, "ok")
            self.disc_btn.set_sensitive(True)
            self.info_vals["address"].set_label(self.bt.addr or "--")
            self.info_vals["model"].set_label("Nothing Ear 3(a)")
            # Save last connected address
            self.cfg["last_addr"] = self.bt.addr
            save_config(self.cfg)
            # Restart watcher if autoconnect enabled
            if self.cfg.get("autoconnect"):
                self.watcher.stop()
            GLib.timeout_add(300,  lambda: self.bt.send(CMD_GET_INFO)    or False)
            GLib.timeout_add(600,  lambda: self.bt.send(CMD_GET_SERIAL)  or False)
            GLib.timeout_add(900,  lambda: self.bt.send(CMD_GET_BATTERY) or False)
            GLib.timeout_add(1200, lambda: self.bt.send(CMD_GET_ANC)     or False)
            GLib.timeout_add(60000, self._battery_poll)
        else:
            self._set_badge("disconnected")
            self._status(msg, "error" if any(w in msg.lower() for w in
                         ["fail","denied","refused","lost","timeout","down"]) else None)
            self.disc_btn.set_sensitive(False)
            self._reset_ui()
            # Resume watcher if autoconnect on
            if self.cfg.get("autoconnect") and self.cfg.get("last_addr"):
                GLib.timeout_add(3000, lambda: self.watcher.start(self.cfg["last_addr"]) or False)

    def _on_bt_data(self, data):
        p = parse_packet(data)
        if "battery" in p:
            b = p["battery"]
            for k in ("left","right","case"):
                pct  = b.get(k)
                chrg = b.get(k+"_charging", False)
                w = self.batt.get(k)
                if w and isinstance(pct, int) and 0 <= pct <= 100:
                    w["lbl"].set_label(f"{pct}%" + (" +" if chrg else ""))
                    w["bar"].set_fraction(pct / 100)
                    for c in ("batt-pct-charging","batt-pct-low"):
                        w["lbl"].remove_css_class(c)
                    if chrg:        w["lbl"].add_css_class("batt-pct-charging")
                    elif pct <= 20: w["lbl"].add_css_class("batt-pct-low")
        if p.get("firmware"):
            self.info_vals["firmware"].set_label(p["firmware"])
        if p.get("serial"):
            self.info_vals["serial"].set_label(p["serial"])
        if p.get("anc") and p["anc"] in self.anc_btns:
            self.current_anc = p["anc"]; self._refresh_anc()
        if "fit_test" in p:
            ft = p["fit_test"]
            for side in ("left","right"):
                lbl = getattr(self, f"fit_lbl_{side}", None)
                if lbl:
                    result = ft.get(side, "--")
                    lbl.set_label(result)
                    lbl.remove_css_class("fit-result-good"); lbl.remove_css_class("fit-result-bad")
                    lbl.add_css_class("fit-result-good" if result == "Good" else "fit-result-bad")
            self._status("Fit test complete", "ok")

    def _battery_poll(self):
        if self.bt.connected:
            self.bt.send(CMD_GET_BATTERY); return True
        return False

    # -----------------------------------------------------------------------
    # ANC / EQ
    # -----------------------------------------------------------------------

    def _anc_click(self, _, key):
        if not self.bt.connected:
            self._status("Not connected", "error"); return
        if self.bt.send(CMD_ANC[key]):
            self.current_anc = key; self._refresh_anc()

    def _eq_preset_click(self, _, key):
        if not self.bt.connected:
            self._status("Not connected", "error"); return
        if self.bt.send(CMD_EQ_PRESET[key]):
            self.current_eq = key; self._refresh_eq()

    def _refresh_anc(self):
        for k, b in self.anc_btns.items():
            if k == self.current_anc:
                b.remove_css_class("anc-btn"); b.add_css_class("anc-btn-active")
            else:
                b.remove_css_class("anc-btn-active"); b.add_css_class("anc-btn")

    def _refresh_eq(self):
        for k, b in self.eq_btns.items():
            if k == self.current_eq:
                b.remove_css_class("eq-btn"); b.add_css_class("eq-btn-active")
            else:
                b.remove_css_class("eq-btn-active"); b.add_css_class("eq-btn")

    # -----------------------------------------------------------------------
    def _set_badge(self, state):
        txt = {"connected":"● CONNECTED","disconnected":"● DISCONNECTED","connecting":"◌ CONNECTING"}
        self.badge.set_label(txt.get(state, state))
        for c in ("badge-connected","badge-disconnected","badge-connecting"):
            self.badge.remove_css_class(c)
        self.badge.add_css_class("badge-" + state)

    def _status(self, msg, kind=None):
        self.status_lbl.set_label(msg)
        for c in ("status-msg-error","status-msg-ok","status-msg-warn"):
            self.status_lbl.remove_css_class(c)
        if kind == "error": self.status_lbl.add_css_class("status-msg-error")
        elif kind == "ok":  self.status_lbl.add_css_class("status-msg-ok")
        elif kind == "warn":self.status_lbl.add_css_class("status-msg-warn")

    def _reset_ui(self):
        for w in self.batt.values():
            w["lbl"].set_label("--"); w["bar"].set_fraction(0)
        for v in self.info_vals.values():
            v.set_label("--")
        self.current_anc = None; self._refresh_anc()


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app = NothingEarApp()
    app.run(None)
