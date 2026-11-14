# SPDX-License-Identifier: MIT
"""The host setup helpers in init/host: narrow, valid, and root-only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / 'init/host'
TOOL = HOST / 'tftp-forward'

# Stand-ins for the host's tools: each logs its call; FAIL names what is broken.
FAKES = {
    'id': 'echo 0\n',
    'systemctl': '''echo "systemctl $*" >> "$LOG"
case $1 in
is-active) exit 3 ;;
restart) case " $FAIL " in *" start "*) exit 1 ;; esac ;;
esac
exit 0
''',
    'nft': '''echo "nft $*" >> "$LOG"
case "$*" in *"-f -"*) cat > /dev/null ;; esac
if [ "$1" = -c ]; then
	case " $FAIL " in *" nft "*)
		echo "Error: Could not process rule: Operation not supported" >&2; exit 1 ;;
	esac
fi
exit 0
''',
    'iptables-restore': '''echo "iptables-restore $*" >> "$LOG"
cat > /dev/null
case " $FAIL " in *" iptables "*)
	echo "iptables-restore: can not initialize table nat" >&2; exit 1 ;;
esac
exit 0
''',
    # No chain of ours exists beforehand (-S, -D fail); the jump is found after (-C).
    'iptables': '''echo "iptables $*" >> "$LOG"
case " $* " in *" -S "*|*" -D "*) exit 1 ;; esac
exit 0
''',
}


class Host:
    """A host made of stand-ins, with the tool believing it runs as root."""

    def __init__(self, test, systemd=True, ufw=False):
        self.dir = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.dir)
        self.root = self.dir / 'root'
        (self.root / 'sys/class/net/eth9').mkdir(parents=True)
        (self.root / 'etc/systemd/system').mkdir(parents=True)
        if systemd:
            (self.root / 'run/systemd/system').mkdir(parents=True)
        fakes = dict(FAKES)
        if ufw:
            fakes['ufw'] = 'echo "Status: active"\n'
        (self.dir / 'bin').mkdir()
        for name, body in fakes.items():
            path = self.dir / 'bin' / name
            path.write_text('#!/bin/sh\n' + body)
            path.chmod(0o755)
        self.log = self.dir / 'log'
        self.log.write_text('')

    def run(self, *argv, fail=''):
        env = {'PATH': f'{self.dir / "bin"}:/usr/bin:/bin', 'LOG': str(self.log), 'FAIL': fail,
               'W1700K_TFTP_SYSROOT': str(self.root)}
        return subprocess.run([str(TOOL), *argv], capture_output=True, text=True, env=env)

    def files(self):
        etc = self.root / 'etc'
        return sorted(str(path.relative_to(etc)) for path in etc.rglob('*') if path.is_file())

    def calls(self):
        return self.log.read_text().splitlines()

    def read(self, name):
        return (self.root / 'etc' / name).read_text()


UNIT = 'systemd/system/w1700k-tftp-forward.service'
NFT_RULE = 'w1700k-tftp-forward.nft'
IPTABLES_RULE = 'w1700k-tftp-forward.iptables'


class Enable(unittest.TestCase):
    def test_nftables_is_used_where_it_works(self):
        host = Host(self)
        result = host.run('enable', 'eth9')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(host.files(), [UNIT, NFT_RULE])
        self.assertIn('iifname "eth9" ip saddr 192.168.1.1 ip daddr 192.168.1.10 udp dport 69 '
                      'redirect to :7071', host.read(NFT_RULE))
        # The installed unit names this host's nft, wherever that is.
        self.assertIn(f'ExecStart={host.dir}/bin/nft -f /etc/w1700k-tftp-forward.nft',
                      host.read(UNIT))
        self.assertIn('systemctl restart w1700k-tftp-forward.service', host.calls())
        # iptables is only asked whether an earlier install left its chain.
        self.assertEqual([call for call in host.calls() if call.startswith('iptables')],
                         ['iptables -w -t nat -S W1700K_TFTP'])
        self.assertIn('(nft), now and after every boot', result.stdout)

    def test_the_rule_is_checked_against_the_kernel_before_anything_is_written(self):
        host = Host(self)
        host.run('enable', 'eth9')
        calls = host.calls()
        self.assertLess(calls.index('nft -c -f -'),
                        calls.index('systemctl daemon-reload'))

    def test_iptables_is_the_fallback_and_says_why(self):
        host = Host(self)
        result = host.run('enable', 'eth9', '10.0.0.2', '10.0.0.1', '7072', fail='nft')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('nftables cannot load the redirect here (Error: Could not process rule: '
                      'Operation not supported); using iptables', result.stderr)
        self.assertEqual(host.files(), [UNIT, IPTABLES_RULE])
        self.assertEqual(host.read(IPTABLES_RULE).splitlines(), [
            '*nat', ':W1700K_TFTP - [0:0]',
            '-A W1700K_TFTP -i eth9 -s 10.0.0.1 -d 10.0.0.2 -p udp --dport 69 '
            '-j REDIRECT --to-ports 7072',
            '-I PREROUTING -p udp --dport 69 -j W1700K_TFTP', 'COMMIT'])
        unit = host.read(UNIT)
        self.assertIn(f'ExecStart={host.dir}/bin/iptables-restore -w --noflush '
                      '/etc/w1700k-tftp-forward.iptables', unit)
        self.assertIn(f'ExecStop=-{host.dir}/bin/iptables -w -t nat -X W1700K_TFTP', unit)
        self.assertNotIn('/usr/sbin/', unit)

    def test_the_iptables_check_leaves_its_scratch_chain_behind_nowhere(self):
        host = Host(self)
        host.run('enable', '--backend', 'iptables', 'eth9')
        calls = host.calls()
        self.assertIn('iptables -w -t nat -X W1700K_TFTP_CHECK', calls)
        self.assertLess(calls.index('iptables -w -t nat -X W1700K_TFTP_CHECK'),
                        calls.index('systemctl daemon-reload'))

    def test_neither_working_changes_nothing_and_names_both_reasons(self):
        host = Host(self)
        result = host.run('enable', 'eth9', fail='nft iptables')
        self.assertEqual(result.returncode, 1)
        self.assertIn('nothing was changed', result.stderr)
        self.assertIn('nftables: Error: Could not process rule', result.stderr)
        self.assertIn('iptables: iptables-restore: can not initialize table nat', result.stderr)
        self.assertEqual(host.files(), [])
        self.assertFalse([call for call in host.calls() if call.startswith('systemctl')])

    def test_a_named_backend_is_not_replaced_by_the_other(self):
        host = Host(self)
        result = host.run('enable', '--backend', 'nft', 'eth9', fail='nft')
        self.assertEqual(result.returncode, 1)
        self.assertIn('nftables cannot load the redirect here', result.stderr)
        self.assertEqual(host.files(), [])
        self.assertFalse([call for call in host.calls() if call.startswith('iptables')])

    def test_bad_arguments_stop_before_any_tool_runs(self):
        for argv, said in ((['eth9', '999.1.1.1'], 'not an IPv4 address'),
                           (['eth9', '192.168.1.10', '1.2.3'], 'not an IPv4 address'),
                           (['eth9', '192.168.1.10', '192.168.1.1', '69'], '1024 to 65535'),
                           (['eth9', '192.168.1.10', '192.168.1.1', '70000'], '1024 to 65535'),
                           (['eth9', '192.168.1.10', '192.168.1.1', '7071; reboot'], 'not a port'),
                           (['eth9"; flush ruleset; "'], 'not an interface name'),
                           (['nosuch0'], 'no interface nosuch0'),
                           (['--backend', 'ipfw', 'eth9'], 'nft or iptables')):
            with self.subTest(argv):
                host = Host(self)
                result = host.run('enable', *argv)
                self.assertEqual(result.returncode, 1)
                self.assertIn(said, result.stderr)
                self.assertEqual(host.calls(), [])
                self.assertEqual(host.files(), [])

    def test_without_systemd_only_once_is_offered(self):
        host = Host(self, systemd=False)
        result = host.run('enable', 'eth9')
        self.assertEqual(result.returncode, 1)
        self.assertIn('does not run systemd', result.stderr)
        self.assertIn('--once', result.stderr)
        self.assertEqual(host.calls(), [])

    def test_once_loads_the_rule_and_installs_nothing(self):
        for backend, load in (('nft', 'nft -f -'),
                              ('iptables', 'iptables-restore -w --noflush')):
            with self.subTest(backend):
                host = Host(self, systemd=False)
                result = host.run('enable', '--once', '--backend', backend, 'eth9')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(host.files(), [])
                self.assertIn(load, host.calls())
                self.assertIn('nothing is installed', result.stdout)

    def test_a_unit_that_does_not_start_is_not_left_installed(self):
        host = Host(self)
        result = host.run('enable', 'eth9', fail='start')
        self.assertEqual(result.returncode, 1)
        self.assertIn('nothing is left installed', result.stderr)
        self.assertEqual(host.files(), [])

    def test_enabling_again_replaces_the_other_backends_files(self):
        host = Host(self)
        host.run('enable', 'eth9')
        result = host.run('enable', '--backend', 'iptables', 'eth9')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(host.files(), [UNIT, IPTABLES_RULE])
        self.assertIn('nft delete table ip w1700k_tftp', host.calls())

    def test_disable_removes_every_file(self):
        for backend in ('nft', 'iptables'):
            with self.subTest(backend):
                host = Host(self)
                host.run('enable', '--backend', backend, 'eth9')
                result = host.run('disable')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(host.files(), [])
                self.assertIn('systemctl disable --now w1700k-tftp-forward.service',
                              host.calls())

    def test_an_active_firewall_is_pointed_out(self):
        result = Host(self, ufw=True).run('enable', 'eth9')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ufw is active', result.stderr)
        self.assertIn('UDP 7071 from 192.168.1.1 on eth9', result.stderr)

    def test_status_names_what_is_installed(self):
        host = Host(self)
        self.assertIn('nothing is installed', host.run('status').stdout)
        host.run('enable', '--backend', 'iptables', 'eth9')
        self.assertIn('installed: the iptables rule', host.run('status').stdout)


# Run as root of a private user and network namespace: two ends of a virtual
# cable, the far end holding the unit's address and one other, and a datagram to
# UDP 69 sent across it for each backend. Prints what port 7071 received.
PACKETS = r'''
import json, os, socket, subprocess, sys, time
tool, root = sys.argv[1:3]
os.environ.update(PATH='/usr/sbin:/usr/bin:/sbin:/bin', W1700K_TFTP_SYSROOT=root)
def run(*argv, check=True):
    return subprocess.run(argv, check=check, capture_output=True, text=True)
peer = subprocess.Popen(['unshare', '-n', 'sleep', '120'])
seen = {}
try:
    for _ in range(100):
        if os.readlink(f'/proc/{peer.pid}/ns/net') != os.readlink('/proc/self/ns/net'):
            break
        time.sleep(0.05)
    inside = ('nsenter', '-t', str(peer.pid), '-n')
    run('ip', 'link', 'add', 'veth0', 'type', 'veth', 'peer', 'name', 'veth1',
        'netns', str(peer.pid))
    run('ip', 'addr', 'add', '192.168.1.10/24', 'dev', 'veth0')
    run('ip', 'link', 'set', 'veth0', 'up')
    for address in ('192.168.1.1/24', '192.168.1.7/24'):
        run(*inside, 'ip', 'addr', 'add', address, 'dev', 'veth1')
    run(*inside, 'ip', 'link', 'set', 'veth1', 'up')
    send = ('import socket, sys; s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); '
            's.bind((sys.argv[1], 0)); s.sendto(b"rrq", ("192.168.1.10", 69))')
    def arrives(source, tries):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(('192.168.1.10', 7071))
            s.settimeout(1.0)
            for _ in range(tries):
                run(*inside, sys.executable, '-I', '-c', send, source, check=False)
                try:
                    return s.recvfrom(16)[1][0] == source
                except socket.timeout:
                    pass
        return False
    for backend in ('nft', 'iptables'):
        run(tool, 'enable', '--once', '--backend', backend, 'veth0')
        seen[backend] = {'unit': arrives('192.168.1.1', 10), 'other': arrives('192.168.1.7', 1)}
        seen[backend]['status'] = run(tool, 'status').stdout
        run(tool, 'disable')
        seen[backend]['after'] = arrives('192.168.1.1', 1)
    seen['left'] = run('nft', 'list', 'ruleset').stdout
finally:
    peer.kill()
print(json.dumps(seen))
'''


class RealRules(unittest.TestCase):
    """The kernel's own verdict on both rules, where a private namespace allows it."""

    def test_each_backend_redirects_the_units_requests_and_nothing_else(self):
        tools = os.environ.get('PATH', '') + ':/usr/sbin:/sbin'
        missing = [name for name in ('unshare', 'nsenter', 'ip', 'nft', 'iptables',
                                     'iptables-restore') if not shutil.which(name, path=tools)]
        if missing:
            self.skipTest('not installed: ' + ', '.join(missing))
        if subprocess.run(['unshare', '-Urn', 'true'], capture_output=True).returncode:
            self.skipTest('this host does not allow a private namespace')
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        (root / 'sys/class/net/veth0').mkdir(parents=True)
        result = subprocess.run(['unshare', '-Urn', sys.executable, '-I', '-c', PACKETS,
                                 str(TOOL), str(root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = json.loads(result.stdout.splitlines()[-1])
        for backend in ('nft', 'iptables'):
            with self.subTest(backend):
                self.assertTrue(seen[backend]['unit'], 'the unit\'s request did not arrive')
                self.assertFalse(seen[backend]['other'], 'another host\'s request arrived')
                self.assertFalse(seen[backend]['after'], 'the redirect outlived disable')
                self.assertIn('redirect to :7071' if backend == 'nft' else
                              '-j REDIRECT --to-ports 7071', seen[backend]['status'])
        self.assertNotIn('w1700k', seen['left'].lower())


class TftpForward(unittest.TestCase):
    def test_the_redirect_matches_only_the_unit_to_host_tftp_request(self):
        script = (HOST / 'tftp-forward').read_text()
        self.assertIn('iifname "$iface" ip saddr $unit_ip ip daddr $host_ip udp dport 69 '
                      'redirect to :$port', script)
        self.assertIn('table ip w1700k_tftp', script)
        self.assertNotIn('flush ruleset', script)
        # The iptables rule matches the same, and nothing is flushed but our chain.
        self.assertIn('-A $1 -i $iface -s $unit_ip -d $host_ip -p udp --dport 69 '
                      '-j REDIRECT --to-ports $port', script)
        self.assertNotIn('-F PREROUTING', script)

    def test_the_service_reloads_only_its_own_table(self):
        unit = (HOST / 'w1700k-tftp-forward.service').read_text()
        self.assertIn('ExecStartPre=-/usr/sbin/nft delete table ip w1700k_tftp', unit)
        self.assertIn('ExecStart=/usr/sbin/nft -f /etc/w1700k-tftp-forward.nft', unit)
        self.assertIn('CapabilityBoundingSet=CAP_NET_ADMIN', unit)
        unit = (HOST / 'w1700k-tftp-forward-iptables.service').read_text()
        self.assertIn('ExecStart=/usr/sbin/iptables-restore -w --noflush '
                      '/etc/w1700k-tftp-forward.iptables', unit)
        self.assertNotIn(' -F PREROUTING', unit)
        self.assertNotIn(' -t nat -F\n', unit)

    def test_systemd_accepts_the_service(self):
        tool = shutil.which('systemd-analyze')
        if not tool:
            self.skipTest('systemd-analyze is unavailable')
        for name in ('w1700k-tftp-forward.service', 'w1700k-tftp-forward-iptables.service'):
            with self.subTest(name):
                result = subprocess.run([tool, 'verify', str(HOST / name)],
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
