#!/usr/bin/env python3

import os
import sys
import json
import time
import threading
import requests
import subprocess
from datetime import datetime

# Deteccion de plataforma: en RPi usa hardware real, en PC usa mocks
IS_RPI = sys.platform == "linux" and os.path.exists("/proc/device-tree/model")

if IS_RPI:
    import serial
    import RPi.GPIO as GPIO  # type: ignore
else:
    from mock_hardware import MockSerial as _MockSerial
    from mock_hardware import MockGPIO as GPIO
    # serial.Serial se reemplaza por MockSerial
    class _serial_module:
        Serial = _MockSerial
    serial = _serial_module()

from gui import OrdeneGUI, show_shutdown_screen
from flowmeter import FlowMeter
from hardware import HardwareController


# ===============================
# Utilidades de archivos/config
# ===============================
def cargar_config():
    with open("config.json", "r") as f:
        return json.load(f)


def guardar_json(nombre, data):
    # Escritura atomica: escribir a .tmp y renombrar para evitar corrupcion
    tmp = nombre + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, nombre)


def cargar_json(nombre):
    if os.path.exists(nombre):
        try:
            with open(nombre, "r") as f:
                contenido = f.read().strip()
                if not contenido:
                    print(f"[WARN] {nombre} esta vacio, usando {{}}.")
                    return {}
                return json.loads(contenido)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"[WARN] {nombre} corrupto ({e}), usando {{}}.")
            return {}
    return {}


def clave_dia_turno():
    fecha = datetime.now().strftime("%Y-%m-%d")
    turno = "M" if datetime.now().hour < 12 else "T"
    return f"{fecha}_{turno}"


