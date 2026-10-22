#!/usr/bin/env bash
# Install the system side of the MSI Fan & RGB plugin.
#
# The bar widget itself is a user plugin: symlink this repository into
# ~/.config/omarchy/plugins/io.github.kamil.msi-fan-rgb (the script prints the
# exact command). This installer handles what needs root: the fan daemon, its
# systemd unit, the CLI helpers and the default config.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "run this with sudo: sudo ./install.sh" >&2
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="/usr/local"
LIB="${PREFIX}/lib/msi-fan-rgb"
CONFIG_DIR="/etc/msi-fan-rgb"

echo "==> installing daemon and helpers"
install -d -m 755 "${LIB}"
install -m 755 "${HERE}/daemon/msi-fan-rgbd.py" "${LIB}/msi-fan-rgbd"
install -m 644 "${HERE}/README.md" "${LIB}/README.md"
install -m 755 "${HERE}/bin/msi-fanctl" "${PREFIX}/bin/msi-fanctl"
install -m 755 "${HERE}/bin/msi-rgb" "${PREFIX}/bin/msi-rgb"

echo "==> default configuration"
install -d -m 755 "${CONFIG_DIR}"
if [[ -f "${CONFIG_DIR}/config.json" ]]; then
  echo "    keeping existing ${CONFIG_DIR}/config.json"
else
  install -m 644 "${HERE}/config.example.json" "${CONFIG_DIR}/config.json"
fi

echo "==> systemd unit"
install -m 644 "${HERE}/systemd/msi-fan-rgbd.service" \
  /etc/systemd/system/msi-fan-rgbd.service
systemctl daemon-reload

if ! modinfo nct6687 >/dev/null 2>&1; then
  echo
  echo "!! The nct6687 kernel module is not installed."
  echo "   Fan control needs it; the in-kernel nct6683 driver is read-only."
  echo "   Install it first, then re-run this script:"
  echo "     yay -S nct6687d-dkms-git"
  echo "     echo 'blacklist nct6683' | sudo tee /etc/modprobe.d/nct6683-blacklist.conf"
  echo "     echo 'nct6687' | sudo tee /etc/modules-load.d/nct6687.conf"
  echo
else
  modprobe nct6687 2>/dev/null || true
fi

if [[ -f /usr/lib/udev/rules.d/60-openrgb.rules ]]; then
  echo "==> RGB access already covered by OpenRGB's udev rules"
else
  echo "==> installing Mystic Light udev rule"
  install -m 644 "${HERE}/udev/60-msi-mystic-light.rules" \
    /usr/lib/udev/rules.d/60-msi-mystic-light.rules
  udevadm control --reload-rules
  udevadm trigger --subsystem-match=hidraw
fi

echo "==> starting msi-fan-rgbd"
systemctl enable --now msi-fan-rgbd.service
sleep 1
systemctl --no-pager --lines=5 status msi-fan-rgbd.service || true

echo
echo "==> fan status"
"${PREFIX}/bin/msi-fanctl" --pretty status | head -40 || true

echo
echo "The widget is a user plugin. If it is not linked yet, run as your user:"
echo "  ln -s \"${HERE}\" \"\$HOME/.config/omarchy/plugins/io.github.kamil.msi-fan-rgb\""
