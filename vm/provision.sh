#!/usr/bin/env bash
#
# Rabbit Ears — reproducible KVM VM provisioning (host side).
#
# Runs on the desktop (EVO-X2). Idempotent: safe to re-run. Pins a specific
# Alpine release + SHA256 so the VM is always built from the same inputs.
#
# Usage (self-elevates under the NOPASSWD rule from vm/setup-sudoers.sh):
#   ./vm/provision.sh            # create (if needed) + install the base OS
#   ./vm/provision.sh status     # show VM / ISO / disk status
#   ./vm/provision.sh stop       # shut the domain down (idempotent)
#   ./vm/provision.sh eject      # detach the ISO so the VM boots from disk
#   ./vm/provision.sh destroy    # delete the VM + its disk (keeps the ISO)
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Pinned, reproducible inputs
# ---------------------------------------------------------------------------
ALPINE_VERSION="3.24.2"
ALPINE_ISO="alpine-standard-${ALPINE_VERSION}-x86_64.iso"
ALPINE_SHA256="20c026e3a788bfb75fc8b50a54bcc12aee85e3c75909740bba6a6f4563d63296"
ALPINE_URL="https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/x86_64/${ALPINE_ISO}"

# VM spec — mirrors the Wyse 3040 (4 Atom cores, 2GB RAM, 8GB eMMC).
VM_NAME="rabbitears-vm"
VM_VCPUS=4
VM_RAM_MB=2048
VM_DISK_GB=8
VM_NET="default"          # libvirt NAT network

# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
# VM storage lives under /var/lib so the unprivileged libvirt-qemu user can
# always traverse to it (a path under $HOME is not world-traversable).
VM_BASE="/var/lib/rabbitears-vm"
ISO_PATH="${VM_BASE}/isos/${ALPINE_ISO}"
BOOT_DIR="${VM_BASE}/boot"                 # kernel + initrd extracted from the ISO
DISK_PATH="/var/lib/libvirt/images/${VM_NAME}.qcow2"
SERIAL_PORT="${RB_SERIAL_PORT:-5555}"      # TCP serial the installer attaches to
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RAM_KIB=$((VM_RAM_MB * 1024))

log()  { printf '\033[1;32m[provision]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[provision]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[provision]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Privilege: run as root (re-exec with sudo if needed).
# ---------------------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
  exec sudo "$0" "$@"
fi
REAL_USER="${SUDO_USER:-root}"

cmd="${1:-create}"

# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
if [ "$cmd" = "status" ]; then
  virsh list --all
  echo
  echo "ISO:  ${ISO_PATH} $([ -f "$ISO_PATH" ] && echo '(present)' || echo '(missing)')"
  if virsh list --all --name 2>/dev/null | grep -qx "$VM_NAME"; then
    echo "Disk: $(virsh domblklist "$VM_NAME" 2>/dev/null | awk 'NR>1 && $2!="" {print $4}')"
  else
    echo "Disk: (no VM)"
  fi
  exit 0
fi

# ---------------------------------------------------------------------------
# console — dump the current serial console output (debugging; runs as root)
# ---------------------------------------------------------------------------
if [ "$cmd" = "console" ]; then
  pty="$(virsh dumpxml "$VM_NAME" 2>/dev/null | sed -n "s/.*<source path='\([^']*\)'.*/\1/p" | head -1)"
  if [ -z "$pty" ]; then echo "no serial pty (is the VM running?)"; exit 1; fi
  python3 - "$pty" <<'PY'
import sys, time
import serial
pty = sys.argv[1]
ser = serial.Serial(pty, timeout=0.5)
ser.write(b"\r\n"); time.sleep(0.5)
d = b""
for _ in range(40):
    d += ser.read(ser.in_waiting or 1); time.sleep(0.1)
ser.close()
print(d.decode("utf-8", "replace")[-3000:])
PY
  exit 0
fi

# ---------------------------------------------------------------------------
# console-cmd <cmd> — run a command on the serial console and read the result
# ---------------------------------------------------------------------------
if [ "$cmd" = "console-cmd" ]; then
  c="${2:-lsblk}"
  pty="$(virsh dumpxml "$VM_NAME" 2>/dev/null | sed -n "s/.*<source path='\([^']*\)'.*/\1/p" | head -1)"
  if [ -z "$pty" ]; then echo "no serial pty (is the VM running?)"; exit 1; fi
  python3 - "$pty" "$c" <<'PY'
