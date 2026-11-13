# SPDX-License-Identifier: MIT
"""Controller state transitions on a simulated unit running our firmware, and the `serve`
protocol."""
import io
import json
import threading
import time
import unittest
from dataclasses import replace
from unittest import mock

import host_fake
from host_fake import RADIO_ERR_HW, RADIO_ERR_ORDER
from w1700k import radio, rf, stream
from w1700k.stream import StreamError
from w1700k.controller import (LOCK_MARGIN_HZ, LOCK_RESOLUTION_HZ, ControlError, Controller, handle, packing_fits,
                               packing_for, serve)

def setup(band, hz, bw):
    """SETUP's arguments: band, the LO's low and high words, the bandwidth class."""
    return ('setup', (band, hz & 0xFFFFFFFF, hz >> 32, bw))


STOCK_LOAD = [('bringup', ()), setup(0, 2_442_000_000, 3), ('capture', (0, 3)), setup(2, 6_105_000_000, 3),
              ('capture', (2, 3)), setup(1, 5_250_000_000, 3), ('capture', (1, 3)), ('select', (1,))]


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = host_fake.temp_dir()
        self.config = host_fake.make_config(self.tmp.name)
        self.unit = host_fake.FakeUnit()
        self.dev = self.unit.device
        self.events = []
        self.ctl = Controller(self.config, emit=self.events.append, unit=self.unit)
        self.ctl.attach()

    def tearDown(self):
        self.ctl.close()
        self.tmp.cleanup()

    def loaded(self, band=1, narrow0=False):
        """Loaded with `band` captured; narrow0: band 0 at 40 Msps (raw fits one link)."""
        if narrow0:
            self.ctl.bands[0] = radio.BandConfig(0, 2_442_000_000, 40_000_000).validate()
        self.ctl.load(band)
        self.dev.radio_commands.clear()
        self.dev.rf_ops.clear()
        self.events.clear()

    def commands(self):
        return [name for name, _ in self.dev.radio_commands]

    def logs(self):
        return [e['text'] for e in self.events if e.get('event') == 'log']


class Load(ControllerTest):
    def test_load_sequence(self):
        """Driver reload, ownership, RESET_MT, our set; the EEPROM, BRINGUP, per band (the captured band
        last) its analog filter (FILTER, before SETUP's DCOC), SETUP and CAPTURE, SELECT;
        every band's lock read."""
        self.ctl.load(1)
        self.assertEqual(self.unit.module_replacements, 1)
        self.assertEqual(self.dev.downloads, [('acquire',), ('--reset',), ('acquire',), ('w1700k_rom_patch.bin',),
                                              ('w1700k_wm.bin',), ('w1700k_wa.bin', '--wa')])
        self.assertEqual(self.dev.eeprom(), self.config.eeprom.read_bytes()[:7680])
        self.assertEqual(self.dev.radio_commands, STOCK_LOAD)
        ops = self.dev.rf_ops
        self.assertEqual([o for o in ops if o[0] == rf.RF_FILTER], [(rf.RF_FILTER, b, 320, 0) for b in (0, 2, 1)])
        self.assertEqual(ops[-1], (rf.RF_LOCKS, 0, 0, 0))
        self.assertEqual({o[0] for o in ops[:-1]}, {rf.RF_FILTER, rf.RF_TUNE}, 'filters, then the lock probe')
        # the lock probe: each band's span LOCK_MARGIN_HZ inside the edges, to the search's resolution
        for band, (lo, hi) in self.dev.lock_span.items():
            got_lo, got_hi = self.ctl.lo_lock[band]
            for got, edge, inward in ((got_lo, lo, 1), (got_hi, hi, -1)):
                self.assertLessEqual(0, (got - edge) * inward - LOCK_MARGIN_HZ, band)
                self.assertLessEqual((got - edge) * inward - LOCK_MARGIN_HZ, LOCK_RESOLUTION_HZ, band)
        self.assertEqual(self.dev.lo, {0: 2_441_999_999, 1: 5_250_000_000, 2: 6_105_000_000}, 'retuned home')
        self.assertEqual(self.dev.icap_band, 1)
        self.assertFalse(self.dev.wa_up, 'the WA starts with the first packed stream, not at load')
        state = self.ctl.state()
        self.assertTrue(state['radio']['loaded'])
        self.assertEqual(state['radio']['bands_ready'], [0, 1, 2])
        self.assertEqual(state['radio']['synth_locked'], [True, True, True])
        self.assertEqual(state['stream'], {'running': False, 'packing': None, 'packing_manual': False,
                                           'link_mbps': None, 'sample_rate': 320_000_000,
                                           'center_hz': 5_250_000_000})
        progress = [e['fraction'] for e in self.events if e.get('event') == 'progress']
        self.assertEqual((progress[0], progress[-1]), (0.0, 1.0))
        self.assertIsNone(state['error'])

    def test_second_load_skips_staged_files(self):
        self.ctl.load(0)
        pushes = [c for c in self.unit.log if isinstance(c, str) and c.startswith('push /tmp/w1700k-fw/')]
        self.ctl.load(0)
        again = [c for c in self.unit.log if isinstance(c, str) and c.startswith('push /tmp/w1700k-fw/')]
        self.assertEqual(len(pushes), 3)
        self.assertEqual(again, pushes)
        self.assertEqual(self.unit.module_replacements, 2)

    def test_radio_command_failure(self):
        """A refused radio command names the command, the firmware's reason and its failure record."""
        self.dev.radio_failures['setup'] = (RADIO_ERR_HW, 2, 0x24CA7)
        with self.assertRaises(ControlError) as caught:
            self.ctl.load(1)
        self.assertEqual(str(caught.exception), 'setup: hardware access failed (A-die poll timed out at 0x24ca7)')
        state = self.ctl.state()
        self.assertFalse(state['radio']['loaded'])
        self.assertEqual(state['error'], str(caught.exception))
        self.assertEqual(self.commands(), ['bringup', 'setup'])

    def test_radio_result_without_failure_record(self):
        self.dev.radio_failures['bringup'] = (RADIO_ERR_ORDER, 0, 0)
        with self.assertRaisesRegex(ControlError, '^bringup: a prerequisite has not run$'):
            self.ctl.load()

    def test_download_failure(self):
        self.dev.download_failure = 'w1700k_wm.bin'
        with self.assertRaisesRegex(RuntimeError, 'wm download failed: phase_name WAIT_DOWNLOAD, errno Timer expired'):
            self.ctl.load()
        self.assertEqual(self.dev.radio_commands, [])
        self.assertFalse(self.ctl.state()['radio']['loaded'])

    def test_state_survives_a_new_controller(self):
        self.ctl.load(2)
        self.ctl.start()
        other = Controller(self.config, unit=self.unit)
        other.attach()
        self.assertEqual(other.state()['radio'], self.ctl.state()['radio'])
        self.assertEqual(other.state()['stream'], self.ctl.state()['stream'])
        other.stop()
        self.assertFalse(self.dev.streaming)
        other.close()

    def test_state_of_another_boot_is_ignored(self):
        self.ctl.load(2)
        record = json.loads(self.unit.files['/tmp/w1700k-state.json'])
        record['boot_id'] = 'an earlier boot'
        self.unit.files['/tmp/w1700k-state.json'] = json.dumps(record).encode()
        other = Controller(self.config, unit=self.unit)
        other.attach()
        self.assertFalse(other.state()['radio']['loaded'])
        other.close()

    def test_missing_eeprom(self):
        self.config.eeprom.unlink()
        with self.assertRaises(ControlError):
            self.ctl.load()
        self.assertIn('EEPROM', self.ctl.state()['error'])
        self.assertEqual(self.unit.module_replacements, 0, 'checked before the driver is touched')


