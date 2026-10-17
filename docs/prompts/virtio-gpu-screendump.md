# Agent prompt: virtio-gpu VM + QMP screendump — end-to-end display validation

**Goal: boot a VM with a real virtio-gpu framebuffer, play video, and capture a screenshot. If it renders, the display path is proven.**

Repository is at `/home/michael/rabbitears`. Read `vm/provision.sh`, `vm/vmctl.py`, `vm/VAGRANT.md`, and `alpine/packages.txt` in full before making any changes.

---

## Context

The VM currently uses cirrus + VNC — a software-only display device unrelated to what the Wyse 3040 will run. The target is `virtio-gpu-pci` + VAAPI in DRM mode, and we need visual proof that the GPU path actually renders. This prompt closes that gap in two parts:

1. **Upgrade `provision.sh`** — swap cirrus+VNC for virtio-gpu+SPICE in the libvirt XML.
2. **Add QMP `screendump` to `vmctl.py`** — capture a PNG from a headless VM via the QEMU monitor socket. This is the primary screenshot path; it works without any VNC endpoint.

The VNC-based screenshot already in `vmctl.py` remains in place (it's useful for live VNC sessions); the QMP path supplements it.

---

## Part 1: `vm/provision.sh` — replace cirrus+VNC with virtio-gpu+SPICE

In **all three XML domain blocks** (`create`, `diskboot`, and `liveboot`), replace the graphics and video stanzas:

```xml
<!-- BEFORE (remove this) -->
<graphics type='vnc' port='-1' listen='0.0.0.0'/>
<video><model type='cirrus' vram='16384' heads='1'/></video>
```

```xml
<!-- AFTER (use this) -->
<graphics type='spice' autoport='yes' listen='127.0.0.1'>
  <listen type='address' address='127.0.0.1'/>
  <image compression='off'/>
</graphics>
<video>
  <model type='virtio' heads='1' primary='yes'/>
</video>
<channel type='spicevmc'>
  <target type='virtio' name='com.redhat.spice.0'/>
</channel>
```

Leave `machine='pc'` unchanged in `diskboot` and `liveboot`. Only replace the graphics/video devices.

Also in the `create` block: add a QMP monitor socket so the installed VM exposes a QMP endpoint by default. Add this device after the `<serial>` stanza:

```xml
<channel type='unix'>
  <source mode='bind' path='/tmp/rabbitears-qmp.sock'/>
  <target type='virtio' name='org.qemu.guest_agent.0'/>
</channel>
```

Note: a proper QMP socket is added via `-qmp` in QEMU args (see Part 2 for `vmctl.py`). The libvirt channel above is the guest-agent channel; `vmctl.py`'s `--monitor` flag handles the QMP socket separately when running under `vmctl.py` rather than libvirt.

In the host prerequisites section (the `apt-get install` block):
- Remove `vinagre` from the install list — it is VNC-only and no longer needed.
- Add `virt-viewer` and `spicy` (package `spice-client-gtk`) if not already present.

Remove the `VM_VNC_PORT` variable at the top of the script and all references to it (including the `(VNC is still at localhost:${VM_VNC_PORT}...)` line in the next-steps message). Replace that line with:

```
   3. Watch it:   virt-viewer --connect qemu:///system ${VM_NAME}
```

---

## Part 2: `alpine/packages.txt` — add mesa packages for virtio-gpu

Append at the end of `packages.txt`:

```
# --- VM only: mesa drivers for virtio-gpu (not needed on the Wyse 3040) ---
mesa-dri-gallium      # virtio-gpu DRM/GL driver
mesa-gl               # OpenGL runtime for mpv --vo=gpu inside the VM
```

---

## Part 3: `vm/vmctl.py` — QMP monitor socket + screendump

### 3a. `qemu_args()` — add `monitor_sock` and `display` parameters

Add two keyword parameters to `qemu_args()`:

- `monitor_sock: Path | None = None` — when set, append `-qmp unix:<path>,server,nowait` to the QEMU args.
- `display: str = "none"` — controls the display device:
  - `"none"` (default): keep `-display none` (current behaviour).
  - `"spice"`: replace `-display none` with:
    ```
    -spice port=5910,disable-ticketing=on
    -device virtio-serial-pci
    -chardev spicevmc,id=spicechannel0,name=vdagent
    -device virtserialport,chardev=spicechannel0,name=com.redhat.spice.0
    ```

These parameters do not affect anything else in `qemu_args()`.

### 3b. New function: `screendump(monitor_sock, output_path)`

```python
def screendump(monitor_sock: Path | str, output_path: Path | str) -> None:
```

Connects to the QMP socket at `monitor_sock`, performs the QMP handshake, sends `screendump`, and tells QEMU to write the frame to `output_path` (a `.ppm` file). QEMU writes the file itself — `output_path` must be an absolute path string that the QEMU process can write.

QMP handshake sequence:
1. Read the server greeting: `{"QMP": {"version": ..., "capabilities": [...]}}`
2. Send: `{"execute": "qmp_capabilities"}`
3. Read the `{"return": {}}` acknowledgement
4. Send: `{"execute": "screendump", "arguments": {"filename": "<str(output_path)>"}}`
5. Read the `{"return": {}}` acknowledgement

Use only stdlib (`socket`, `json`). Each exchange is a single newline-delimited JSON message. Raise `RuntimeError` on any protocol error (wrong message type, non-empty error key).

### 3c. New function: `ppm_to_png(ppm_path, png_path)`

```python
def ppm_to_png(ppm_path: Path | str, png_path: Path | str) -> None:
```

Converts the PPM file written by QEMU to a PNG. Try in order:
1. `PIL` / Pillow (`from PIL import Image`) — if installed, open the PPM and save as PNG.
2. `subprocess` call to `convert` (ImageMagick) — if `PIL` is not available.
3. Fall back: copy the PPM to `png_path` unchanged and print a warning to stderr — never raise.

### 3d. New subcommand: `vmctl.py screenshot`

```
vmctl.py screenshot --monitor SOCK --out FILE.png
```

Connects to an already-running VM's QMP socket at `SOCK`, issues `screendump` to a temp PPM in `tempfile.mkdtemp()`, converts to PNG at `FILE.png`, prints the output path on success. Best-effort: any failure prints a warning to stderr and exits 0.

### 3e. Changes to `cmd_run()`

Add two new arguments to the `run` subparser:

- `--monitor SOCK` — path to the QMP Unix socket. When provided, pass `Path(args.monitor)` to `qemu_args()` as `monitor_sock=`.
- `--display {none,spice}` — default `none`. Pass through to `qemu_args()` as `display=`. When `display=spice`, also print to stderr:
  ```
  [vmctl] SPICE display on port 5910 — connect with: spicy --host=127.0.0.1 --port=5910
  ```

Do not change the existing `--screenshot` / VNC behaviour.

---

## Part 4: `vm/VAGRANT.md` — document the new paths

Add a section **"Taking a screenshot via QMP (headless)"** after the existing "Screenshot (visual confirmation)" section:

```markdown
## Taking a screenshot via QMP (headless)

The QMP `screendump` path works without a VNC or SPICE display window. Boot with
`--monitor` and optionally `--screenshot` to capture automatically at shutdown:

```sh
# Terminal 1: boot with a QMP socket
python3 vm/vmctl.py run --disk /path/to/rabbitears-vm.qcow2 \
    --monitor /tmp/qmp.sock

# Terminal 2: grab a frame from the running VM
python3 vm/vmctl.py screenshot --monitor /tmp/qmp.sock --out /tmp/screen.png
```

QEMU writes a PPM framebuffer; `vmctl` converts it to PNG automatically (using
Pillow if available, otherwise ImageMagick's `convert`, otherwise copies the PPM
as-is). Unlike the VNC grab, this path does not require a VNC port and works even
when `-display none` is in effect.
```

Add a section **"Live display with SPICE"**:

```markdown
## Live display with SPICE

Boot with `--display spice` to open a SPICE server on port 5910 and watch the
VM output live:

```sh
python3 vm/vmctl.py run --disk /path/to/rabbitears-vm.qcow2 --display spice

# From another terminal (or after connecting over SSH):
spicy --host=127.0.0.1 --port=5910
# or
virt-viewer spice://127.0.0.1:5910
```

The SPICE server is on localhost only and requires no password
(`disable-ticketing=on`). Combine with `--monitor /tmp/qmp.sock` to get both
a live view and the QMP screendump capability.
```

---

## Part 5: `tests/test_vmctl.py` — new tests

Add a class `QmpScreendumpTest` with at least:

- `test_qemu_args_includes_qmp_socket` — verify that when `monitor_sock=Path("/tmp/qmp.sock")` is passed to `qemu_args()`, the string `-qmp` and `unix:/tmp/qmp.sock,server,nowait` both appear in the returned list.
- `test_qemu_args_no_monitor_by_default` — verify `-qmp` does **not** appear when `monitor_sock` is not provided.
- `test_qemu_args_display_none_default` — verify `-display` and `none` appear by default.
- `test_qemu_args_display_spice` — verify the SPICE port and device flags appear and `-display none` is absent when `display="spice"`.
- `test_screendump_sends_correct_qmp_sequence` — use `unittest.mock.patch` on `socket.create_connection` to inject a mock socket. Feed it the server greeting and two `{"return": {}}` messages (for `qmp_capabilities` and `screendump`). Assert that the calls to `sendall` contained (in order) `"qmp_capabilities"` and `"screendump"` as substrings.
- `test_screendump_raises_on_protocol_error` — feed a mock socket a greeting followed by `{"error": {"class": "CommandNotFound", "desc": "screendump"}}`. Assert `RuntimeError` is raised.

All **existing** tests must still pass.

---

## Completion criteria

1. Run `python3 -m unittest discover tests/ -v` from the repo root. All tests pass.
2. The three domain XML blocks in `provision.sh` use `virtio` video and `spice` graphics.
3. `vm/vmctl.py run --disk DISK --monitor /tmp/qmp.sock` includes `-qmp unix:/tmp/qmp.sock,server,nowait` in the QEMU command.
4. `vm/vmctl.py screenshot --monitor /tmp/qmp.sock --out /tmp/screen.png` connects, sends the QMP sequence, and produces a PNG (or PPM fallback).

---

## Report back with

- Summary of every file changed and what changed in each.
- Full `python3 -m unittest discover tests/ -v` output.
- Which `ppm_to_png` fallback path was selected (PIL / convert / copy).
- Any QMP protocol quirks or SPICE config issues encountered.
