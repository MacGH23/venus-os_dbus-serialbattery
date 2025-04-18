# -*- coding: utf-8 -*-

# Notes
# Added by https://github.com/baranator
# https://github.com/Louisvdw/dbus-serialbattery/pull/372
# Updated by https://github.com/mr-manuel

from battery import Battery, Cell
from typing import Callable
from utils import logger, AUTO_RESET_SOC, BLUETOOTH_FORCE_RESET_BLE_STACK, BLUETOOTH_USE_POLLING
from utils_ble import restart_ble_hardware_and_bluez_driver
from time import sleep, time
from bms.jkbms_brn import Jkbms_Brn
import os
import sys

# from bleak import BleakScanner, BleakError
# import asyncio


class Jkbms_Ble(Battery):
    BATTERYTYPE = "JKBMS BLE"
    resetting = False

    def __init__(self, port, baud, address):
        super(Jkbms_Ble, self).__init__(port, baud, address)
        self.address = address
        self.type = self.BATTERYTYPE
        self.jk = Jkbms_Brn(address, lambda: self.reset_bluetooth())
        self.unique_identifier_tmp = ""
        self.history.exclude_values_to_calculate = ["charge_cycles"]

        logger.info("Init of Jkbms_Ble at " + address)

    def connection_name(self) -> str:
        return "BLE " + self.address

    def custom_name(self) -> str:
        return "SerialBattery(" + self.type + ") " + self.address[-5:]

    def test_connection(self):
        """
        call a function that will connect to the battery, send a command and retrieve the result.
        The result or call should be unique to this BMS. Battery name or version, etc.
        Return True if success, False for failure
        """
        result = False
        try:
            if self.address and self.address != "":
                result = True

            if result:
                # start scraping
                self.jk.start_scraping()
                tries = 0

                # wait for self.jk.bt_client.is_connected to be True
                while not getattr(self.jk.bt_client, "is_connected", False) and getattr(self.jk, "run", True):
                    sleep(1)

                while self.jk.get_status() is None and tries < 10:
                    sleep(1)
                    tries += 1

                # load initial data, from here on get_status has valid values to be served to the dbus
                status = self.jk.get_status()

                if status is None:
                    if "device_info" not in self.jk.bms_status:
                        logger.info("   |- Device info MISSING")

                    if "cell_info" not in self.jk.bms_status:
                        logger.info("   |- Cell info MISSING")

                    if "settings" not in self.jk.bms_status:
                        logger.info("   |- Settings MISSING")

                    self.jk.stop_scraping()
                    result = False

                if result and not status["device_info"]["vendor_id"].startswith(("JK-", "JK_")):
                    self.jk.stop_scraping()
                    result = False

                # get first data
                result = result and self.get_settings()
                result = result and self.refresh_data()

        except Exception:
            (
                exception_type,
                exception_object,
                exception_traceback,
            ) = sys.exc_info()
            file = exception_traceback.tb_frame.f_code.co_filename
            line = exception_traceback.tb_lineno
            logger.error(f"Exception occurred: {repr(exception_object)} of type {exception_type} in {file} line #{line}")
            result = False

        return result

    def get_settings(self):
        # After successful connection get_settings() will be called to set up the battery
        # Set the current limits, populate cell count, etc
        # Return True if success, False for failure
        st = self.jk.get_status()["settings"]
        self.cell_count = st["cell_count"]
        self.max_battery_charge_current = st["max_charge_current"]
        self.max_battery_discharge_current = st["max_discharge_current"]

        # Persist initial OVP and OPVR settings of JK BMS BLE
        if self.jk.ovp_initial_voltage is None or self.jk.ovpr_initial_voltage is None:
            self.jk.ovp_initial_voltage = st["cell_ovp"]
            self.jk.ovpr_initial_voltage = st["cell_ovpr"]

        # "User Private Data" field in APP
        tmp = self.jk.get_status()["device_info"]["production"]
        self.custom_field = tmp if tmp != "Input Us" else None

        tmp = self.jk.get_status()["device_info"]["manufacturing_date"]
        self.production = "20" + tmp if tmp and tmp != "" else None

        self.unique_identifier_tmp = self.jk.get_status()["device_info"]["serial_number"]

        for c in range(self.cell_count):
            self.cells.append(Cell(False))

        self.capacity = self.jk.get_status()["cell_info"]["capacity_nominal"]

        self.hardware_version = (
            "JKBMS "
            + self.jk.get_status()["device_info"]["hw_rev"]
            + " "
            + str(self.cell_count)
            + "S"
            + (" (" + self.production + ")" if self.production else "")
        )
        logger.debug("BAT: " + self.hardware_version)
        return True

    def unique_identifier(self) -> str:
        """
        Used to identify a BMS when multiple BMS are connected
        """
        return self.unique_identifier_tmp

    def use_callback(self, callback: Callable) -> bool:
        if BLUETOOTH_USE_POLLING:
            return False

        self.jk.set_callback(callback)
        return callback is not None

    def refresh_data(self):
        # call all functions that will refresh the battery data.
        # This will be called for every iteration (1 second)
        # Return True if success, False for failure

        # result = self.read_soc_data()
        # TODO: check for errors
        try:
            st = self.jk.get_status()
            if st is None:
                return False

            last_update = int(time() - st["last_update"])
            if last_update >= 15 and last_update % 15 == 0:
                logger.info(f"Jkbms_Ble: Bluetooth connection interrupted. Got no fresh data since {last_update} s.")

                # show Bluetooth signal strength (RSSI)
                bluetoothctl_info = os.popen(
                    "bluetoothctl info " + self.address + ' | grep -i -E "device|name|alias|pair|trusted|blocked|connected|rssi|power"'
                )
                logger.info(bluetoothctl_info.read())
                bluetoothctl_info.close()

                # if the thread is still alive but data too old there is something
                # wrong with the bt-connection; restart whole stack
                if BLUETOOTH_FORCE_RESET_BLE_STACK and not self.resetting and last_update >= 30:
                    logger.error("Jkbms_Ble: Bluetooth died. Restarting Bluetooth system driver.")
                    self.reset_bluetooth()
                    sleep(2)
                    self.jk.start_scraping()
                    sleep(2)

                return False
            else:
                self.resetting = False

            # update cell voltages
            for c in range(self.cell_count):
                if st["cell_info"]["voltages"][c] >= 1 and st["cell_info"]["voltages"][c] <= 5:
                    self.cells[c].voltage = st["cell_info"]["voltages"][c]
                else:
                    logger.warning(f"Jkbms_Ble: Cell {c} voltage out of range (1 - 5 V): {st['cell_info']['voltages'][c]}")

            temperature_mos = st["cell_info"]["temperature_mos"]
            self.to_temperature(0, temperature_mos if temperature_mos < 32767 else (65535 - temperature_mos) * -1)

            temperature_1 = st["cell_info"]["temperature_sensor_1"]
            self.to_temperature(1, temperature_1 if temperature_1 < 32767 else (65535 - temperature_1) * -1)

            temperature_2 = st["cell_info"]["temperature_sensor_2"]
            self.to_temperature(2, temperature_2 if temperature_2 < 32767 else (65535 - temperature_2) * -1)

            self.current = round(st["cell_info"]["current"], 1)
            self.voltage = round(st["cell_info"]["total_voltage"], 2)

            self.soc = st["cell_info"]["battery_soc"]
            self.history.charge_cycles = st["cell_info"]["cycle_count"]

            self.charge_fet = st["settings"]["charging_switch"]
            self.discharge_fet = st["settings"]["discharging_switch"]
            self.balance_fet = st["settings"]["balancing_switch"]

            self.balancing = False if st["cell_info"]["balancing_action"] == 0.000 else True
            self.balancing_current = (
                st["cell_info"]["balancing_current"]
                if st["cell_info"]["balancing_current"] < 32767
                else (65535 / 1000 - st["cell_info"]["balancing_current"]) * -1
            )
            self.balancing_action = st["cell_info"]["balancing_action"]

            # show wich cells are balancing
            for c in range(self.cell_count):
                if self.balancing and (st["cell_info"]["min_voltage_cell"] == c or st["cell_info"]["max_voltage_cell"] == c):
                    self.cells[c].balance = True
                else:
                    self.cells[c].balance = False

            # protection bits
            # self.protection.low_soc = 2 if status["cell_info"]["battery_soc"] < 10.0 else 0

            # trigger cell imbalance warning when delta is to great
            if st["cell_info"]["delta_cell_voltage"] > min(st["settings"]["cell_ovp"] * 0.05, 0.400):
                self.protection.cell_imbalance = 2
            elif st["cell_info"]["delta_cell_voltage"] > min(st["settings"]["cell_ovp"] * 0.03, 0.300):
                self.protection.cell_imbalance = 1
            else:
                self.protection.cell_imbalance = 0

            self.protection.high_cell_voltage = 2 if st["warnings"]["cell_overvoltage"] else 0
            self.protection.low_cell_voltage = 2 if st["warnings"]["cell_undervoltage"] else 0

            self.protection.high_charge_current = 2 if (st["warnings"]["charge_overcurrent"] or st["warnings"]["discharge_overcurrent"]) else 0
            self.protection.set_IC_inspection = 2 if st["cell_info"]["temperature_mos"] > 80 else 0
            self.protection.high_charge_temperature = 2 if st["warnings"]["charge_overtemp"] else 0
            self.protection.low_charge_temperature = 2 if st["warnings"]["charge_undertemp"] else 0
            self.protection.high_temperature = 2 if st["warnings"]["discharge_overtemp"] else 0

            if int(time()) % 60 == 0:
                voltages_rounded = [round(v, 3) for v in st["cell_info"]["voltages"]]
                logger.debug(
                    f"current: {self.current} - voltage: {self.voltage}"
                    + f" - temp MOS: {self.temperature_mos} - temp1: {self.temperature_1} - temp2: {self.temperature_2} - last update: {last_update}s"
                    + f" - cell voltages: {voltages_rounded}"
                )

            return True
        except Exception:
            (
                exception_type,
                exception_object,
                exception_traceback,
            ) = sys.exc_info()
            file = exception_traceback.tb_frame.f_code.co_filename
            line = exception_traceback.tb_lineno
            logger.error(f"Exception occurred: {repr(exception_object)} of type {exception_type} in {file} line #{line}")
            return False

    def reset_bluetooth(self):
        restart_ble_hardware_and_bluez_driver()

    def get_balancing(self):
        return 1 if self.balancing else 0

    def trigger_soc_reset(self):
        if AUTO_RESET_SOC:
            self.jk.max_cell_voltage = self.get_max_cell_voltage()
            self.jk.trigger_soc_reset = True
        return

    def disconnect(self):
        self.jk.stop_scraping()
