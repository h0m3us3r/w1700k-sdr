# SPDX-License-Identifier: MIT
"""Where the stream service and its consumers run beside the network stack
(host/stream/src/placement.cpp): the CPU that takes every packet of an interface with one receive
queue is found from sysfs and procfs, here over made-up trees, and the process keeps off it."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CXX = os.environ.get('CXX', 'c++')


class Placement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tool = os.path.join(cls.tmp.name, 'placement_check')
        source = ROOT / 'host' / 'stream' / 'src'
        subprocess.run([CXX, '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', f'-I{source}',
                        str(ROOT / 'tests' / 'placement_check.cpp'), str(source / 'placement.cpp'), '-o', cls.tool],
                       check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.tree = tempfile.TemporaryDirectory()
        self.addCleanup(self.tree.cleanup)
        self.sys, self.proc = Path(self.tree.name, 'sys'), Path(self.tree.name, 'proc')

    def interface(self, name, device, queues=1):
        """An interface whose device is `device` (a path under sys/devices), with receive queues."""
        (self.sys / 'devices' / device).mkdir(parents=True, exist_ok=True)
        net = self.sys / 'class' / 'net' / name
        for queue in range(queues):
            (net / 'queues' / f'rx-{queue}').mkdir(parents=True)
        (net / 'device').symlink_to(self.sys / 'devices' / device)

    def interrupt(self, number, cpus):
        (self.proc / 'irq' / str(number)).mkdir(parents=True)
        (self.proc / 'irq' / str(number) / 'effective_affinity_list').write_text(cpus + '\n')

    def cpu(self, name):
        out = subprocess.run([self.tool, 'cpu', name, str(self.sys), str(self.proc)], capture_output=True, text=True,
                             check=True).stdout
        return int(out)

    def test_a_usb_adapter_is_taken_by_its_controllers_interrupt(self):
        self.interface('enx0', 'pci0000:00/0000:00:0d.0/usb2/2-2/2-2:1.0')
        controller = self.sys / 'devices' / 'pci0000:00' / '0000:00:0d.0'
        for number in (132, 125, 126):                         # the first, not the first listed
            (controller / 'msi_irqs' / str(number)).mkdir(parents=True)
        self.interrupt(125, '7')
        self.interrupt(126, '2')
        self.assertEqual(self.cpu('enx0'), 7)

    def platform_adapter(self):
        """An adapter on bus 2 of a USB controller that is a platform device: no interrupt in sysfs."""
        controller = 'platform/soc@0/a600000.usb/xhci-hcd.2.auto'
        self.interface('enx1', f'{controller}/usb2/2-1/2-1.1/2-1.1:1.0')
        (self.sys / 'devices' / controller / 'usb1').mkdir()
        self.interrupt(283, '7')
        self.interrupt(284, '2')

    def test_a_platform_usb_controllers_interrupt_is_found_by_its_bus(self):
        self.platform_adapter()
        (self.proc / 'interrupts').write_text(
            '            CPU0       CPU1\n'
            ' 21:          5          0  GICv3  21 Level     usb11-phy\n'
            '284:          5          0  GICv3 164 Level     dwc3-otg, other:usb21\n'
            '283:  511885689          0  GICv3 165 Level     xhci-hcd:usb1\n')
        self.assertEqual(self.cpu('enx1'), 7, 'listed under the controller\'s other bus')
        (self.proc / 'interrupts').write_text('283:  1  0  GICv3 165 Level  ehci:usb3, xhci-hcd:usb2, more\n'
                                              '284:  1  0  GICv3 166 Level  xhci-hcd:usb1\n')
        self.assertEqual(self.cpu('enx1'), 7, 'its own bus first, and one of several on a shared line')

    def test_a_platform_usb_controller_that_lists_none(self):
        self.platform_adapter()
        self.assertEqual(self.cpu('enx1'), -1, 'no /proc/interrupts at all')
        (self.proc / 'interrupts').write_text('283:  1  0  GICv3 165 Level  xhci-hcd:usb3\n')
        self.assertEqual(self.cpu('enx1'), -1)

    def test_an_interface_with_several_queues_is_left_alone(self):
        self.interface('eth0', 'pci0000:00/0000:01:00.0', queues=4)
        (self.sys / 'devices' / 'pci0000:00' / '0000:01:00.0' / 'msi_irqs' / '40').mkdir(parents=True)
        self.interrupt(40, '3')
        self.assertEqual(self.cpu('eth0'), -1)

    def test_a_line_interrupt_counts_and_none_does_not(self):
        self.interface('eth1', 'platform/soc/1f000000.ethernet')
        device = self.sys / 'devices' / 'platform' / 'soc' / '1f000000.ethernet'
        (device / 'irq').write_text('0\n')                      # no interrupt of its own
        self.assertEqual(self.cpu('eth1'), -1)
        (device / 'irq').write_text('16\n')
        self.interrupt(16, '0')
        self.assertEqual(self.cpu('eth1'), 0)

    def test_an_interrupt_on_several_cpus_names_none(self):
        self.interface('eth2', 'pci0000:00/0000:02:00.0')
        (self.sys / 'devices' / 'pci0000:00' / '0000:02:00.0' / 'irq').write_text('20\n')
        self.interrupt(20, '0-3')
        self.assertEqual(self.cpu('eth2'), -1)

    def test_no_such_interface_and_one_without_a_device(self):
        self.assertEqual(self.cpu('nothing'), -1)
        (self.sys / 'class' / 'net' / 'lo' / 'queues' / 'rx-0').mkdir(parents=True)
        self.assertEqual(self.cpu('lo'), -1)

    def test_the_process_keeps_off_a_cpu_and_comes_back(self):
        allowed = sorted(os.sched_getaffinity(0))
        if len(allowed) < 4:
            self.skipTest('needs four CPUs: fewer are not given up')
        out = subprocess.run([self.tool, 'off', str(allowed[-1])], capture_output=True, text=True, check=True).stdout
        self.assertEqual([int(c) for c in out.split()], allowed[:-1], 'off it, back on all, off it again')
        out = subprocess.run([self.tool, 'off', '4000'], capture_output=True, text=True, check=True).stdout
        self.assertEqual([int(c) for c in out.split()], allowed, 'a CPU it does not have changes nothing')


class Awake(unittest.TestCase):
    """host/stream/src/awake.cpp: the kernel is asked to keep the CPUs out of deep idle while a
    stream arrives, through a file that is here an ordinary one."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tool = os.path.join(cls.tmp.name, 'awake_check')
        source = ROOT / 'host' / 'stream' / 'src'
        subprocess.run([CXX, '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-pthread', f'-I{source}',
                        str(ROOT / 'tests' / 'awake_check.cpp'), str(source / 'awake.cpp'), '-o', cls.tool],
                       check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    IDLE, HELD, REFUSED = 0, 1, 2

    def steps(self, device):
        run = subprocess.run([self.tool, str(device)], capture_output=True, text=True, check=True)
        return run.stdout.split(), run.stderr

    def test_the_request_is_100_microseconds_held_until_let_go(self):
        device = Path(self.tmp.name, 'cpu_dma_latency')
        device.write_bytes(b'')
        steps, said = self.steps(device)
        self.assertEqual(steps, [str(self.IDLE), f'1:{self.HELD}', str(self.HELD), str(self.IDLE), str(self.HELD)])
        self.assertEqual(device.read_bytes(), (100).to_bytes(4, sys.byteorder), 'as the kernel reads it: an s32')
        self.assertEqual(said, '')

    def test_a_refusal_is_said_once_and_not_asked_again_until_let_go(self):
        steps, said = self.steps(Path(self.tmp.name, 'none', 'cpu_dma_latency'))
        self.assertEqual(steps, [str(self.IDLE), f'0:{self.REFUSED}', str(self.REFUSED), str(self.IDLE),
                                 str(self.REFUSED)])
        self.assertEqual(said.count('not kept out of deep idle'), 1)
        self.assertIn('init/host/link keep', said)


if __name__ == '__main__':
    unittest.main()