import sys, time
import serial
pty, c = sys.argv[1], sys.argv[2]
ser = serial.Serial(pty, timeout=0.2)
time.sleep(0.3)
ser.write((c + "\r\n").encode())
d = b""
for _ in range(50):
    d += ser.read(ser.in_waiting or 1); time.sleep(0.05)
ser.close()
print(d.decode("utf-8", "replace")[-2000:])
PY
  exit 0
fi

# ---------------------------------------------------------------------------
# diskboot — switch the VM to boot from disk (the installed system) + start it
# ---------------------------------------------------------------------------
if [ "$cmd" = "diskboot" ]; then
  pkill -f "guest=${VM_NAME}" 2>/dev/null || true
  sleep 1
  virsh undefine "$VM_NAME" 2>/dev/null || true
  cat > "/tmp/${VM_NAME}-diskboot.xml" <<XML
<domain type='kvm'>
  <name>${VM_NAME}</name>
  <memory unit='KiB'>${RAM_KIB}</memory>
  <currentMemory unit='KiB'>${RAM_KIB}</currentMemory>
  <vcpu placement='static'>${VM_VCPUS}</vcpu>
  <os>
    <type arch='x86_64' machine='pc'>hvm</type>
    <boot dev='hd'/>
  </os>
  <features><acpi/><apic/></features>
  <cpu mode='host-passthrough'/>
  <clock offset='utc'/>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <on_crash>destroy</on_crash>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='${DISK_PATH}'/>
      <target dev='hda' bus='ide'/>
    </disk>
    <serial type='pty'><target port='0'/></serial>
    <input type='tablet' bus='usb'/>
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
    <interface type='network'>
      <source network='${VM_NET}'/>
      <model type='e1000'/>
    </interface>
    <memballoon model='virtio'/>
  </devices>
</domain>
XML
  virsh define "/tmp/${VM_NAME}-diskboot.xml"
  virsh start "$VM_NAME"
  rm -f "/tmp/${VM_NAME}-diskboot.xml"
  log "VM '$VM_NAME' switched to disk boot and started."
  exit 0
fi

# ---------------------------------------------------------------------------
# liveboot — boot the live ISO (kernel boot) for debugging the installed disk
# ---------------------------------------------------------------------------
if [ "$cmd" = "liveboot" ]; then
  pkill -f "guest=${VM_NAME}" 2>/dev/null || true
  sleep 1
  virsh undefine "$VM_NAME" 2>/dev/null || true
  cat > "/tmp/${VM_NAME}-live.xml" <<XML
<domain type='kvm'>
  <name>${VM_NAME}</name>
  <memory unit='KiB'>${RAM_KIB}</memory>
  <currentMemory unit='KiB'>${RAM_KIB}</currentMemory>
  <vcpu placement='static'>${VM_VCPUS}</vcpu>
  <os>
    <type arch='x86_64' machine='pc'>hvm</type>
    <kernel>${BOOT_DIR}/vmlinuz-lts</kernel>
    <initrd>${BOOT_DIR}/initramfs-lts</initrd>
    <cmdline>modules=loop,squashfs,sd-mod,usb-storage console=ttyS0</cmdline>
  </os>
  <features><acpi/><apic/></features>
  <cpu mode='host-passthrough'/>
  <clock offset='utc'/>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <on_crash>destroy</on_crash>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='${DISK_PATH}'/>
      <target dev='hda' bus='ide'/>
    </disk>
    <serial type='pty'><target port='0'/></serial>
    <input type='tablet' bus='usb'/>
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
    <interface type='network'>
      <source network='${VM_NET}'/>
      <model type='e1000'/>
    </interface>
    <memballoon model='virtio'/>
  </devices>
</domain>
XML
  virsh define "/tmp/${VM_NAME}-live.xml"
  virsh start "$VM_NAME"
  rm -f "/tmp/${VM_NAME}-live.xml"
  log "VM '$VM_NAME' started with the live ISO (kernel boot)."
  exit 0
fi

