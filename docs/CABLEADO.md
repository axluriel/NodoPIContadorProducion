# Cableado del sensor fotoeléctrico

> **Los GPIO de la Raspberry Pi son de 3,3 V.** Aplicarles 5, 12 o 24 V los
> destruye (y puede dañar la placa entera). Los sensores industriales trabajan a
> 12–24 V: siempre se conectan a través de un **optoacoplador** (por ejemplo un
> PC817 o un módulo de optoacoplador de 1 canal para 24 V).

## Pines

| Señal | GPIO (BCM) | Pin físico |
|---|---|---|
| Sensor (predeterminado) | BCM17 | 11 |
| GND de la Pi | — | 6, 9, 14, 20, 25, 30, 34, 39 |
| 3,3 V de la Pi | — | 1, 17 |

Cualquier GPIO de la lista del panel sirve; se evitan los que usan I²C, SPI y
UART.

## Sensor NPN (el más común)

Un sensor NPN *hunde* la salida a 0 V del sensor cuando detecta.

```
Fuente 24 V (+) ──┬───────────────► Sensor marrón (+V)
                  │
                  └─[ 2,2 kΩ ]──► Opto ánodo (1)
Sensor negro (salida NPN) ──────► Opto cátodo (2)
Fuente 24 V (−) ────────────────► Sensor azul (0 V)

Opto colector (4) ──────────────► GPIO17 (pin 11)
Opto emisor (3)   ──────────────► GND de la Pi (pin 9)
```

Al detectar, el LED del opto se enciende, el transistor conduce y **lleva el
GPIO a 0 V**. En el panel:

- **Modo de entrada:** Pull-up interno.
- **Nivel que indica pieza:** Bajo (0 V).

## Sensor PNP

Un sensor PNP *entrega* +V en la salida cuando detecta.

```
Sensor negro (salida PNP) ─[ 2,2 kΩ ]─► Opto ánodo (1)
Fuente 24 V (−) ──────────────────────► Opto cátodo (2) y sensor azul (0 V)
Fuente 24 V (+) ──────────────────────► Sensor marrón (+V)

Opto colector (4) ──► GPIO17 (pin 11)
Opto emisor (3)   ──► GND de la Pi
```

La salida del opto es igual que en el caso NPN: **Pull-up interno** y **nivel
Bajo** indica pieza.

## Luz o sombra (Light-ON / Dark-ON)

Muchos sensores tienen un selector *L.ON / D.ON*. Si el panel marca
«Detectando» cuando **no** hay pieza, cambie el selector del sensor o invierta
**Nivel que indica pieza** en el panel.

## Salidas (meta y parada)

Las salidas entregan 3,3 V y pocos miliamperios: una baliza o torre de luces
necesita un **módulo de relé** con optoacoplador o un transistor intermedio.

## Cómo ajustar los filtros

1. Deje **Pulso mínimo = 0** y **Antirrebote = 50 ms** y haga pasar piezas.
2. Si cuenta de más (una caja cuenta 2), suba el **antirrebote** a algo menor que
   el tiempo entre dos piezas reales. Ejemplo: a 120 piezas/min hay 500 ms entre
   piezas; 200–300 ms es un buen valor.
3. Si aparecen conteos sin piezas (destellos, vibración), suba el **pulso
   mínimo** a 5–20 ms.
4. El panel muestra **pulsos filtrados**: si crece mucho con la línea andando
   normal, los filtros están demasiado altos.

La Zero W con Python atiende sin problema algunas decenas de piezas por
segundo. Para velocidades mayores conviene un contador por hardware o un PLC.
