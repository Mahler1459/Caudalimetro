# Notas de despliegue y operación (leer antes de tocar el caudalímetro)

> Este archivo resume TODO lo que hay que tener en cuenta después del cambio TAM-275
> (envío al backend de Tambo-V + robustez de lectura). Está pensado para quien baje
> el código en la Raspberry Pi del tambo (incluido el Claude que corra ahí).

---

## 1. Qué cambió (resumen)

- **Nuevo destino de datos**: se dejó de postear al webhook de n8n. Ahora se postea al
  backend de Tambo-V: `POST https://tambo-v-production.up.railway.app/ingest/caudalimetro`.
- **Formato nuevo del POST**: `{fecha, turno, lotes:[{rodeo, litros, inicio?, fin?}]}`.
  `litros` es el **acumulado del turno por rodeo** (no el delta). El backend hace
  **upsert por (fecha, turno, rodeo)** → el último envío pisa al anterior → reenviar es seguro.
- **Cola durable = `litros.json`**: cada turno lleva un flag `pendiente_envio`. Se reenvía
  todo lo no confirmado al arrancar, cada 15 min y antes de cada reboot.
- **Robustez de lectura**: validación de frames Modbus (CRC, etc.) + guardia de "salto imposible"
  para no volver a cargar el total histórico (~4M L) como si fuera del día.
- **Credencial cargada** en `config.json` (usuario/contraseña generados en la web).

Ver detalle técnico en `CLAUDE.md` (secciones "Lógica de lectura", "Turnos y persistencia",
"Envío de datos").

---

## 2. Cómo bajar el cambio en la Raspberry

```bash
cd <carpeta-del-proyecto>
git pull origin master
# reiniciar el proceso (o el Pi) para que corra el código nuevo
```

### Ojo con `config.json` (¡puede dar conflicto!)
`config.json` **está versionado** y ahora trae:
- el endpoint nuevo (`post_url`),
- la **credencial** (`user` / `password`),
- `max_delta_litros` (guardia de sanidad, default 2000),
- `shutdown_post_url` vacío (el backend nuevo no tiene endpoint de apagado).

Si en el Pi `config.json` estaba modificado localmente (por ejemplo otro `rs485_port`,
u otros nombres de rodeos), el `git pull` puede dar **conflicto**. Al resolverlo:
- **Mantené** del repo: `post_url`, `user`, `password`, `max_delta_litros`.
- **Mantené** del Pi lo que sea específico del hardware de esa máquina: `rs485_port`,
  pines GPIO reales, nombres de rodeos si difieren.

### `litros.json` NO se toca con el pull
Está en `.gitignore`, así que los datos locales del Pi se conservan. Importante:
- Los registros **viejos** (de antes del cambio) no tienen `pendiente_envio`, así que
  **no se reenvían** al backend nuevo (ya se habían mandado al n8n viejo). Esto es lo esperado.
- Los turnos **nuevos** sí se envían solos.

---

## 3. Comportamiento ante cortes (lo importante del tambo)

- **Se corta la luz / se apaga la Pi**: los litros acumulados están en `litros.json`
  (escritura atómica), así que **no se pierden**. Al volver a arrancar, se reenvía todo
  lo que haya quedado con `pendiente_envio=True`.
- **Se cae internet**: el turno queda `pendiente_envio=True` y se reintenta cada 15 min
  y al próximo arranque. No se duplica nada (upsert idempotente).
- **Se resetea el caudalímetro** (vuelve el contador a ~0): se detecta como reset, se
  re-basa y **no** se suma un delta gigante.
- **Frame Modbus corrupto / lectura basura**: se descarta (validación CRC) y, si aun así
  se colara un salto imposible, la **guardia `max_delta_litros`** lo descarta y re-basa.
  El sistema se auto-recupera en la lectura siguiente.

---

## 4. Setup en la web (lo hace el encargado, una vez)

1. **Credencial**: ya generada y cargada. Si se rota en la web, hay que actualizar
   `user`/`password` en `config.json` del Pi (la contraseña se muestra una sola vez).
2. **Mapear rodeos → lotes**: cada etiqueta de rodeo del device (ej. "Punta", "Frescas",
   "Otros 1") se mapea a un lote del tambo desde la web. Aparece sola al llegar la primera
   lectura, o se agrega a mano. Si un rodeo no está mapeado, **la lectura igual se guarda**
   (aparece como `unmapped`) y no suma a ningún lote hasta que se mapee.

> El device solo manda la **etiqueta** del rodeo; el mapeo a lote vive en la web. Por eso
> los nombres de `config.json` no tienen que coincidir con los del tambo.

---

## 5. Rodeos / botones (hardware)

7 botones + 7 LEDs (un par por rodeo) + 1 botón de power. Fuente de verdad: `config.json`
→ `rodeos_gpio`. Selección exclusiva (se enciende solo el LED del rodeo activo). Un botón
dispara si se mantiene presionado ≥ `button_hold_ms` (default 500 ms).

| Rodeo        | Botón GPIO | LED GPIO |
|--------------|-----------|----------|
| Frescas      | 6         | 21       |
| Punta        | 5         | 20       |
| Vaquillonas  | 1         | 26       |
| Cola         | 0         | 16       |
| Otros 1      | 7         | 19       |
| Otros 2      | 8         | 13       |
| Otros 3      | 11        | 12       |
| Power        | 17        | — (no implementado) |

---

## 6. Reloj del device (crítico)

`fecha` y `turno` salen del reloj del Pi:
- Turno **M** (mañana): hora < 12. Turno **T** (tarde): hora >= 12.
- Reinicio automático a las **00:00** y **12:00** (postea lo pendiente antes de reiniciar).

Si el reloj del Pi está mal, la fecha/turno de los datos sale mal. Asegurar que el Pi tenga
hora correcta (NTP o RTC). Sin conexión, un RTC es lo ideal.

---

## 7. Cómo verificar que anda (POST de prueba)

```bash
python3 - <<'PY'
import json, requests
cfg = json.load(open("config.json"))
r = requests.post(cfg["post_url"],
                  json={"fecha":"2026-01-01","turno":"M","lotes":[]},
                  auth=(cfg["user"], cfg["password"]), timeout=15)
print(r.status_code, r.text)  # esperado: 200 {"...":"...","mapped":0,...}
PY
```

- **200** → auth y endpoint OK.
- **401** → credencial inválida/inactiva → regenerar en la web y actualizar `config.json`.
- **422** → body inválido (revisar formato).
- **5xx** → problema del server → reintentar (el sistema ya reintenta solo).

---

## 8. Cosas a tener en cuenta / pendientes

- Quedó un lote de prueba **`TEST_FIRMWARE` (1 L) en el parte del 2026-07-29 turno T** como
  `unmapped` (no suma a ningún lote real). Se puede borrar/ignorar desde la web; el endpoint
  no tiene borrado por API.
- `config.json` guarda la **credencial en texto plano** y está versionado (decisión tomada).
  Si algún día se quiere sacar del repo: mover a `.gitignore` + dejar `config.example.json`.
- `max_delta_litros = 2000` L por lectura. Si el caudal pico por intervalo se acercara a eso,
  subir el valor para no descartar leche real (hoy hay 3 órdenes de magnitud de margen).
- El botón de power (GPIO 17) está configurado pero **no se monitorea** aún.
- Los registros de alarma del caudalímetro (caño vacío, etc.) no se leen; podrían servir para
  detectar fin de ordeñe.
