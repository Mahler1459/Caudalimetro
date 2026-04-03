"""
Mocks de hardware para desarrollo en PC (Windows/Linux sin GPIO).
Simula RPi.GPIO y serial.Serial (respuestas Modbus del FMC-200E).

En la Raspberry Pi real este modulo NO se usa.
"""

import struct
import time
import threading


# ============================================================
# Mock RPi.GPIO
# ============================================================
class MockGPIO:
    BCM = 11
    OUT = 0
    IN = 1
    HIGH = 1
    LOW = 0
    PUD_UP = 22

    _pin_states = {}
    _output_states = {}

    @staticmethod
    def setwarnings(flag):
        pass

    @staticmethod
    def setmode(mode):
        pass

    @staticmethod
    def cleanup():
        pass

    @staticmethod
    def setup(pin, mode, pull_up_down=None):
        if pin is None:
            return
        MockGPIO._pin_states[pin] = 1  # botones default HIGH (pull-up)
        MockGPIO._output_states[pin] = 0

    @staticmethod
    def input(pin):
        return MockGPIO._pin_states.get(pin, 1)

    @staticmethod
    def output(pin, value):
        if pin is None:
            return
        MockGPIO._output_states[pin] = value

    @staticmethod
    def simular_boton(pin, duracion=0.3):
        """Simula una pulsacion momentanea de un boton GPIO."""
        MockGPIO._pin_states[pin] = 0
        time.sleep(duracion)
        MockGPIO._pin_states[pin] = 1


# ============================================================
# Mock Serial  (simula respuestas Modbus del FMC-200E)
# ============================================================
class MockSerial:
    """Simula el puerto serial RS485 con respuestas Modbus RTU.

    Genera volumen positivo creciente y negativo pequeno (reflujo),
    simulando un ordene real.
    """

    def __init__(self, port=None, baudrate=9600, bytesize=8,
                 parity="N", stopbits=1, timeout=1):
        self.port = port
        self.baudrate = baudrate
        self._vol_pos = 0.0          # litros positivos acumulados
        self._vol_neg = 0.0          # litros negativos (reflujo) acumulados
        self._incremento = 2.5       # litros positivos por lectura
        self._reflujo = 0.3          # litros de reflujo por lectura
        self._ultimo_comando = b''
        self._lock = threading.Lock()
        self._running = True

        # Comandos conocidos del FMC-200E (primeros 6 bytes)
        self.CMD_VOL_POS_ENT = b'\x01\x04\x10\x18\x00\x02'
        self.CMD_VOL_POS_DEC = b'\x01\x04\x10\x1A\x00\x02'
        self.CMD_VOL_NEG_ENT = b'\x01\x04\x10\x1C\x00\x02'
        self.CMD_VOL_NEG_DEC = b'\x01\x04\x10\x1E\x00\x02'
        self.CMD_CAUDAL      = b'\x01\x04\x10\x10\x00\x02'

    def write(self, data):
        with self._lock:
            self._ultimo_comando = data

    def _respuesta(self, valor_entero=None, valor_float=None):
        if valor_entero is not None:
            payload = struct.pack('>I', valor_entero)
        else:
            payload = struct.pack('>f', valor_float)
        return b'\x01\x04\x04' + payload + b'\x00\x00'

    def read(self, size):
        with self._lock:
            cmd = self._ultimo_comando[:6]

        if cmd == self.CMD_VOL_POS_ENT:
            self._vol_pos += self._incremento
            return self._respuesta(valor_entero=int(self._vol_pos))

        elif cmd == self.CMD_VOL_POS_DEC:
            return self._respuesta(valor_float=self._vol_pos - int(self._vol_pos))

        elif cmd == self.CMD_VOL_NEG_ENT:
            self._vol_neg += self._reflujo
            return self._respuesta(valor_entero=int(self._vol_neg))

        elif cmd == self.CMD_VOL_NEG_DEC:
            return self._respuesta(valor_float=self._vol_neg - int(self._vol_neg))

        elif cmd == self.CMD_CAUDAL:
            # Simula caudal instantaneo variable (entre 20 y 35 L/min)
            import random
            caudal = random.uniform(20.0, 35.0)
            return self._respuesta(valor_float=caudal)

        # Comando desconocido
        return b'\x00' * size

    def close(self):
        self._running = False

    def set_incremento(self, litros_por_lectura):
        """Cambiar ritmo de simulacion (util para testing)."""
        self._incremento = litros_por_lectura

    @property
    def is_open(self):
        return self._running
