# SPDX-License-Identifier: MIT
"""What a stream is made of on the host: the band configurations and the EEPROM file (radio.py),
the pump's command line and the producer's result page (stream.py, against probe_page.h)."""
import struct
import unittest

import host_fake
from host_fake import ROOT
from w1700k import radio, stream
from w1700k.config import ExportConfig
from w1700k.radio import BandConfig


class Bands(unittest.TestCase):
    def test_band_config(self):
        cfg = BandConfig(2, 6_105_000_000, 320_000_000).validate()
        self.assertEqual((cfg.rate_code, cfg.lo_hz, cfg.rf_band), (3, 6_105_000_000, '6g'))
        self.assertEqual(BandConfig(0, 2_442_000_000.0, 80_000_000).validate().lo_hz, 2_442_000_000)
        self.assertEqual(BandConfig(0, 2_441_000_123, 320_000_000).validate().rate_code, 3)
        self.assertEqual(BandConfig(1, 4_650_000_000, 40_000_000).validate().as_dict(),
                         {'lo_hz': 4_650_000_000, 'sample_rate': 40_000_000, 'capture_rate': 40_000_000})
        half = BandConfig(2, 6_105_000_000, 320_000_000, 640_000_000).validate()      # 640/2
        self.assertEqual((half.rate_code, half.sample_rate, half.capture_rate), (4, 320_000_000, 640_000_000))
        # outside TUNE's reach (radio.LO_RANGE); the span a unit locks is the controller's (lo_lock)
        for bad in (BandConfig(2, 6_105_000_000, 640_000_000), BandConfig(1, 5_250_000_000, 20_000_000),
                    BandConfig(1, 5_250_000_000, 320_000_000, 640_000_000),    # 640/2: band 2 only
                    BandConfig(0, 2_442_000_000, 320_000_000, 640_000_000),
                    BandConfig(2, 6_105_000_000, 160_000_000, 640_000_000),
                    BandConfig(0, 2_069_999_999, 40_000_000), BandConfig(0, 2_750_000_001, 40_000_000),
                    BandConfig(1, 4_639_999_999, 40_000_000), BandConfig(2, 8_000_000_001, 40_000_000),
                    BandConfig(3, 5_250_000_000, 40_000_000)):
            with self.assertRaises(ValueError):
                bad.validate()

    def test_lo_grid(self):
        """The LO each synthesizer realises (rfctl.c divider): 2 FRAC LSBs a step, ties to even."""
        self.assertEqual([round(radio.lo_step(b, hz), 4) for b, hz in ((0, 2.4e9), (1, 5.0e9), (1, 6.0e9))],
                         [2.9802, 11.9209, 14.3051])
        for band, hz in ((0, 2_442_000_001), (0, 2_080_000_000), (1, 5_250_000_006), (2, 7_409_999_999),
                         (1, 5_925_999_999), (1, 5_926_000_000)):
            lo = radio.actual_lo(band, hz)
            self.assertLessEqual(abs(lo - hz), radio.lo_step(band, hz) / 2 + 1, (band, hz, lo))
        self.assertEqual(radio.actual_lo(0, 2_450_000_000), 2_450_000_000)   # N = 98 exactly
        self.assertEqual(radio.actual_lo(0, 2_442_000_000), 2_441_999_999)   # N = 97.68: the nearest grid point

    def test_defaults_and_codes(self):
        self.assertEqual({b: (c.lo_hz, c.sample_rate) for b, c in radio.DEFAULT_BANDS.items()},
                         {0: (2_442_000_000, 320_000_000), 1: (5_250_000_000, 320_000_000),
                          2: (6_105_000_000, 320_000_000)})
        # radio_cmd.h SETUP / RATE bandwidth classes 20 / 40 / 80 / 160 MHz
        self.assertEqual(radio.RATE_CODE, {(40_000_000, 40_000_000): 0, (80_000_000, 80_000_000): 1,
                                           (160_000_000, 160_000_000): 2, (320_000_000, 320_000_000): 3,
                                           (320_000_000, 640_000_000): 4})

    def test_eeprom_file(self):
        data = bytes(range(256)) * 30 + b'\xff' * (16384 - 7680)
        self.assertEqual(radio.eeprom_image(data), data[:7680])
        self.assertEqual(radio.eeprom_image(data[:7680]), data[:7680])
        with self.assertRaises(radio.RadioError):
            radio.eeprom_image(bytes(7680) + b'\x01' + bytes(16384 - 7681))
        with self.assertRaises(radio.RadioError):
            radio.eeprom_image(bytes(100))