# ===============================
# Clase principal del sistema
# ===============================
class OrdeneSystem:
    def __init__(self):
        self.config = cargar_config()
        self.rodeos_gpio = self.config["rodeos_gpio"]

        # Estado
        self.current_rodeo = None          # No sumar hasta que haya seleccion
        self.litros_total_vista = 0.0      # Lo que mostramos en GUI (total del turno)
        self.litros_rodeo_sesion = 0.0     # Litros acumulados desde que se eligio el rodeo actual
        self.ultima_lectura = None         # BASE: se setea con la primera lectura valida
        self.caudal_actual = 0.0           # Caudal instantaneo (L/min)
        # Salto maximo plausible de litros entre dos lecturas. Un delta mayor no es
        # leche real (frame corrupto, re-lectura del historico de vida, etc.): se
        # descarta y se re-basa. Evita cargar el total historico como si fuera del dia.
        self.max_delta_litros = self.config.get("max_delta_litros", 2000)

        # RS485
        self.serial_port = serial.Serial(
            port=self.config["rs485_port"],
            baudrate=self.config["rs485_baudrate"],
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=1
        )
        self.flow = FlowMeter(self.serial_port)

        # GUI y Hardware
        self.gui = OrdeneGUI()
        self.hw = HardwareController(self.config, self.select_rodeo, None)

        # En PC: atajos de teclado 1-7 para simular botones de rodeo
        if not IS_RPI:
            rodeos_lista = list(self.rodeos_gpio.keys())
            for i, rodeo in enumerate(rodeos_lista):
                self.gui.root.bind(str(i + 1), lambda e, r=rodeo: self.select_rodeo(r))

        # Datos
        self.litros_data = cargar_json("litros.json")
        self._limpiar_registros_antiguos()
        self._asegurar_registro(clave_dia_turno())

        # Hilos
        threading.Thread(target=self.actualizar_lectura, daemon=True).start()
        threading.Thread(target=self.post_loop, daemon=True).start()
        threading.Thread(target=self.reinicio_programado, daemon=True).start()

        self.gui.loop()

    # ===============================
    # Inicialización / mantenimiento
    # ===============================
    def _limpiar_registros_antiguos(self):
        """Elimina registros de mas de 30 dias en litros.json."""
        try:
            hoy = datetime.now()
            nuevos = {}
            for clave, datos in self.litros_data.items():
                fecha_str = clave.split("_")[0]
                fecha_reg = datetime.strptime(fecha_str, "%Y-%m-%d")
                # Conservar si es reciente O si aun no se confirmo el envio (no perder dias)
                if (hoy - fecha_reg).days <= 30 or datos.get("pendiente_envio"):
                    nuevos[clave] = datos
            if len(nuevos) < len(self.litros_data):
                print(f"[LIMPIEZA] {len(self.litros_data) - len(nuevos)} registros antiguos eliminados.")
            self.litros_data = nuevos
            guardar_json("litros.json", self.litros_data)
        except Exception as e:
            print(f"[LIMPIEZA] Error limpiando registros: {e}")

    def _asegurar_registro(self, clave):
        """Crea registro (dia+turno) si no existe, con todos los rodeos en 0."""
        if clave not in self.litros_data:
            turno = clave.split("_")[1]
            self.litros_data[clave] = {
                "turno": turno,
                "flujo_total": 0.0,
                "datos_rodeos": {r: 0.0 for r in self.rodeos_gpio.keys()},
                "tiempos_rodeos": {},      # {rodeo: {inicio, fin}} en ISO 8601 con offset
                "pendiente_envio": False   # True cuando hay litros sin confirmar en el backend
            }
            guardar_json("litros.json", self.litros_data)

    # ===============================
    # Logica de funcionamiento
    # ===============================
    def select_rodeo(self, rodeo):
        """Callback desde hardware.py cuando se presiona un boton."""
        self.current_rodeo = rodeo
        self.hw.encender_led(self.rodeos_gpio, self.current_rodeo)
        self.litros_rodeo_sesion = 0.0
        self.update_gui()

    def actualizar_lectura(self):
        """Loop de lectura periodica. Delega cada lectura a _procesar_lectura()."""
        intervalo = 5 if not IS_RPI else 30
        while True:
            time.sleep(intervalo)
            try:
                lectura = self.flow.leer()
                # Leer caudal instantaneo (solo para mostrar en la GUI)
                self.caudal_actual = self.flow.leer_caudal()
            except Exception as e:
                print(f"[RS485] Error leyendo: {e}")
                continue
            self._procesar_lectura(lectura)

    def _procesar_lectura(self, lectura):
        """Aplica las reglas robustas sobre una lectura de volumen acumulado:
           - lectura None/<=0       → ignorar (no mover base)
           - base None              → primera lectura valida se toma como BASE (no sumar)
           - lectura < base*0.1     → reset real del caudalimetro → re-basar (no sumar)
           - lectura <= base        → ignorar (glitch / sin caudal)
           - delta > max_delta      → salto imposible (frame corrupto/historico) → re-basar (no sumar)
           - lectura > base         → delta = lectura - base; sumar SOLO si hay rodeo seleccionado
        """
        # Ignorar lecturas nulas / error / no positivas
        if lectura is None or lectura <= 0:
            self.update_gui()
            return

        # Primera lectura valida despues de arrancar → usar como BASE sin sumar
        if self.ultima_lectura is None:
            self.ultima_lectura = lectura
            return

        # Cambio de dia/turno en caliente → asegurar registro
        clave = clave_dia_turno()
        self._asegurar_registro(clave)

        # Detectar reset real (gran salto hacia abajo): no sumar, solo actualizar BASE
        if lectura < (self.ultima_lectura * 0.1):
            print(f"[RS485] Reset detectado (base {self.ultima_lectura:.1f} -> {lectura:.1f}), re-basando")
            self.ultima_lectura = lectura
            return

        # Sin incremento (igual o baja pequena que no es reset) → ignorar
        if lectura <= self.ultima_lectura:
            return

        delta = lectura - self.ultima_lectura

        # GUARDIA DE SANIDAD: un salto imposible entre dos lecturas no es leche real
        # (frame corrupto, re-lectura del total historico, base mal seteada). Se
        # descarta el delta y se re-basa para auto-recuperarse en la proxima lectura.
        if delta > self.max_delta_litros:
            print(f"[RS485] Delta anomalo descartado: {delta:.1f} L > max {self.max_delta_litros} L "
                  f"(base {self.ultima_lectura:.1f} -> {lectura:.1f}). No se suma.")
            self.ultima_lectura = lectura
            return

        # Lectura normal: delta positivo y plausible
        self.ultima_lectura = lectura

        # Si no hay rodeo seleccionado → NO sumar (segun definicion del usuario)
        if not self.current_rodeo:
            return

        # Sumar al rodeo actual
        datos = self.litros_data[clave]["datos_rodeos"]
        datos.setdefault(self.current_rodeo, 0.0)
        datos[self.current_rodeo] += delta

        # Registrar inicio/fin del ordeñe del rodeo (ISO 8601 con offset local)
        ahora_iso = datetime.now().astimezone().isoformat(timespec="seconds")
        tiempos = self.litros_data[clave].setdefault("tiempos_rodeos", {})
        t = tiempos.setdefault(self.current_rodeo, {})
        if not t.get("inicio"):
            t["inicio"] = ahora_iso
        t["fin"] = ahora_iso

        # Actualizar totales del dia/turno y GUI
        self.litros_data[clave]["flujo_total"] = round(sum(datos.values()), 2)
        self.litros_total_vista = self.litros_data[clave]["flujo_total"]
        self.litros_rodeo_sesion += delta

        # Marcar el turno como pendiente de envio y persistir (cola durable)
        self.litros_data[clave]["pendiente_envio"] = True
        guardar_json("litros.json", self.litros_data)
        self.update_gui()

    # ===============================
    # GUI / Post / Reinicio
    # ===============================
    def update_gui(self):
        nombre = self.current_rodeo if self.current_rodeo else "Sin seleccion"
        caudal = self.caudal_actual
        self.gui.root.after(
            0, lambda: self.gui.update_display(nombre, self.litros_rodeo_sesion, self.litros_total_vista, caudal)
        )

    def post_loop(self):
        # Al arrancar, reintentar todo lo que quedo sin confirmar (incluye lo que
        # no se llego a enviar antes de un apagon/corte de luz).
        self.enviar_pendientes()
        while True:
            time.sleep(900)  # cada 15 minutos
            self.enviar_pendientes()

    def _construir_payload(self, clave, registro):
        """Arma el body del contrato: {fecha, turno, lotes:[{rodeo, litros, inicio?, fin?}]}.
           litros es el ACUMULADO del turno por rodeo (el backend hace upsert, no delta)."""
        fecha, turno = clave.split("_")
        datos = registro.get("datos_rodeos", {})
        tiempos = registro.get("tiempos_rodeos", {})

        lotes = []
        for rodeo, litros in datos.items():
            if not litros or litros <= 0:
                continue  # solo rodeos con leche en el turno
            item = {"rodeo": rodeo, "litros": round(litros, 2)}
            t = tiempos.get(rodeo, {})
            if t.get("inicio"):
                item["inicio"] = t["inicio"]
            if t.get("fin"):
                item["fin"] = t["fin"]
            lotes.append(item)

        return {"fecha": fecha, "turno": turno, "lotes": lotes}

    def enviar_pendientes(self):
        """Recorre litros.json y reenvia cada turno con pendiente_envio=True.
           litros.json es la cola durable: sobrevive cortes de luz y reinicios.
           El backend upserta por (fecha, turno, rodeo), asi que reenviar es idempotente."""
        hubo_cambios = False
        for clave, registro in list(self.litros_data.items()):
            if not registro.get("pendiente_envio"):
                continue

            payload = self._construir_payload(clave, registro)
            if not payload["lotes"]:
                # Turno marcado pendiente pero sin litros: nada que enviar
                registro["pendiente_envio"] = False
                hubo_cambios = True
                continue

            if self._hacer_post(payload):
                registro["pendiente_envio"] = False
                hubo_cambios = True
            # Si falla, se deja pendiente_envio=True para reintentar en el proximo ciclo

        if hubo_cambios:
            guardar_json("litros.json", self.litros_data)

    def _hacer_post(self, data):
        """Envia un POST al backend. Retorna True solo si el backend confirmo (2xx)."""
        try:
            r = requests.post(
                self.config["post_url"],
                json=data,
                auth=(self.config["user"], self.config["password"]),
                timeout=10
            )
            etiqueta = f"{data['fecha']}_{data['turno']} ({len(data['lotes'])} lotes)"
            if 200 <= r.status_code < 300:
                print(f"[POST] OK {r.status_code} {etiqueta}")
                return True
            if r.status_code == 401:
                print(f"[POST] 401 credencial invalida/inactiva ({etiqueta}). "
                      f"Regenerar en la web y actualizar config.json. Se reintentara.")
            elif r.status_code == 422:
                print(f"[POST] 422 body invalido ({etiqueta}): {r.text[:200]}. Se reintentara.")
            else:
                print(f"[POST] {r.status_code} ({etiqueta}): {r.text[:200]}. Se reintentara.")
            return False
        except Exception as e:
            # Sin conectividad: no se pierde nada, queda pendiente para el proximo ciclo
            print(f"[POST] Error de red: {e}. Se reintentara.")
            return False

    def reinicio_programado(self):
        """Reinicio automatico a las 00:00 y 12:00 (con post y pantalla)."""
        ultimo = ""
        while True:
            hora_actual = datetime.now().strftime("%H:%M")
            if hora_actual in ["00:00", "12:00"] and hora_actual != ultimo:
                ultimo = hora_actual
                print(f"[REINICIO] Reinicio automatico programado ({hora_actual})")
                self.safe_shutdown(reboot=True)
            time.sleep(60)

    def safe_shutdown(self, reboot=False):
        try:
            print("[APAGADO] Iniciando apagado seguro...")
            shutdown_window = show_shutdown_screen()

            # Post final: enviar todo lo pendiente (incluye el turno que esta cerrando)
            try:
                self.enviar_pendientes()
            except Exception as e:
                print(f"[APAGADO] Error posteando antes de apagar: {e}")

            # Notificacion opcional si tenes shutdown_post_url en config
            try:
                url = self.config.get("shutdown_post_url", "")
                if url:
                    data_apagado = {
                        "fecha": datetime.now().strftime("%Y-%m-%d"),
                        "hora": datetime.now().strftime("%H:%M:%S"),
                        "evento": "reboot" if reboot else "apagado",
                        "mensaje": f"{'Reinicio' if reboot else 'Apagado'} seguro ejecutado"
                    }
                    r = requests.post(
                        url,
                        json=data_apagado,
                        auth=(self.config["user"], self.config["password"]),
                        timeout=10
                    )
                    print(f"[APAGADO] Notificacion enviada ({r.status_code})")
            except Exception as e:
                print(f"[APAGADO] Error en notificacion: {e}")

            if shutdown_window:
                time.sleep(2)
                shutdown_window.destroy()

            if IS_RPI:
                if reboot:
                    subprocess.run(["/sbin/reboot"], check=False)
                else:
                    subprocess.run(["/sbin/shutdown", "-h", "now"], check=False)
            else:
                print(f"[PC] Simulando {'reboot' if reboot else 'shutdown'} (sin efecto en PC)")

        except Exception as e:
            print(f"[APAGADO] Error en apagado seguro: {e}")


if __name__ == "__main__":
    try:
        OrdeneSystem()
    except KeyboardInterrupt:
        print("Saliendo...")