class Streaming(ControllerTest):
    def test_start_raw_and_stop(self):
        self.loaded(0, narrow0=True)
        self.ctl.start()
        self.assertEqual(self.dev.radio_commands, [('stream', (0,))])
        self.assertTrue(self.dev.streaming and self.dev.route_armed)
        argv = ' '.join(self.dev.pump_argv)
        self.assertIn('--go 0x0090f008 0x474f474f', argv)
        self.assertIn('--stop 0x0090f008 0x504f5453', argv)
        self.assertIn('--export eth1 10.77.0.1 10.77.0.10 02:00:00:00:00:01 7700 --ports 8', argv)
        self.assertFalse(self.dev.wa_up, 'no WA for raw')
        state = self.ctl.state()
        self.assertEqual((state['stream']['running'], state['stream']['packing']), (True, 'raw'))
        self.ctl.stop()
        self.assertFalse(self.dev.streaming or self.dev.route_armed)
        self.assertIsNone(self.ctl.state()['stream']['packing'])
        self.assertIn('stream stopped: 1000 chunks in 1.0 s', self.logs())

    def test_a_stream_this_host_would_drop_is_not_started(self):
        """With no destination MAC configured the stream comes to this host, whose interface must
        take its packets: one with a 1500-byte MTU drops 8 KiB slices and says nothing."""
        self.loaded(0, narrow0=True)
        self.ctl.config = replace(self.config, export=replace(self.config.export, dst_mac=None))
        link = ('enx0', '02:00:00:00:00:02', 1500)
        with mock.patch('w1700k.controller.local_link_for', return_value=link) as lookup:
            with self.assertRaisesRegex(StreamError, "this host's enx0 has an MTU of 1500, and packing raw sends "
                                                     'packets of 8236 bytes.*sudo init/host/link up enx0 '
                                                     r'10\.77\.0\.10/24 9216'):
                self.ctl.start()
            lookup.assert_called_with('10.77.0.10')
            self.assertFalse(self.dev.streaming)
            self.assertEqual(self.dev.radio_commands, [], 'refused before anything was asked of the radio')
            lookup.return_value = ('enx0', '02:00:00:00:00:02', 9000)
            self.ctl.start()
            self.assertIn('--export eth1 10.77.0.1 10.77.0.10 02:00:00:00:00:02 7700', ' '.join(self.dev.pump_argv))
            self.ctl.stop()
            # seven-bit slices of 1792 bytes: five to a datagram of the unit's 9216-byte MTU
            with self.assertRaisesRegex(StreamError, 'packing 7 sends packets of 9068 bytes'):
                self.ctl.start('7')

    def test_raw_then_half_without_reload(self):
        self.loaded(0, narrow0=True)
        self.ctl.start()
        self.ctl.stop()
        self.ctl.configure(1)
        self.ctl.start()
        self.assertTrue(self.dev.wa_up)
        self.assertEqual(self.ctl.state()['stream']['packing'], 'half')
        self.ctl.stop()
        self.ctl.configure(0)
        self.ctl.start()
        self.assertEqual(self.dev.radio_commands, [('stream', (0,)), ('select', (1,)), ('stream', (1,)),
                                                   ('select', (0,)), ('stream', (0,))])
        self.assertEqual(self.unit.module_replacements, 1)

    def test_packing_follows_the_rate(self):
        self.loaded(1)
        self.ctl.start()
        self.assertEqual(self.ctl.state()['stream']['packing'], 'half')
        self.ctl.start()                                    # already running so: nothing
        self.assertEqual(self.commands(), ['stream'])

    def test_a_slower_link_packs_every_chunk(self):
        """160 Msps does not fit a 5 Gbit/s link raw: every chunk goes out packed (full), which
        the WA packs half of; the port's speed is read at the start."""
        self.loaded(1)
        self.ctl.configure(1, sample_rate=160_000_000)
        self.dev.radio_commands.clear()
        self.dev.link_mbps = 5000
        self.ctl.start()
        self.assertEqual(self.dev.radio_commands, [('stream', (2,))])
        self.assertTrue(self.dev.wa_up)
        self.assertEqual(self.ctl.state()['stream']['packing'], 'full')
        self.ctl.stop()
        self.assertFalse(self.dev.streaming or self.dev.route_armed)

    def test_packing_by_link(self):
        """Raw where it fits, else every chunk packed up to 160 Msps, else every other chunk, else
        reduced to the most bits per sample that fit."""
        for mbps, rate, packing in ((10000, 160_000_000, 'raw'), (10000, 320_000_000, 'half'),
                                    (5000, 80_000_000, 'raw'), (5000, 160_000_000, 'full'),
                                    (2500, 40_000_000, 'raw'), (2500, 80_000_000, 'full'),
                                    (2500, 160_000_000, '14'), (1000, 80_000_000, '8'), (1000, 40_000_000, '16'),
                                    (5000, 320_000_000, 'split')):
            self.assertEqual(packing_for(rate, mbps), packing, (mbps, rate))
        for mbps, rate in ((2500, 320_000_000), (1000, 160_000_000), (100, 40_000_000)):
            with self.assertRaises(ControlError):
                packing_for(rate, mbps)
        self.assertFalse(packing_fits('full', 320_000_000, 10000), 'the packers carry every chunk to 160 Msps')
        self.assertFalse(packing_fits('14', 320_000_000, 10000), 'the SoC alone reduces up to 160 Msps')
        self.assertFalse(packing_fits('split', 160_000_000, 10000), 'split is for where the SoC alone cannot')
        # small datagrams: more of the link, and no more of them than the senders manage
        self.assertEqual(packing_for(40_000_000, 5000, 1456), 'raw')
        self.assertEqual(packing_for(80_000_000, 5000, 1456), 'full', '80 Msps raw would be 234k datagrams/s')
        self.assertEqual(packing_for(80_000_000, 1000, 1456), '8')
        self.assertEqual(packing_for(160_000_000, 10000, 1456), '8', '156k datagrams/s; split is whole slices only')
        with self.assertRaisesRegex(ControlError, 'more than the unit sends'):
            packing_for(320_000_000, 10000, 1456)

    def test_a_rate_the_link_cannot_carry_is_refused(self):
        self.loaded(1)
        self.dev.link_mbps = 2500
        with self.assertRaisesRegex(ControlError, r'320 Msps needs 4\.59 Gbit/s on the link even reduced to 14 bits '
                                                  r'\(split between the radio and the unit.*carries 2\.5 '):
            self.ctl.start()
        with self.assertRaisesRegex(ControlError, 'packing 14 is set by hand.*only up to 160 Msps, not 320'):
            self.ctl.start('14')
        self.assertIsNone(self.ctl.state()['error'], 'a refused request is not an error of the receiver')
        self.assertEqual(self.dev.radio_commands, [])
        self.assertFalse(self.dev.streaming)
        self.ctl.configure(1, sample_rate=160_000_000)
        self.dev.link_mbps = 1000
        with self.assertRaisesRegex(ControlError, r'160 Msps needs 1\.17 Gbit/s on the link even at 7 bits per '
                                                  r'sample, and the link carries 1 '):
            self.ctl.start()

    def test_split_packing_at_320_msps(self):
        """320 Msps on 5 Gbit/s: the radio reduces every other chunk, the unit's senders the rest;
        the driver's export and the senders both run."""
        self.loaded(1)
        self.dev.link_mbps = 5000
        self.ctl.start()
        self.assertEqual(self.dev.radio_commands, [('stream', (3,))])
        self.assertTrue(self.dev.wa_up)
        self.assertEqual(self.ctl.state()['stream']['packing'], 'split')
        text = ' '.join(self.dev.pump_argv)
        self.assertIn('--export eth1 10.77.0.1 10.77.0.10 ', text)
        self.assertIn('--drain-ms 3000 --send 10.77.0.1 10.77.0.10 7700 --ports 8 --senders 2 --format 14 --split '
                      '--datagram 9188 --cpu 3', text)
        self.assertNotIn('--export-cpu', text, 'the export thread shares the senders\' CPUs')
        self.ctl.stop()
        self.assertFalse(self.dev.streaming or self.dev.route_armed)
        # by hand it is for 320 Msps only
        self.ctl.configure(1, sample_rate=160_000_000)
        with self.assertRaisesRegex(ControlError, 'packing split is set by hand; it is for whole slices above 160'):
            self.ctl.start('split')

    def test_a_reduced_packing_where_no_lossless_one_fits(self):
        """160 Msps on 2.5 Gbit/s: the radio streams raw and the unit's senders reduce it to 14 bits."""
        self.loaded(1)
        self.ctl.configure(1, sample_rate=160_000_000)
        self.dev.radio_commands.clear()
        self.dev.link_mbps = 2500
        self.ctl.start()
        self.assertEqual(self.dev.radio_commands, [('stream', (0,))])
        self.assertFalse(self.dev.wa_up)
        self.assertEqual(self.ctl.state()['stream']['packing'], '14')
        text = ' '.join(self.dev.pump_argv)
        self.assertIn('--send 10.77.0.1 10.77.0.10 7700 --ports 8 --senders 2 --format 14 --datagram 9188 --cpu 3', text)
        self.assertNotIn('--export', text)
        # a rate a lossless packing carries: restarted with that one
        self.ctl.configure(1, sample_rate=80_000_000)
        self.assertEqual(self.ctl.state()['stream']['packing'], 'full')
        self.assertIn('--export eth1', ' '.join(self.dev.pump_argv))
        self.ctl.stop()
        self.assertFalse(self.dev.streaming or self.dev.route_armed)

    def test_small_datagrams(self):
        """export.piece: every packing leaves through the senders, in pieces a small MTU carries."""
        self.loaded(0, narrow0=True)
        self.dev.mtu = 1500
        self.ctl.config = replace(self.config, export=replace(self.config.export, piece=1456))
        self.ctl.start()
        self.assertEqual(self.ctl.state()['stream']['packing'], 'raw')
        text = ' '.join(self.dev.pump_argv)
        self.assertIn('--send 10.77.0.1 10.77.0.10 7700 --ports 8 --senders 2 --piece 1456 --cpu 3', text)
        self.assertNotIn('--format', text)

    def test_link_cap_from_the_configuration(self):
        """export.link_mbps below the port's speed is the link (an adapter that carries less)."""
        self.loaded(1)
        self.ctl.configure(1, sample_rate=160_000_000)
        self.ctl.config = replace(self.config, export=replace(self.config.export, link_mbps=5000))
        self.ctl.start()
        self.assertEqual(self.ctl.state()['stream']['packing'], 'full')

    def test_a_port_without_a_speed_counts_as_10g(self):
        self.loaded(1)
        self.dev.link_mbps = None
        self.ctl.start()
        self.assertEqual(self.ctl.state()['stream']['packing'], 'half')

    def test_packing_override(self):
        self.loaded(0, narrow0=True)
        self.ctl.start('full')
        self.assertEqual(self.dev.radio_commands, [('stream', (2,))])
        self.assertEqual(self.ctl.state()['stream']['packing'], 'full')
        with self.assertRaises(ValueError):
            self.ctl.start('lzma')

    def test_packing_set_by_hand_in_the_configuration(self):
        """export.packing is every stream's packing, whatever the link: the state says it was set
        by hand, and a band switch keeps the stream running where the packers carry the rate."""
        self.ctl.config = replace(self.config, export=replace(self.config.export, packing='half'))
        self.loaded(0, narrow0=True)
        self.dev.link_mbps = 1000                       # not consulted
        self.ctl.start()
        stream_state = self.ctl.state()['stream']
        self.assertEqual((stream_state['packing'], stream_state['packing_manual'], stream_state['link_mbps']),
                         ('half', True, 1000))
        self.ctl.configure(1)                           # 320 Msps: still half, switched mid-stream
        self.assertEqual(self.dev.radio_commands, [('stream', (1,))])
        self.assertEqual(self.ctl.state()['radio']['band'], 1)
        self.ctl.stop()
        self.assertFalse(self.ctl.state()['stream']['packing_manual'])

    def test_packing_set_by_hand_stays_across_a_restart(self):
        """`start --packing` holds for that stream, also where a new rate restarts it."""
        self.loaded(0, narrow0=True)
        self.ctl.start('half')
        self.ctl.configure(0, sample_rate=80_000_000)
        self.assertEqual([c for c in self.dev.radio_commands if c[0] == 'stream'], [('stream', (1,)), ('stream', (1,))])
        self.assertEqual((self.ctl.state()['stream']['packing'], self.ctl.state()['stream']['packing_manual']),
                         ('half', True))
        self.ctl.stop()
        self.ctl.start()                                # the next stream is chosen automatically again
        self.assertEqual((self.ctl.state()['stream']['packing'], self.ctl.state()['stream']['packing_manual']),
                         ('raw', False))

    def test_full_by_hand_beyond_the_packers_is_refused(self):
        self.ctl.config = replace(self.config, export=replace(self.config.export, packing='full'))
        self.loaded(1)                                  # 320 Msps
        with self.assertRaisesRegex(ControlError, 'only up to 160 Msps'):
            self.ctl.start()
        self.assertEqual(self.dev.radio_commands, [])

    def test_mid_stream_change_the_link_cannot_carry(self):
        """Refused before the stream is touched: it keeps running as it was."""
        self.loaded(0, narrow0=True)
        self.dev.link_mbps = 1500
        self.ctl.start()
        for change in (lambda: self.ctl.configure(1), lambda: self.ctl.configure(0, sample_rate=320_000_000)):
            with self.assertRaises(ControlError):
                change()
            self.assertEqual(self.dev.radio_commands, [('stream', (0,))])
            self.assertTrue(self.dev.streaming)
            self.assertEqual((self.ctl.state()['radio']['band'], self.ctl.state()['stream']['packing']), (0, 'raw'))

    def test_start_loads_when_needed(self):
        self.ctl.start()
        self.assertEqual(self.unit.module_replacements, 1)
        self.assertEqual(self.commands(), [name for name, _ in STOCK_LOAD] + ['stream'])
        self.assertTrue(self.ctl.state()['stream']['running'])

    def test_small_mtu_refused(self):
        self.loaded(0)
        self.dev.mtu = 1500
        with self.assertRaisesRegex(stream.StreamError, 'MTU is 1500'):
            self.ctl.start()
        self.assertFalse(self.dev.streaming)
        self.assertEqual(self.dev.radio_commands, [])

    def test_ctl_timeout_while_the_stream_sets_up(self):
        self.loaded(0)
        self.dev.setup_timeouts = 2
        self.ctl.start()
        self.assertTrue(self.ctl.state()['stream']['running'])

    def test_stream_ending_on_its_own(self):
        self.loaded(0)
        self.ctl.start()
        self.unit.end_stream(3)                              # the producer saw an overrun
        self.ctl.poll()
        state = self.ctl.state()
        self.assertFalse(state['stream']['running'])
        self.assertEqual(state['error'], 'the stream ended: overrun')
        self.ctl.start()
        self.assertTrue(self.ctl.state()['stream']['running'])
        self.assertEqual(self.unit.module_replacements, 1)

    def test_wa_left_in_the_packer(self):
        self.loaded(1)
        self.ctl.start()
        self.dev.wa_stuck = True
        self.ctl.stop()
        self.assertIn('stream clean-up: the WA did not leave the packer; reload the firmware', self.logs())
        self.assertTrue(self.dev.route_armed, 'the route stays while a core may read the pool')
        self.assertFalse(self.ctl.state()['stream']['running'])

    def test_unlocked_synthesizer_is_reported(self):
        self.loaded(1)
        self.dev.sx_status[2] = 2
        self.dev.unlocking[2] = 2             # and its retunes do not lock either: only a power cycle helps
        self.ctl.start()
        state = self.ctl.state()
        self.assertTrue(state['stream']['running'])
        self.assertEqual(state['radio']['synth_locked'], [True, True, False])
        self.assertIn('band 2 synthesizer unlocked', state['error'])
        self.assertIn('power-cycle', state['error'])
        self.ctl.stop()
        self.ctl.load()
        self.assertEqual(self.ctl.state()['radio']['synth_locked'], [True, True, False],
                         'a reload does not reset the A-dies')


