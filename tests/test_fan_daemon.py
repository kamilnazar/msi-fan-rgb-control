#!/usr/bin/env python3
"""Unit tests for msi-fan-rgbd. Run with: python3 -m unittest discover tests

Nothing here touches real hardware: a tiny fake hwmon tree stands in for the
NCT6687, so the curve engine, slew limiting, thermal override and state file
can be exercised without root.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DAEMON_PATH = os.path.join(HERE, "..", "daemon", "msi-fan-rgbd.py")

spec = importlib.util.spec_from_file_location("msi_fan_rgbd", DAEMON_PATH)
daemon = importlib.util.module_from_spec(spec)
sys.modules["msi_fan_rgbd"] = daemon
spec.loader.exec_module(daemon)


class FakeHwmon:
    def __init__(self, base):
        self.base = base
        self.nct = os.path.join(base, "hwmon0")
        self.cpu = os.path.join(base, "hwmon1")
        os.makedirs(self.nct)
        os.makedirs(self.cpu)
        self.write(self.nct, "name", "nct6687")
        self.write(self.cpu, "name", "k10temp")
        self.write(self.cpu, "temp1_label", "Tctl")
        self.write(self.cpu, "temp1_input", 50000)
        self.write(self.nct, "temp1_label", "CPU")
        self.write(self.nct, "temp1_input", 50000)
        self.write(self.nct, "temp2_label", "System")
        self.write(self.nct, "temp2_input", 40000)
        self.fans = {
            1: ("CPU Fan", 1000),
            2: ("Pump Fan", 0),
            3: ("System Fan #1", 800),
            4: ("System Fan #2", 900),
        }
        for channel, (label, rpm) in self.fans.items():
            self.write(self.nct, f"fan{channel}_label", label)
            self.write(self.nct, f"fan{channel}_input", rpm)
            self.write(self.nct, f"pwm{channel}", 128)
            self.write(self.nct, f"pwm{channel}_enable", 2)

    @staticmethod
    def write(directory, name, value):
        with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
            handle.write(str(value))

    def read(self, name):
        with open(os.path.join(self.nct, name), "r", encoding="utf-8") as handle:
            return handle.read().strip()

    def set_temp(self, celsius):
        self.write(self.cpu, "temp1_input", int(celsius * 1000))
        self.write(self.nct, "temp1_input", int(celsius * 1000))


class DaemonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hw = FakeHwmon(self.tmp.name)
        self.state_dir = os.path.join(self.tmp.name, "state")
        self.config_path = os.path.join(self.tmp.name, "config.json")
        self.socket_path = os.path.join(self.tmp.name, "control.sock")
        self.daemon = daemon.FanDaemon(
            config_path=self.config_path,
            state_dir=self.state_dir,
            socket_path=self.socket_path,
            hwmon_root=self.hw.nct,
            sysfs=self.tmp.name,
        )

    def tearDown(self):
        self.daemon._restore_firmware()
        self.tmp.cleanup()

    def test_auto_leaves_pwm_alone(self):
        self.daemon._apply_all()
        self.assertEqual(self.hw.read("pwm1"), "128")
        self.assertEqual(self.hw.read("pwm1_enable"), "2")

    def test_curve_profile_interpolates_and_slews(self):
        self.daemon.handle({"cmd": "profile", "profile": "silent"})
        self.assertEqual(self.hw.read("pwm1_enable"), "1")
        # At 50 C the silent CPU curve asks for 25% (64/255); slew starts at
        # 128 and walks down 16 per tick.
        for _ in range(10):
            self.daemon._apply_all()
        self.assertEqual(int(self.hw.read("pwm1")), 64)
        # The pump has no curve in the profile, so it stays on firmware.
        self.assertEqual(self.hw.read("pwm2_enable"), "2")

    def test_manual_holds_percentage(self):
        self.daemon.handle({"cmd": "manual", "channel": 1, "percent": 40})
        self.assertEqual(self.hw.read("pwm1_enable"), "1")
        self.assertEqual(int(self.hw.read("pwm1")), 102)
        self.daemon._apply_all()
        self.assertEqual(int(self.hw.read("pwm1")), 102)

    def test_thermal_override_forces_full_and_recovers(self):
        self.hw.set_temp(99)
        self.daemon._apply_all()
        self.assertTrue(self.daemon._thermal)
        self.assertEqual(self.hw.read("pwm1_enable"), "1")
        self.assertEqual(self.hw.read("pwm1"), "255")
        self.hw.set_temp(80)
        self.daemon._apply_all()
        self.assertFalse(self.daemon._thermal)
        self.assertEqual(self.hw.read("pwm1_enable"), "2")

    def test_state_survives_restart(self):
        self.daemon.handle({"cmd": "profile", "profile": "performance"})
        self.daemon.handle({"cmd": "manual", "channel": 3, "percent": 66})
        restarted = daemon.FanDaemon(
            config_path=self.config_path,
            state_dir=self.state_dir,
            socket_path=self.socket_path,
            hwmon_root=self.hw.nct,
            sysfs=self.tmp.name,
        )
        status = restarted.status()
        self.assertEqual(status["mode"], "manual")
        self.assertEqual(int(self.hw.read("pwm3")), 168)

    def test_curve_command_validates_points(self):
        with self.assertRaises(daemon.FanError):
            self.daemon.handle({"cmd": "curve", "channel": 1,
                                "points": [[40, 20]]})
        self.daemon.handle({"cmd": "curve", "channel": 1,
                            "points": [[40, 20], [80, 80]]})
        self.assertEqual(self.daemon.state["mode"], "curve")
        self.assertEqual(self.daemon._effective_curve(1), [[40, 20], [80, 80]])

    def test_curve_source_switch(self):
        self.assertIn("tctl", self.daemon.status()["temps"])
        self.daemon.handle({"cmd": "curve_source", "source": "tctl"})
        self.assertEqual(self.daemon.state["curve_source"], "tctl")
        with self.assertRaises(daemon.FanError):
            self.daemon.handle({"cmd": "curve_source", "source": "nope"})


class CurveMathTest(unittest.TestCase):
    def test_clamps_and_interpolates(self):
        points = [[30, 20], [50, 40], [70, 100]]
        self.assertEqual(daemon.curve_percent(points, 10), 20)
        self.assertEqual(daemon.curve_percent(points, 40), 30)
        self.assertEqual(daemon.curve_percent(points, 60), 70)
        self.assertEqual(daemon.curve_percent(points, 90), 100)

    def test_normalize_sorts_and_rejects_duplicates(self):
        self.assertEqual(daemon.normalize_curve([[80, 100], [30, 20]]),
                         [[30.0, 20.0], [80.0, 100.0]])
        with self.assertRaises(daemon.FanError):
            daemon.normalize_curve([[40, 20], [40, 30]])
        with self.assertRaises(daemon.FanError):
            daemon.normalize_curve([[40, -1], [80, 100]])


if __name__ == "__main__":
    unittest.main()