# ---------------------------------------------------------------------------
# proxy — start the local Alpine CDN caching proxy (background, as root)
# ---------------------------------------------------------------------------
if [ "$cmd" = "proxy" ]; then
  if pgrep -f "apk-proxy.py" >/dev/null 2>&1; then
    log "apk-proxy already running (pid $(pgrep -f 'apk-proxy.py' | head -1))."
  else
    nohup python3 "${REPO_ROOT}/vm/apk-proxy.py" > /var/log/rabbitears-apk-proxy.log 2>&1 &
    log "apk-proxy started (pid $!) on 192.168.122.1:8081; log: /var/log/rabbitears-apk-proxy.log."
  fi
  exit 0
fi

# ---------------------------------------------------------------------------
# destroy
# ---------------------------------------------------------------------------
if [ "$cmd" = "destroy" ]; then
  if virsh list --all --name 2>/dev/null | grep -qx "$VM_NAME"; then
    log "Destroying VM '$VM_NAME' (and its disk)..."
    virsh destroy "$VM_NAME" 2>/dev/null || true
    virsh undefine "$VM_NAME" --nvram --delete-all-disks 2>/dev/null \
      || virsh undefine "$VM_NAME" --delete-all-disks 2>/dev/null \
      || virsh undefine "$VM_NAME"
  else
    log "VM '$VM_NAME' not found; nothing to destroy."
  fi
  pkill -f "guest=${VM_NAME}" 2>/dev/null || true
  rm -f "$DISK_PATH" 2>/dev/null || true
  log "Done. (ISO kept at ${ISO_PATH})"
  exit 0
fi

# ---------------------------------------------------------------------------
# eject — detach the ISO cdrom so the VM boots from disk (post-install)
# ---------------------------------------------------------------------------
if [ "$cmd" = "eject" ]; then
  xml="$(virsh dumpxml "$VM_NAME" 2>/dev/null | awk '/<disk[^>]*cdrom/,/<\/disk>/')"
  if [ -n "$xml" ]; then
    printf '%s\n' "$xml" | virsh detach-device "$VM_NAME" -
    log "Ejected ISO from '$VM_NAME'."
  else
    log "No cdrom attached to '$VM_NAME' (already ejected?)."
  fi
  exit 0
fi

# ---------------------------------------------------------------------------
# stop — shut the domain down (idempotent; no-op if already off)
# ---------------------------------------------------------------------------
if [ "$cmd" = "stop" ]; then
  state="$(virsh domstate "$VM_NAME" 2>/dev/null || true)"
  if [ -z "$state" ] || [ "$state" = "error" ] || printf '%s' "$state" | grep -qi "not found"; then
    log "VM '${VM_NAME}' is not defined; nothing to stop."
    exit 0
  fi
  if [ "$state" = "shut off" ]; then
    log "VM '${VM_NAME}' is already shut off."
    exit 0
  fi
  log "Shutting down VM '${VM_NAME}'..."
  virsh shutdown "$VM_NAME" 2>/dev/null || true
  for _ in $(seq 1 60); do
    state="$(virsh domstate "$VM_NAME" 2>/dev/null || true)"
    [ "$state" = "shut off" ] && break
    sleep 1
  done
  if [ "$state" != "shut off" ]; then
    log "Graceful shutdown timed out; forcing destroy."
    virsh destroy "$VM_NAME" 2>/dev/null || true
  fi
  log "VM '${VM_NAME}' is shut off."
  exit 0
fi

[ "$cmd" = "create" ] || die "unknown command: $cmd (use: create | status | console | console-cmd | diskboot | liveboot | proxy | stop | eject | destroy)"

# ---------------------------------------------------------------------------
# 1. Prerequisites
# ---------------------------------------------------------------------------
[ -e /dev/kvm ] || die "/dev/kvm not found — enable virtualization in BIOS and load the kvm module."

# ---------------------------------------------------------------------------
# 2. Host packages (idempotent)
# ---------------------------------------------------------------------------
if ! dpkg -s qemu-system-x86 >/dev/null 2>&1; then
  log "Installing host packages (qemu, libvirt, virtinst, VNC viewer)..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq \
    qemu-system-x86 libvirt-daemon-system libvirt-clients virtinst \
    usbutils bridge-utils dnsmasq sshpass virt-viewer spice-client-gtk
else
  log "Host packages already present."
fi

# Python deps for the zero-touch installer (idempotent).
if ! python3 -c "import paramiko, serial" >/dev/null 2>&1; then
  log "Installing python3-paramiko + python3-serial..."
  apt-get install -y -qq python3-paramiko python3-serial
fi

# Make sure the libvirt daemon is up.
systemctl enable --now libvirtd 2>/dev/null || true

