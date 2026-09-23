# NodoPIContadorProducion

Contador de producción para **Raspberry Pi Zero W** (ARMv6, 512 MB). Cuenta las
piezas que cortan el haz de un **sensor fotoeléctrico** conectado a una entrada
digital y muestra en un panel web el conteo del turno, el ritmo, las paradas y
la producción por hora.

Nace de [`SphPiNodoPython`](https://github.com/axluriel/SphPiNodoPython): conserva
su forma de instalarse (un proceso `systemd` detrás de Nginx con autenticación
básica), la actualización automática validada por GitHub Actions, la
administración de red Wi-Fi/Ethernet con reversión y el manejo de GPIO con
`gpiozero` + `lgpio`. Todo lo propio de la codificadora SPH se reemplazó por el
contador.

## Qué hace

- **Cuenta piezas** en un GPIO (BCM17 / pin físico 11 de forma predeterminada).
- **Filtra** falsos pulsos:
  - *Antirrebote / separación mínima*: tiempo mínimo entre dos piezas. Una caja
    con asas que corta el haz dos veces seguidas cuenta una sola vez.
  - *Pulso mínimo*: si es mayor que cero, la pieza se cuenta al liberar el haz y
    solo si estuvo cortado al menos ese tiempo. Descarta destellos, polvo o
    vibración.
- **Unidades por pulso**: si el sensor ve cajas de 12 unidades, cada pulso suma 12.
- **Turnos**: se cierran solos en los horarios configurados (por ejemplo
  `06:00, 14:00, 22:00`) o a mano desde el panel. Cerrar un turno reinicia el
  contador del turno pero no el total acumulado.
- **Paradas**: si pasan `SegundosParada` sin piezas, la línea se marca parada y
  se registra desde cuándo; al volver a producir se cierra con su duración.
- **Meta por turno** con barra de avance.
- **Salidas digitales opcionales**: una se enciende al cumplir la meta y otra
  mientras la línea está parada (para una baliza o torre de luces).
- **Ritmo**: unidades del último minuto y promedio de los últimos diez.
- **Central opcional**: entrega el conteo por minuto a una aplicación central.
  Ver [`docs/CONTRATO_CENTRAL.md`](docs/CONTRATO_CENTRAL.md).

## Cableado del sensor

Los GPIO de la Pi son de **3,3 V** y no toleran 12–24 V. Un sensor fotoeléctrico
industrial **no se conecta directo**: vea [`docs/CABLEADO.md`](docs/CABLEADO.md)
para los esquemas con optoacoplador (NPN y PNP) y cómo elegir el modo de
entrada y el nivel activo en el panel.

## Por qué por minuto y no por pieza

Las piezas se suman en memoria en el hilo de GPIO y el bucle principal las
vuelca a SQLite **cada cinco segundos**, agrupadas **por minuto**. Una línea a
60 piezas por minuto serían 86 400 escrituras diarias sobre la tarjeta SD; por
minuto son 1 440 filas al día. Un corte de luz pierde como mucho los últimos
cinco segundos.

La central recibe valores **absolutos** por minuto, así que reenviar un minuto
es inofensivo: si se corta la red, el nodo sigue contando y entrega todo lo
pendiente cuando vuelve.

## Desarrollo

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
CONTADOR_GPIO_MOCK=1 nodocontador
```

Con `CONTADOR_GPIO_MOCK=1` el panel muestra un botón **Simular pieza**. La
aplicación escucha en `http://127.0.0.1:5000`.

```bash
CONTADOR_GPIO_MOCK=1 python -m pytest -q
```

## Variables de entorno

- `CONTADOR_DATA_DIR`: directorio persistente; por defecto `/var/lib/NodoPIContador`.
- `CONTADOR_HOST`: interfaz HTTP; por defecto `127.0.0.1`.
- `CONTADOR_PORT`: puerto HTTP; por defecto `5000`.
- `CONTADOR_GPIO_MOCK=1`: GPIO simulado para desarrollo y pruebas.
- `CONTADOR_LOG_LEVEL`: nivel de registro; por defecto `INFO`.

## Configuración

Se edita en el panel (**Sensor, turnos y central → Modificar parámetros**) y se
guarda en `/var/lib/NodoPIContador/contador-settings.json`:

| Campo | Predeterminado | Descripción |
|---|---|---|
| `Habilitado` | `false` | Activa el conteo y los GPIO. |
| `NombreNodo`, `NombreLinea` | | Se muestran en el panel y viajan a la central. |
| `PinSensor` | `17` | GPIO BCM del sensor. |
| `ModoEntrada` | `InputPullUp` | `InputPullUp`, `InputPullDown` o `Input`. |
| `ActivoEnAlto` | `false` | Nivel eléctrico que significa «hay pieza». |
| `AntirreboteMs` | `50` | Separación mínima entre piezas. |
| `PulsoMinimoMs` | `0` | Ancho mínimo del pulso (0 = sin filtro). |
| `UnidadesPorPulso` | `1` | Unidades que suma cada pieza detectada. |
| `SegundosParada` | `60` | Tiempo sin piezas para marcar la línea parada. |
| `MetaTurno` | `0` | Meta por turno (0 = sin meta). |
| `PinSalidaMeta`, `PinSalidaParada` | `0` | Salidas opcionales (0 = sin salida). |
| `InicioTurnos` | `06:00, 14:00, 22:00` | Cierres automáticos; vacío = solo manual. |
| `UrlCentral`, `NodoId`, `ClaveNodo` | | Entrega opcional a la central. |
| `ClaveConsulta` | | Clave `X-Api-Key` de las rutas de consulta externa. |
| `SegundosSincronizacion` | `30` | Cada cuánto se entrega a la central. |

Las claves nunca vuelven al navegador: el panel las recibe enmascaradas y
guardar la máscara conserva el valor anterior.

## API

| Método | Ruta | Uso |
|---|---|---|
| `GET` | `/api/contador` | Estado en vivo (turno, ritmo, sensor, central). |
| `GET` | `/api/contador/produccion?horas=24` | Unidades por hora, turnos y paradas. |
| `POST` | `/api/contador/turno` | Cierra el turno y abre otro. |
| `PUT` | `/api/contador/configuracion` | Guarda la configuración. |
| `POST` | `/api/contador/central/probar` | Diagnóstico de la central. |
| `POST` | `/api/contador/simular` | Suma pulsos (solo con GPIO simulado). |
| `GET` | `/api/consulta/estado` | Estado, con `X-Api-Key: <ClaveConsulta>`. |
| `GET` | `/api/consulta/minutos?desde=2026-09-23T06:00` | Conteo por minuto, con `X-Api-Key`. |
| `GET` | `/api/status` | CPU, memoria, temperatura y GPIO. |

Las rutas `/api/consulta/*` no usan la autenticación básica de Nginx (para que
las consulten una central, un SCADA o Power BI) pero exigen `X-Api-Key`.

## Raspberry Pi Zero W

Raspberry Pi OS Lite de 32 bits. Para no compilar nada en el ARMv6 se usan los
paquetes del sistema dentro del entorno virtual:

```bash
git clone https://github.com/axluriel/NodoPIContadorProducion.git
cd NodoPIContadorProducion
sudo ./deploy/install.sh
```

El instalador crea el usuario `nodocontador`, el servicio
`NodoPIContadorProducion.service`, el sitio Nginx en el puerto 80 y el
temporizador de actualización. Al terminar muestra la URL. El usuario web es
`axel` y la contraseña se consulta con:

```bash
sudo cat /etc/NodoPIContador/web-admin-password
sudo cat /etc/NodoPIContador/network-admin-token   # token para el panel de red
```

### Actualizaciones automáticas

`NodoPIContadorProducion-update.timer` consulta `origin/main` cada dos minutos
y solo despliega un commit cuando la acción **Pruebas Python** de GitHub terminó
bien. Usa la sesión de GitHub CLI (`gh auth login`) del dueño del clon.

```bash
systemctl list-timers NodoPIContadorProducion-update.timer
sudo journalctl -u NodoPIContadorProducion-update.service --no-pager -n 50
```

Actualización manual solo de la aplicación:

```bash
git pull --ff-only
sudo ./deploy/update-app.sh
```

### Registros

```bash
sudo journalctl -u NodoPIContadorProducion.service -f
```

## Administración de red

Igual que en `SphPiNodoPython`: el panel permite buscar y conectar redes Wi-Fi y
configurar `eth0` con DHCP o IPv4 manual. Los cambios se revierten solos si el
navegador no los confirma en 60 segundos.
