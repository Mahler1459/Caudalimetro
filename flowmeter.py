import struct
import time


def crc16_modbus(data):
    """CRC16 Modbus RTU (polinomio 0xA001). Retorna entero de 16 bits."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


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

    def _leer_registro(self, comando, fmt):
        """Envia un comando Modbus y devuelve el valor desempaquetado, o None si
        el frame es invalido. Valida direccion, funcion, byte-count y CRC para
        descartar respuestas corruptas o desincronizadas (bytes viejos en el buffer),
        que si se toman como validas producen lecturas basura (deltas gigantes)."""
        # Limpiar el buffer de entrada para evitar leer bytes de una respuesta previa
        try:
            self.serial_port.reset_input_buffer()
        except Exception:
            pass

        self.serial_port.write(comando)
        time.sleep(0.1)
        resp = self.serial_port.read(9)

        # Respuesta esperada: [addr][func=04][byte_count=04][data 4B][crc_lo][crc_hi] = 9 bytes
        if len(resp) != 9:
            return None
        if resp[0] != comando[0] or resp[1] != 0x04 or resp[2] != 0x04:
            return None
        crc_recibido = resp[7] | (resp[8] << 8)   # CRC va little-endian (lo, hi)
        if crc16_modbus(resp[0:7]) != crc_recibido:
            return None

        return struct.unpack(fmt, resp[3:7])[0]

    def _leer_registro_entero(self, comando):
        return self._leer_registro(comando, '>I')

    def _leer_registro_float(self, comando):
        return self._leer_registro(comando, '>f')

    def leer(self):
        """Lee el volumen neto acumulado (historico de vida del caudalimetro).
        Retorna None si CUALQUIER sub-lectura es invalida, para no devolver un
        valor parcial/corrupto que luego se interpretaria como un salto de litros."""
        try:
            p1 = self._leer_registro_entero(self.volpos1)
            p2 = self._leer_registro_float(self.volpos2)
            n1 = self._leer_registro_entero(self.volneg1)
            n2 = self._leer_registro_float(self.volneg2)

            if None in (p1, p2, n1, n2):
                print("[RS485] Frame invalido/incompleto, lectura descartada")
                return None

            vol_pos = p1 + p2
            vol_neg = n1 + n2
            neto = vol_pos - vol_neg
            print(f"[RS485] pos={vol_pos:.3f}, neg={vol_neg:.3f}, neto={neto:.3f}")
            return neto
        except Exception as e:
            print(f"[RS485] Error: {e}")
            return None

    def leer_caudal(self):
        """Lee el caudal instantaneo (registro 0x1010). Retorna float o 0.0 en error.
        Solo se usa para mostrar en la GUI, asi que 0.0 es aceptable ante fallo."""
        try:
            caudal = self._leer_registro_float(self.cmd_caudal)
            return caudal if caudal is not None else 0.0
        except Exception as e:
            print(f"[RS485] Error leyendo caudal: {e}")
            return 0.0
