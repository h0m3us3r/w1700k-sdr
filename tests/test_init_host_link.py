# SPDX-License-Identifier: MIT
"""init/host/link beside NetworkManager: `keep` has the address held by a connection that will be
there the next time the interface appears and does not wait for DHCP; `up` and `down` cope with
what NetworkManager was doing. NetworkManager, `ip` and `ethtool` are stand-ins (init_fake_nm.py)
that behave as a host without a DHCP server on the link was seen to."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / 'init/host/link'
sys.path.insert(0, str(ROOT / 'tests'))
import init_fake_nm as nm  # noqa: E402

IFACE, CIDR, MTU = 'eth9', '10.77.0.10/24', '9216'
AUTO = 'Wired connection 1'         # what NetworkManager makes for an adapter it has not seen


class Host:
    def __init__(self, test, profiles=None, networkmanager=True):
        self.dir = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.dir)
        self.root = self.dir / 'root'
        (self.root / 'sys/class/net' / IFACE).mkdir(parents=True)
        (self.root / 'etc/NetworkManager/dispatcher.d').mkdir(parents=True)
        (self.dir / 'bin').mkdir()
        for name in ['ip', 'ethtool'] + (['nmcli'] if networkmanager else []):
            self.script(name, f'exec {sys.executable} {ROOT / "tests/init_fake_nm.py"} {name} "$@"\n')
        if not networkmanager:      # the program is installed and its service is not running
            self.script('nmcli', 'echo "Error: NetworkManager is not running." >&2\nexit 8\n')
        self.script('id', 'echo 0\n')
        self.state = self.dir / 'state.json'
        state = nm.new_state(IFACE)
        state['profiles'].update(profiles or {})
        self.state.write_text(json.dumps(state))
        self.env = {'PATH': f'{self.dir / "bin"}:/usr/bin:/bin', 'FAKE_NM': str(self.state),
                    'W1700K_LINK_SYSROOT': str(self.root)}
        self.world('replug', IFACE)         # the adapter is plugged in

    def script(self, name, body):
        path = self.dir / 'bin' / name
        path.write_text('#!/bin/sh\n' + body)
        path.chmod(0o755)

    def link(self, *argv):
        return subprocess.run([str(TOOL), *argv], capture_output=True, text=True, env=self.env)

    def world(self, *argv):
        subprocess.run([sys.executable, str(ROOT / 'tests/init_fake_nm.py'), 'world', *argv], check=True, env=self.env)

    def minutes(self, count):
        for _ in range(count * 60 // 45 + 1):
            self.world('wait')

    def read(self):
        return json.loads(self.state.read_text())

    @property
    def addresses(self):
        return self.read()['ifaces'][IFACE]['addresses']

    @property
    def nic(self):
        return self.read()['ifaces'][IFACE]

    @property
    def device(self):
        return self.read()['devices'][IFACE]

    @property
    def profiles(self):
        return self.read()['profiles']

    @property
    def hook(self):
        return self.root / 'etc/NetworkManager/dispatcher.d' / f'60-w1700k-{IFACE}'


def auto_profile(**more):
    return {AUTO: nm.profile(IFACE, 'auto', priority=-999, **more)}


class Keep(unittest.TestCase):
    def kept(self, host):
        """The address, the MTU and the ring are there, held by a connection NetworkManager will
        bring up again, and no DHCP request is under way."""
        self.assertEqual(host.addresses, [CIDR])
        self.assertEqual((host.nic['mtu'], host.nic['ring']), (int(MTU), nm.MAX_RING))
        device = host.device
        self.assertEqual(device['state'], 'connected')
        held_by = host.profiles[device['connection']]
        self.assertEqual((held_by['ipv4.method'], held_by['connection.autoconnect'], held_by['external']),
                         ('manual', 'yes', False))
        return device['connection']

    def test_a_fresh_adapter_whose_profile_asks_for_dhcp(self):
        """NetworkManager made "Wired connection N" for the adapter and is waiting for a lease no
        unit gives. The address must still be there three minutes on, and come back by itself."""
        host = Host(self, auto_profile())
        self.assertEqual(host.device['state'], 'connecting')
        result = host.link('keep', IFACE, CIDR, MTU)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.kept(host), f'w1700k-{IFACE}')
        self.assertIn(f'kept in the NetworkManager connection "w1700k-{IFACE}"', result.stdout)
        host.minutes(3)
        self.kept(host)
        self.assertFalse([line for line in host.read()['log'] if 'ip-config-unavailable' in line],
                         'NetworkManager asked for no lease after keep')
        ours, theirs = host.profiles[f'w1700k-{IFACE}'], host.profiles[AUTO]
        self.assertEqual(ours['ipv4.never-default'], 'yes')
        self.assertGreater(ours['connection.autoconnect-priority'], theirs['connection.autoconnect-priority'])
        self.assertEqual((theirs['ipv4.method'], theirs['ipv4.addresses']), ('auto', []), 'left as NetworkManager made it')

    def test_it_comes_back_when_the_adapter_is_plugged_in_again(self):
        host = Host(self, auto_profile())
        host.link('keep', IFACE, CIDR, MTU)
        for _ in range(2):
            host.world('replug', IFACE)
            self.assertEqual(self.kept(host), f'w1700k-{IFACE}', 'no command run')
            host.minutes(3)
            self.kept(host)

    def test_after_networkmanager_gave_up_and_assumed_a_connection(self):
        """DHCP failed for good, then an address was set by hand: NetworkManager shows a connection
        named after the interface, "connected (externally)". That one is gone at the next replug,
        so it is not where the address is kept."""
        host = Host(self, auto_profile())
        host.minutes(4)
        self.assertEqual(host.device['state'], 'disconnected')
        subprocess.run(['ip', 'address', 'add', CIDR, 'dev', IFACE], check=True, env=host.env)
        self.assertEqual((host.device['state'], host.device['connection']), ('external', IFACE))
        result = host.link('keep', IFACE, CIDR, MTU)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.kept(host), f'w1700k-{IFACE}')
        host.world('replug', IFACE)
        host.minutes(3)
        self.assertEqual(self.kept(host), f'w1700k-{IFACE}')

    def test_what_an_earlier_keep_left_in_the_dhcp_profile_is_taken_back_out(self):
        """Before, keep added the address to the connection it found, a DHCP one included, and
        NetworkManager saved it so."""
        host = Host(self, auto_profile(**{'ipv4.addresses': [CIDR], '802-3-ethernet.mtu': MTU}))
        result = host.link('keep', IFACE, CIDR, MTU)
        self.assertIn(f'took {CIDR} back out of "{AUTO}"', result.stdout)
        self.assertEqual(host.profiles[AUTO]['ipv4.addresses'], [])
        host.world('replug', IFACE)
        host.minutes(3)
        self.assertEqual(self.kept(host), f'w1700k-{IFACE}')

    def test_no_profile_at_all_gets_one_as_before(self):
        host = Host(self)
        self.assertEqual(host.device['state'], 'disconnected')
        result = host.link('keep', IFACE, CIDR, MTU)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.kept(host), f'w1700k-{IFACE}')
        self.assertEqual(host.profiles[f'w1700k-{IFACE}']['ipv6.method'], 'disabled')
        host.world('replug', IFACE)
        self.kept(host)

    def test_a_connection_of_the_users_with_its_address_set_by_hand_is_used_as_it_is(self):
        """Their own static connection manages the interface: the address joins it, and nothing
        of theirs is replaced."""
        mine = {'office': nm.profile(IFACE, 'manual', ['192.168.50.2/24'])}
        host = Host(self, mine)
        result = host.link('keep', IFACE, CIDR, MTU)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('kept in the NetworkManager connection "office"', result.stdout)
        self.assertNotIn(f'w1700k-{IFACE}', host.profiles)
        self.assertEqual(host.profiles['office']['ipv4.addresses'], ['192.168.50.2/24', CIDR])
        self.assertEqual(host.addresses, ['192.168.50.2/24', CIDR])
        host.world('replug', IFACE)
        self.assertEqual(host.addresses, ['192.168.50.2/24', CIDR])
        self.assertEqual(host.nic['mtu'], int(MTU))

    def test_a_second_keep_changes_nothing_and_interrupts_nothing(self):
        host = Host(self, auto_profile())
        host.link('keep', IFACE, CIDR, MTU)
        before = host.read()
        result = host.link('keep', IFACE, CIDR, MTU)
        self.assertEqual(result.returncode, 0, result.stderr)
        after = host.read()
        self.assertEqual(after['profiles'], before['profiles'])
        self.assertEqual([line for line in after['log'][len(before['log']):] if line.startswith('activate')], [],
                         'the connection was not brought up a second time')

    def test_the_hook_spreads_the_packets_when_the_interface_comes_up(self):
        host = Host(self, auto_profile())
        result = host.link('keep', IFACE, CIDR, MTU)
        self.assertIn(str(host.hook), result.stdout)
        text = host.hook.read_text()
        self.assertIn(f'[ "$1" = "{IFACE}" ] && [ "$2" = up ] || exit 0', text)
        self.assertIn(f'spread "{IFACE}" on', text)
        self.assertTrue(os.access(host.hook, os.X_OK))
        self.assertEqual(subprocess.run(['sh', '-n', str(host.hook)]).returncode, 0)

    def test_without_networkmanager_the_address_is_set_and_it_says_nothing_is_kept(self):
        host = Host(self, networkmanager=False)
        result = host.link('keep', IFACE, CIDR, MTU)
        self.assertEqual(result.returncode, 1)
        self.assertIn('NetworkManager does not run here: nothing kept', result.stderr)
        self.assertEqual(host.addresses, [CIDR])


    def test_the_user_is_given_the_hard_nice_limit_alone(self):
        """keep lets the user who ran sudo raise w1700k-stream to nice -10 by the hard limit alone:
        the kernel judges a nice level by the soft limit, so no other program of the user runs
        higher for it. What an earlier keep wrote, both limits, is replaced."""
        host = Host(self, auto_profile())
        (host.root / 'dev').mkdir()
        (host.root / 'dev/cpu_dma_latency').symlink_to('/dev/null')      # a character device, as the real one is
        limit = host.root / 'etc/security/limits.d/60-w1700k-stream.conf'
        for directory in (host.root / 'etc/udev/rules.d', limit.parent):
            directory.mkdir(parents=True)
        limit.write_text('# Written by init/host/link keep\nsomeone - nice -10\n')
        host.script('chown', 'exit 0\n')
        host.env.update(SUDO_UID='1000', SUDO_USER='someone')
        result = host.link('keep', IFACE, CIDR, MTU)
        self.assertEqual(result.returncode, 0, result.stderr)
        rules = [line for line in limit.read_text().splitlines() if not line.startswith('#')]
        self.assertEqual(rules, ['someone hard nice -10'])
        self.assertIn('w1700k-stream run by someone may raise itself to nice -10 from the next login', result.stdout)

class Up(unittest.TestCase):
    def test_beside_a_dhcp_request_that_nobody_answers(self):
        """The address added by hand went when the request timed out. `up` stops the request and
        says so; the address then lasts as long as the interface does."""
        host = Host(self, auto_profile())
        result = host.link('up', IFACE, CIDR, MTU)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('waiting for an address by DHCP', result.stderr)
        self.assertIn(f'keep {IFACE} {CIDR} {MTU}', result.stderr)
        host.minutes(3)
        self.assertEqual(host.addresses, [CIDR])
        self.assertEqual(host.profiles[AUTO]['ipv4.addresses'], [], 'nothing was written into the profile')
        host.world('replug', IFACE)
        self.assertEqual(host.addresses, [], 'up lasts until the interface goes away')

    def test_beside_a_connection_that_is_up_nothing_is_stopped(self):
        host = Host(self, {'office': nm.profile(IFACE, 'manual', ['192.168.50.2/24'])})
        result = host.link('up', IFACE, CIDR)
        self.assertEqual((result.returncode, result.stderr), (0, ''))
        self.assertEqual(host.addresses, ['192.168.50.2/24', CIDR])
        self.assertEqual(host.device['connection'], 'office')
        self.assertEqual(host.profiles['office']['ipv4.addresses'], ['192.168.50.2/24'])


class Down(unittest.TestCase):
    def test_it_removes_what_keep_made(self):
        host = Host(self, auto_profile())
        host.link('keep', IFACE, CIDR, MTU)
        result = host.link('down', IFACE, CIDR)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(f'w1700k-{IFACE}', host.profiles)
        self.assertNotIn(CIDR, host.addresses)
        self.assertFalse(host.hook.exists())
        host.world('replug', IFACE)
        host.minutes(4)
        self.assertEqual(host.addresses, [], 'nothing brings the address back')
        self.assertEqual(host.profiles[AUTO]['ipv4.method'], 'auto')

    def test_it_takes_the_address_back_out_of_the_users_connection(self):
        host = Host(self, {'office': nm.profile(IFACE, 'manual', ['192.168.50.2/24'])})
        host.link('keep', IFACE, CIDR)
        result = host.link('down', IFACE, CIDR)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(host.profiles['office']['ipv4.addresses'], ['192.168.50.2/24'])
        self.assertEqual(host.addresses, ['192.168.50.2/24'])

    def test_an_address_that_is_a_connections_only_one_stays_in_it_and_it_says_so(self):
        host = Host(self, {'office': nm.profile(IFACE, 'manual', [CIDR])})
        result = host.link('down', IFACE, CIDR)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('still has 10.77.0.10/24: it is its only address', result.stderr)
        self.assertEqual(host.addresses, [])


if __name__ == '__main__':
    unittest.main()
