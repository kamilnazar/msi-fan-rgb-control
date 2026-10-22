#!/usr/bin/env python3
"""Unit tests for bin/msi-rgb. Run with: python3 -m unittest discover tests

The tests replace OpenRGB with a fake recorder, so they can check the cached
brightness rules without a Mystic Light controller attached.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
RGB_PATH = os.path.join(HERE, "..", "bin", "msi-rgb")

_TMP = tempfile.TemporaryDirectory()
CACHE_FILE = os.path.join(_TMP.name, "rgb-state.json")
os.environ["MSI_FAN_RGB_CACHE"] = CACHE_FILE

spec = importlib.util.spec_from_loader("msi_rgb", SourceFileLoader("msi_rgb", RGB_PATH))
rgb = importlib.util.module_from_spec(spec)
sys.modules["msi_rgb"] = rgb
spec.loader.exec_module(rgb)


def fake_detect(device_filter=None, verbose=False):
    return {
        "available": True,
        "error": None,
        "device": "MSI B650M GAMING PLUS WIFI (MS-7E24)",
        "index": 0,
        "zones": ["JRGB1", "JRGB2"],
        "modes": ["Direct", "Static", "Breathing", "Rainbow Wave"],
        "devices": ["MSI B650M GAMING PLUS WIFI (MS-7E24)"],
    }


def args(**overrides):
    base = dict(preset=None, mode=None, color=None, brightness=None,
                zone=None, device=None)
    base.update(overrides)
    return SimpleNamespace(**base)


class RgbCliTests(unittest.TestCase):
    def setUp(self):
        with open(CACHE_FILE, "w", encoding="utf-8") as handle:
            json.dump({
                "device": "MSI B650M GAMING PLUS WIFI (MS-7E24)",
                "mode": "Static",
                "color": "3366FF",
                "brightness": 60,
                "preset": "static",
                "off": False,
            }, handle)

        self.calls = []
        self.detect = mock.patch.object(rgb, "detect", fake_detect)
        self.openrgb = mock.patch.object(
            rgb, "run_openrgb",
            lambda command, timeout=15: self.calls.append(command) or "")
        self.detect.start()
        self.openrgb.start()

    def tearDown(self):
        self.openrgb.stop()
        self.detect.stop()

    def apply(self, **overrides):
        return rgb.apply(args(**overrides))

    def test_off_writes_zero_but_keeps_cached_brightness(self):
        result = self.apply(preset="off")

        self.assertTrue(result["ok"])
        self.assertTrue(result["off"])
        self.assertEqual(result["brightness"], 60)
        self.assertEqual(self.calls[-1][-2:], ["-b", "0"])

    def test_preset_after_off_restores_last_brightness(self):
        self.apply(preset="off")
        self.calls.clear()

        result = self.apply(preset="static", color="34C759")

        self.assertTrue(result["ok"])
        self.assertFalse(result["off"])
        self.assertEqual(result["brightness"], 60)
        self.assertEqual(result["color"], "34C759")
        self.assertEqual(self.calls[-1][-2:], ["-b", "60"])

    def test_brightness_change_clears_off_flag(self):
        self.apply(preset="off")

        result = self.apply(brightness=30)

        self.assertTrue(result["ok"])
        self.assertFalse(result["off"])
        self.assertEqual(result["brightness"], 30)
        self.assertEqual(self.calls[-1][-2:], ["-b", "30"])

    def test_off_uses_cached_mode_and_color(self):
        self.apply(preset="off")

        command = " ".join(self.calls[-1])

        self.assertIn("-m Static", command)
        self.assertIn("-c 3366FF", command)


if __name__ == "__main__":
    unittest.main()
