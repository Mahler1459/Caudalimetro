import struct
import time


class FlowMeter:
    def __init__(self, serial_port):
        self.serial_port = serial_port
        # Volumen positivo (entero + decimal)
        self.volpos1 = b'\x01\x04\x10\x18\x00\x02\xF5\x0C'
        self.volpos2 = b'\x01\x04\x10\x1A\x00\x02\x54\xCC'
        # Volumen negativo (entero + decimal)
        self.volneg1 = b'\x01\x04\x10\x1C\x00\x02\xB4\xCD'
        self.volneg2 = b'\x01\x04\x10\x1E\x00\x02\x15\x0D'
        # Caudal instantaneo (float IEEE754)
        self.cmd_caudal = b'\x01\x04\x10\x10\x00\x02\x74\xCE'

    def _leer_registro_entero(self, comando):
        self.serial_port.write(comando)
        time.sleep(0.1)
        resp = self.serial_port.read(9)
        if len(resp) >= 7:
            return struct.unpack('>I', resp[3:7])[0]
        return 0

    def _leer_registro_float(self, comando):
        self.serial_port.write(comando)
        time.sleep(0.1)
        resp = self.serial_port.read(9)
        if len(resp) >= 7:
            return struct.unpack('>f', resp[3:7])[0]
        return 0.0

    def leer(self):
        try:
            vol_pos = self._leer_registro_entero(self.volpos1) + self._leer_registro_float(self.volpos2)
            vol_neg = self._leer_registro_entero(self.volneg1) + self._leer_registro_float(self.volneg2)
            neto = vol_pos - vol_neg
            print(f"[RS485] pos={vol_pos:.3f}, neg={vol_neg:.3f}, neto={neto:.3f}")
            return neto
        except Exception as e:
            print(f"[RS485] Error: {e}")
            return 0.0

    def leer_caudal(self):
        """Lee el caudal instantaneo (registro 0x1010). Retorna float o 0.0 en error."""
        try:
            caudal = self._leer_registro_float(self.cmd_caudal)
            return caudal
        except Exception as e:
            print(f"[RS485] Error leyendo caudal: {e}")
            return 0.0
