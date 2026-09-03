# API de impresión Nexus

El proyecto expone la API FastAPI existente y un worker independiente que consulta
continuamente la tabla `persons` de PostgreSQL. El worker toma registros pendientes,
los imprime en las posiciones 1, 2 y 3 del flujo actual y actualiza su estado.

La secuencia física de la tira avanza desde abajo hacia arriba: posición 1 inferior,
posición 2 central y posición 3 superior. Después se crea una nueva tira.

La configuración de la tira incluye `form_padding_left_mm`, con `6 mm` por defecto,
para separar el contenido del borde izquierdo. Puede ajustarse mediante `PUT /print-layout`
sin desplazar la hoja completa.

Cada posición ocupa `80 mm` de alto. En la tira de `279,4 mm`, esto deja una
separación uniforme de `9,85 mm` antes, entre y después de los tres formularios.

## Preparación

Instala las dependencias:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Aplica una vez la migración [001_persons_print_queue.sql](migrations/001_persons_print_queue.sql)
en la base PostgreSQL. La conexión se debe proporcionar preferentemente mediante una
variable de entorno:

```powershell
$env:POSTGRES_DATABASE_URL = "postgresql://usuario:clave@servidor:5432/base"
```

Por compatibilidad, el worker también reconoce `url_db_postgres` dentro de
`config.json`. No se recomienda versionar credenciales en ese archivo.

## Ejecución

Inicia la API y el worker en procesos separados:

```powershell
.\.venv\Scripts\python.exe app.py
.\.venv\Scripts\python.exe print_worker.py
```

El worker imprime físicamente por defecto. Estas variables permiten configurarlo:

| Variable | Predeterminado | Uso |
| --- | ---: | --- |
| `PRINT_POLL_SECONDS` | `3` | Intervalo de consulta cuando no hay trabajo |
| `PRINT_SIMULATE` | `false` | Registra posiciones sin usar la impresora |
| `PRINT_TRANSPORT` | `windows` | `windows` o `bluetooth` |
| `PRINT_PRINTER_NAME` | vacía | Impresora de Windows; vacía usa la predeterminada |
| `BLUETOOTH_COM_PORT` | vacía | Puerto asignado a la PT-210, por ejemplo `COM7` |
| `BLUETOOTH_BAUDRATE` | `9600` | Velocidad del puerto serie Bluetooth |
| `LOG_LEVEL` | `INFO` | Nivel de los mensajes del worker |

Ejecuta una sola instancia del worker por impresora física.

### Ejecución controlada

El archivo `worker_config.json` controla cuántos registros procesa cada ejecución:

```json
{
  "print_all": false,
  "max_records": 1,
  "record_order": "newest",
  "poll_seconds": 3,
  "simulate": false,
  "transport": "windows",
  "printer_name": "EPSON L3310 Series"
}
```

- `print_all: false` hace que el worker termine al alcanzar `max_records`.
- `max_records: 1` permite probar exactamente un registro.
- `print_all: true` ignora el límite y continúa consultando la base.
- `record_order: "newest"` selecciona primero el último registro creado.
- `record_order: "oldest"` selecciona primero el registro más antiguo.

Aunque se habiliten todos, cada ciclo reclama e imprime solamente un registro.

## Solicitar una impresión

```sql
UPDATE persons
SET pending_to_print = 0,
    print_requested_at = NOW(),
    print_claimed_at = NULL,
    printed_at = NULL,
    print_error = NULL
WHERE id = :person_id;
```

`pending_to_print` es la única señal consultada por el worker:

- `null`: el registro no debe imprimirse.
- `0`: el registro está pendiente.
- `1`: el registro fue reclamado o impreso.

El worker reclama un solo registro por ciclo, cambia `pending_to_print` de `0` a `1` antes
de enviarlo a la impresora y conserva `1` si tiene éxito. Si ocurre un error, vuelve
el campo a `0` y guarda el detalle en `print_error`. `print_status` no participa en
la selección ni en las actualizaciones del worker.

Los datos recuperados de PostgreSQL se imprimen sin validación de correo, teléfono,
longitudes ni campos obligatorios. La validación de Pydantic se conserva únicamente
para las solicitudes recibidas por los endpoints HTTP.
Un registro fallido conserva el detalle en `print_error` y puede reintentarse mediante
el mismo `UPDATE`.

Si el proceso se apaga mientras un registro está en `processing`, debe verificarse si
la hoja salió físicamente antes de regresarlo manualmente a `pending`; hacerlo de forma
automática podría producir una impresión duplicada.

## GOOJPRT PT-210 por Bluetooth

La PT-210 utiliza papel de 57 mm, un ancho imprimible de 48 mm (384 puntos a 203 DPI)
y comandos ESC/POS. El proyecto renderiza el gafete como imagen monocroma para evitar
problemas con tildes o páginas de códigos.

1. Enciende la impresora.
2. En Windows, abre **Configuración > Bluetooth y dispositivos > Agregar dispositivo**.
3. Empareja el dispositivo que aparezca como `PT-210`, `MTP-II` o un nombre parecido.
   Si Windows solicita un PIN, normalmente es `0000`.
4. Verifica el puerto asignado mediante `GET /bluetooth/ports` o desde las opciones
   avanzadas de Bluetooth de Windows.
5. Realiza primero una simulación y luego una impresión real:

```http
POST /print/bluetooth?port=COM7&baudrate=9600&simulate=true
POST /print/bluetooth?port=COM7&baudrate=9600&simulate=false
```

Para que la cola PostgreSQL imprima automáticamente en la térmica:

```powershell
$env:PRINT_TRANSPORT = "bluetooth"
$env:BLUETOOTH_COM_PORT = "COM7"
$env:BLUETOOTH_BAUDRATE = "9600"
.\.venv\Scripts\python.exe print_worker.py
```

Ejecuta solamente un worker por impresora. Si la PT-210 no crea un puerto COM después
de emparejarse, debe habilitarse su servicio **Serial Port / SPP** en Windows; esta
integración usa Bluetooth clásico serial, no una impresora instalada en el spooler.
