# SPDX-License-Identifier: MIT
"""Test helper: a host's NetworkManager, `ip` and `ethtool`, as far as init/host/link uses them.

Run under the name of the tool it stands in for (the tests put small launchers on PATH):

    init_fake_nm.py nmcli|ip|ethtool ARGS...     what the tool would do, on the state in $FAKE_NM
    init_fake_nm.py world wait                   45 s pass: a DHCP request nobody answers fails
    init_fake_nm.py world replug IFACE           the adapter is pulled and plugged in again

The state is one JSON file. What NetworkManager does here is what was seen on a host (Ubuntu
24.04, NetworkManager 1.46, links with no DHCP server):

  - A profile whose ipv4.method is `auto` puts its static addresses on the interface and asks for
    a lease. When none comes it fails the activation and takes every address off the interface,
    one added by hand included; it tries again, and after its last try leaves the device
    disconnected with no address.
  - An address added by hand to a device it is not managing makes it assume a connection named
    after the interface, with autoconnect off, that it leaves alone (`connected (externally)`)
    and forgets when the interface goes away.
  - When an interface appears it brings up the autoconnecting profile for it with the highest
    priority, the most recently used among equals. Its own "Wired connection N" has priority -999.
"""
import json
import os
from pathlib import Path
import sys

STATE = Path(os.environ.get('FAKE_NM', 'fake-nm.json'))
TRIES = 4               # DHCP attempts before NetworkManager gives up
DEFAULT_MTU, DEFAULT_RING, MAX_RING = 1500, 256, 4096
FIELDS = ('connection.id', 'connection.autoconnect', 'connection.autoconnect-priority',
          'connection.interface-name', 'ipv4.method', 'ipv4.addresses', 'ipv4.never-default', 'ipv6.method',
          '802-3-ethernet.mtu', 'ethtool.ring-rx')


def new_state(iface='eth9'):
    return {'clock': 0, 'log': [],
            'ifaces': {iface: {'mtu': DEFAULT_MTU, 'ring': DEFAULT_RING, 'up': False, 'addresses': []}},
            'devices': {iface: {'state': 'disconnected', 'connection': None, 'tries': 0, 'held': False}},
            'profiles': {}}


def profile(iface, method='auto', addresses=(), autoconnect='yes', priority=0, **more):
    made = {'connection.autoconnect': autoconnect, 'connection.autoconnect-priority': priority,
            'connection.interface-name': iface, 'ipv4.method': method, 'ipv4.addresses': list(addresses),
            'ipv4.never-default': 'no', 'ipv6.method': 'auto', '802-3-ethernet.mtu': 'auto', 'ethtool.ring-rx': '',
            'external': False, 'used': 0}
    made.update(more)
    return made


