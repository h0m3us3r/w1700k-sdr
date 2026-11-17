# SPDX-License-Identifier: MIT
"""Our WM from the host: the status block and radio constants against the C headers wm.py
mirrors, the status, heartbeat and traps, reads (with faults), writes, calls, the WA start, radio
commands posted and waited for, and the EEPROM delivery."""
import unittest

import host_fake
from host_fake import BROUGHT_UP, EE_BUFFER, RADIO_ERR_ARG, RADIO_ERR_ORDER, ROOT
from w1700k import radio, rf, wm

HEADERS = [ROOT / 'include' / 'hw' / 'hw_bits.h', ROOT / 'include' / 'hw' / 'mt7996' / 'probe_page.h',
           ROOT / 'firmware' / 'wm' / 'wm.h', ROOT / 'firmware' / 'wa' / 'wa.h',
           ROOT / 'firmware' / 'radio' / 'radio_cmd.h', ROOT / 'firmware' / 'radio' / 'radio.h',
           ROOT / 'firmware' / 'radio' / 'hal.h']


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds

    def __call__(self):
        return self.now


class Mirrors(unittest.TestCase):
    """wm.py's constants are the headers' (firmware/wm/wm.h, wa/wa.h, radio/radio_cmd.h, hal.h,
    include/hw/mt7996/probe_page.h)."""

    @classmethod
    def setUpClass(cls):
        cls.h = host_fake.header_defines(*HEADERS)

    def test_status_block(self):
        h = self.h
        self.assertEqual((wm.STATUS, wm.MAGIC), (h['WM_STATUS'], h['WM_MAGIC']))
        self.assertEqual((wm.STATUS, wm.MAGIC), (h['MT7996_PROBE_OWN_WM_STATUS'], h['MT7996_PROBE_OWN_WM_MAGIC']))
        self.assertEqual(len(wm.STATUS_WORDS), h['WM_STATUS_WORDS'])
        self.assertEqual((wm.STATE_RUNNING, wm.STATE_TRAPPED), (h['WM_STATE_RUNNING'], h['WM_STATE_TRAPPED']))
        self.assertEqual((wm.WA_START_REQ, wm.WA_STARTED), (h['WM_WA_START_REQ'], h['WM_WA_STARTED']))
        for i, name in enumerate(wm.STATUS_WORDS):
            if name in ('magic', 'version', 'beats', 'state'):
                offset = h[f'WM_{name.upper()}_AT']
            elif name[:-1] in ('arg', 'radio_arg'):
                offset = h[f'WM_{name[:-1].upper()}'](int(name[-1]))
            else:
                offset = h[f'WM_{name.upper()}']
            self.assertEqual(4 * i, offset, name)
        for name in ('call', 'ret', 'wa_start', 'radio_ee', 'radio_req', 'radio_state', 'rf_build', 'unlock', 'denied'):
            self.assertEqual(wm.STATUS + 4 * wm.STATUS_WORDS.index(name), h[f'MT7996_PROBE_OWN_WM_{name.upper()}'])
        self.assertEqual(wm.UNLOCK_MAGIC, h['WM_UNLOCK_MAGIC'])
        self.assertEqual(wm.UNLOCK_MAGIC, h['MT7996_PROBE_OWN_WM_UNLOCK_MAGIC'])
        self.assertEqual(host_fake.OWN_IMAGE, (h['WM_ENTRY'], h['WM_LIMIT']))

    def test_wa_status_block(self):
        h = self.h
        self.assertEqual(wm.WA_STATUS, h['WA_STATUS'])
        self.assertEqual(wm.WA_STATUS, h['MT7996_PROBE_OWN_WA_STATUS'])
        self.assertEqual(wm.WA_MAGIC, h['WA_MAGIC'])
        self.assertEqual(len(wm.WA_STATUS_WORDS), h['WA_STATUS_WORDS'])
        for i, name in enumerate(wm.WA_STATUS_WORDS):
            self.assertEqual(4 * i, h[f'WA_{name.upper()}_AT'], name)
        self.assertLessEqual(wm.STATUS + 4 * len(wm.STATUS_WORDS), wm.WA_STATUS, 'after the WM radio words')
        self.assertLessEqual(wm.WA_STATUS + 4 * len(wm.WA_STATUS_WORDS), rf.CTL_BASE)

    def test_radio_commands(self):
        h = self.h
        self.assertEqual(wm.RADIO_OPS, {name: h[f'RADIO_OP_{name.upper()}'] for name in wm.RADIO_OPS})
        self.assertEqual(sorted(wm.RADIO_OPS.values()), list(range(1, 11)))
        self.assertEqual(wm.RADIO_STEPS, {name: h[f'RADIO_STEP_{name.upper()}'] for name in wm.RADIO_STEPS})
        self.assertEqual(wm.RADIO_PACKING, {name: h[f'RADIO_PACKING_{name.upper()}'] for name in wm.RADIO_PACKING})
        self.assertEqual(sorted(wm.RADIO_RESULTS), [h[n] for n in ('RADIO_OK', 'RADIO_ERR_OP', 'RADIO_ERR_ARG',
                                                                   'RADIO_ERR_ORDER', 'RADIO_ERR_HW')])
        self.assertEqual(wm.RADIO_SELFTEST_MAGIC, h['RADIO_SELFTEST_MAGIC'])
        fails = [h[n] for n in h if n.startswith('HAL_FAIL_') and n != 'HAL_FAIL_NONE']
        self.assertEqual(sorted(wm.RADIO_FAILS), sorted(fails), 'a reason for every HAL failure code')
        self.assertEqual(wm.EEPROM_BYTES, h['RADIO_EE_BYTES'])
        self.assertEqual(wm.EEPROM_BYTES, radio.EEPROM_BYTES)


