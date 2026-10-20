#!/usr/bin/env bash
#
# Rabbit Ears — reproducible KVM VM provisioning (host side).
#
# Runs on the desktop (EVO-X2). Idempotent: safe to re-run. Pins a specific
# Alpine release + SHA256 so the VM is always built from the same inputs.
#
# Usage (self-elevates under the NOPASSWD rule from vm/setup-sudoers.sh):
#   ./vm/provision.sh [--keep-on-failure]
#                                # create (if needed) + install the base OS;
#                                # --keep-on-failure leaves a failed live VM up
#                                # for `virsh -c qemu:///system console rabbitears-vm`
#   ./vm/provision.sh status     # show VM / ISO / disk status
#   ./vm/provision.sh stop       # shut the domain down + verify its QEMU is gone
#   ./vm/provision.sh eject      # detach the ISO so the VM boots from disk
#   ./vm/provision.sh destroy    # delete the VM + its disk (keeps the ISO);
#                                # refuses while a vmctl QEMU holds the disk
#
# The install's serial transcript is written to
# /var/lib/rabbitears-vm/install-serial.log (readable without sudo).
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

# `./vm/provision.sh [--keep-on-failure]` means `create [--keep-on-failure]`.
case "${1:-}" in
  ""|--*) cmd="create" ;;
  *)      cmd="$1"; shift ;;
esac
KEEP_ON_FAILURE=0
if [ "$cmd" = "create" ]; then
  for _a in "$@"; do
    case "$_a" in
      --keep-on-failure) KEEP_ON_FAILURE=1 ;;
      *) die "unknown option for create: $_a (use --keep-on-failure)" ;;
    esac
  done
fi
INSTALL_LOG="${VM_BASE}/install-serial.log"
PROXY_LOG="/var/log/rabbitears-apk-proxy.log"

# ---------------------------------------------------------------------------
# Process helpers. Never `pkill -f` a pattern that might also appear in a
# caller's command line (it kills the caller); match the libvirt QEMU by its
# exact `-name guest=<vm>,` argument and the disk holders with fuser.
# ---------------------------------------------------------------------------
libvirt_qemu_pids() { pgrep -f "guest=${VM_NAME}," 2>/dev/null || true; }

# PIDs of every process that has the VM disk open (libvirt's or vmctl's QEMU).
disk_holders() {
  [ -e "$DISK_PATH" ] || return 0
  fuser "$DISK_PATH" 2>/dev/null | tr -s ' \t' '\n' | grep -E '^[0-9]+$' || true
}

describe_pids() {
  local p
  for p in "$@"; do
    ps -o pid=,user=,args= -p "$p" 2>/dev/null | cut -c1-200 || true
  done
}

# Stop the libvirt domain and make sure its QEMU process is really gone.
ensure_domain_stopped() {
  local p
  virsh destroy "$VM_NAME" >/dev/null 2>&1 || true
  for _ in $(seq 1 20); do
    [ -z "$(libvirt_qemu_pids)" ] && return 0
    sleep 0.5
  done
  warn "libvirt QEMU for ${VM_NAME} survived 'virsh destroy'; killing it."
  for p in $(libvirt_qemu_pids); do kill -TERM "$p" 2>/dev/null || true; done
  sleep 3
  for p in $(libvirt_qemu_pids); do kill -KILL "$p" 2>/dev/null || true; done
  sleep 1
  if [ -n "$(libvirt_qemu_pids)" ]; then
    # shellcheck disable=SC2046
    die "libvirt QEMU for ${VM_NAME} won't die:
$(describe_pids $(libvirt_qemu_pids))"
  fi
}

