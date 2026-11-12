# INA228 driver conversion and telemetry regression tests
#
# Copyright (C) 2026 Donald A. Cupp Jr. <doncuppjr@yahoo.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'klippy'))
from extras import ina228


def raw24(value):
    return ((value & 0xfffff) << 4).to_bytes(3, 'big')


class FakeReactor:
    NOW = 0.

    def __init__(self):
        self.time = 20.

    def monotonic(self):
        return self.time


class FakeI2C:
    def __init__(self, fail=False):
        self.fail = fail
        self.writes = []
        self.reads = []
        self.values = {
            0x04: raw24(6400),
            0x05: raw24(128000),
            0x06: (6400).to_bytes(2, 'big', signed=True),
            0x07: raw24(52428),
            0x08: (32000).to_bytes(3, 'big'),
            0x09: (50000).to_bytes(5, 'big'),
            0x0a: (30000).to_bytes(5, 'big'),
            0x3e: (0x5449).to_bytes(2, 'big'),
            0x3f: (0x2280).to_bytes(2, 'big'),
        }

    def i2c_transfer(self, args, length=0, **kwargs):
        self.reads.append((args, length, kwargs))
        if self.fail:
            raise RuntimeError('NACK')
        return {'response': self.values[args[0]]}

    def i2c_read(self, args, size):
        return {'response': self.values[args[0]]}

    def i2c_write(self, data):
        self.writes.append(data)

    def get_i2c_address(self):
        return 0x40


class FakePrinter:
    def __init__(self):
        self.failures = []

    def config_error(self, message):
        return ValueError(message)


def sensor():
    s = ina228.INA228.__new__(ina228.INA228)
    s.name = '24V'
    s.reactor = FakeReactor()
    s.i2c = FakeI2C()
    s.printer = FakePrinter()
    s.adc_range = 0
    s.shunt_resistance = 0.002
    s.max_current = 60.
    s.calibration, s.current_lsb = ina228.get_shunt_calibration(
        0.002, 60., 0)
    s.average = 2
    s.conversion_time = 5
    s.sample_interval = 1.
    s.timer = None
    s.last_sample_time = None
    s.status = {'sample_errors': 0, 'available': False}
    return s


class TestINA228(unittest.TestCase):
    def test_2_milliohm_shunt_calibration(self):
        cal, lsb = ina228.get_shunt_calibration(0.002, 60., 0)
        self.assertEqual(3000, cal)
        self.assertAlmostEqual(60. / (1 << 19), lsb)
        cal_small, _ = ina228.get_shunt_calibration(
            0.002, 20., 1)
        self.assertEqual(4000, cal_small)

    def test_invalid_calibration(self):
        for resistance, maximum in ((0., 10.), (0.002, 0.),
                                    (100., 500.)):
            with self.assertRaises(ValueError):
                ina228.get_shunt_calibration(resistance, maximum, 0)

    def test_decodes_signed_20bit_current(self):
        values = {
            'shunt': raw24(-6400),
            'bus': raw24(25600),
            'temperature': (-128).to_bytes(2, 'big', signed=True),
            'current': raw24(-52428),
            'power': (500).to_bytes(3, 'big'),
            'energy': (50000).to_bytes(5, 'big'),
            'charge': (-4096).to_bytes(5, 'big', signed=True),
        }
        converted = ina228.decode_measurements(values, 60. / (1 << 19), 0)
        self.assertAlmostEqual(-0.002, converted['shunt_voltage'])
        self.assertAlmostEqual(5., converted['voltage'])
        self.assertAlmostEqual(-6., converted['current'], delta=0.001)
        self.assertAlmostEqual(-1., converted['die_temperature'])
        self.assertLess(converted['charge'], 0)
        self.assertGreater(converted['energy'], 0)

    def test_shunt_adc_range(self):
        values = {
            'shunt': raw24(12800),
            'bus': raw24(5120),
            'temperature': bytes(2),
            'current': raw24(0),
            'power': bytes(3),
            'energy': bytes(5),
            'charge': bytes(5),
        }
        a = ina228.decode_measurements(values, 0.0001, 0)
        b = ina228.decode_measurements(values, 0.0001, 1)
        self.assertAlmostEqual(a['shunt_voltage'] / 4.,
                               b['shunt_voltage'])

    def test_successful_sample_status(self):
        s = sensor()
        s._sample(0.)
        status = s.get_status(21.)
        self.assertTrue(status['available'])
        self.assertAlmostEqual(25., status['voltage'])
        self.assertAlmostEqual(6., status['current'], delta=0.001)
        self.assertEqual(1., status['sample_age'])
        self.assertEqual(7, len(s.i2c.reads))
        self.assertTrue(all(not x[2]['shutdown_on_error']
                            for x in s.i2c.reads))

    def test_sample_nack_does_not_shutdown_printer(self):
        s = sensor()
        s.i2c.fail = True
        s._sample(0.)
        status = s.get_status(21.)
        self.assertFalse(status['available'])
        self.assertEqual(1, status['sample_errors'])
        self.assertEqual(None, status['sample_age'])
        s.i2c.fail = False
        s._sample(0.)
        self.assertTrue(s.get_status(21.)['available'])
        self.assertEqual(1, s.status['sample_errors'])

    def test_device_identity_check(self):
        s = sensor()
        s.i2c.values[0x3f] = (0x2260).to_bytes(2, 'big')
        with self.assertRaises(ValueError):
            s._handle_connect()

    def test_configuration_writes(self):
        s = sensor()
        s.reactor.update_timer = lambda *args: None
        s._handle_connect()
        self.assertEqual([0x00, 0, 0], s.i2c.writes[0])
        self.assertEqual([0x02, 0x0b, 0xb8], s.i2c.writes[2])
        self.assertEqual(3, len(s.i2c.writes))


if __name__ == '__main__':
    unittest.main()