class Own(unittest.TestCase):
    def setUp(self):
        self.unit = host_fake.FakeUnit()
        self.dev = self.unit.device
        self.clock = FakeClock()

    def wm(self):
        return wm.Wm(self.unit, sleep=self.clock.sleep, clock=self.clock)

    def running(self):
        self.dev.boot_wm()
        w = self.wm()
        w.wait_running()
        return w

    def test_status_and_trap(self):
        w = self.running()
        st = w.status()
        self.assertEqual((st['magic'], st['state'], st['radio_ee']), (wm.MAGIC, wm.STATE_RUNNING, EE_BUFFER))
        self.dev.set_st('state', wm.STATE_TRAPPED)
        self.dev.set_st('trap_cause', 2)
        with self.assertRaisesRegex(wm.WmError, 'our WM trapped: cause 0x2'):
            w.wait_running()

    def test_not_started(self):
        with self.assertRaisesRegex(wm.WmError, 'our WM did not start'):
            self.wm().wait_running()

    def test_heartbeat_still(self):
        self.dev.boot_wm()
        self.dev.wm_up = False                        # the status block written, the loop not running
        with self.assertRaisesRegex(wm.WmError, 'does not run its loop'):
            self.wm().wait_running()

    def test_reads_mark_faults(self):
        w = self.running()
        addrs = [0x83000000 + 4 * i for i in range(300)]
        for a in addrs:
            self.dev.mem[a] = a & 0xFFFF
        self.dev.faulting = {addrs[5], addrs[200]}
        values = w.read(addrs)
        self.assertIsNone(values[5])
        self.assertIsNone(values[200])
        self.assertEqual([v for k, v in enumerate(values) if k not in (5, 200)],
                         [a & 0xFFFF for k, a in enumerate(addrs) if k not in (5, 200)])

    def test_rf_reads_use_the_ctl_encoding(self):
        w = self.running()
        self.dev.adie[2 << 16 | 4 << 12 | 0xA4B] = 0x1234
        self.assertEqual(w.read_rf([(0x24, 0xA4B)]), [0x1234])

    def test_writes_and_route_grants(self):
        w = self.running()
        w.write([(0x83000010, 5), (0x88009000, 0), (0x88009004, 0xFF000000)])
        self.assertEqual(self.dev.mem[0x83000010], 5)
        with self.assertRaises(ValueError):
            w.write([(0x88009008, 0x10)])

    def test_call(self):
        w = self.running()
        self.assertEqual(w.call(0x00930000, 0x100, 0, 0x1C0, 1), 0x2C1)
        self.assertEqual(self.dev.calls, [(0x00930000, [0x100, 0, 0x1C0, 1])])
        with self.assertRaises(ValueError):
            w.call(0x1000, 1, 2, 3, 4, 5)

    def test_locked_our_wm_calls_only_into_its_own_image(self):
        """It starts locked. The ROM is callable code too, an eFuse writer among it."""
        w = self.running()
        self.assertTrue(w.locked())
        with self.assertRaisesRegex(wm.WmError, 'call to 0x00822b92 was refused: our WM is locked.*"raw_access": true'):
            w.call(0x00822B92, 0x100, 0, 0x1C0, 1)
        self.assertEqual(self.dev.calls, [])
        w.unlock()
        self.assertFalse(w.locked())
        self.assertEqual(w.call(0x00822B92, 0x100, 0, 0x1C0, 1), 0x2C1)
        self.assertEqual(self.dev.calls, [(0x00822B92, [0x100, 0, 0x1C0, 1])])

    def test_locked_a_raw_program_does_not_reach_what_the_guard_keeps(self):
        w = self.running()
        for what, ops in (('the eFuse control word', [rf.w32(0x70021008, 0x400000C0)]),
                          ('semaphore 12, which a read takes', [rf.r32(0x7C070030)]),
                          ("a band's transmit disable", [rf.r32(0x0090F300), rf.rmw32(0x820F3000, 0x100, 0)]),
                          ('a read that wedges a die', [rf.ar(rf.reg(1, 15, 0x222))])):
            with self.subTest(what):
                with self.assertRaisesRegex(wm.WmError, r'op \d: refused by the guard.*"raw_access": true'):
                    w.run(ops)
        self.assertNotIn(0x70021008, self.dev.mem)
        self.assertEqual(w.run([rf.r32(0x820F3000)]), [0], 'what is not guarded runs as before')
        w.unlock()
        w.run([rf.w32(0x70021008, 0), rf.r32(0x7C070030), rf.ar(rf.reg(1, 15, 0x222))])
        self.assertEqual(self.dev.mem[0x70021008], 0)

    def test_a_load_locks_it_again(self):
        w = self.running()
        w.unlock()
        self.dev.reset()
        self.dev.boot_wm()
        self.assertTrue(wm.Wm(self.unit).locked())

    def test_start_wa(self):
        w = self.running()
        self.assertEqual(w.start_wa()['magic'], wm.WA_MAGIC)
        self.assertEqual(w.status()['wa_start'], wm.WA_STARTED)
        self.dev.dropped.add(wm.WA_START)
        self.assertEqual(w.start_wa()['magic'], wm.WA_MAGIC, 'started already: no new request')

    def test_wa_does_not_start(self):
        w = self.running()
        self.dev.dropped.add(wm.WA_START)
        with self.assertRaisesRegex(wm.WmError, 'our WA did not start'):
            w.start_wa()


