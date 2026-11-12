# BME690 I2C recovery regression tests
#
# Copyright (C) 2026 Donald A. Cupp Jr. <doncuppjr@yahoo.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "klippy"))
from extras import bme690, bus


class FakeMCU:
    class Error(Exception):
        pass

    def __init__(self):
        self.shutdowns = []
        self.estimated = []

    def get_name(self):
        return "sensor"

    def get_printer(self):
        return self

    def invoke_shutdown(self, message):
        self.shutdowns.append(message)

    def estimated_print_time(self, when):
        self.estimated.append(when)
        return when

    def is_fileoutput(self):
        return False


class FakeReactor:
    NEVER = -1

    def __init__(self):
        self.time = 1.0

    def monotonic(self):
        return self.time

    def pause(self, until):
        self.time = until


class FakeBus:
    def __init__(self, fail=False):
        self.fail = fail
        self.requests = []

    def i2c_transfer(self, data, read_len=0, retry=True,
                     shutdown_on_error=True):
        self.requests.append((data, read_len, retry, shutdown_on_error))
        if self.fail:
            raise RuntimeError("NACK")
        if read_len:
            return {"response": bytes([0x80] + [0] * 16)}
        return {"response": b""}


def sample_sensor():
    sensor = bme690.BME690.__new__(bme690.BME690)
    sensor.reactor = FakeReactor()
    sensor.printer = FakeMCU()
    sensor.mcu = sensor.printer
    sensor.i2c = FakeBus()
    sensor.name = "BME690"
    sensor.start_time = sensor.reactor.time
    sensor.last_sample_time = None
    sensor.last_gas_time = 0.
    sensor.consecutive_errors = 0
    sensor.total_errors = 0
    sensor.max_sample_age = 5.
    sensor.gas_enabled = False
    sensor.gas_target = 320
    sensor.gas_duration = 150
    sensor.gas_interval = 3.
    sensor.os_temp = 2
    sensor.os_pres = 2
    sensor.os_hum = 2
    sensor.iir_filter = 1
    sensor.temp = 25.
    sensor.humidity = 10.
    sensor.pressure = 1000.
    sensor.gas = 0.
    sensor.gas_valid = False
    sensor.min_temp = -20.
    sensor.max_temp = 85.
    sensor.dig = {}
    sensor.measurements = []
    sensor._callback = lambda t, v: sensor.measurements.append((t, v))
    return sensor


