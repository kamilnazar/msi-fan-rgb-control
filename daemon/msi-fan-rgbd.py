#!/usr/bin/env python3
"""msi-fan-rgbd -- fan curves for MSI motherboards with an NCT6687 Super I/O.

Why a daemon: the PWM registers live in sysfs and are owned by root.  The
Omarchy shell runs as the logged-in user, so the widget cannot write them
itself.  This daemon owns the hardware, applies fan curves on a timer, and
exposes a tiny line-oriented JSON socket.  ``msi-fanctl`` is the client.

Safety rules, in order:

* ``mode=auto`` never writes a PWM value; it only selects the firmware's
  own curve (``pwmN_enable=2``) and lets the BIOS do the work.
* A thermal override latches when the CPU reaches ``thermal.max_c`` and
  forces every header to 100% until the CPU falls below ``thermal.resume_c``.
  It applies to auto mode too: a stuck BIOS curve must not cook the CPU.
* SIGTERM, a clean shutdown, or a service stop hands every channel back to
  the firmware.  A crash cannot leave a fan at 0%.

The daemon deliberately knows nothing about RGB.  Lighting is a user-space
USB HID job (OpenRGB) and needs no privileges beyond the udev ACL.
"""

from __future__ import annotations

import argparse
import copy
import grp
import json
import os
import pwd
import signal
import socket
import struct
import sys
import threading
import time

DEFAULT_CONFIG = {
    "version": 1,
    "poll_ms": 1000,
    "curve_source": "cpu",
    "thermal": {"max_c": 95.0, "resume_c": 88.0},
    "slew": {"up": 48, "down": 16},
    "channels": {
        "1": {"group": "cpu"},
        "2": {"group": "pump"},
        "3": {"group": "system"},
        "4": {"group": "system"},
        "5": {"group": "system"},
        "6": {"group": "system"},
        "7": {"group": "system"},
        "8": {"group": "system"},
    },
    "profiles": {
        "silent": {
            "label": "Silent",
            "cpu": [[30, 18], [50, 25], [65, 40], [80, 65], [90, 100]],
            "system": [[30, 20], [50, 30], [65, 45], [80, 70], [90, 100]],
        },
        "balanced": {
            "label": "Balanced",
            "cpu": [[30, 22], [50, 34], [65, 50], [80, 75], [90, 100]],
            "system": [[30, 25], [50, 36], [65, 55], [80, 80], [90, 100]],
        },
        "performance": {
            "label": "Performance",
            "cpu": [[30, 35], [50, 50], [65, 70], [80, 90], [90, 100]],
            "system": [[30, 40], [50, 55], [65, 75], [80, 90], [90, 100]],
        },
    },
}

DEFAULT_STATE = {
    "mode": "auto",  # auto | curve | manual
    "profile": "balanced",
    "curve_source": "cpu",
    "manual": {},
}