# Let the real user manage VMs without sudo (takes effect on next login).
for g in kvm libvirt; do
  if ! id -nG "$REAL_USER" 2>/dev/null | tr ' ' '\n' | grep -qx "$g"; then
    usermod -aG "$g" "$REAL_USER"
    log "Added '$REAL_USER' to group '$g' (effective on next login)."
  fi
done

# Ensure the default NAT network is active.
if virsh net-list --name 2>/dev/null | grep -qx "$VM_NET"; then
  virsh net-start "$VM_NET" 2>/dev/null || true
  virsh net-autostart "$VM_NET" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 3. Alpine ISO (pinned + checksummed, idempotent)
# ---------------------------------------------------------------------------
install -d -m 0755 "$(dirname "$ISO_PATH")"
if [ -f "$ISO_PATH" ] && echo "${ALPINE_SHA256}  ${ISO_PATH}" | sha256sum -c --status 2>/dev/null; then
  log "Alpine ISO present and verified."
else
  log "Downloading ${ALPINE_ISO} ..."
  curl -fSL --retry 3 -o "${ISO_PATH}.part" "$ALPINE_URL"
  if ! echo "${ALPINE_SHA256}  ${ISO_PATH}.part" | sha256sum -c --status 2>/dev/null; then
    rm -f "${ISO_PATH}.part"
    die "SHA256 mismatch for ${ALPINE_ISO} — aborting (will not use an unverified ISO)."
  fi
  mv "${ISO_PATH}.part" "$ISO_PATH"
  chmod 0644 "$ISO_PATH"
  log "ISO downloaded and verified."
fi

# ---------------------------------------------------------------------------
# 4. Kernel + initrd (for direct kernel boot with console=ttyS0)
# ---------------------------------------------------------------------------
install -d -m 0755 "$BOOT_DIR"
if [ ! -f "$BOOT_DIR/vmlinuz-lts" ] || [ ! -f "$BOOT_DIR/initramfs-lts" ]; then
  log "Extracting kernel + initrd from the ISO..."
  7z e -y "$ISO_PATH" "boot/vmlinuz-lts"   -o"$BOOT_DIR" >/dev/null
  7z e -y "$ISO_PATH" "boot/initramfs-lts" -o"$BOOT_DIR" >/dev/null
  chmod 0644 "$BOOT_DIR/vmlinuz-lts" "$BOOT_DIR/initramfs-lts"
fi

# ---------------------------------------------------------------------------
# 4b. SSH keypair (passwordless root access; idempotent) + sync the answerfile
#     The private key is git-ignored; on a fresh clone it's regenerated here and
#     the answerfile's ROOTSSHKEY is updated to match, so the install always works.
# ---------------------------------------------------------------------------
KEY_DIR="${REPO_ROOT}/vm/keys"
KEY_PATH="${KEY_DIR}/rabbitears-vm"
ANSWERFILE="${REPO_ROOT}/alpine/answerfile"
install -d -m 0700 "$KEY_DIR"
if [ ! -f "$KEY_PATH" ]; then
  log "Generating VM SSH keypair at vm/keys/rabbitears-vm ..."
  ssh-keygen -t ed25519 -N "" -C "rabbitears-vm" -f "$KEY_PATH" -q
  chmod 0600 "$KEY_PATH"
fi
if [ -f "$ANSWERFILE" ]; then
  PUBKEY="$(cat "${KEY_PATH}.pub")"
  _tmp="$(mktemp)"
  if sed "s|^ROOTSSHKEY=.*|ROOTSSHKEY=\"${PUBKEY}\"|" "$ANSWERFILE" > "$_tmp"; then
    mv "$_tmp" "$ANSWERFILE"
  else
    rm -f "$_tmp"
  fi
fi

# ---------------------------------------------------------------------------
# 5. Disk (idempotent) with correct ownership for the libvirt-qemu user
# ---------------------------------------------------------------------------
install -d -m 0755 /var/lib/libvirt/images
if [ ! -f "$DISK_PATH" ]; then
  log "Creating ${VM_DISK_GB}GB qcow2 disk..."
  qemu-img create -f qcow2 "$DISK_PATH" "${VM_DISK_GB}G"
fi
QEMU_GROUP="$(id -gn libvirt-qemu 2>/dev/null || echo kvm)"
chown root:"$QEMU_GROUP" "$DISK_PATH"
chmod 0660 "$DISK_PATH"

