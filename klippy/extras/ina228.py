# INA228 I2C power and energy monitor
#
# Copyright (C) 2026 Donald A. Cupp Jr. <doncuppjr@yahoo.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
from . import bus

REG_CONFIG = 0x00
REG_ADC_CONFIG = 0x01
REG_SHUNT_CAL = 0x02
REG_SHUNT_VOLTAGE = 0x04
REG_BUS_VOLTAGE = 0x05
REG_DIE_TEMPERATURE = 0x06
REG_CURRENT = 0x07
REG_POWER = 0x08
REG_ENERGY = 0x09
REG_CHARGE = 0x0a
REG_MANUFACTURER_ID = 0x3e
REG_DEVICE_ID = 0x3f

CONVERSION_TIMES = (50, 84, 150, 280, 540, 1052, 2074, 4120)
AVERAGING_COUNTS = (1, 4, 16, 64, 128, 256, 512, 1024)
TI_ID = 0x5449
INA228_ID = 0x228


def _unsigned(data):
    val = 0
    for byte in data:
        val = (val << 8) | byte
    return val


def _signed(data, bits):
    value = _unsigned(data)
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def decode_measurements(raw, current_lsb, adc_range):
    # 20-bit shunt, bus, and current values are left-justified in 24 bits.
    shunt = _signed(raw['shunt'], 24) >> 4
    voltage = _unsigned(raw['bus']) >> 4
    current = _signed(raw['current'], 24) >> 4
    power = _unsigned(raw['power'])
    energy = _unsigned(raw['energy'])
    charge = _signed(raw['charge'], 40)
    die_temp = _signed(raw['temperature'], 16)
    return {
        'voltage': voltage * 195.3125e-6,
        'shunt_voltage': shunt * (78.125e-9 if adc_range else 312.5e-9),
        'current': current * current_lsb,
        'power': power * 3.2 * current_lsb,
        'energy': energy * 51.2 * current_lsb,
        'charge': charge * current_lsb,
        'die_temperature': die_temp * 7.8125e-3,
    }


def get_shunt_calibration(resistance, max_current, adc_range):
    if resistance <= 0 or max_current <= 0:
        raise ValueError("INA228 resistance and current must be positive")
    current_lsb = max_current / (1 << 19)
    register = int(13107.2e6 * current_lsb * resistance
                   * (4 if adc_range else 1))
    if not 1 <= register <= 65535:
        raise ValueError("INA228 shunt calibration register out of range")
    return register, current_lsb


class INA228:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split(None, 1)[1]
        self.reactor = self.printer.get_reactor()
        self.i2c = bus.MCU_I2C_from_config(
            config, default_addr=0x40, default_speed=100000)
        self.shunt_resistance = config.getfloat(
            'shunt_resistance', above=0.)
        self.max_current = config.getfloat('max_current', above=0.)
        self.adc_range = config.getint('adc_range', 0, minval=0, maxval=1)
        self.sample_interval = config.getfloat(
            'sample_interval', 1., minval=0.1)
        self.average = config.getchoice(
            'averaging', {str(value): index for index, value in
                          enumerate(AVERAGING_COUNTS)}, default='16')
        self.conversion_time = config.getchoice(
            'conversion_time', {str(value): index for index, value in
                                enumerate(CONVERSION_TIMES)}, default='1052')
        shunt_limit = 0.04096 if self.adc_range else 0.16384
        if self.max_current * self.shunt_resistance > shunt_limit:
            raise config.error(
                "INA228: max_current * shunt_resistance exceeds "
                "configured shunt ADC range")
        self.calibration, self.current_lsb = get_shunt_calibration(
            self.shunt_resistance, self.max_current, self.adc_range)
        self.status = {
            'voltage': 0., 'shunt_voltage': 0., 'current': 0.,
            'power': 0., 'energy': 0., 'charge': 0.,
            'die_temperature': 0., 'sample_errors': 0,
            'sample_age': None, 'available': False,
        }
        self.last_sample_time = None
        self.timer = self.reactor.register_timer(self._sample)
        self.printer.register_event_handler(
            "klippy:connect", self._handle_connect)

    def _read(self, reg, nbytes, recoverable=False):
        if recoverable:
            response = self.i2c.i2c_transfer(
                [reg], nbytes, retry=False, shutdown_on_error=False)
        else:
            response = self.i2c.i2c_read([reg], nbytes)
        return bytes(response['response'])

    def _write16(self, reg, value):
        data = [reg, (value >> 8) & 0xff, value & 0xff]
        self.i2c.i2c_write(data)

    def _handle_connect(self):
        manufacturer = _unsigned(self._read(REG_MANUFACTURER_ID, 2))
        device = _unsigned(self._read(REG_DEVICE_ID, 2)) >> 4
        if manufacturer != TI_ID or device != INA228_ID:
            raise self.printer.config_error(
                "INA228 %s: invalid device identity at 0x%02x"
                % (self.name, self.i2c.get_i2c_address()))
        self._write16(REG_CONFIG, self.adc_range << 4)
        adc_config = (0xf << 12) | (self.conversion_time << 9) | (
            self.conversion_time << 6) | (
                self.conversion_time << 3) | self.average
        self._write16(REG_ADC_CONFIG, adc_config)
        self._write16(REG_SHUNT_CAL, self.calibration)
        logging.info("INA228 %s: Rshunt=%g ohm, max_current=%g A, "
                     "calibration=%d", self.name, self.shunt_resistance,
                     self.max_current, self.calibration)
        self.reactor.update_timer(self.timer, self.reactor.NOW)

    def _sample(self, eventtime):
        try:
            fields = {
                'shunt': (REG_SHUNT_VOLTAGE, 3),
                'bus': (REG_BUS_VOLTAGE, 3),
                'temperature': (REG_DIE_TEMPERATURE, 2),
                'current': (REG_CURRENT, 3),
                'power': (REG_POWER, 3),
                'energy': (REG_ENERGY, 5),
                'charge': (REG_CHARGE, 5),
            }
            raw = {name: self._read(reg, size, recoverable=True)
                   for name, (reg, size) in fields.items()}
            values = decode_measurements(
                raw, self.current_lsb, self.adc_range)
        except Exception as exc:
            self.status['sample_errors'] += 1
            self.status['available'] = False
            logging.warning("INA228 %s: sample failed: %s", self.name, exc)
            return self.reactor.monotonic() + self.sample_interval
        self.status.update(values)
        self.status['available'] = True
        self.last_sample_time = self.reactor.monotonic()
        return self.last_sample_time + self.sample_interval

    def get_status(self, eventtime):
        status = dict(self.status)
        status['sample_age'] = (
            None if self.last_sample_time is None
            else max(0., eventtime - self.last_sample_time))
        return status


def load_config_prefix(config):
    return INA228(config)
