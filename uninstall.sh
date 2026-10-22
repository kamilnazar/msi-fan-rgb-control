#!/usr/bin/env bash
# Remove the system side of the plugin. The kernel driver and OpenRGB are
# deliberately left alone: they may be used by other tools.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "run this with sudo: sudo ./uninstall.sh" >&2
  exit 1
fi

echo "==> stopping msi-fan-rgbd"
systemctl disable --now msi-fan-rgbd.service 2>/dev/null || true

echo "==> removing unit and helpers"
rm -f /etc/systemd/system/msi-fan-rgbd.service
systemctl daemon-reload
rm -f /usr/local/bin/msi-fanctl /usr/local/bin/msi-rgb
rm -rf /usr/local/lib/msi-fan-rgb

if [[ "${1:-}" == "--purge" ]]; then
  echo "==> removing configuration"
  rm -rf /etc/msi-fan-rgb /var/lib/msi-fan-rgb
else
  echo "    kept /etc/msi-fan-rgb and /var/lib/msi-fan-rgb (use --purge to remove)"
fi

echo
echo "The widget symlink, if any, is yours to delete:"
echo "  rm ~/.config/omarchy/plugins/io.github.kamil.msi-fan-rgb"
echo
echo "To also remove the fan driver:"
echo "  dkms status nct6687d        # note the version number"
echo "  sudo dkms remove nct6687d/<version> --all"
echo "  sudo rm /etc/modprobe.d/nct6683-blacklist.conf /etc/modules-load.d/nct6687.conf"
