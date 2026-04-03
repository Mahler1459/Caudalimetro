import sys
import os
import time
import threading
import requests
from datetime import datetime

IS_RPI = sys.platform == "linux" and os.path.exists("/proc/device-tree/model")

if IS_RPI:
    import RPi.GPIO as GPIO  # type: ignore
else:
    from mock_hardware import MockGPIO as GPIO


class HardwareController:
    def __init__(self, config, on_rodeo_change, on_shutdown):
        self.config = config
        self.on_rodeo_change = on_rodeo_change
        self.on_shutdown = on_shutdown
        self.rodeos_gpio = config["rodeos_gpio"]

        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.cleanup()

        for rodeo, pins in self.rodeos_gpio.items():
            btn = pins["button"]
            led = pins["led"]

            # Saltar rodeos sin hardware asignado
            if btn is not None:
                GPIO.setup(btn, GPIO.IN, pull_up_down=GPIO.PUD_UP)

            if led is not None:
                GPIO.setup(led, GPIO.OUT)
                GPIO.output(led, GPIO.LOW)


        GPIO.setup(self.config["power_gpio"], GPIO.IN, pull_up_down=GPIO.PUD_UP)

        threading.Thread(target=self._monitor_buttons, daemon=True).start()

        print("[GPIO] Inicializado (polling y apagado seguro en GPIO2).")

    def _monitor_buttons(self):
        HOLD_TIME = self.config.get("button_hold_ms", 500) / 1000.0
        press_start = {r: None for r in self.rodeos_gpio.keys()}
        fired = {r: False for r in self.rodeos_gpio.keys()}

        while True:
            for rodeo, pins in self.rodeos_gpio.items():
                if pins["button"] is None:
                    continue
                state = GPIO.input(pins["button"])

                if state == 0:  # boton presionado (LOW)
                    if press_start[rodeo] is None:
                        press_start[rodeo] = time.time()
                    elif not fired[rodeo] and (time.time() - press_start[rodeo]) >= HOLD_TIME:
                        print(f"[BOTON] {rodeo} confirmado ({HOLD_TIME*1000:.0f}ms)")
                        self.on_rodeo_change(rodeo)
                        fired[rodeo] = True
                else:  # boton suelto (HIGH)
                    press_start[rodeo] = None
                    fired[rodeo] = False

            time.sleep(0.05)


    @staticmethod
    def apagar_leds(rodeos_gpio):
        for pins in rodeos_gpio.values():
            GPIO.output(pins["led"], GPIO.LOW)

    @staticmethod
    def encender_led(rodeos_gpio, rodeo):
        for r, pins in rodeos_gpio.items():
            GPIO.output(pins["led"], GPIO.HIGH if r == rodeo else GPIO.LOW)
