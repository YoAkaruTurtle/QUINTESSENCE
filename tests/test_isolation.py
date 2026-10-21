"""Confirm the normal offline runner enforces its input/output boundaries."""
import os
from pathlib import Path
import socket
import sys
import unittest

class IsolationTests(unittest.TestCase):
    def test_runner_guard_is_active(self):
        self.assertTrue(getattr(sys, '_quintessence_offline_guard', False),
                        'Run through tools/run_tests.py to enable the offline guard')

    def test_output_is_outside_checkout(self):
        root = Path(__file__).resolve().parents[1]
        output = Path(os.environ['QUINTESSENCE_TEST_OUTPUT']).resolve()
        self.assertNotEqual(root, output)
        self.assertNotIn(root, output.parents)
        with self.assertRaises(PermissionError):
            (root / 'forbidden-test-output.txt').write_text('must not be written')

    def test_sibling_assets_cannot_be_read(self):
        sibling = Path(__file__).resolve().parents[2] / 'private-instance-never-read.txt'
        with self.assertRaises(PermissionError):
            sibling.read_bytes()

    def test_non_loopback_and_dns_are_rejected(self):
        with socket.socket() as sock:
            with self.assertRaises(PermissionError):
                sock.connect(('192.0.2.1', 80))
        with self.assertRaises(PermissionError):
            socket.getaddrinfo('example.invalid', 443)

if __name__ == '__main__':
    unittest.main()
