# Nothing Ear Linux 🎧

Unofficial Linux companion app for Nothing Ear 3(a) — and compatible with Ear (1), (2), (a), CMF Buds.

Replicates the core features of the Nothing X Android app using the reverse-engineered RFCOMM Bluetooth protocol.

---

## Features

| Feature | Status |
|---|---|
| ANC (Off / Transparency / Low / Mid / High / Adaptive) | ✅ |
| Battery levels (L / R / Case) | ✅ |
| EQ Presets (Balanced / Bass+ / Treble+ / Voice) | ✅ |
| In-Ear Detection toggle | ✅ |
| Low Latency Mode toggle | ✅ |
| Find My Buds (ring L / R / both) | ✅ |
| Device Info (firmware, serial, MAC) | ✅ |
| Auto-scan for paired Nothing devices | ✅ |

---

## Requirements

```bash
sudo apt install python3 python3-gi python3-gi-cairo \
     gir1.2-gtk-4.0 gir1.2-adw-1 \
     bluez bluetooth
```

> No extra Python packages needed — uses standard library `socket` (AF_BLUETOOTH) + D-Bus.

---

## Setup

### 1. Pair your earbuds

```bash
bluetoothctl
> scan on
> pair XX:XX:XX:XX:XX:XX
> trust XX:XX:XX:XX:XX:XX
> connect XX:XX:XX:XX:XX:XX
> quit
```

Or use GNOME Bluetooth settings (Settings → Bluetooth).

### 2. Run the app

```bash
python3 nothing_ear_linux.py
```

The app will automatically scan for paired Nothing devices. Select yours from the dropdown and click **CONNECT**.

---

## Bluetooth Permissions

On some distros, connecting raw RFCOMM sockets requires your user to be in the `bluetooth` group:

```bash
sudo usermod -aG bluetooth $USER
# then log out and back in
```

---

## Troubleshooting

**"Connection failed: [Errno 13] Permission denied"**
→ Add yourself to the `bluetooth` group (see above), or run once with `sudo python3 nothing_ear_linux.py` to verify.

**"Connection failed: [Errno 111] Connection refused"**
→ Make sure the earbuds are out of the case and powered on. Try disconnecting from your phone first.

**Device not showing in dropdown**
→ Make sure you've paired (not just discovered) the earbuds. Use `bluetoothctl devices` to verify. You can also type the MAC address manually.

**ANC commands sent but no change**
→ The Ear 3(a) uses the same protocol family as Ear (2). If a command doesn't work, the device may use a slightly different packet. The app will be updated as more packets are documented.

---

## Protocol Notes

Nothing Ear devices communicate over **Bluetooth Classic RFCOMM**, channel 15.  
Packets start with `0x55` and follow a common format across the Ear product line.  
Protocol was reverse-engineered by the community (see bharadwaj-raju.github.io/posts/nothing-ear-2-on-linux/).

---

## Desktop Shortcut

Create `~/.local/share/applications/nothing-ear.desktop`:

```ini
[Desktop Entry]
Name=Nothing Ear
Comment=Nothing Ear Linux Companion
Exec=python3 /path/to/nothing_ear_linux.py
Icon=audio-headphones
Terminal=false
Type=Application
Categories=Audio;
```

---

*Not affiliated with Nothing Technology Limited.*
