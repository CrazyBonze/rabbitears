# Zapbox — Remote Dossier

> **Goal:** Turn a Dell Wyse 3040 (running Arch) into a dumb streaming box where
> folders are "channels" — old-school cable/OTA-style zapping, controlled by a
> 2.4GHz wireless remote. This document is the verified reference for the remote.

**Date:** 2026-10-04 · **Learned on:** michael-EVO-X2 (X11) · **Remote photo:** [remote.jpg](remote.jpg)

---

## 1. Hardware

| Property | Value |
|---|---|
| Vendor / Product | Adomax Technology Co., Ltd — "2.4G Composite Devic" |
| USB ID | **`0627:697d`** |
| Type | 2.4GHz wireless nano receiver (pre-paired with remote, factory) |
| USB spec | USB 1.1, 50mA, bus-powered, **Remote Wakeup capable** |
| Kernel drivers | `hid-generic` (no vendor driver needed) |
| Input interfaces | 3: keyboard (boot protocol), consumer control, system control — plus a 5-button mouse (gyro) |
| udev paths | `/dev/input/by-id/usb-0627_2.4G_Composite_Devic-*` |

**Remote form factor:** 13 physical controls (see photo) — red power, mute,
mouse-mode, sun/brightness, screen-arrow, D-pad + OK, home, back, menu,
and a VOL rocker. No QWERTY keyboard.

## 2. Complete Button Map (verified by live capture)

| Button (icon) | evdev code | Keysym | Interface |
|---|---|---|---|
| 🔴 Power (red, top-left) | `KEY_POWER` (116) | `XF86PowerOff` | System Control |
| Mute (speaker-X) | `KEY_MUTE` (113) | `XF86AudioMute` | Consumer Control |
| Mouse (mouse icon) | *none — local gyro-mode toggle* | — | — |
| Sun (brightness) | `KEY_BACKSPACE` (14) | `BackSpace` | Keyboard |
| Screen-arrow (input switch) | `KEY_DELETE` (111) | `Delete` | Keyboard |
| OK (center of D-pad) | `KEY_ENTER` (28) | `Return` | Keyboard |
| D-pad ↑ | `KEY_UP` (103) | `Up` | Keyboard |
| D-pad ↓ | `KEY_DOWN` (108) | `Down` | Keyboard |
| D-pad ← | `KEY_LEFT` (105) | `Left` | Keyboard |
| D-pad → | `KEY_RIGHT` (106) | `Right` | Keyboard |
| Home (house) | `KEY_HOMEPAGE` (172) | `XF86HomePage` | Consumer Control |
| Back (curved arrow) | `KEY_BACK` (158) | `XF86Back` | Consumer Control |
| Menu (≡) | `KEY_COMPOSE` (127) | `Menu` | Keyboard |
| VOL rocker − | `KEY_VOLUMEDOWN` (114) | `XF86AudioLowerVolume` | Consumer Control |
| VOL rocker + | `KEY_VOLUMEUP` (115) | `XF86AudioRaiseVolume` | Consumer Control |

## 3. Verified Behaviors

- ✅ **Clean press/release pairs, no key repeats** — the app owns repeat
  logic (e.g., hold ↓ to keep zapping channels).
- ✅ **No play/pause key** — fits the "folders = channels" design: you just zap.
- ⚠️ **Gyro air-mouse is weak/unreliable** — cursor barely moves when waving.
  The mouse-icon button only toggles the remote's local gyro mode (sends no
  key event). **Design the UI for D-pad + OK only; ignore the mouse interface.**
- ✅ Remote supports USB remote wakeup (can wake the Wyse from sleep).

## 4. Suggested Role Mapping (draft)

| Button | Role in Zapbox |
|---|---|
| D-pad ↑ / ↓ | **Channel up / down** (hold to auto-repeat) |
| OK | Select / toggle info OSD |
| Home | Channel guide |
| Back | Close OSD / stop |
| Menu | Settings |
| VOL rocker | Volume |
| Mute | Mute |
| Sun (`KEY_BACKSPACE`) | Show channel info / OSD |
| Screen-arrow (`KEY_DELETE`) | Jump to channel guide |
| Power | Standby / wake |
| Mouse icon | Unused |

## 5. Arch Build Notes

- Match the dongle with a udev rule on vendor `0x0627` (or use the
  `by-id` symlinks above) so the app can find it regardless of port.
- Read **all three** input interfaces (keyboard, consumer, system) — the
  buttons are split across them. Simplest: open every `/dev/input/event*`
  whose `ID_USB_VENDOR_ID=0627` and filter by code.
- No vendor driver, no config files, no pairing steps — plug and play.
- The Wyse 3040 (Atom x5-Z8400, 2GB RAM) is underpowered for heavy stacks;
  plan for a lightweight player (mpv) + a small controller process.

## 6. Project State

- [x] Remote identified and fully mapped
- [x] Photo archived
- [ ] Name / branding (working title: **zapbox**)
- [ ] UI flow design (guide screen, zapping behavior, mid-playback OK)
- [ ] Wyse 3040: Arch install
- [ ] Controller app + player
- [ ] udev rule + autostart