class RfControl(ControllerTest):
    def test_tune_gain_filter_trim(self):
        """RF commands on our WM, with or without a stream."""
        self.loaded(1)
        self.ctl.tune(5_300_000_000)
        self.ctl.gain(lna=2, lpf=9)
        self.ctl.filter(80)
        self.assertEqual(self.ctl.trim(7), [0x10700] * 4)
        self.assertEqual(self.dev.rf_ops, [(rf.RF_TUNE, 1, 5_300_000_000 & 0xFFFFFFFF, 1), (rf.RF_GAIN, 1, 2, 9),
                                           (rf.RF_FILTER, 1, 80, 0), (rf.RF_TRIM, 1, 7, 0)])
        state = self.ctl.state()
        self.assertEqual((state['rf']['lo_hz'], state['stream']['center_hz']), (5_300_000_000, 5_300_000_000))
        self.assertEqual((state['rf']['agc'], state['rf']['lna'], state['rf']['lpf']), (False, 2, 9))
        self.assertEqual(state['rf']['filter_mhz'], 80)
        self.assertEqual(state['rf']['lo_range_hz'], list(self.ctl.lo_lock[1]))
        self.ctl.gain(agc=True)
        self.assertEqual(self.dev.rf_ops[-1], (rf.RF_GAIN, 1, rf.RF_GAIN_AGC, 0))
        self.assertEqual((self.ctl.state()['rf']['agc'], self.ctl.state()['rf']['lna']), (True, None))
        self.assertEqual(self.dev.radio_commands, [], 'RF control is no radio command')

    def test_the_filter_follows_the_sample_rate(self):
        """Each band's analog filter is as wide as its sample rate, at load and at a new rate: it
        is the anti-alias filter, and the width the roll-off correction applies to."""
        self.ctl.bands[0] = radio.BandConfig(0, 2_442_000_000, 40_000_000).validate()
        self.ctl.load(0)
        filters = {o[1]: o[2] for o in self.dev.rf_ops if o[0] == rf.RF_FILTER}
        self.assertEqual(filters, {0: 40, 1: 320, 2: 320})
        self.assertEqual((self.ctl.state()['rf']['filter_mhz'], self.ctl.state()['rf']['filter_follows_rate']), (40, True))
        self.dev.rf_ops.clear()
        self.ctl.configure(0, sample_rate=160_000_000)
        self.assertEqual([o for o in self.dev.rf_ops if o[0] == rf.RF_FILTER], [(rf.RF_FILTER, 0, 160, 0)])
        self.assertEqual(self.ctl.state()['rf']['filter_mhz'], 160)
        self.dev.rf_ops.clear()
        self.ctl.configure(2, sample_rate=320_000_000, capture_rate=640_000_000)     # 640/2: the widest
        self.assertEqual([o for o in self.dev.rf_ops if o[0] == rf.RF_FILTER], [], 'already 320 MHz')
        self.assertEqual(self.ctl.state()['rf']['filter_mhz'], 320)

    def test_a_filter_set_by_hand_holds_until_told_to_follow(self):
        self.ctl.bands[0] = radio.BandConfig(0, 2_442_000_000, 160_000_000).validate()
        self.ctl.load(0)
        self.ctl.filter(80)
        self.assertEqual((self.ctl.state()['rf']['filter_mhz'], self.ctl.state()['rf']['filter_follows_rate']), (80, False))
        self.dev.rf_ops.clear()
        self.ctl.configure(0, sample_rate=40_000_000)
        self.assertEqual([o for o in self.dev.rf_ops if o[0] == rf.RF_FILTER], [], 'kept across a new rate')
        self.assertEqual(self.ctl.state()['rf']['filter_mhz'], 80)
        self.ctl.filter_follow(True)                         # every band back to its rate
        self.assertEqual([o for o in self.dev.rf_ops if o[0] == rf.RF_FILTER], [(rf.RF_FILTER, 0, 40, 0)])
        self.assertEqual((self.ctl.state()['rf']['filter_mhz'], self.ctl.state()['rf']['filter_follows_rate']), (40, True))
        self.ctl.filter_follow(False)                        # nothing moves: the filters stay as they are
        self.assertEqual(len([o for o in self.dev.rf_ops if o[0] == rf.RF_FILTER]), 1)
        self.assertFalse(self.ctl.state()['rf']['filter_follows_rate'])
        # a controller that attaches later sees the choice
        other = Controller(self.config, unit=self.unit)
        other.attach()
        self.assertFalse(other.state()['rf']['filter_follows_rate'])
        other.close()

    def test_filter_follow_request(self):
        self.loaded(1)
        self.ctl.filter(80)
        handle(self.ctl, {'id': 1, 'op': 'filter_follow', 'on': True})
        self.assertEqual((self.ctl.state()['rf']['filter_mhz'], self.ctl.state()['rf']['filter_follows_rate']), (320, True))
        with self.assertRaises(ValueError):
            handle(self.ctl, {'id': 2, 'op': 'filter_follow', 'on': 'yes'})

    def test_while_streaming(self):
        self.loaded(0)
        self.ctl.start()
        self.ctl.tune(2_450_000_000)
        self.assertTrue(self.dev.streaming)
        self.assertEqual(self.dev.lo[0], 2_450_000_000)
        self.assertEqual(self.ctl.state()['rf']['lo_range_hz'], list(self.ctl.lo_lock[0]))

    def test_status(self):
        self.loaded(1)
        self.ctl.tune(5_300_000_000)
        self.ctl.gain(lna=2, lpf=9)
        self.ctl.filter(80)
        out = self.ctl.rf_status()
        self.assertEqual(out, {'band': 1, 'vco_hz': 8_480_000_000, 'vco': 'locked', 'firmware_lo_khz': 5_300_000,
                               'filter_codes': [2] * 4, 'gain': ['lna 2 lpf 9'] * 4})
        self.assertEqual(self.dev.rf_ops[-2:], [(rf.RF_STATUS, 1, 0, 0), (rf.RF_LOCKS, 0, 0, 0)])

    def test_refusals(self):
        self.loaded(1)
        for call in (lambda: self.ctl.tune(3_000_000_000), lambda: self.ctl.filter(30),
                     lambda: self.ctl.gain(lna=2), lambda: self.ctl.gain(lna=5, lpf=0)):
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(self.dev.rf_ops, [])

    def test_not_loaded(self):
        with self.assertRaisesRegex(ControlError, 'not loaded'):
            self.ctl.tune(5_300_000_000)

    def test_synth_unlocked(self):
        self.loaded(1)
        self.dev.unlocking[1] = 1
        with self.assertRaisesRegex(rf.SynthUnlocked, 'band 1 synthesizer unlocked: the VCO did not lock at '
                                                      '5300.000 MHz \\(too low\\)'):
            self.ctl.tune(5_300_000_000)
        state = self.ctl.state()
        self.assertEqual(state['radio']['synth_locked'], [True, False, True])
        self.assertEqual(state['rf']['lo_hz'], 5_250_000_000)
        del self.dev.unlocking[1]
        self.ctl.tune(5_300_000_000)
        self.assertEqual(self.ctl.state()['radio']['synth_locked'], [True, True, True])