CONFIG_PATH = "/etc/msi-fan-rgb/config.json"
STATE_DIR = "/var/lib/msi-fan-rgb"
SOCKET_PATH = "/run/msi-fan-rgb/control.sock"
SOCKET_GROUP = "wheel"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def merge_dict(base, override):
    """Deep-merge ``override`` into ``base``. Lists replace, they do not merge."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_dict(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def clamp(value, low, high):
    return max(low, min(high, value))


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def read_int(path, fallback=None):
    try:
        return int(read_text(path))
    except (TypeError, ValueError):
        return fallback


def write_value(path, value):
    try:
        with open(path, "w", encoding="ascii") as handle:
            handle.write(str(int(value)))
        return True
    except OSError as error:
        # EIO/ENODEV while the machine suspends is normal; the next tick
        # retries. Anything else is surfaced through the status warnings.
        raise FanError(f"cannot write {path}: {error}") from error


def normalize_curve(points):
    """Validate ``[[temp, percent], ...]`` and return it sorted by temp."""
    cleaned = []
    for point in points or []:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise FanError("a curve point must be [temperature, percent]")
        temp, percent = float(point[0]), float(point[1])
        if not 0 <= temp <= 120:
            raise FanError("curve temperature must be between 0 and 120 C")
        if not 0 <= percent <= 100:
            raise FanError("curve percent must be between 0 and 100")
        cleaned.append([temp, percent])
    if len(cleaned) < 2:
        raise FanError("a curve needs at least two points")
    cleaned.sort(key=lambda item: item[0])
    for before, after in zip(cleaned, cleaned[1:]):
        if after[0] == before[0]:
            raise FanError("two curve points share the same temperature")
    return cleaned


def curve_percent(points, temp):
    """Piecewise-linear interpolation, clamped at both ends."""
    if not points or temp is None:
        return None
    if temp <= points[0][0]:
        return float(points[0][1])
    if temp >= points[-1][0]:
        return float(points[-1][1])
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        if t0 <= temp <= t1:
            span = t1 - t0
            if span <= 0:
                return float(p1)
            ratio = (temp - t0) / span
            return p0 + (p1 - p0) * ratio
    return float(points[-1][1])


class FanError(Exception):
    pass


# --------------------------------------------------------------------------- #
# hardware
# --------------------------------------------------------------------------- #

TEMP_LABELS = {
    "cpu": "cpu",
    "system": "system",
    "vrm mos": "vrm",
    "vrm": "vrm",
    "pch": "chipset",
    "chipset": "chipset",
    "cpu socket": "socket",
    "pcie x1": "pcie",
    "m2_1": "m2",
}


class Hwmon:
    """The NCT6687 hwmon device plus the CPU temperature sensor."""

    def __init__(self, root=None, sysfs=None):
        self.sysfs = sysfs or os.environ.get("MSI_FAN_RGB_SYSFS",
                                             "/sys/class/hwmon")
        self.root = root or self._find_nct(self.sysfs)
        if not self.root:
            raise FanError(
                "no nct6687 hwmon device; load the nct6687 module "
                "(see README, 'Fan driver')"
            )

    @staticmethod
    def _find_nct(base):
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            return None
        for entry in entries:
            path = os.path.join(base, entry)
            if read_text(os.path.join(path, "name")) in ("nct6687", "nct6683"):
                return path
        return None

    def _find_cpu_sensor(self):
        try:
            entries = sorted(os.listdir(self.sysfs))
        except OSError:
            return None
        for entry in entries:
            path = os.path.join(self.sysfs, entry)
            if read_text(os.path.join(path, "name")) == "k10temp":
                return path
        return None

    def channels(self):
        found = []
        for channel in range(1, 9):
            if os.path.exists(self.path(f"pwm{channel}")):
                found.append(channel)
            elif os.path.exists(self.path(f"fan{channel}_input")):
                found.append(channel)
        return found

    def path(self, name):
        return os.path.join(self.root, name)

    def fan_label(self, channel):
        label = read_text(self.path(f"fan{channel}_label"))
        return label or f"Fan {channel}"

    def fan_rpm(self, channel):
        return read_int(self.path(f"fan{channel}_input"))

    def pwm_raw(self, channel):
        return read_int(self.path(f"pwm{channel}"))

    def pwm_enable(self, channel):
        return read_int(self.path(f"pwm{channel}_enable"))

    def set_pwm(self, channel, raw):
        path = self.path(f"pwm{channel}")
        if read_int(path) != int(raw):
            write_value(path, raw)

    def set_enable(self, channel, mode):
        path = self.path(f"pwm{channel}_enable")
        if read_int(path) != int(mode):
            write_value(path, mode)

    def read_temps(self):
        temps = {}
        for channel in range(1, 8):
            label = read_text(self.path(f"temp{channel}_label"))
            value = read_int(self.path(f"temp{channel}_input"))
            if value is None:
                continue
            key = TEMP_LABELS.get(label.lower())
            if key and value > 0:
                temps[key] = value / 1000.0
        tctl = self._read_k10temp()
        if tctl is not None:
            temps["tctl"] = tctl
        return temps

    def _read_k10temp(self):
        sensor = self._find_cpu_sensor()
        if not sensor:
            return None
        for channel in range(1, 4):
            label = read_text(os.path.join(sensor, f"temp{channel}_label"))
            if label in ("Tctl", "Tdie"):
                value = read_int(os.path.join(sensor, f"temp{channel}_input"))
                if value:
                    return value / 1000.0
        value = read_int(os.path.join(sensor, "temp1_input"))
        return value / 1000.0 if value else None


# --------------------------------------------------------------------------- #
# daemon
# --------------------------------------------------------------------------- #


class FanDaemon:
    def __init__(self, config_path=CONFIG_PATH, state_dir=STATE_DIR,
                 socket_path=SOCKET_PATH, hwmon_root=None, sysfs=None):
        self.config_path = config_path
        self.state_dir = state_dir
        self.socket_path = socket_path
        self.lock = threading.RLock()
        self.hw = Hwmon(hwmon_root, sysfs)
        self.channels = self.hw.channels()
        if not self.channels:
            raise FanError("the sensor exposes no fan channels")

        self.cfg = self._load_config()
        self.state = self._load_state()
        self.state["curve_source"] = self.state.get(
            "curve_source", self.cfg.get("curve_source", "cpu"))

        self._enable = {}
        self._last_written = {}
        self._curve_temp = {}
        self._curve_target = {}
        self._thermal = False
        self._stop = threading.Event()
        self._warnings = []
        self._apply_all(force=True)

    # -- configuration ------------------------------------------------------ #

    def _load_config(self):
        try:
            with open(self.config_path, "r", encoding="utf-8") as handle:
                user = json.load(handle)
        except FileNotFoundError:
            user = {}
        except (OSError, ValueError) as error:
            raise FanError(f"cannot read {self.config_path}: {error}") from error
        return merge_dict(DEFAULT_CONFIG, user)

    def _load_state(self):
        path = os.path.join(self.state_dir, "state.json")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                user = json.load(handle)
        except (FileNotFoundError, OSError):
            user = {}
        except ValueError:
            user = {}
        state = merge_dict(DEFAULT_STATE, user)
        if state.get("mode") not in ("auto", "curve", "manual"):
            state["mode"] = "auto"
        if state.get("profile") not in self.profiles():
            state["profile"] = next(iter(self.cfg["profiles"]), "balanced")
        return state

    def profiles(self):
        return self.cfg.get("profiles", {})

    def _save_state(self):
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            path = os.path.join(self.state_dir, "state.json")
            temporary = path + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temporary, path)
        except OSError as error:
            self._warn(f"cannot save state: {error}")

    def _warn(self, message):
        if message not in self._warnings:
            self._warnings.append(message)
            while len(self._warnings) > 8:
                self._warnings.pop(0)
        print(f"msi-fan-rgbd: {message}", file=sys.stderr, flush=True)

    # -- curves ------------------------------------------------------------- #

    def _effective_curve(self, channel):
        entry = self.cfg.get("channels", {}).get(str(channel), {})
        if entry.get("curve"):
            return entry["curve"]
        group = entry.get("group")
        if not group:
            return None
        profile = self.profiles().get(self.state.get("profile"), {})
        return profile.get(group)

    def _source_temp(self, temps):
        return temps.get(self.state.get("curve_source") or "cpu")

    def _target_raw(self, channel, temps):
        curve = self._effective_curve(channel)
        if not curve:
            return None
        temp = self._source_temp(temps)
        if temp is None:
            return None
        previous_temp = self._curve_temp.get(channel)
        if previous_temp is not None and abs(temp - previous_temp) < 0.7:
            return self._curve_target.get(channel)
        percent = curve_percent(curve, temp)
        target = int(round(percent * 255 / 100))
        self._curve_temp[channel] = temp
        self._curve_target[channel] = target
        return target

    def _slew(self, channel, target):
        current = self.hw.pwm_raw(channel)
        if current is None:
            return target
        delta = target - current
        if delta > 0:
            limit = int(self.cfg.get("slew", {}).get("up", 48))
        else:
            limit = int(self.cfg.get("slew", {}).get("down", 16))
        if abs(delta) <= limit:
            return target
        return current + (limit if delta > 0 else -limit)

    # -- applying ----------------------------------------------------------- #

    def _apply_all(self, force=False):
        with self.lock:
            temps = self.hw.read_temps()
            cpu = temps.get("cpu", temps.get("tctl"))
            thermal = self.cfg.get("thermal", {})
            max_c = float(thermal.get("max_c", 95))
            resume_c = float(thermal.get("resume_c", 88))

            if cpu is not None and cpu >= max_c:
                if not self._thermal:
                    self._warn(f"CPU at {cpu:.0f} C; forcing all fans to 100%")
                self._thermal = True
            elif self._thermal and cpu is not None and cpu <= resume_c:
                self._warn("CPU cooled down; returning to the selected profile")
                self._thermal = False
                force = True

            if self._thermal:
                for channel in self.channels:
                    try:
                        self.hw.set_enable(channel, 1)
                        self.hw.set_pwm(channel, 255)
                        self._enable[channel] = 1
                        self._last_written[channel] = 255
                    except FanError as error:
                        self._warn(str(error))
                return

            mode = self.state.get("mode", "auto")
            for channel in self.channels:
                entry = self.cfg.get("channels", {}).get(str(channel), {})
                try:
                    if mode == "auto" or (
                        mode == "curve" and not self._effective_curve(channel)
                    ):
                        self.hw.set_enable(channel, 2)
                        self._enable[channel] = 2
                        continue

                    if mode == "manual":
                        target = self.state.get("manual", {}).get(str(channel))
                        if target is None:
                            target = self.hw.pwm_raw(channel)
                            if target is None:
                                continue
                            target = int(target)
                    else:
                        target = self._target_raw(channel, temps)
                        if target is None:
                            self.hw.set_enable(channel, 2)
                            self._enable[channel] = 2
                            continue

                    self.hw.set_enable(channel, 1)
                    self._enable[channel] = 1
                    if force:
                        next_value = target
                    else:
                        next_value = self._slew(channel, target)
                    self.hw.set_pwm(channel, next_value)
                    self._last_written[channel] = next_value
                    if mode == "manual":
                        self.state.setdefault("manual", {})[str(channel)] = next_value
                except FanError as error:
                    self._warn(str(error))

    def _restore_firmware(self):
        with self.lock:
            for channel in self.channels:
                try:
                    self.hw.set_enable(channel, 2)
                except FanError:
                    pass
            self._thermal = False

    # -- status ------------------------------------------------------------- #

    def status(self):
        with self.lock:
            temps = self.hw.read_temps()
            fans = []
            for channel in self.channels:
                entry = self.cfg.get("channels", {}).get(str(channel), {})
                curve = entry.get("curve") or self._effective_curve(channel)
                rpm = self.hw.fan_rpm(channel)
                raw = self.hw.pwm_raw(channel)
                enable = self.hw.pwm_enable(channel)
                fans.append({
                    "channel": channel,
                    "label": entry.get("label") or self.hw.fan_label(channel),
                    "group": entry.get("group"),
                    "rpm": rpm if rpm is not None else 0,
                    "pwm": raw if raw is not None else 0,
                    "percent": round((raw or 0) * 100 / 255),
                    "enable": enable,
                    "controlled": enable == 1,
                    "curve": curve,
                })

            mode = "thermal" if self._thermal else self.state.get("mode", "auto")
            profile_id = self.state.get("profile")
            profile = self.profiles().get(profile_id, {})
            choices = [{"id": "auto", "label": "Auto"}]
            for key, value in self.profiles().items():
                choices.append({"id": key, "label": value.get("label", key.title())})
            choices.append({"id": "manual", "label": "Manual"})

            return {
                "ok": True,
                "daemon": True,
                "driver": read_text(os.path.join(self.hw.root, "name")),
                "hwmon": self.hw.root,
                "mode": mode,
                "profile": profile_id,
                "profileLabel": "Auto" if mode == "auto" else (
                    "Manual" if mode == "manual" else profile.get("label", profile_id)),
                "choices": choices,
                "curveSource": self.state.get("curve_source", "cpu"),
                "sources": [key for key in ("cpu", "tctl", "system", "vrm",
                                            "chipset", "socket") if key in temps],
                "temps": {key: round(value, 1) for key, value in temps.items()},
                "cpuTemp": round(temps.get("cpu", temps.get("tctl", 0)), 1),
                "fans": fans,
                "thermal": self._thermal,
                "warnings": list(self._warnings),
                "updated": int(time.time()),
            }

    # -- commands ----------------------------------------------------------- #

    def handle(self, request):
        command = str(request.get("cmd", "")).strip().lower()
        with self.lock:
            if command in ("status", "ping"):
                return self.status()
            if command == "mode":
                mode = str(request.get("mode", ""))
                if mode not in ("auto", "curve", "manual"):
                    raise FanError("mode must be auto, curve or manual")
                if mode == "manual":
                    for channel in self.channels:
                        self.state.setdefault("manual", {}).setdefault(
                            str(channel), self.hw.pwm_raw(channel) or 0)
                self.state["mode"] = mode
                self._thermal = False
                self._apply_all(force=True)
                self._save_state()
                return self.status()
            if command == "profile":
                profile = str(request.get("profile", ""))
                if profile not in self.profiles():
                    raise FanError(f"unknown profile: {profile}")
                self.state["mode"] = "curve"
                self.state["profile"] = profile
                self._thermal = False
                self._apply_all(force=True)
                self._save_state()
                return self.status()
            if command == "manual":
                if self.state.get("mode") != "manual":
                    self.state["mode"] = "manual"
                channel = int(request.get("channel", 0))
                if channel not in self.channels:
                    raise FanError(f"no fan channel {channel}")
                percent = float(request.get("percent", -1))
                if not 0 <= percent <= 100:
                    raise FanError("percent must be between 0 and 100")
                raw = int(round(percent * 255 / 100))
                self.state.setdefault("manual", {})[str(channel)] = raw
                self._apply_all(force=True)
                self._save_state()
                return self.status()
            if command == "curve":
                channel = int(request.get("channel", 0))
                if channel not in self.channels:
                    raise FanError(f"no fan channel {channel}")
                points = normalize_curve(request.get("points"))
                self.cfg.setdefault("channels", {}).setdefault(
                    str(channel), {})["curve"] = points
                self.state["mode"] = "curve"
                self._apply_all(force=True)
                return self.status()
            if command == "curve_source":
                source = str(request.get("source", ""))
                temps = self.hw.read_temps()
                if source not in temps:
                    raise FanError(f"temperature sensor not available: {source}")
                self.state["curve_source"] = source
                self._apply_all(force=True)
                self._save_state()
                return self.status()
            if command == "save":
                self._save_state()
                return {"ok": True, "saved": True}
            if command == "reload":
                self.cfg = self._load_config()
                self._apply_all(force=True)
                return self.status()
            raise FanError(f"unknown command: {command}")

    # -- lifecycle ---------------------------------------------------------- #

    def stop(self):
        self._stop.set()

    def run(self, socket_path=None, group_name=SOCKET_GROUP):
        socket_path = socket_path or self.socket_path
        server = ControlServer(socket_path, self, group_name)
        server.start()
        signal.signal(signal.SIGTERM, lambda *_: self.stop())
        signal.signal(signal.SIGINT, lambda *_: self.stop())
        poll = max(0.25, float(self.cfg.get("poll_ms", 1000)) / 1000)
        print(f"msi-fan-rgbd: {self.hw.root}, channels "
              f"{','.join(map(str, self.channels))}, socket {socket_path}",
              file=sys.stderr, flush=True)
        try:
            while not self._stop.wait(poll):
                self._apply_all()
        finally:
            server.stop()
            self._restore_firmware()
            self._save_state()
            print("msi-fan-rgbd: stopped, fans returned to firmware",
                  file=sys.stderr, flush=True)


class ControlServer(threading.Thread):
    def __init__(self, path, daemon, group_name):
        super().__init__(daemon=True, name="control")
        self.path = path
        self.daemon_ref = daemon
        self.group_name = group_name
        self._stop = threading.Event()
        self.sock = None
        self._bind()

    def _bind(self):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        self.sock.listen(8)
        self.sock.settimeout(0.5)
        gid = -1
        try:
            gid = grp.getgrnam(self.group_name).gr_gid
        except KeyError:
            pass
        try:
            os.chown(self.path, 0, gid)
        except OSError:
            pass
        os.chmod(self.path, 0o660)

    def stop(self):
        self._stop.set()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _allowed(self, connection):
        # The socket mode already restricts access to root and wheel; this
        # second check keeps the daemon safe if someone edits the unit's
        # RuntimeDirectoryMode or the socket is re-created by hand.
        try:
            size = struct.calcsize("3i")
            _pid, uid, _gid = struct.unpack(
                "3i",
                connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size))
        except OSError:
            return False
        if uid == 0:
            return True
        try:
            user = pwd.getpwuid(uid)
            groups = os.getgrouplist(user.pw_name, user.pw_gid)
            wheel = grp.getgrnam(self.group_name).gr_gid
            return wheel in groups
        except (KeyError, OSError):
            return False

    def run(self):
        while not self._stop.is_set():
            try:
                connection, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with connection:
                connection.settimeout(5)
                if not self._allowed(connection):
                    self._reply(connection, {"ok": False,
                                             "error": "permission denied"})
                    continue
                try:
                    data = connection.recv(65536)
                    request = json.loads(data.decode("utf-8") or "{}")
                    response = self.daemon_ref.handle(request)
                except (ValueError, FanError, OSError) as error:
                    response = {"ok": False, "error": str(error)}
                except Exception as error:  # never let a request kill the server
                    response = {"ok": False, "error": f"internal error: {error}"}
                self._reply(connection, response)

    @staticmethod
    def _reply(connection, payload):
        try:
            connection.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        except OSError:
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="MSI fan curve daemon")
    parser.add_argument("--config", default=os.environ.get(
        "MSI_FAN_RGB_CONFIG", CONFIG_PATH))
    parser.add_argument("--state-dir", default=os.environ.get(
        "MSI_FAN_RGB_STATE_DIR", STATE_DIR))
    parser.add_argument("--socket", default=os.environ.get(
        "MSI_FAN_RGB_SOCKET", SOCKET_PATH))
    parser.add_argument("--hwmon", default=os.environ.get("MSI_FAN_RGB_HWMON"))
    parser.add_argument("--group", default=SOCKET_GROUP)
    parser.add_argument("--print-status", action="store_true",
                        help="print one status document and exit")
    args = parser.parse_args(argv)

    try:
        daemon = FanDaemon(config_path=args.config, state_dir=args.state_dir,
                           socket_path=args.socket, hwmon_root=args.hwmon)
    except FanError as error:
        print(f"msi-fan-rgbd: {error}", file=sys.stderr)
        return 1
    if args.print_status:
        print(json.dumps(daemon.status(), indent=2))
        return 0
    daemon.run(socket_path=args.socket, group_name=args.group)
    return 0


if __name__ == "__main__":
    sys.exit(main())