# Refuse to touch the disk while any process (e.g. a `vm-run` QEMU) holds it.
require_disk_free() {
  local pids
  pids="$(disk_holders | tr '\n' ' ')"
  [ -z "${pids// /}" ] && return 0
  # shellcheck disable=SC2086
  die "the VM disk ${DISK_PATH} is in use by:
$(describe_pids $pids)
Stop it first: uv run task vm-stop  (stops vmctl's QEMU and the libvirt domain)."
}

start_proxy() {
  if pgrep -f "vm/apk-proxy.py" >/dev/null 2>&1; then
    log "apk-proxy already running (pid $(pgrep -f 'vm/apk-proxy.py' | head -1))."
  else
    nohup python3 "${REPO_ROOT}/vm/apk-proxy.py" > "$PROXY_LOG" 2>&1 &
    log "apk-proxy started (pid $!) on 192.168.122.1:8081; log: ${PROXY_LOG}."
  fi
}

# The install's "APK Mirror" step and every package install need the repos in
# the answerfile to answer. Check them from the host before booting anything.
check_apk_repos() {
  local answerfile="${REPO_ROOT}/alpine/answerfile" repos url ok
  repos="$(sed -n 's/^APKREPOSOPTS="\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "$answerfile" | head -1)"
  [ -n "$repos" ] || die "no APKREPOSOPTS in ${answerfile}"
  case "$repos" in *192.168.122.1:8081*) start_proxy ;; esac
  for url in $repos; do
    case "$url" in http://*|https://*) ;; *) continue ;; esac
    ok=0
    for _ in $(seq 1 10); do
      if curl -fsS -o /dev/null --max-time 60 "${url}/x86_64/APKINDEX.tar.gz" 2>/dev/null; then
        ok=1; break
      fi
      sleep 1
    done
    [ "$ok" -eq 1 ] || die "apk repo ${url} does not answer (${url}/x86_64/APKINDEX.tar.gz).
The install would fail at its 'APK Mirror' step. Check the proxy: uv run task vm-proxy (log: ${PROXY_LOG})."
  done
  log "apk repos answer: ${repos}"
}

# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
if [ "$cmd" = "status" ]; then
  virsh list --all
  echo
  echo "ISO:  ${ISO_PATH} $([ -f "$ISO_PATH" ] && echo '(present)' || echo '(missing)')"
  if virsh list --all --name 2>/dev/null | grep -qx "$VM_NAME"; then
    echo "Disk: $(virsh domblklist "$VM_NAME" 2>/dev/null | awk 'NR>2 && $2!="" && $2!="-" {print $2}' | head -1)"
  else
    echo "Disk: (no VM)"
  fi
  _holders="$(disk_holders | tr '\n' ' ')"
  if [ -n "${_holders// /}" ]; then
    echo "Disk in use by:"
    # shellcheck disable=SC2086
    describe_pids $_holders | sed 's/^/  /'
  else
    echo "Disk in use by: (nothing)"
  fi
  echo "Install transcript: ${INSTALL_LOG} $([ -f "$INSTALL_LOG" ] && echo '(present)' || echo '(none yet)')"
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
  ensure_domain_stopped
  require_disk_free
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
  ensure_domain_stopped
  require_disk_free
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
  start_proxy
  exit 0
fi