class RadioCommands(unittest.TestCase):
    def setUp(self):
        self.unit = host_fake.FakeUnit()
        self.dev = self.unit.device
        self.clock = FakeClock()
        self.dev.boot_wm()
        self.w = wm.Wm(self.unit, sleep=self.clock.sleep, clock=self.clock)

    def test_command_and_outcome(self):
        out = self.w.radio('bringup')
        self.assertEqual(out, {'ee': EE_BUFFER, 'res': 0, 'fail_code': 0, 'fail_arg': 0, 'fails': 0, 'us': 1000,
                               'state': BROUGHT_UP})
        out = self.w.radio('setup', 1, 5_250_000_000 & 0xFFFFFFFF, 1, 3)
        self.assertEqual(self.dev.radio_commands[-1], ('setup', (1, 5_250_000_000 & 0xFFFFFFFF, 1, 3)))
        self.assertEqual(out['res'], 0)

    def test_result_and_failure_record(self):
        self.assertEqual(self.w.radio('capture', 0, 0)['res'], RADIO_ERR_ORDER)
        self.dev.radio_failures['bringup'] = (4, 3, 0x2F02C)
        out = self.w.radio('bringup')
        self.assertEqual((out['res'], out['fail_code'], out['fail_arg'], out['fails']), (4, 3, 0x2F02C, 1))

    def test_development_steps(self):
        step = wm.RADIO_STEPS
        self.assertEqual(self.w.radio('step', step['common'])['res'], RADIO_ERR_ORDER)
        self.assertEqual(self.w.radio('step', step['patch_init'])['res'], 0)
        self.assertEqual(self.w.radio('step', step['common'])['res'], 0)
        self.assertEqual(self.w.radio('step', step['band'], 3)['res'], RADIO_ERR_ARG)
        self.assertEqual(self.dev.radio_commands[-1], ('step', (step['band'], 3)))
        self.assertEqual(self.w.radio('selftest')['res'], wm.RADIO_SELFTEST_MAGIC)

    def test_one_at_a_time(self):
        """A STREAM is acknowledged when the stream ends; nothing else is posted meanwhile."""
        req = self.w.radio_post('stream', wm.RADIO_PACKING['raw'])
        with self.assertRaisesRegex(wm.WmError, 'still running'):
            self.w.radio_post('select', 1)
        with self.assertRaisesRegex(wm.WmError, 'radio stream did not finish in 2 s'):
            self.w.radio_wait(req, 'stream', timeout=2)
        self.dev.pump_start(['mt7996-stream'])
        self.unit.end_stream(7)
        self.assertEqual(self.w.radio_wait(req, 'stream')['res'], 7)

    def test_trap_while_waiting(self):
        req = self.w.radio_post('stream', wm.RADIO_PACKING['half'])
        self.dev.set_st('state', wm.STATE_TRAPPED)
        with self.assertRaisesRegex(wm.WmError, 'trapped during radio stream'):
            self.w.radio_wait(req, 'stream')

    def test_request_numbers_skip_zero(self):
        self.dev.set_st('radio_req', 0xFFFFFFFF)
        self.dev.set_st('radio_ack', 0xFFFFFFFF)
        self.assertEqual(self.w.radio_post('selftest'), 1)
        self.assertEqual(self.w.radio_wait(1)['res'], wm.RADIO_SELFTEST_MAGIC)

    def test_eeprom(self):
        image = bytes(range(256)) * 30
        self.w.put_eeprom(image)
        self.assertEqual(self.dev.eeprom(), image)
        self.w.put_eeprom(b'\x01\x02\x03')
        self.assertEqual(self.dev.eeprom(), b'\x01\x02\x03' + bytes(7677))
        self.w.put_eeprom(image + b'\x55' * 64)
        self.assertEqual(self.dev.eeprom(), image)

    def test_eeprom_checked(self):
        self.dev.dropped.add(EE_BUFFER + 4 * 1000)
        with self.assertRaisesRegex(wm.WmError, 'the EEPROM image did not land'):
            self.w.put_eeprom(bytes(range(256)) * 30)


if __name__ == '__main__':
    unittest.main()
