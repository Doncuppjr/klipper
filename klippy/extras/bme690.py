# BME690 environmental sensor support
#
# Copyright (C) 2026 Donald A. Cupp Jr. <doncuppjr@yahoo.com>
#
# Compensation algorithms adapted from Bosch Sensortec BME690 SensorAPI,
# licensed under BSD-3-Clause.
# https://github.com/boschsensortec/BME690_SensorAPI
#
# This file may be distributed under the terms of the GNU GPLv3 license.
# BME690 uses different temperature, pressure, and humidity calibration and
# 24-bit ADC conversion formulas from Klipper's existing BME280/BME680 driver.
import logging
from . import bus

REPORT_TIME = 0.8
CHIP_ID = 0x61
VARIANT_ID = 0x02
REG_CHIP_ID = 0xD0
REG_VARIANT_ID = 0xF0
REG_FIELD0 = 0x1D


def _signed(value, bits):
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def _u16(buf, offset):
    return buf[offset] | (buf[offset + 1] << 8)


def _s16(buf, offset):
    return _signed(_u16(buf, offset), 16)


def parse_calibration(c):
    if len(c) != 42:
        raise ValueError("BME690 calibration block must be 42 bytes")
    d = {
        't1': _u16(c, 31), 't2': _u16(c, 0),
        't3': _signed(c[2], 8),
        'p1': _s16(c, 10), 'p2': _u16(c, 12),
        'p3': _signed(c[14], 8), 'p4': _signed(c[15], 8),
        'p5': _s16(c, 4), 'p6': _s16(c, 6),
        'p7': _signed(c[8], 8), 'p8': _signed(c[9], 8),
        'p9': _s16(c, 18), 'p10': _signed(c[20], 8),
        'p11': _signed(c[21], 8),
        'h1': _signed((c[25] << 4) | (c[24] & 0x0F), 12),
        'h2': _signed(c[26], 8), 'h3': c[28],
        'h4': _signed(c[27], 8),
        'h5': _signed((c[23] << 4) | (c[24] >> 4), 12),
        'h6': c[29],
        'g1': _signed(c[35], 8), 'g2': _s16(c, 33),
        'g3': _signed(c[36], 8),
        'res_heat_val': _signed(c[37], 8),
        'res_heat_range': (c[39] & 0x30) >> 4,
    }
    return d


def compensate_temperature(raw, d):
    # Bosch BME69x floating-point compensation; raw is *24-bit*.
    cf = float(raw) - d['t1'] * 256.0
    return (cf * d['t2'] / (1 << 30)
            + cf * cf * d['t3'] / (1 << 48))


def compensate_pressure(raw, temperature, d):
    # Bosch BME69x algorithm, output Pa (not hPa).
    t = float(temperature)
    o = d['p1'] * 8.0
    tk10 = d['p2'] / 64.0
    tk20 = d['p3'] / 256.0
    tk30 = d['p4'] / 32768.0
    s = (d['p5'] - 16384.0) / (1 << 20)
    tk1s = (d['p6'] - 16384.0) / (1 << 29)
    tk2s = d['p7'] / float(1 << 32)
    tk3s = d['p8'] / float(1 << 37)
    nls = d['p9'] / float(1 << 48)
    tknls = d['p10'] / float(1 << 48)
    nls3 = d['p11'] / float(1 << 65)
    p = float(raw)
    return (o + tk10*t + tk20*t*t + tk30*t*t*t
            + p*(s + tk1s*t + tk2s*t*t + tk3s*t*t*t)
            + p*p*(nls + tknls*t) + p*p*p*nls3)


def compensate_humidity(raw, temperature, d):
    # Bosch BME69x floating-point compensation, output %RH.
    tc = float(temperature) * 5120.0 - 76800.0
    oh = d['h1'] * 64.0
    sh = d['h5'] / 65536.0
    tk10h = d['h2'] / 16384.0
    tk1sh = d['h4'] / float(1 << 26)
    tk2sh = d['h3'] / float(1 << 26)
    hlin2 = d['h6'] / float(1 << 19)
    hoff = float(raw) - (oh + tk10h * tc)
    hsens = hoff * sh * (1.0 + tk1sh*tc + tk1sh*tk2sh*tc*tc)
    return max(0.0, min(100.0, hsens * (1.0 - hlin2 * hsens)))


