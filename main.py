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
    with open(nombre, "w") as f:
        json.dump(data, f, indent=2)


def cargar_json(nombre):
    if os.path.exists(nombre):
        with open(nombre, "r") as f:
            return json.load(f)
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
        self.hubo_cambio = False
        self.ultima_lectura = None         # BASE: se setea con la primera lectura valida
        self.caudal_actual = 0.0           # Caudal instantaneo (L/min)
        self.caudal_max_intervalo = 0.0    # Caudal maximo en el intervalo de POST

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
                if (hoy - fecha_reg).days <= 30:
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
                "datos_rodeos": {r: 0.0 for r in self.rodeos_gpio.keys()}
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
        """Lectura periodica con reglas robustas:
           - lectura == 0           → ignorar (no mover base)
           - base None              → primera lectura valida se toma como BASE (no sumar)
           - lectura < base*0.1     → reset real → actualizar BASE a lectura (no sumar)
           - lectura > base         → delta = lectura - base; sumar SOLO si hay rodeo seleccionado
           - resto                  → ignorar (glitch)
        """
        intervalo = 5 if not IS_RPI else 30
        while True:
            time.sleep(intervalo)
            try:
                lectura = self.flow.leer()
                # Leer caudal instantaneo
                self.caudal_actual = self.flow.leer_caudal()
                if self.caudal_actual > self.caudal_max_intervalo:
                    self.caudal_max_intervalo = self.caudal_actual
            except Exception as e:
                print(f"[RS485] Error leyendo: {e}")
                continue

            # Ignorar lecturas nulas / error
            if lectura is None or lectura == 0:
                self.update_gui()
                continue

            # Primera lectura valida despues de arrancar → usar como BASE sin sumar
            if self.ultima_lectura is None:
                self.ultima_lectura = lectura
                continue

            # Cambio de dia/turno en caliente → asegurar registro
            clave = clave_dia_turno()
            self._asegurar_registro(clave)

            # Detectar reset real (gran salto hacia abajo): no sumar, solo actualizar BASE
            if lectura < (self.ultima_lectura * 0.1):
                self.ultima_lectura = lectura
                continue

            # Lectura normal: sumar solo delta positivo
            if lectura > self.ultima_lectura:
                delta = lectura - self.ultima_lectura
                self.ultima_lectura = lectura

                # Si no hay rodeo seleccionado → NO sumar (segun definicion del usuario)
                if not self.current_rodeo:
                    continue

                # Sumar al rodeo actual
                datos = self.litros_data[clave]["datos_rodeos"]
                datos.setdefault(self.current_rodeo, 0.0)
                datos[self.current_rodeo] += delta

                # Actualizar totales del dia/turno y GUI
                self.litros_data[clave]["flujo_total"] = round(sum(datos.values()), 2)
                self.litros_total_vista = self.litros_data[clave]["flujo_total"]
                self.litros_rodeo_sesion += delta

                guardar_json("litros.json", self.litros_data)
                self.hubo_cambio = True
                self.update_gui()

            # Si lectura == base o lectura < base (sin ser reset) → ignorar

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
        while True:
            time.sleep(900)  # cada 15 minutos
            self._enviar_cola()
            if self.hubo_cambio:
                self.enviar_post()
                self.hubo_cambio = False

    def enviar_post(self):
        clave = clave_dia_turno()
        if clave not in self.litros_data:
            return

        registro = self.litros_data[clave]
        datos = dict(registro.get("datos_rodeos", {}))

        # Asegurar inclusion de todos los rodeos definidos en config
        for r in self.rodeos_gpio.keys():
            datos.setdefault(r, 0.0)

        # Convertir a enteros SOLO para el post
        datos_enteros = {k: int(round(v)) for k, v in datos.items()}
        flujo_entero = int(round(registro.get("flujo_total", 0.0)))

        data = {
            "fecha": clave.split("_")[0],
            "hora": datetime.now().strftime("%H:%M:%S"),
            "turno": registro.get("turno"),
            "flujo_total": flujo_entero,
            "datos_rodeos": datos_enteros,
            "caudal_max": round(self.caudal_max_intervalo, 1),
            "id": clave
        }

        # Resetear maximo del intervalo despues de capturarlo
        self.caudal_max_intervalo = 0.0

        if not self._hacer_post(data):
            self._encolar_post(data)

    def _hacer_post(self, data):
        """Intenta enviar un POST. Retorna True si fue exitoso."""
        try:
            r = requests.post(
                self.config["post_url"],
                json=data,
                auth=(self.config["user"], self.config["password"]),
                timeout=10
            )
            print(f"[POST] {r.status_code}")
            return True
        except Exception as e:
            print(f"[POST] Error: {e}")
            return False

    def _encolar_post(self, data):
        """Guarda un POST fallido en cola_post.json para reintento."""
        cola = cargar_json("cola_post.json")
        if not isinstance(cola, dict):
            cola = {"pendientes": []}
        cola.setdefault("pendientes", [])
        cola["pendientes"].append(data)
        guardar_json("cola_post.json", cola)
        print(f"[COLA] Encolado ({len(cola['pendientes'])} pendientes)")

    def _enviar_cola(self):
        """Intenta enviar los POSTs pendientes en la cola."""
        cola = cargar_json("cola_post.json")
        if not isinstance(cola, dict) or not cola.get("pendientes"):
            return

        pendientes = cola["pendientes"]
        enviados = []

        for i, data in enumerate(pendientes):
            if self._hacer_post(data):
                enviados.append(i)
            else:
                break  # si falla uno, no seguir intentando (sin internet)

        if enviados:
            cola["pendientes"] = [d for i, d in enumerate(pendientes) if i not in enviados]
            guardar_json("cola_post.json", cola)
            print(f"[COLA] {len(enviados)} enviados, {len(cola['pendientes'])} pendientes")

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

            # Post final con el ultimo estado
            try:
                self.enviar_post()
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
