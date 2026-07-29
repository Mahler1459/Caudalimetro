# Caudalimetro - Sistema de Ordene

> **IMPORTANTE (desplegar/operar en la Raspberry):** leer primero `DESPLIEGUE.md`.
> Resume el cambio TAM-275 (envio al backend Tambo-V + robustez de lectura), como bajar
> el codigo en el Pi, el manejo de `config.json`/`litros.json`, el comportamiento ante
> cortes de luz/internet, el setup en la web y las cosas a tener en cuenta.

## Que es esto
Sistema embebido que corre en una **Raspberry Pi** en un tambo (establecimiento lechero).
Mide litros de leche durante el ordene usando un caudalimetro electromagnetico **FlowMeet FMC-200E**
conectado por **RS485 Modbus RTU**, y asigna los litros a distintos rodeos (grupos de vacas)
seleccionados con botones fisicos + LEDs.

## Hardware en produccion
- **Raspberry Pi** (Raspbian/Linux)
- **Caudalimetro FlowMeet FMC-200E** via RS485 (conversor USB)
- **7 botones fisicos** (GPIO, pull-up) para seleccionar rodeo
- **7 LEDs** (GPIO) para indicar rodeo activo
- **1 boton de power** (GPIO 17) - configurado pero no implementado aun
- Pantalla conectada a la RPi (GUI Tkinter fullscreen)

## Arquitectura de archivos
- `main.py` — Clase principal `OrdeneSystem`. Orquesta todo: lectura, GUI, POST, reinicio.
- `flowmeter.py` — Clase `FlowMeter`. Lee volumen positivo acumulado via Modbus (2 registros).
- `gui.py` — `OrdeneGUI` (Tkinter). Muestra rodeo activo, litros parciales, litros totales.
- `hardware.py` — `HardwareController`. Maneja GPIO: botones (polling) y LEDs.
- `config.json` — Configuracion: pines GPIO, puerto serial, URLs de webhook, credenciales.
- `mock_hardware.py` — Mocks de GPIO y Serial para desarrollo en PC sin hardware.
- `litros.json` — Persistencia local de datos (NO se commitea, esta en .gitignore).

## Protocolo Modbus RTU del FMC-200E
El caudalimetro usa Modbus RTU, funcion 04 (Read Input Registers), address 01, baudrate 9600.

### Registros usados actualmente
| Registro | Comando completo                     | Dato                          |
|----------|--------------------------------------|-------------------------------|
| 0x1018   | `01 04 10 18 00 02 F5 0C`           | Volumen positivo (entero, 4 bytes unsigned int big-endian) |
| 0x101A   | `01 04 10 1A 00 02 54 CC`           | Volumen positivo (decimal, 4 bytes float IEEE754) |

Volumen total = parte entera + parte decimal.

### Registros disponibles (no usados aun)
| Registro | Comando                              | Dato                          |
|----------|--------------------------------------|-------------------------------|
| 0x1010   | `01 04 10 10 00 02 74 CE`           | Caudal instantaneo (float)    |
| 0x1012   | `01 04 10 12 00 02 D5 0E`           | Velocidad del fluido (float)  |
| 0x101C   | `01 04 10 1C 00 02 B4 CD`           | Volumen negativo entero       |
| 0x101E   | `01 04 10 1E 00 02 15 0D`           | Volumen negativo decimal      |
| 0x1022   | `01 04 10 22 00 01 95 00`           | Alarma alto caudal (0/1)      |
| 0x1023   | `01 04 10 23 00 01 C4 C0`           | Alarma bajo caudal (0/1)      |
| 0x1024   | `01 04 10 24 00 01 75 01`           | Alarma caneria vacia (0/1)    |
| 0x1025   | `01 04 10 25 00 01 24 C1`           | Alarma sistema (0/1)          |
| 0x1020   | `01 04 10 20 00 01 34 C0`           | Unidad de caudal              |
| 0x1021   | `01 04 10 21 00 01 65 00`           | Unidad de volumen             |

### Formato de respuesta Modbus
Respuesta del esclavo: `[addr 1B][func 1B][byte_count 1B][data 4B][CRC 2B]` = 9 bytes.
Los 4 bytes de data estan en posiciones [3:7] de la respuesta.

## Logica de lectura (cada 30 segundos) — `main.py:_procesar_lectura`
El volumen que reporta el caudalimetro es el ACUMULADO historico de vida (no del dia).
Se trabaja siempre por DELTA contra una BASE en memoria:
1. lectura None o <= 0 → ignorar (no mover base)
2. primera lectura valida → guardar como BASE, no sumar
3. lectura < base * 0.1 → reset del caudalimetro, actualizar BASE, no sumar
4. lectura <= base → ignorar (glitch / sin caudal)
5. delta = lectura - base; si delta > `max_delta_litros` (config, def. 2000) →
   salto imposible (frame corrupto, re-lectura del historico, base mal seteada) →
   DESCARTAR y re-basar (auto-recuperacion). NO sumar. Este es el backstop contra
   cargar el total historico (~4M L) como si fuera del dia.
