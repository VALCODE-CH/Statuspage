#!/usr/bin/env python3
"""Offline self test for monitor.py - no network, no secrets required."""

import json
import os
import tempfile
import unittest

import monitor


def fake_checks(sequence):
    """Return a check_once replacement that yields the given ok/fail sequence."""
    results = iter(sequence)

    def _check(_monitor, _url, _headers):
        ok = next(results)
        return monitor.CheckResult(ok, 200 if ok else 503, 12, "test")

    return _check


class ThresholdTest(unittest.TestCase):
    def setUp(self):
        self.monitor = {**monitor.DEFAULTS, "name": "t", "url": "https://example.test/up"}
        self._real_check = monitor.check_once

    def tearDown(self):
        monitor.check_once = self._real_check

    def evaluate(self, sequence, currently_up):
        monitor.check_once = fake_checks(sequence)
        return monitor.evaluate(self.monitor, "https://example.test/up", {}, currently_up,
                                sleeper=lambda _s: None)

    def test_healthy_component_needs_one_success(self):
        verdict, results = self.evaluate([True], currently_up=True)
        self.assertEqual(verdict, monitor.UP)
        self.assertEqual(len(results), 1)

    def test_single_blip_does_not_take_the_component_down(self):
        verdict, _ = self.evaluate([False, True], currently_up=True)
        self.assertEqual(verdict, monitor.UP)

    def test_three_consecutive_failures_go_down(self):
        verdict, results = self.evaluate([False, False, False], currently_up=True)
        self.assertEqual(verdict, monitor.DOWN)
        self.assertEqual(len(results), 3)

    def test_recovery_needs_two_consecutive_successes(self):
        verdict, results = self.evaluate([True, True], currently_up=False)
        self.assertEqual(verdict, monitor.UP)
        self.assertEqual(len(results), 2)

    def test_flapping_leaves_component_unchanged(self):
        # Component is currently down, so recovery needs two consecutive
        # successes; an alternating result never reaches either threshold.
        verdict, results = self.evaluate([True, False, True], currently_up=False)
        self.assertEqual(verdict, monitor.FLAPPING)
        self.assertEqual(len(results), 3)


class ConfigTest(unittest.TestCase):
    def write(self, data):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(data, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_repository_config_is_valid(self):
        _, monitors = monitor.load_config("monitors.json")
        self.assertTrue(monitors)
        self.assertTrue(all(m["url"].startswith("https://") for m in monitors))

    def test_plain_http_is_rejected(self):
        path = self.write({"monitors": [{"name": "a", "url": "http://example.test/up"}]})
        with self.assertRaises(monitor.ConfigError):
            monitor.load_config(path)

    def test_unknown_component_status_is_rejected(self):
        path = self.write({"monitors": [{"name": "a", "url": "https://e.test", "down_status": "broken"}]})
        with self.assertRaises(monitor.ConfigError):
            monitor.load_config(path)

    def test_duplicate_names_are_rejected(self):
        path = self.write({"monitors": [
            {"name": "a", "url": "https://e.test"},
            {"name": "a", "url": "https://f.test"},
        ]})
        with self.assertRaises(monitor.ConfigError):
            monitor.load_config(path)

    def test_placeholders_resolve_from_environment(self):
        os.environ["SELFTEST_COMPONENT"] = "abc123"
        self.addCleanup(os.environ.pop, "SELFTEST_COMPONENT", None)
        self.assertEqual(monitor.resolve("${SELFTEST_COMPONENT}", "test"), "abc123")

    def test_missing_placeholder_raises(self):
        with self.assertRaises(monitor.ConfigError):
            monitor.resolve("${SELFTEST_DOES_NOT_EXIST}", "test")


class SafetyTest(unittest.TestCase):
    def test_secret_values_are_redacted(self):
        os.environ["SELFTEST_API_KEY"] = "super-secret-value"
        self.addCleanup(os.environ.pop, "SELFTEST_API_KEY", None)
        monitor.collect_sensitive_values()
        self.addCleanup(monitor.collect_sensitive_values)
        self.assertNotIn("super-secret-value", monitor.redact("key=super-secret-value"))

    def test_client_refuses_unknown_status(self):
        client = monitor.StatuspageClient("dummy-key")
        with self.assertRaises(monitor.StatuspageError):
            client.set_component_status("page", "component", "totally_fine")

    def test_api_base_is_https(self):
        self.assertTrue(monitor.API_BASE.startswith("https://"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