class Pump(unittest.TestCase):
    def test_command_line(self):
        argv = stream.stream_argv('/usr/sbin/mt7996-stream', ExportConfig(), '02:00:00:00:00:02')
        text = ' '.join(argv)
        self.assertEqual(argv[:2], ['/usr/sbin/mt7996-stream', '/dev/w1700k-sdr'])
        self.assertIn('--stop 0x0090f008 0x504f5453', text)
        self.assertIn('--go 0x0090f008 0x474f474f', text)
        self.assertIn('--export eth1 10.77.0.1 10.77.0.10 02:00:00:00:00:02 7700 --ports 8', text)
        self.assertIn('--idle-ms 300', text)
        self.assertIn('--drain-ms 3000', text)
        self.assertIn('--export-cpu 2 --cpu 3', text)
        self.assertNotIn('--pulse', argv, 'our WM runs the producer: no IRQ5 pulse')

    def test_command_line_of_the_senders(self):
        """A reduced packing, or small datagrams, leave through the pump's own senders."""
        argv = stream.stream_argv('/usr/sbin/mt7996-stream', ExportConfig(), '02:00:00:00:00:02', '7')
        text = ' '.join(argv)
        self.assertIn('--send 10.77.0.1 10.77.0.10 7700 --ports 8 --senders 2 --format 7 --cpu 3', text)
        self.assertNotIn('--export', text)
        self.assertIn('--go 0x0090f008 0x474f474f', text)
        argv = stream.stream_argv('/usr/sbin/mt7996-stream', ExportConfig(piece=1456), '02:00:00:00:00:02', 'full')
        self.assertIn('--send 10.77.0.1 10.77.0.10 7700 --ports 8 --senders 2 --piece 1456 --cpu 3', ' '.join(argv))
        # with the port's MTU known, a datagram carries as many whole slices as fit
        argv = stream.stream_argv('/usr/sbin/mt7996-stream', ExportConfig(), '02:00:00:00:00:02', '14', 9216)
        self.assertIn('--format 14 --datagram 9188 --cpu 3', ' '.join(argv))

    def test_datagram_rate(self):
        self.assertAlmostEqual(stream.datagram_rate('raw', 80_000_000, 1456), 234375)
        self.assertAlmostEqual(stream.datagram_rate('14', 160_000_000, 1456), 234375)
        self.assertAlmostEqual(stream.datagram_rate('half', 320_000_000, 1456), 859375)


