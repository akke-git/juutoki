import tempfile
import unittest
from pathlib import Path

import launcher


class LauncherTests(unittest.TestCase):
    def test_duplicate_instance_lock_is_released_after_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            first = launcher.InstanceLock(Path(folder))
            second = launcher.InstanceLock(Path(folder))
            self.assertTrue(first.acquire())
            try:
                self.assertFalse(second.acquire())
            finally:
                first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_busy_port_falls_back_to_another_loopback_port(self):
        first = launcher.reserve_socket(0)
        try:
            second = launcher.reserve_socket(first.getsockname()[1])
            try:
                self.assertEqual(second.getsockname()[0], "127.0.0.1")
                self.assertNotEqual(second.getsockname()[1], first.getsockname()[1])
            finally:
                second.close()
        finally:
            first.close()

    def test_duplicate_launch_does_not_open_external_url(self):
        self.assertFalse(launcher.valid_local_url("https://example.com"))
        self.assertFalse(launcher.valid_local_url("http://127.0.0.1:8000@evil.example"))
        self.assertTrue(launcher.valid_local_url("http://127.0.0.1:54321/"))