# ---------------------------------------------------------------------------
# destroy
# ---------------------------------------------------------------------------
if [ "$cmd" = "destroy" ]; then
  ensure_domain_stopped
  # A vmctl QEMU (uv run task vm-run) may still hold the disk: refuse rather
  # than delete a disk that a running VM is using.
  require_disk_free
  if virsh list --all --name 2>/dev/null | grep -qx "$VM_NAME"; then
    log "Destroying VM '$VM_NAME' (and its disk)..."
    virsh undefine "$VM_NAME" --nvram --delete-all-disks 2>/dev/null \
      || virsh undefine "$VM_NAME" --delete-all-disks 2>/dev/null \
      || virsh undefine "$VM_NAME"
  else
    log "VM '$VM_NAME' not found; nothing to destroy."
  fi
  rm -f "$DISK_PATH" 2>/dev/null || true
  # media/ (transcoded content) and usb.img (the mock stick) are content, not
  # VM state — keep them so a rebuild doesn't force a re-transcode.
  log "Done. (ISO kept at ${ISO_PATH}; ${VM_BASE}/media and usb.img kept)"
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
  if [ "$state" = "running" ] || [ "$state" = "paused" ]; then
    log "Shutting down VM '${VM_NAME}'..."
    virsh shutdown "$VM_NAME" 2>/dev/null || true
    for _ in $(seq 1 60); do
      state="$(virsh domstate "$VM_NAME" 2>/dev/null || true)"
      [ "$state" = "shut off" ] && break
      sleep 1
    done
    [ "$state" = "shut off" ] || log "Graceful shutdown timed out; forcing destroy."
  elif [ -z "$state" ]; then
    log "VM '${VM_NAME}' is not defined."
  else
    log "VM '${VM_NAME}' is ${state}."
  fi
  # Whatever libvirt says, make sure no QEMU for this domain survives
  # (an orphan from an aborted install keeps the disk locked).
  ensure_domain_stopped
  log "VM '${VM_NAME}' is shut off; no libvirt QEMU left for it."
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
# 4b. SSH keypair (passwordless root access; idempotent)
#     The keypair is git-ignored; on a fresh clone it's generated here. The
#     repo's answerfile is never rewritten: install.py injects the public key
#     into the copy it sends to the VM.
#     The files belong to the real user (this script runs as root), so
#     deploy.sh/vmctl can read the key without sudo.
# ---------------------------------------------------------------------------
KEY_DIR="${REPO_ROOT}/vm/keys"
KEY_PATH="${KEY_DIR}/rabbitears-vm"
install -d -m 0700 -o "$REAL_USER" "$KEY_DIR"
if [ ! -f "$KEY_PATH" ]; then
  log "Generating VM SSH keypair at vm/keys/rabbitears-vm ..."
  ssh-keygen -t ed25519 -N "" -C "rabbitears-vm" -f "$KEY_PATH" -q
  chmod 0600 "$KEY_PATH"
  chown "$REAL_USER" "$KEY_PATH" "${KEY_PATH}.pub"
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
# 5b. Media staging dir (user-owned): holds the transcoded content that
#     vm/make-usb.sh syncs onto the mock USB stick. Created BEFORE the
#     "already installed" early-skip so a no-op re-run still ensures it.
# ---------------------------------------------------------------------------
install -d -m 0755 "${VM_BASE}/media/channels"
chown "${REAL_USER}":"${REAL_USER}" "${VM_BASE}/media/channels"

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

# Stop any orphaned libvirt QEMU for this VM (from a crashed/aborted run), then
# refuse to go on while anything else (e.g. a `vm-run` QEMU) holds the disk:
# two QEMUs on one qcow2 corrupt it.
ensure_domain_stopped
require_disk_free

# The install fails (or hangs) at its APK Mirror step without working repos.
check_apk_repos

if ! virsh list --all --name 2>/dev/null | grep -qx "$VM_NAME"; then
  log "Defining VM '$VM_NAME' (kernel boot, serial pty)..."
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
log "Running the zero-touch install (serial console + paramiko); transcript: ${INSTALL_LOG}"
if ! RB_VM_NAME="$VM_NAME" RB_DISK_PATH="$DISK_PATH" RB_INSTALL_LOG="$INSTALL_LOG" \
    RB_KEEP_VM="$KEEP_ON_FAILURE" python3 "${REPO_ROOT}/vm/install.py"; then
  die "the zero-touch install failed (see above). Serial transcript: ${INSTALL_LOG}
Retry with: uv run task vm-rebuild   (add --keep-on-failure to vm-build to leave the live VM up for: virsh -c qemu:///system console ${VM_NAME})"
fi

# ---------------------------------------------------------------------------
# 8. Leave the disk at rest: shut the domain down so vmctl.py can open it.
# ---------------------------------------------------------------------------
# install.py powers the installed system off over SSH; this is the fallback.
state="$(virsh domstate "$VM_NAME" 2>/dev/null || true)"
if [ "$state" != "shut off" ]; then
  log "Shutting down the domain (disk left at rest for vmctl.py)..."
  virsh shutdown "$VM_NAME" 2>/dev/null || true
  for _ in $(seq 1 60); do
    state="$(virsh domstate "$VM_NAME" 2>/dev/null || true)"
    [ "$state" = "shut off" ] && break
    sleep 1
  done
  [ "$state" = "shut off" ] || log "Graceful shutdown timed out; forcing destroy."
fi
ensure_domain_stopped
require_disk_free
log "Domain '${VM_NAME}' is shut off (still defined for console/liveboot); the disk is free."

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