class BandSwitch(ControllerTest):
    def test_mid_stream_switch(self):
        """A ready band at the same sample rate: one RF band command, the stream keeps running."""
        self.loaded(1)
        self.ctl.start()
        self.ctl.configure(2)
        self.assertTrue(self.dev.streaming)
        self.assertEqual(self.dev.rf_ops[-2:], [(rf.RF_BAND, 2, 0, 0), (rf.RF_LOCKS, 0, 0, 0)])
        self.assertEqual(self.dev.icap_band, 2)
        self.assertEqual(self.dev.radio_commands, [('stream', (1,))])
        self.assertTrue(any(t.startswith('capturing band 2 from ICAP0 write pointer 0x') for t in self.logs()))
        state = self.ctl.state()
        self.assertEqual((state['radio']['band'], state['stream']['center_hz']), (2, 6_105_000_000))
        self.assertEqual(self.unit.module_replacements, 1)

    def test_raw_stream_to_a_wide_band_restarts_packed(self):
        self.loaded(0, narrow0=True)
        self.ctl.start()
        self.ctl.configure(1)
        self.assertEqual(self.dev.radio_commands, [('stream', (0,)), ('select', (1,)), ('stream', (1,))])
        self.assertEqual(self.dev.icap_band, 1)
        self.assertEqual(self.ctl.state()['stream']['packing'], 'half')
        self.assertEqual(self.unit.module_replacements, 1)

    def test_idle_switch(self):
        self.loaded(1)
        self.ctl.configure(0)
        self.assertEqual(self.dev.radio_commands, [('select', (0,))])
        self.assertEqual(self.ctl.state()['radio']['band'], 0)
        self.ctl.configure(0, 320_000_000, 2_442_000_000)
        self.assertEqual(len(self.dev.radio_commands), 1, 'already captured')

    def test_new_rate_on_the_running_band(self):
        """Stop, RATE and CAPTURE of that band, SELECT, start again with the new rate's packing; no
        SETUP, the LO stays."""
        self.loaded(1)
        self.ctl.start()
        self.ctl.configure(1, sample_rate=160_000_000)
        self.assertEqual(self.dev.radio_commands, [('stream', (1,)), ('rate', (1, 2)), ('capture', (1, 2)),
                                                   ('select', (1,)), ('stream', (0,))])
        state = self.ctl.state()
        self.assertTrue(state['stream']['running'])
        self.assertEqual((state['radio']['lo_hz'], state['radio']['sample_rate']), (5_250_000_000, 160_000_000))
        self.assertEqual((state['stream']['sample_rate'], state['stream']['packing']), (160_000_000, 'raw'))
        self.assertEqual(self.unit.module_replacements, 1)

    def test_lo_alone_mid_stream(self):
        """A new LO is one TUNE, the stream keeps running; the band keeps the LO the grid realises."""
        self.loaded(1)
        self.ctl.start()
        self.ctl.configure(1, lo_hz=5_530_000_007)
        self.assertEqual(self.dev.radio_commands, [('stream', (1,))])
        self.assertEqual(self.dev.rf_ops[-1], (rf.RF_TUNE, 1, 5_530_000_007 & 0xFFFFFFFF, 1))
        state = self.ctl.state()
        self.assertTrue(state['stream']['running'])
        lo = radio.actual_lo(1, 5_530_000_007)
        self.assertEqual((state['radio']['lo_hz'], state['rf']['lo_hz']), (lo, lo))
        self.assertLess(abs(lo - 5_530_000_007), radio.lo_step(1, lo))

    def test_failed_rate_is_not_ready(self):
        """A band whose RATE failed is not ready; configuring it again runs RATE again."""
        self.loaded(1)
        self.dev.radio_failures['rate'] = (RADIO_ERR_HW, 2, 0x14CA7)
        with self.assertRaises(ControlError):
            self.ctl.configure(1, sample_rate=160_000_000)
        self.assertEqual(self.ctl.state()['radio']['bands_ready'], [0, 2])
        other = Controller(self.config, unit=self.unit)
        other.attach()
        self.assertEqual(other.state()['radio']['bands_ready'], [0, 2], 'saved so')
        other.close()
        self.ctl.configure(1, sample_rate=160_000_000)
        self.assertEqual(self.commands(), ['rate', 'rate', 'capture', 'select'])
        self.assertEqual(self.ctl.state()['radio']['bands_ready'], [0, 1, 2])

    def test_configure_before_load_loads(self):
        self.ctl.configure(2, 320_000_000)
        self.assertIn(setup(2, 6_105_000_000, 3), self.dev.radio_commands)
        self.assertEqual(self.dev.radio_commands[-1], ('select', (2,)))
        self.assertTrue(self.ctl.state()['radio']['loaded'])

    def test_bad_configuration(self):
        self.loaded(1)
        for bad in (dict(sample_rate=640_000_000), dict(lo_hz=3_000_000_000), dict(lo_hz=2_079_999_999)):
            with self.assertRaises(ValueError):
                self.ctl.configure(0, **bad)
        self.assertEqual(self.dev.radio_commands, [])

    def test_half_640_on_band_2(self):
        """640/2: RATE and CAPTURE at the 320 MHz class, the stream at 320 Msps, packed half; the
        320 Msps class again is another RATE. Other bands refuse it before any command."""
        self.loaded(2)
        self.ctl.start()
        self.ctl.configure(2, 320_000_000, capture_rate=640_000_000)
        self.assertEqual(self.dev.radio_commands[1:], [('rate', (2, 4)), ('capture', (2, 4)), ('select', (2,)),
                                                       ('stream', (1,))])
        state = self.ctl.state()
        self.assertEqual((state['radio']['sample_rate'], state['radio']['capture_rate'], state['stream']['packing']),
                         (320_000_000, 640_000_000, 'half'))
        self.dev.radio_commands.clear()
        self.ctl.configure(2, 320_000_000)
        self.assertEqual(self.dev.radio_commands[:2], [('rate', (2, 3)), ('capture', (2, 3))])
        self.assertEqual(self.ctl.state()['radio']['capture_rate'], 320_000_000)
        self.dev.radio_commands.clear()
        for band in (0, 1):
            with self.assertRaisesRegex(ValueError, 'band 2 only'):
                self.ctl.configure(band, 320_000_000, capture_rate=640_000_000)
        self.assertEqual(self.dev.radio_commands, [])

    def test_rate_alone(self):
        """Another sample rate is RATE + CAPTURE: the LO, the gain and the filter stay."""
        self.loaded(0)
        self.ctl.tune(2_450_000_000)
        self.ctl.filter(20)
        self.dev.radio_commands.clear()
        self.ctl.configure(0, sample_rate=40_000_000)
        self.assertEqual(self.dev.radio_commands, [('rate', (0, 0)), ('capture', (0, 0)), ('select', (0,))])
        rfs = self.ctl.state()['rf']
        self.assertEqual((rfs['lo_hz'], rfs['filter_mhz'], self.ctl.state()['stream']['sample_rate']),
                         (2_450_000_000, 20, 40_000_000))