def compensate_gas(raw, gas_range):
    # BME690 high-gas variant gas-resistance formula, output ohms.
    v1 = 262144 >> gas_range
    v2 = 4096 + 3 * (raw - 512)
    return 1000000.0 * v1 / v2


def calc_res_heat(target, ambient, d):
    target = min(400, target)
    v1 = d['g1'] / 16.0 + 49.0
    v2 = (d['g2'] / 32768.0) * 0.0005 + 0.00235
    v3 = d['g3'] / 1024.0
    v5 = v1 * (1.0 + v2 * target) + v3 * ambient
    r = 3.4 * (v5 * (4.0 / (4.0 + d['res_heat_range']))
               / (1.0 + d['res_heat_val'] * 0.002) - 25.0)
    return max(0, min(255, int(r)))


def calc_gas_wait(duration_ms):
    if duration_ms >= 4032:
        return 0xFF
    factor = 0
    while duration_ms > 0x3F:
        duration_ms //= 4
        factor += 1
    return duration_ms + factor * 64


class BME690:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.reactor = self.printer.get_reactor()
        self.i2c = bus.MCU_I2C_from_config(
            config, default_addr=0x77, default_speed=100000)
        self.mcu = self.i2c.get_mcu()
        self.os_temp = config.getint('bme690_oversample_temp', 2,
                                     minval=1, maxval=5)
        self.os_pres = config.getint('bme690_oversample_pressure', 2,
                                     minval=1, maxval=5)
        self.os_hum = config.getint('bme690_oversample_hum', 2,
                                    minval=1, maxval=5)
        self.iir_filter = config.getint('bme690_iir_filter', 1,
                                        minval=0, maxval=7)
        self.gas_enabled = config.getboolean('bme690_gas_enable', False)
        self.gas_target = config.getint('bme690_gas_target_temp', 320,
                                        minval=200, maxval=400)
        self.gas_duration = config.getint('bme690_gas_heat_duration', 150,
                                          minval=1, maxval=4032)
        self.gas_interval = config.getfloat('bme690_gas_interval', 3.,
                                            minval=1.)
        self.temp = 25.0
        self.pressure = self.humidity = 0.0
        self.gas = 0.0
        self.gas_valid = False
        self.last_gas_time = 0.0
        self.last_sample_time = None
        self.consecutive_errors = 0
        self.total_errors = 0
        self.max_sample_age = config.getfloat(
            'bme690_max_sample_age', 5., above=REPORT_TIME)
        self.min_temp = -40.0
        self.max_temp = 85.0
        self.dig = None
        self.sample_timer = self.reactor.register_timer(self._sample)
        self.printer.add_object("bme690 " + self.name, self)
        if self.printer.get_start_args().get('debugoutput') is None:
            self.printer.register_event_handler("klippy:connect",
                                                self._handle_connect)

    def setup_minmax(self, min_temp, max_temp):
        self.min_temp, self.max_temp = min_temp, max_temp

    def setup_callback(self, cb):
        self._callback = cb

    def get_report_time_delta(self):
        return REPORT_TIME

    def _read(self, reg, count, recoverable=False):
        if recoverable:
            r = self.i2c.i2c_transfer([reg], count, retry=False,
                                      shutdown_on_error=False)
        else:
            r = self.i2c.i2c_read([reg], count)
        return bytearray(r['response'])

    def _write(self, reg, value, recoverable=False):
        if isinstance(value, int):
            value = [value]
        data = [reg] + list(value)
        if recoverable:
            self.i2c.i2c_transfer(data, retry=False,
                                  shutdown_on_error=False)
        else:
            self.i2c.i2c_write(data)

    def _handle_connect(self):
        self._write(0xE0, 0xB6)
        self.reactor.pause(self.reactor.monotonic() + .02)
        chip = self._read(REG_CHIP_ID, 1)[0]
        variant = self._read(REG_VARIANT_ID, 1)[0]
        if chip != CHIP_ID or variant != VARIANT_ID:
            raise self.printer.config_error(
                "BME690: expected chip 0x61/variant 0x02 at 0x%02x, got "
                "chip 0x%02x/variant 0x%02x" %
                (self.i2c.i2c_address, chip, variant))
        c = (self._read(0x8A, 23) + self._read(0xE1, 14)
             + self._read(0x00, 5))
        self.dig = parse_calibration(c)
        self._write(0x75, self.iir_filter << 2)
        self._write(0x72, self.os_hum)
        self._write(0x70, 0x00)  # heater enabled when gas requested
        self._write(0x71, 0x00)  # gas off outside measurement window
        if self.gas_enabled:
            self._write(0x64, calc_gas_wait(self.gas_duration))
            self._write(0x5A, calc_res_heat(self.gas_target,
                                             self.temp, self.dig))
        logging.info("BME690 %s: identified variant 0x02 on I2C 0x%02x",
                     self.name, self.i2c.i2c_address)
        self.start_time = self.reactor.monotonic()
        self.reactor.update_timer(self.sample_timer, self.reactor.NOW)

    def _sample_failed(self, now, error):
        self.consecutive_errors += 1
        self.total_errors += 1
        logging.warning("BME690 %s: sample failed (%d consecutive): %s",
                        self.name, self.consecutive_errors, error)
        last_good = (self.last_sample_time if self.last_sample_time is not None
                     else self.start_time)
        if (self.consecutive_errors >= 5 or
                now - last_good > self.max_sample_age):
            self.printer.invoke_shutdown(
                "BME690 %s: temperature data unavailable: %s" %
                (self.name, error))

    def _sample(self, eventtime):
        now = self.reactor.monotonic()
        run_gas = (self.gas_enabled
                   and now - self.last_gas_time >= self.gas_interval)
        try:
            if run_gas:
                self._write(0x5A, calc_res_heat(self.gas_target,
                                                 self.temp, self.dig),
                            recoverable=True)
            self._write(0x71, 0x20 if run_gas else 0x00,
                        recoverable=True)
            self._write(0x74, (self.os_temp << 5) | (self.os_pres << 2) | 1,
                        recoverable=True)
            cycles = sum((0, 1, 2, 4, 8, 16)[i] for i in (
                self.os_temp, self.os_pres, self.os_hum))
            meas_s = (cycles * 1963 + 477 * 9 + 1000) / 1000000.0
            delay = meas_s + (self.gas_duration / 1000. if run_gas else 0)
            self.reactor.pause(self.reactor.monotonic() + delay + 0.01)
            for _ in range(4):
                f = self._read(REG_FIELD0, 17, recoverable=True)
                if f[0] & 0x80:
                    break
                self.reactor.pause(self.reactor.monotonic() + .015)
            else:
                raise RuntimeError("no fresh measurement available")
            pres_raw = (f[2] << 16) | (f[3] << 8) | f[4]
            temp_raw = (f[5] << 16) | (f[6] << 8) | f[7]
            hum_raw = (f[8] << 8) | f[9]
            temp = compensate_temperature(temp_raw, self.dig)
            pressure = compensate_pressure(
                pres_raw, temp, self.dig) / 100.0
            humidity = compensate_humidity(
                hum_raw, temp, self.dig)
            gas, gas_valid = self.gas, self.gas_valid
            if run_gas:
                if f[16] & 0x20:
                    raw = (f[15] << 2) | (f[16] >> 6)
                    gas = compensate_gas(raw, f[16] & 0x0F)
                    gas_valid = bool(f[16] & 0x10)
                else:
                    logging.warning("BME690: gas measurement not valid")
            if temp < self.min_temp or temp > self.max_temp:
                self.printer.invoke_shutdown(
                    "BME690 temperature %.1f outside %.1f:%.1f" %
                    (temp, self.min_temp, self.max_temp))
                return self.reactor.NEVER
            self.temp, self.pressure, self.humidity = temp, pressure, humidity
            self.gas, self.gas_valid = gas, gas_valid
            measured = self.reactor.monotonic()
            self.last_sample_time = measured
            if run_gas:
                self.last_gas_time = measured
            self.consecutive_errors = 0
        except Exception as error:
            self._sample_failed(self.reactor.monotonic(), error)
            return self.reactor.monotonic() + REPORT_TIME
        self._callback(self.mcu.estimated_print_time(measured), temp)
        return self.reactor.monotonic() + REPORT_TIME

    def get_status(self, eventtime):
        return {
            'temperature': round(self.temp, 2),
            'humidity': round(self.humidity, 2),
            'pressure': round(self.pressure, 2),
            'gas': round(self.gas, 1),
            'gas_valid': self.gas_valid,
            'sample_errors': self.total_errors,
            'consecutive_errors': self.consecutive_errors,
            'sample_age': (None if self.last_sample_time is None else
                           max(0., eventtime - self.last_sample_time)),
        }


def load_config(config):
    heaters = config.get_printer().load_object(config, 'heaters')
    heaters.add_sensor_factory("BME690", BME690)