# ---------------------------------------------------------------------------
# 6. Define the domain (direct kernel boot + pty serial), idempotent
# ---------------------------------------------------------------------------
# Check if the domain is already installed BEFORE killing anything.
# Re-running create on an installed VM must be a no-op, not a hard-kill.
_xml="$(virsh dumpxml "$VM_NAME" 2>/dev/null || true)"
if [ -n "$_xml" ] && ! printf '%s' "$_xml" | grep -q '<kernel>' && printf '%s' "$_xml" | grep -q "<boot dev='hd'"; then
  log "VM '${VM_NAME}' is already installed (boots from disk). Use 'destroy' to rebuild."
  exit 0
fi

# Kill any orphaned qemu process for this VM (from a crashed/aborted run).
pkill -f "guest=${VM_NAME}" 2>/dev/null || true
if ! virsh list --all --name 2>/dev/null | grep -qx "$VM_NAME"; then
  log "Defining VM '$VM_NAME' (kernel boot, serial tcp 127.0.0.1:${SERIAL_PORT})..."
  cat > "/tmp/${VM_NAME}.xml" <<XML
<domain type='kvm'>
  <name>${VM_NAME}</name>
  <memory unit='KiB'>${RAM_KIB}</memory>
  <currentMemory unit='KiB'>${RAM_KIB}</currentMemory>
  <vcpu placement='static'>${VM_VCPUS}</vcpu>
  <os>
    <type arch='x86_64' machine='pc'>hvm</type>
    <kernel>${BOOT_DIR}/vmlinuz-lts</kernel>
    <initrd>${BOOT_DIR}/initramfs-lts</initrd>
    <cmdline>modules=loop,squashfs,sd-mod,usb-storage console=ttyS0</cmdline>
  </os>
  <features><acpi/><apic/></features>
  <cpu mode='host-passthrough'/>
  <clock offset='utc'/>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <on_crash>destroy</on_crash>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='${DISK_PATH}'/>
      <target dev='hda' bus='ide'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='${ISO_PATH}'/>
      <target dev='hdb' bus='ide'/>
      <readonly/>
    </disk>
    <serial type='pty'><target port='0'/></serial>
    <input type='tablet' bus='usb'/>
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
    <interface type='network'>
      <source network='${VM_NET}'/>
      <model type='e1000'/>
    </interface>
    <memballoon model='virtio'/>
  </devices>
</domain>
XML
  virsh define "/tmp/${VM_NAME}.xml"
fi

# ---------------------------------------------------------------------------
# 7. Drive the zero-touch install (serial + paramiko); install.py starts the VM
# ---------------------------------------------------------------------------
log "Running the zero-touch install (serial console + paramiko)..."
RB_VM_NAME="$VM_NAME" RB_SERIAL_PORT="$SERIAL_PORT" python3 "${REPO_ROOT}/vm/install.py"

# ---------------------------------------------------------------------------
# 8. Leave the disk at rest: shut the domain down so vmctl.py can open it.
# ---------------------------------------------------------------------------
log "Shutting down the domain (disk left at rest for vmctl.py)..."
virsh shutdown "$VM_NAME" 2>/dev/null || true
for _ in $(seq 1 60); do
  state="$(virsh domstate "$VM_NAME" 2>/dev/null || true)"
  [ "$state" = "shut off" ] && break
  sleep 1
done
if [ "$state" != "shut off" ]; then
  log "Graceful shutdown timed out; forcing destroy."
  virsh destroy "$VM_NAME" 2>/dev/null || true
fi
log "Domain '${VM_NAME}' is shut off (still defined for console/liveboot)."

# ---------------------------------------------------------------------------
# 9. Next steps
# ---------------------------------------------------------------------------
cat <<EOF

============================================================
 Rabbit Ears VM installed: ${VM_NAME}
   vCPU ${VM_VCPUS}   RAM ${VM_RAM_MB}MB   Disk ${VM_DISK_GB}GB
   ISO  ${ALPINE_ISO} (SHA256 verified)

 The base install + SSH verification ran automatically (see above).
 The domain is shut off; the disk is ready for vmctl.py.
 Next:
   1. Boot the VM:   uv run task vm-run
   2. Deploy:        uv run task deploy-vm
   3. Screenshot:    uv run task vm-shot
============================================================
EOF