class Serve(ControllerTest):
    def test_protocol(self):
        lines = [{'id': 1, 'op': 'status'}, {'id': 2, 'op': 'load', 'band': 0}, {'id': 3, 'op': 'start'},
                 {'id': 4, 'op': 'tune', 'hz': 2_450_000_000}, {'id': 5, 'op': 'bogus'},
                 {'id': 6, 'op': 'gain', 'agc': True}, {'id': 7, 'op': 'configure', 'band': 1, 'sample_rate': 320_000_000},
                 {'id': 8, 'op': 'filter', 'mhz': 'wide'}, {'id': 9, 'op': 'stop'}]
        stdin = io.StringIO(''.join(json.dumps(x) + '\n' for x in lines) + 'not json\n')
        stdout = io.StringIO()
        ctl = Controller(self.config, unit=self.unit)
        thread = threading.Thread(target=serve, args=(ctl, stdin, stdout, 0.05))
        thread.start()
        thread.join(30)
        self.assertFalse(thread.is_alive())
        out = [json.loads(line) for line in stdout.getvalue().splitlines()]
        replies = {o['id']: o for o in out if 'id' in o}
        self.assertEqual(sorted(replies), list(range(1, 10)))
        self.assertEqual(len([o for o in out if 'id' in o]), 9, 'exactly one reply per request')
        self.assertEqual([i for i in replies if not replies[i]['ok']], [5, 8])
        self.assertEqual(replies[5]['error'], "unknown op 'bogus'")
        self.assertEqual(replies[8]['error'], 'mhz: expected int')
        self.assertEqual(replies[4]['state']['rf']['lo_hz'], 2_450_000_000)
        self.assertEqual(replies[7]['state']['stream']['packing'], 'half')
        self.assertEqual(replies[7]['state']['radio']['band'], 1)
        self.assertFalse(replies[9]['state']['stream']['running'])
        events = [o['event'] for o in out if 'event' in o]
        self.assertIn('state', events)
        self.assertIn('progress', events)
        self.assertIn('log', events)                    # the malformed line


    def test_handle_checks_arguments(self):
        for request, error in (({'op': 'configure', 'band': 1, 'sample_rate': '80'}, 'sample_rate: expected int'),
                               ({'op': 'tune', 'hz': True}, 'hz: expected'),
                               ({'op': 'load', 'band': '1'}, 'band: expected int')):
            with self.assertRaisesRegex(ValueError, error):
                handle(self.ctl, request)
        self.assertEqual(self.dev.radio_commands, [])
        self.assertEqual(handle(self.ctl, {'op': 'load'}), {})
        self.assertEqual(self.ctl.state()['radio']['band'], 1)
        handle(self.ctl, {'op': 'gain', 'lna': 1, 'lpf': 2})
        self.assertEqual(self.dev.rf_ops[-1], (rf.RF_GAIN, 1, 1, 2))

