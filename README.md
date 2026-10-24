# MSI Fan & RGB for Omarchy

An Omarchy shell plugin for MSI motherboards: fan curves, fan RPM and
temperatures in the bar, plus Mystic Light RGB presets in the same popup.

Verified on an **MSI B650M GAMING PLUS WIFI (MS-7E24)**, but it works on any
board where the `nct6687` driver exposes a fan controller and OpenRGB knows the
Mystic Light device.

```
󰈐 51°          <- bar: CPU temperature (configurable)
  MSI Fan & RGB
  Balanced · 51°C
  CPU 51°  System 44°  VRM 43°  Chipset 47°
  Fan profile   [Auto] [Silent] [Balanced] [Performance] [Manual]
  Fans
    CPU Fan      1142 RPM · 25%
    System Fan #1 870 RPM · 60%
    System Fan #2 1421 RPM · 60%
  RGB · MSI B650M GAMING PLUS WIFI (MS-7E24)
    [Off] [Static] [Breathing] [Rainbow] [Wave] [Meteor]
    ● ● ● ● ● ● ● ●
    brightness ──────●────
```

## How it works

Two helpers sit under `bin/`, and the QML only polls them:

| Piece | Runs as | Job |
|---|---|---|
| `daemon/msi-fan-rgbd.py` | root (systemd) | Owns the PWM registers, applies curves every second, hands fans back to firmware on stop/crash |
| `bin/msi-fanctl` | user | Reads sysfs for status; sends profile/manual/curve commands to the daemon socket |
| `bin/msi-rgb` | user | Wraps `openrgb` for the Mystic Light controller and caches the last applied state |

Fan reads never need the daemon: if the service is down, the popup still shows
temperatures and RPM and says it is read-only.

## Dependencies

1. **Fan driver** — `nct6687d` DKMS (`nct6687d-dkms-git` on the AUR). The
   in-kernel `nct6683` driver exposes PWM **read-only** on these boards, so it
   cannot set a fan speed. `nct6683` must be blacklisted.
2. **OpenRGB** — `pacman -S openrgb` (extra). It ships the udev rules that give
   the logged-in user access to the Mystic Light hidraw device.
3. `dkms` and your kernel headers (already present on Omarchy).

### One-time fan driver setup

```bash
yay -S nct6687d-dkms-git
echo 'blacklist nct6683' | sudo tee /etc/modprobe.d/nct6683-blacklist.conf
echo 'nct6687' | sudo tee /etc/modules-load.d/nct6687.conf
sudo modprobe -r nct6683 2>/dev/null || true
sudo modprobe nct6687
sensors | grep -A15 nct6687
```

You should see `CPU Fan`, `Pump Fan`, `System Fan #1..#6`, `pwm1..pwm8` and
`pwmN_enable` in `/sys/class/hwmon/hwmonX/`.

If `sensors` only shows CPU and Pump RPM, see the driver's README: on some MSI
boards `msi_fan_brute_force=1` is needed. This board (B650M, plain NCT6687D)
uses the default mapping and does not need it.

## Install the plugin

System side:

```bash
sudo ./install.sh
```

This installs:

- `/usr/local/lib/msi-fan-rgb/msi-fan-rgbd`
- `/usr/local/bin/msi-fanctl`, `/usr/local/bin/msi-rgb`
- `/etc/msi-fan-rgb/config.json` (kept on reinstall)
- `/etc/systemd/system/msi-fan-rgbd.service`
- a Mystic Light udev rule **only if** OpenRGB's rule is missing

User side:

```bash
ln -s "$PWD" ~/.config/omarchy/plugins/io.github.kamil.msi-fan-rgb
```

Then add `io.github.kamil.msi-fan-rgb` to the bar in
`~/.config/omarchy/shell.json` (or via `omarchy bar`). The shell hot-reloads,
so saving `shell.json` is enough.

When the plugin is symlinked out of a git checkout, run
`omarchy restart shell` after editing the QML. The shell watches
`~/.config/omarchy/plugins` with inotify, and inotify does not follow the
symlink into the repository.

## Using it

- **Bar label**: CPU temperature by default. The settings panel (bar widget
  settings) switches between CPU temp, fan RPM, both, or icon only.
- **Fan profile**: `Auto` returns all channels to the firmware curve.
  `Silent`, `Balanced` and `Performance` run the daemon's curves. `Manual`
  freezes each header at its current PWM and reveals sliders per fan.
- **RGB**: presets map to OpenRGB modes, color swatches apply a static color,
  the slider sets brightness. `Off` sets brightness to 0 and keeps the mode so
  the next preset can restore it.

### Command line

```bash
msi-fanctl status --pretty        # JSON: temps, RPM, PWM, profile
msi-fanctl profile silent
msi-fanctl mode auto              # firmware control
msi-fanctl manual 1 60            # CPU fan to 60%, manual mode
msi-fanctl curve 3 '40,25;60,50;80,100'
msi-fanctl curve-source tctl
msi-fanctl save                   # persist mode/profile now
sudo systemctl restart msi-fan-rgbd

msi-rgb status
msi-rgb preset static --color 3366FF --brightness 60
msi-rgb preset rainbow
msi-rgb preset off
```

The same actions are available over Quickshell IPC, which is handy from a
keybinding or another script:

