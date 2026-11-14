# SPDX-License-Identifier: MIT
"""The host setup helpers in init/host: narrow, valid, and root-only."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / 'init/host'


class TftpForward(unittest.TestCase):
    def test_the_redirect_matches_only_the_unit_to_host_tftp_request(self):
        script = (HOST / 'tftp-forward').read_text()
        self.assertIn('iifname "$iface" ip saddr $unit_ip ip daddr $host_ip udp dport 69 '
                      'redirect to :$port', script)
        self.assertIn('table ip w1700k_tftp', script)
        self.assertNotIn('flush ruleset', script)

    def test_the_service_reloads_only_its_own_table(self):
        unit = (HOST / 'w1700k-tftp-forward.service').read_text()
        self.assertIn('ExecStartPre=-/usr/sbin/nft delete table ip w1700k_tftp', unit)
        self.assertIn('ExecStart=/usr/sbin/nft -f /etc/w1700k-tftp-forward.nft', unit)
        self.assertIn('CapabilityBoundingSet=CAP_NET_ADMIN', unit)

    def test_systemd_accepts_the_service(self):
        tool = shutil.which('systemd-analyze')
        if not tool:
            self.skipTest('systemd-analyze is unavailable')
        result = subprocess.run([tool, 'verify', str(HOST / 'w1700k-tftp-forward.service')],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class Scripts(unittest.TestCase):
    def test_scripts_parse(self):
        for name in ('tftp-forward', 'link'):
            with self.subTest(name):
                subprocess.run(['sh', '-n', str(HOST / name)], check=True)

    @unittest.skipIf(os.geteuid() == 0, 'runs as root')
    def test_changes_need_root(self):
        for argv in (['tftp-forward', 'enable', 'lo'], ['link', 'up', 'lo']):
            with self.subTest(argv[0]):
                result = subprocess.run([str(HOST / argv[0]), *argv[1:]],
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('root', result.stderr)

    @unittest.skipIf(os.geteuid() == 0, 'runs as root')
    def test_status_without_root_does_not_claim_the_redirect_is_missing(self):
        # nft lists rules for root alone, so its failure here says nothing.
        result = subprocess.run([str(HOST / 'tftp-forward'), 'status'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('not loaded', result.stdout)
        self.assertIn('takes root', result.stdout)


if __name__ == '__main__':
    unittest.main()