class Link(unittest.TestCase):
    def test_wire_rate(self):
        """One datagram per chunk: 8192 or 7168 bytes and 82 of headers, preamble and gap."""
        self.assertAlmostEqual(stream.wire_rate('raw', 320_000_000) / 1e9, 10.3425, places=4)
        self.assertAlmostEqual(stream.wire_rate('half', 320_000_000) / 1e9, 9.7025, places=4)
        self.assertAlmostEqual(stream.wire_rate('full', 160_000_000) / 1e9, 4.53125, places=4)
        self.assertEqual(stream.WIRE_OVERHEAD, 16 + 8 + 20 + 14 + 4 + 8 + 12)

    def test_wire_rate_reduced_and_in_pieces(self):
        """A reduced chunk is 2048 samples at its bits per pair; each piece pays the 82 bytes again."""
        for packing, bits in stream.REDUCED.items():
            self.assertEqual(stream.REDUCED_CHUNK[packing], 2048 * bits // 8)
        self.assertAlmostEqual(stream.wire_rate('14', 320_000_000) / 1e9, 4.5825, places=4)
        self.assertAlmostEqual(stream.wire_rate('7', 320_000_000) / 1e9, 2.3425, places=4)
        self.assertAlmostEqual(stream.wire_rate('8', 80_000_000) / 1e9, 0.665625, places=5)
        self.assertAlmostEqual(stream.wire_rate('14', 320_000_000, 1456) / 1e9, 4.7875, places=4)   # 3 pieces
        self.assertAlmostEqual(stream.wire_rate('raw', 40_000_000, 1456) / 1e9, 1.356875, places=5)  # 6 pieces

    def test_mtu_needed(self):
        self.assertEqual(stream.mtu_needed('raw'), 8236)
        self.assertEqual(stream.mtu_needed('full'), 8236, 'the driver checks for a whole raw slice')
        self.assertEqual(stream.mtu_needed('14'), 3584 + 44)
        self.assertEqual(stream.mtu_needed('7'), 1792 + 44)
        self.assertEqual(stream.mtu_needed('raw', 1456), 1500)
        self.assertEqual(stream.mtu_needed('8', 4000), 2048 + 44)

    def test_interface_speed(self):
        unit = host_fake.FakeUnit()
        s = stream.Stream(unit)
        self.assertEqual(s.check_interface('eth1'), (10000, 9216))
        unit.device.link_mbps = 5000
        self.assertEqual(s.check_interface('eth1'), (5000, 9216))
        unit.device.link_mbps = None
        self.assertEqual(s.check_interface('eth1'), (None, 9216))
        with self.assertRaisesRegex(stream.StreamError, 'MTU is 1500; packing 14 needs at least 3628'):
            s.check_mtu('eth1', 1500, '14')
        s.check_mtu('eth1', 1500, '14', 1456)
        with self.assertRaises(stream.StreamError):
            s.check_interface('eth9')


class ResultPage(unittest.TestCase):
    def page(self, status, wa_status=0):
        values = [0] * (len(stream.RES32) + 2 * len(stream.RES64))
        values[0], values[stream.RES32.index('wa_status')] = status, wa_status
        values[len(stream.RES32)] = 500                                   # chunks
        values[len(stream.RES32) + 2] = 720_000_000                       # cycles
        return struct.pack('<2I', stream.DONE_MAGIC, status) + bytes(8) + struct.pack(f'<{len(values)}I', *values)

    def test_parse(self):
        result = stream.parse_result(self.page(7), {'at': 'stream'})
        self.assertEqual((result.status, result.chunks, result.seconds, result.ok, result.text), (7, 500, 1.0, True,
                                                                                              'stopped'))
        self.assertEqual(result.reader, {'at': 'stream'})
        overrun = stream.parse_result(self.page(3))
        self.assertEqual((overrun.overrun, overrun.ok, overrun.text), (True, False, 'overrun'))
        self.assertEqual(stream.parse_result(bytes(len(self.page(7)))).text, 'not reached')
        self.assertEqual(stream.parse_result(b'').text, 'no result page')
        self.assertTrue(stream.wa_exited(self.page(7, 0x101)))
        self.assertFalse(stream.wa_exited(self.page(7, 0x001)))

    def test_layout_mirrors_probe_page(self):
        h = host_fake.header_defines(ROOT / 'include' / 'hw' / 'hw_bits.h',
                                     ROOT / 'include' / 'hw' / 'mt7996' / 'probe_page.h')
        self.assertEqual((stream.DONE_AT, stream.DONE_MAGIC, stream.GO_ADDR, stream.GO_MAGIC, stream.STOP_MAGIC),
                         (h['MT7996_PROBE_WM_DONE'], h['MT7996_PROBE_DONE_MAGIC'], h['MT7996_PROBE_WM_GO'],
                          h['MT7996_PROBE_GO_MAGIC'], h['MT7996_PROBE_STOP_MAGIC']))
        res = h['MT7996_PROBE_WM_RES'] - stream.DONE_AT
        for i, name in enumerate(stream.RES32):
            self.assertEqual(4 * i, h[f'MT7996_PROBE_RES_{name.upper()}'], name)
        for i, name in enumerate(stream.RES64):
            self.assertEqual(4 * len(stream.RES32) + 8 * i, h[f'MT7996_PROBE_RES_{name.upper()}'], name)
        self.assertEqual(4 * stream.RESULT_WORDS, res + h['MT7996_PROBE_RES_SIZE'])
        codes = {h[n]: n for n in h if n.startswith('MT7996_PROBE_RES_ST_')}
        self.assertEqual(sorted(stream.STATUS), [-1] + sorted(set(codes) - {1}), '1 (the full duration): not ours')
        self.assertEqual(stream.STATUS[7], 'stopped')
        self.assertEqual(stream.MIN_MTU, 8192 + 44)                       # a slice and its headers


if __name__ == '__main__':
    unittest.main()