6. delta plausible → sumar al rodeo si hay uno seleccionado.

### Validacion de frames Modbus (`flowmeter.py`)
Cada respuesta se valida antes de usarse: longitud 9 bytes, direccion, funcion (0x04),
byte-count (0x04) y CRC16 Modbus. Ademas se hace `reset_input_buffer()` antes de cada
lectura para evitar desincronizacion por bytes viejos. Un frame invalido devuelve `None`
(no un valor parcial/basura), y `leer()` devuelve `None` si CUALQUIER sub-registro fallo.

## Turnos y persistencia
- Turno M (manana): hora < 12. Turno T (tarde): hora >= 12.
- Clave de registro: `YYYY-MM-DD_T` o `YYYY-MM-DD_M`.
- Se guarda en `litros.json` con estructura:
  `{clave: {turno, flujo_total, datos_rodeos: {rodeo: litros}, tiempos_rodeos: {rodeo: {inicio, fin}}, pendiente_envio: bool}}`.
  `datos_rodeos[rodeo]` es el ACUMULADO del turno (no delta).
- Registros de mas de 30 dias se limpian automaticamente, salvo que tengan `pendiente_envio=True` (no perder dias sin conectividad).
- Reinicio automatico a las 00:00 y 12:00 (con POST final antes del reboot).

## Envio de datos (contrato backend Tambo-V — TAM-275)
- Endpoint: `POST https://tambo-v-production.up.railway.app/ingest/caudalimetro` (en `config.json:post_url`). Sin JWT ni farm en la URL.
- Auth: HTTP Basic con `user`/`password` de `config.json`. La credencial se genera en la web
  (Parte diario → Configuración → Credencial del caudalímetro → Generar); la password se muestra UNA sola vez.
- Body: `{fecha:"YYYY-MM-DD", turno:"M"|"T", lotes:[{rodeo, litros, inicio?, fin?}]}`.
  `litros` es el ACUMULADO del turno por rodeo (el backend hace upsert por (fecha,turno,rodeo), el ultimo pisa). `inicio`/`fin` son ISO 8601 con offset (opcionales).
  Solo se incluyen rodeos con litros > 0.
- Cadencia: al arrancar (reenvia pendientes), cada 15 min, y POST final antes de cada reboot/shutdown.
- Persistencia/robustez: `litros.json` es la cola durable. Cada delta marca el turno `pendiente_envio=True`
  y se persiste con escritura atomica; un turno se marca enviado solo tras un 200. Ante corte de luz,
  perdida de conectividad o reboot, al reiniciar se reenvia todo lo pendiente. Reenviar es idempotente (upsert).
- Respuestas: 200 OK (`{mapped, unmapped}`); 401 credencial invalida; 422 body invalido; 5xx reintentar.
  Cualquier respuesta != 2xx o error de red deja el turno pendiente para reintentar.

## Desarrollo en PC (modo mock)
La deteccion de plataforma es automatica (`IS_RPI` en cada modulo).
En PC (Windows/Mac/Linux sin RPi):
- GPIO se reemplaza por `MockGPIO` (mock_hardware.py)
- Serial se reemplaza por `MockSerial` que simula lecturas incrementales
- La GUI abre en ventana 800x600 (no fullscreen)
- Teclas 1-7 simulan los botones de rodeo, Esc cierra
- Shutdown/reboot solo imprime un mensaje, no ejecuta

## Config de rodeos (GPIO)
```
Frescas:     boton=6,  led=21
Punta:       boton=5,  led=20
Vaquillonas: boton=1,  led=26
Cola:        boton=0,  led=16
Rengas:      boton=7,  led=19
Mastitis:    boton=8,  led=13
Otro:        boton=11, led=12
Power:       gpio=17 (no implementado)
```

## Cosas pendientes / a tener en cuenta
- El boton de power (GPIO 17) esta configurado pero no se monitorea.
- `hardware.py:44` no valida `pins["button"] is None` en el loop de polling (si lo valida en setup).
- `flowmeter.leer()` retorna `None` ante frame invalido/error; `main.py` lo maneja (ignora None o <= 0). `leer_caudal()` sigue retornando `0.0` en error (solo es display).
- Los registros de alarma del caudalimetro no se leen — podrian usarse para detectar fin de ordene (caneria vacia).