class TestRecovery(unittest.TestCase):
    def test_one_bad_sample_then_recovery(self):
        s = sample_sensor()
        s.i2c.fail = True
        s._sample(1.)
        self.assertEqual(1, s.consecutive_errors)
        self.assertEqual(1, s.total_errors)
        self.assertEqual([], s.printer.shutdowns)
        self.assertEqual(25., s.temp)
        self.assertFalse(s.measurements)
        s.i2c.fail = False
        with patch.object(bme690, "compensate_temperature",
                          return_value=40.), \
                patch.object(bme690, "compensate_pressure",
                             return_value=100000.), \
                patch.object(bme690, "compensate_humidity",
                             return_value=20.):
            s._sample(s.reactor.time)
        self.assertEqual(0, s.consecutive_errors)
        self.assertEqual(1, s.total_errors)
        self.assertEqual(40., s.temp)
        self.assertEqual(1, len(s.measurements))
        self.assertEqual([], s.printer.shutdowns)
        self.assertTrue(all(req[2:] == (False, False)
                            for req in s.i2c.requests))

    def test_five_consecutive_errors_shutdown(self):
        s = sample_sensor()
        s.i2c.fail = True
        for _ in range(4):
            s._sample(s.reactor.time)
            s.reactor.time += .8
        self.assertEqual([], s.printer.shutdowns)
        s._sample(s.reactor.time)
        self.assertEqual(5, s.total_errors)
        self.assertEqual(1, len(s.printer.shutdowns))
        self.assertIn("temperature data unavailable", s.printer.shutdowns[0])

    def test_stale_temperature_shutdown(self):
        s = sample_sensor()
        s.last_sample_time = 1.
        s.reactor.time = 6.1
        s.i2c.fail = True
        s._sample(s.reactor.time)
        self.assertIn("temperature data unavailable",
                      s.printer.shutdowns[0])

    def test_out_of_range_temperature_immediate_shutdown(self):
        s = sample_sensor()
        with patch.object(bme690, "compensate_temperature",
                          return_value=90.), \
                patch.object(bme690, "compensate_pressure",
                             return_value=100000.), \
                patch.object(bme690, "compensate_humidity",
                             return_value=20.):
            result = s._sample(1.)
        self.assertEqual(s.reactor.NEVER, result)
        self.assertEqual(1, len(s.printer.shutdowns))
        self.assertIn("outside", s.printer.shutdowns[0])
        self.assertFalse(s.measurements)

    def test_no_fresh_data_counts_towards_failure_limit(self):
        s = sample_sensor()
        s.i2c.fail = False
        class NotReady:
            def i2c_transfer(self, data, read_len=0, **kwargs):
                if read_len:
                    return {"response": bytes([0] * 17)}
                return {"response": b""}
        s.i2c = NotReady()
        s._sample(1.)
        self.assertEqual(1, s.consecutive_errors)
        self.assertFalse(s.measurements)
        self.assertFalse(s.printer.shutdowns)

    def test_first_valid_sample_timeout_shutdown(self):
        s = sample_sensor()
        s.reactor.time = 7.
        s.i2c.fail = True
        s._sample(s.reactor.time)
        self.assertIn("temperature data unavailable",
                      s.printer.shutdowns[0])

    def test_status_contains_recovery_diagnostics(self):
        s = sample_sensor()
        s.last_sample_time = 1.
        s.total_errors = 2
        s.consecutive_errors = 1
        status = s.get_status(2.)
        self.assertEqual(2, status['sample_errors'])
        self.assertEqual(1, status['consecutive_errors'])
        self.assertEqual(1., status['sample_age'])

    def test_gas_timestamp_only_updates_after_success(self):
        s = sample_sensor()
        s.gas_enabled = True
        s.i2c.fail = True
        s._sample(1.)
        self.assertEqual(0., s.last_gas_time)


class TestBusOptIn(unittest.TestCase):
    def test_nonfatal_error_raises_to_driver(self):
        mcu = FakeMCU()
        i2c = bus.MCU_I2C.__new__(bus.MCU_I2C)
        i2c.mcu = mcu
        i2c.i2c_address = 0x77
        i2c.oid = 0
        i2c.async_write_only = False
        i2c.cmd_queue = None

        class Query:
            def send(self, args, **kwargs):
                return {"i2c_bus_status": "NACK"}

        i2c.i2c_transfer_cmd = Query()
        with self.assertRaises(bus.mcu.error):
            i2c.i2c_transfer([0x1D], 17, retry=False,
                             shutdown_on_error=False)
        self.assertFalse(mcu.shutdowns)
        i2c.i2c_transfer([0x1D], 17, retry=False)
        self.assertEqual(1, len(mcu.shutdowns))

    def test_normal_i2c_status(self):
        mcu = FakeMCU()
        i2c = bus.MCU_I2C.__new__(bus.MCU_I2C)
        i2c.mcu = mcu
        i2c.i2c_address = 0x77
        i2c.oid = 0
        i2c.async_write_only = False
        i2c.cmd_queue = None

        class Query:
            def send(self, args, **kwargs):
                return {"i2c_bus_status": "SUCCESS", "response": b"hi"}

        i2c.i2c_transfer_cmd = Query()
        result = i2c.i2c_transfer([0x1D], 2, shutdown_on_error=False)
        self.assertEqual(b"hi", result["response"])
        self.assertFalse(mcu.shutdowns)


if __name__ == "__main__":
    unittest.main()