class World:
    def __init__(self):
        self.s = json.loads(STATE.read_text())

    def save(self):
        STATE.write_text(json.dumps(self.s, indent=1))

    def log(self, text):
        self.s['log'].append(text)

    # --- NetworkManager's own doings ---

    def activate(self, iface, name):
        dev, nic, prof = self.s['devices'][iface], self.s['ifaces'][iface], self.s['profiles'][name]
        self.s['clock'] += 1
        prof['used'] = self.s['clock']
        dev.update(connection=name, tries=0, held=False)
        nic['up'] = True
        if prof['802-3-ethernet.mtu'] != 'auto':
            nic['mtu'] = int(prof['802-3-ethernet.mtu'])
        if prof['ethtool.ring-rx']:
            nic['ring'] = int(prof['ethtool.ring-rx'])
        if prof['ipv4.method'] == 'manual':
            nic['addresses'] = list(prof['ipv4.addresses'])
            dev['state'] = 'connected'
        elif prof['ipv4.method'] == 'auto':     # its static addresses at once, and a wait for a lease
            nic['addresses'] = list(dict.fromkeys(nic['addresses'] + prof['ipv4.addresses']))
            dev['state'] = 'connecting'
        else:
            nic['addresses'] = []
            dev['state'] = 'connected'
        self.log(f'activate {name} on {iface}')

    def deactivate(self, iface, held):
        dev = self.s['devices'][iface]
        name = dev['connection']
        if name and self.s['profiles'].get(name, {}).get('external'):
            del self.s['profiles'][name]
        dev.update(state='disconnected', connection=None, tries=0, held=held)
        self.s['ifaces'][iface]['addresses'] = []

    def candidates(self, iface):
        found = [(p['connection.autoconnect-priority'], p['used'], name) for name, p in self.s['profiles'].items()
                 if p['connection.interface-name'] == iface and p['connection.autoconnect'] == 'yes'
                 and not p['external']]
        return [name for _, _, name in sorted(found, reverse=True)]

    def autoconnect(self, iface):
        dev = self.s['devices'][iface]
        if dev['connection'] or dev['held']:
            return
        names = self.candidates(iface)
        if names:
            self.activate(iface, names[0])

    def wait(self):
        """45 seconds: every device still asking for a lease fails, and tries again or gives up."""
        for iface, dev in self.s['devices'].items():
            if dev['state'] != 'connecting':
                continue
            name, tries = dev['connection'], dev['tries'] + 1
            self.log(f'{iface}: ip-config-unavailable ({name}, try {tries})')
            self.s['ifaces'][iface]['addresses'] = []
            if tries < TRIES:
                self.activate(iface, name)
                dev['tries'] = tries
            else:
                self.deactivate(iface, held=True)

    def replug(self, iface):
        for name in [n for n, p in self.s['profiles'].items() if p['external'] and p['connection.interface-name'] == iface]:
            del self.s['profiles'][name]
        self.s['ifaces'][iface] = {'mtu': DEFAULT_MTU, 'ring': DEFAULT_RING, 'up': False, 'addresses': []}
        self.s['devices'][iface] = {'state': 'disconnected', 'connection': None, 'tries': 0, 'held': False}
        self.autoconnect(iface)

    def by_hand(self, iface, cidr):
        """`ip address add`: NetworkManager assumes a connection on a device it is not managing."""
        dev, nic = self.s['devices'][iface], self.s['ifaces'][iface]
        if cidr not in nic['addresses']:
            nic['addresses'].append(cidr)
        if dev['connection'] is None:
            self.s['profiles'][iface] = profile(iface, 'manual', [cidr], autoconnect='no', external=True)
            dev.update(state='external', connection=iface)
        elif dev['state'] == 'external':
            self.s['profiles'][dev['connection']]['ipv4.addresses'].append(cidr)

    # --- the tools ---

    def field(self, name, key):
        prof = self.s['profiles'][name]
        if key == 'connection.id':
            return name
        if key == 'ipv4.addresses':
            return ', '.join(prof['ipv4.addresses'])
        return str(prof[key])

    def nmcli(self, args):
        get = None
        while args and args[0].startswith('-'):
            flag = args.pop(0)
            if flag == '-g':
                get = args.pop(0).split(',')
            elif flag == '-f':
                args.pop(0)
        if args[:2] == ['general', 'status']:
            return 0
        if args[:2] == ['device', 'show']:
            dev = self.s['devices'].get(args[2])
            if dev is None:
                print(f"Error: Device '{args[2]}' not found.", file=sys.stderr)
                return 10
            state = {'disconnected': '30 (disconnected)', 'connecting': '70 (connecting (getting IP configuration))',
                     'connected': '100 (connected)', 'external': '100 (connected (externally))'}[dev['state']]
            for key in get or []:
                print({'GENERAL.STATE': state, 'GENERAL.CONNECTION': dev['connection'] or ''}[key])
            return 0
        if args[:2] == ['device', 'disconnect']:
            self.log(f'disconnect {args[2]}')
            self.deactivate(args[2], held=True)
            return 0
        if args[:2] == ['connection', 'show']:
            if len(args) == 2:
                for name in self.s['profiles']:
                    print(name.replace(':', '\\:'))
                return 0
            if args[2] not in self.s['profiles']:
                print(f'Error: {args[2]} - no such connection profile.', file=sys.stderr)
                return 10
            for key in get or FIELDS:
                print(self.field(args[2], key))
            return 0
        if args[:2] == ['connection', 'add']:
            words = dict(zip(args[2::2], args[3::2]))
            name, iface = words.pop('con-name'), words.pop('ifname')
            assert words.pop('type') == 'ethernet' and name not in self.s['profiles'], args
            made = profile(iface)
            made['connection.autoconnect'] = words.pop('autoconnect', 'yes')
            self.s['profiles'][name] = made
            self.modify(name, [w for pair in words.items() for w in pair])
            self.log(f'add {name}')
            self.autoconnect(iface)
            return 0
        if args[:2] == ['connection', 'modify']:
            return self.modify(args[2], args[3:])
        if args[:2] == ['connection', 'up']:
            name, iface = args[2], args[args.index('ifname') + 1]
            if name not in self.s['profiles']:
                return 10
            current = self.s['devices'][iface]['connection']
            if current and current != name and self.s['profiles'][current]['external']:
                del self.s['profiles'][current]
            self.s['ifaces'][iface]['addresses'] = []
            self.activate(iface, name)
            return 0
        if args[:2] == ['connection', 'delete']:
            name = args[2]
            if name not in self.s['profiles']:
                return 10
            iface = self.s['profiles'][name]['connection.interface-name']
            active = self.s['devices'].get(iface, {}).get('connection') == name
            del self.s['profiles'][name]
            self.log(f'delete {name}')
            if active:
                self.s['devices'][iface]['connection'] = None
                self.deactivate(iface, held=False)
                self.autoconnect(iface)
            return 0
        raise AssertionError(f'nmcli {args}')

    def modify(self, name, words):
        prof = self.s['profiles'].get(name)
        if prof is None:
            return 10
        for key, value in zip(words[0::2], words[1::2]):
            if key == '+ipv4.addresses':
                prof['ipv4.addresses'].append(value)
            elif key == '-ipv4.addresses':
                if prof['ipv4.method'] == 'manual' and prof['ipv4.addresses'] == [value]:
                    print("Error: ipv4.addresses: this property cannot be empty for 'method=manual'.", file=sys.stderr)
                    return 1
                prof['ipv4.addresses'] = [a for a in prof['ipv4.addresses'] if a != value]
            elif key == 'ipv4.addresses':
                prof['ipv4.addresses'] = [a.strip() for a in value.split(',')]
            elif key == 'connection.autoconnect-priority':
                prof[key] = int(value)
            else:
                assert key in prof, key
                prof[key] = value
        self.log(f'modify {name} {" ".join(words)}')
        return 0

    def ip(self, args):
        args = [a for a in args if a not in ('-4', '-o', '-brief')]
        if args[:3] == ['link', 'set', 'dev']:
            nic = self.s['ifaces'][args[3]]
            if args[4] == 'mtu':
                nic['mtu'] = int(args[5])
            elif args[4] == 'up':
                nic['up'] = True
            return 0
        if args[:2] == ['address', 'show']:
            for cidr in self.s['ifaces'][args[3]]['addresses']:
                print(f'2: {args[3]}    inet {cidr} brd 0.0.0.0 scope global {args[3]}')
            return 0
        if args[:2] == ['address', 'add']:
            self.by_hand(args[4], args[2])
            return 0
        if args[:2] == ['address', 'del']:
            nic = self.s['ifaces'][args[4]]
            nic['addresses'] = [a for a in nic['addresses'] if a != args[2]]
            return 0
        raise AssertionError(f'ip {args}')

    def ethtool(self, args):
        nic = self.s['ifaces'][args[1]]
        if args[0] == '-g':
            print(f'Ring parameters for {args[1]}:\nPre-set maximums:\nRX:\t\t{MAX_RING}\nTX:\t\t256\n'
                  f'Current hardware settings:\nRX:\t\t{nic["ring"]}\nTX:\t\t256')
        elif args[0] == '-G':
            nic['ring'] = int(args[3])
        return 0


def main(argv):
    world = World()
    tool, args = argv[1], list(argv[2:])
    if tool == 'world':
        world.wait() if args[0] == 'wait' else world.replug(args[1])
        code = 0
    else:
        code = getattr(world, tool)(args)
    world.save()
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv))