class Sessions(ControllerTest):
    """Two controllers on one unit (the spectrum page's and a SoapySDR program's, say): each reads
    the unit's state record before an operation and whenever it polls, and adopts the other's
    changes; a long operation is written there first, and the other waits its turn."""

    STATE = '/tmp/w1700k-state.json'

    def other(self):
        events = []
        other = Controller(self.config, emit=events.append, unit=self.unit)
        other.attach()
        self.addCleanup(other.close)
        events.clear()
        return other, events

    def record(self):
        return json.loads(self.unit.files[self.STATE])

    def write(self, **changes):
        self.unit.files[self.STATE] = json.dumps(dict(self.record(), **changes)).encode()

    def test_a_poll_adopts_what_another_session_changed(self):
        self.loaded(1)
        other, events = self.other()
        self.ctl.tune(5_300_000_000)
        self.assertNotEqual(other.state()['rf']['lo_hz'], self.ctl.state()['rf']['lo_hz'], 'not before it looks')
        other.poll()
        self.assertEqual(other.state()['rf'], self.ctl.state()['rf'])
        self.assertEqual(events[-1]['state']['rf']['lo_hz'], self.ctl.state()['rf']['lo_hz'], 'and it says so')
        events.clear()
        other.poll()
        self.assertEqual(events, [], 'nothing new: nothing said')
        other.gain(lna=3, lpf=4)
        self.ctl.poll()
        self.assertEqual((self.ctl.state()['rf']['lna'], self.ctl.state()['rf']['lpf']), (3, 4), 'and the other way')

    def test_an_operation_acts_on_the_receiver_as_it_is_now(self):
        self.loaded(1)
        other, _ = self.other()
        self.ctl.configure(0, sample_rate=40_000_000)
        other.start()                                       # without a poll in between
        state = other.state()
        self.assertEqual((state['radio']['band'], state['stream']['sample_rate'], state['stream']['packing']),
                         (0, 40_000_000, 'raw'), 'the band and rate the first session set, not its own stale ones')
        self.ctl.poll()
        self.assertEqual(self.ctl.state()['stream'], state['stream'])

    def test_a_stream_another_session_stopped_did_not_end_by_itself(self):
        self.loaded(0, narrow0=True)
        other, events = self.other()
        self.ctl.start()
        other.poll()
        self.assertTrue(other.state()['stream']['running'])
        self.ctl.stop()
        other.poll()
        self.assertEqual((other.state()['stream']['running'], other.state()['error']), (False, None))
        self.assertFalse([e for e in events if e.get('event') == 'log' and e['level'] == 'error'])

    def test_a_long_operation_is_written_to_the_unit_while_it_runs(self):
        seen = []
        push = self.unit.push

        def watching(data, remote, mode=None):
            if remote == self.STATE:
                record = json.loads(data)
                seen.append((record['busy'], record['session'] == self.ctl.session))
            return push(data, remote, mode)

        self.unit.push = watching
        self.ctl.load(1)
        self.assertEqual(seen[0], ('loading', True), 'before anything else')
        self.assertEqual(seen[-1], (None, True), 'and taken back at the end')
        self.assertTrue(all(busy == 'loading' for busy, _ in seen[:-1]))
        seen.clear()
        self.ctl.tune(5_300_000_000)
        self.assertEqual({busy for busy, _ in seen}, {None}, 'a short one is not')

    def test_another_sessions_long_operation_is_shown_and_waited_for(self):
        self.loaded(1)
        other, events = self.other()
        self.write(session='someone', busy='configuring', busy_since=time.monotonic())
        other.poll()
        self.assertEqual(other.state()['busy'], 'configuring')

        def done():
            time.sleep(0.5)
            self.write(busy=None, busy_since=None)

        waiter = threading.Thread(target=done)
        began = time.monotonic()
        waiter.start()
        other.tune(5_300_000_000)
        waiter.join()
        self.assertGreaterEqual(time.monotonic() - began, 0.5, 'the tune waited for it')
        self.assertIsNone(other.state()['busy'])
        self.assertEqual(other.state()['rf']['lo_hz'], radio.actual_lo(1, 5_300_000_000))

    def test_a_stream_is_not_called_ended_while_another_session_restarts_it(self):
        self.loaded(0, narrow0=True)
        self.ctl.start()
        other, events = self.other()
        self.write(session='someone', busy='configuring', busy_since=time.monotonic())
        self.unit.end_stream()                              # that session stopped it, and has not said so yet
        other.poll()
        self.assertIsNone(other.state()['error'])
        self.assertTrue(other.state()['stream']['running'], 'as the record still says')

    def test_a_session_that_died_in_a_long_operation_is_not_waited_for_for_ever(self):
        self.loaded(1)
        other, _ = self.other()
        self.write(session='someone', busy='loading', busy_since=time.monotonic() - 1000)
        other.poll()
        self.assertIsNone(other.state()['busy'])
        began = time.monotonic()
        other.tune(5_300_000_000)
        self.assertLess(time.monotonic() - began, 2)

    def test_a_reboot_of_the_unit_is_noticed(self):
        self.loaded(1)
        self.unit.boot_id = 'the next boot'
        del self.unit.files[self.STATE]
        self.ctl.poll()
        self.assertFalse(self.ctl.state()['radio']['loaded'])
        self.assertIn('the unit has rebooted: its radio is not loaded', self.logs())

    def test_the_units_own_reader_of_the_record_still_finds_what_it_looks_for(self):
        """w1700k-led takes the first match of each key: what a session adds comes after them all."""
        self.loaded(1)
        keys = list(self.record())
        for key in ('session', 'busy', 'busy_since'):
            self.assertGreater(keys.index(key), max(keys.index(k) for k in ('loaded', 'band', 'bands', 'packing')))

if __name__ == '__main__':
    unittest.main()