```bash
qs ipc -p /usr/share/omarchy/shell call io.github.kamil.msi-fan-rgb toggle
qs ipc -p /usr/share/omarchy/shell call io.github.kamil.msi-fan-rgb profile silent
qs ipc -p /usr/share/omarchy/shell call io.github.kamil.msi-fan-rgb manual 1 60
qs ipc -p /usr/share/omarchy/shell call io.github.kamil.msi-fan-rgb rgbPreset rainbow
qs ipc -p /usr/share/omarchy/shell call io.github.kamil.msi-fan-rgb rgbColor 3366FF
qs ipc -p /usr/share/omarchy/shell call io.github.kamil.msi-fan-rgb rgbBrightness 40
qs ipc -p /usr/share/omarchy/shell call io.github.kamil.msi-fan-rgb status
```

Functions: `open`, `close`, `toggle`, `refresh`, `status`, `profile <id>`,
`manual <channel> <percent>`, `rgbPreset <id>`, `rgbColor <RRGGBB>` and
`rgbBrightness <percent>`. With several monitors, the handler answers on one
bar instance; clicking the widget always opens the popup on the bar you clicked.

### Using with CoolerControl

[CoolerControl](https://docs.coolercontrol.org/) can run alongside the plugin,
but only one program may own the fan PWM registers at a time. Both detect the
same `nct6687` fans and temperatures.

- Leave CoolerControl's fan functions **unassigned** (its default "Unmanaged"
  profile) and use the widget's profiles and manual sliders. The daemon owns
  PWM and CoolerControl just shows dashboards.
- To give fan control to CoolerControl instead: select **Auto** in the widget,
  then `sudo systemctl disable --now msi-fan-rgbd`, and assign fan functions in
  CoolerControl. The widget still shows temperatures and RPM read-only, and RGB
  keeps working.
- To take control back: `sudo systemctl enable --now msi-fan-rgbd`, then pick a
  profile in the widget.
- Never run a CoolerControl fan function while the widget is in Silent,
  Balanced, Performance or Manual: both daemons would write `pwmN` and the fans
  would oscillate.

### Custom curves

Edit `/etc/msi-fan-rgb/config.json`, then `sudo systemctl restart msi-fan-rgbd`
(or `msi-fanctl reload`). Profiles are named groups of curves; a channel is
assigned to a group in `channels`:

```json
"channels": {
  "1": { "group": "cpu", "curve": [[40, 25], [70, 60], [85, 100]] },
  "3": { "group": "system" }
},
"profiles": {
  "quiet": {
    "label": "Quiet",
    "cpu": [[30, 15], [55, 30], [75, 60], [90, 100]],
    "system": [[30, 20], [55, 35], [75, 65], [90, 100]]
  }
}
```

A per-channel `curve` overrides the profile. A group with no curve in the
active profile (the default `pump` group) is left to the firmware.

Other knobs:

| Key | Default | Meaning |
|---|---|---|
| `poll_ms` | `1000` | Curve evaluation period |
| `curve_source` | `cpu` | `cpu`, `tctl`, `system`, `vrm`, `chipset`, `socket` |
| `slew.up` / `slew.down` | `48` / `16` | Max PWM change per tick (0-255) |
| `thermal.max_c` | `95` | Force all fans to 100% at or above this CPU temperature |
| `thermal.resume_c` | `88` | Return to the profile below this temperature |

## Safety

- `Auto` mode never writes a PWM value; it only sets `pwmN_enable=2`.
- CPU at `thermal.max_c` latches a 100% override, including in Auto mode.
  It clears at `thermal.resume_c`.
- The daemon restores `pwmN_enable=2` on SIGTERM/SIGINT. The systemd unit also
  runs the same restore as `ExecStopPost`, so a crash cannot leave a fan at 0%.
- The control socket is `/run/msi-fan-rgb/control.sock`, mode `0660 root:wheel`,
  and the daemon re-checks peer credentials.
- Writing to the RGB controller is user-level (udev ACL); the daemon never
  touches RGB.

## Troubleshooting

**"Fan daemon not running"** — `systemctl status msi-fan-rgbd` and
`journalctl -u msi-fan-rgbd -n 50`. Common cause: `nct6687` is not loaded.

**No fan sliders** — the board only shows headers with RPM by default. Enable
"Show empty fan headers" in the widget settings to see all eight.

**`openrgb` cannot find the board** — check `openrgb -ld`; the device should
appear as `MSI <board> (MS-xxxx)`. The udev ACL on `/dev/hidraw*` is applied to
the active session; re-plugging the controller or a reboot fixes stale ACLs.

**Fans do not change speed** — some MSI boards need
`options nct6687 msi_fan_brute_force=1` in `/etc/modprobe.d/`, and a full
reboot. That option is for the `msi_alt1` register layout; do not force it on
this B650 board.

**Fans oscillate between two speeds** — two controllers are writing PWM, for
example CoolerControl functions plus `msi-fan-rgbd`. Keep only one active; see
[Using with CoolerControl](#using-with-coolercontrol).

**After suspend** — the daemon retries failed writes on the next tick and the
systemd unit restarts it if needed. If the board resets fan control on resume,
`systemctl restart msi-fan-rgbd` re-applies the profile.

## Uninstall

```bash
sudo ./uninstall.sh          # keeps /etc/msi-fan-rgb
sudo ./uninstall.sh --purge  # removes config and state too
rm ~/.config/omarchy/plugins/io.github.kamil.msi-fan-rgb
```

The kernel driver stays installed; remove it with `dkms remove nct6687d/<ver>
--all` and delete the blacklist/modules-load files if you no longer want it.
