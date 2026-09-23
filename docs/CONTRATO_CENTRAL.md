# Contrato con la central

La central es **opcional**. Sin ella el nodo cuenta, guarda y muestra todo
localmente.

## Entrega del conteo (nodo → central)

```
POST {UrlCentral}/api/contador/{NodoId}/conteo
X-Api-Key: {ClaveNodo}
Content-Type: application/json
```

Se envía cada `SegundosSincronizacion` (30 s por defecto):

```json
{
  "nodoId": 7,
  "nodo": "Nodo L1",
  "linea": "Línea 1 - Envasado",
  "estado": { "estado": "PRODUCIENDO", "turno": { "id": 12, "unidades": 1840, "meta": 5000 }, "...": "..." },
  "minutos": [
    { "minuto": "2026-09-23T10:41", "turnoId": 12, "unidades": 58, "pulsos": 58 },
    { "minuto": "2026-09-23T10:42", "turnoId": 12, "unidades": 61, "pulsos": 61 }
  ],
  "turnos": [
    { "id": 12, "inicio": "2026-09-23T06:00:00-03:00", "fin": null, "unidades": 1840, "pulsos": 1840, "motivoCierre": null }
  ]
}
```

- `minuto` es la hora local del nodo, sin segundos (`YYYY-MM-DDTHH:MM`).
- `unidades` es el **valor absoluto** de ese minuto en ese turno, no un
  incremento. La central debe guardar con *upsert* sobre
  `(nodoId, minuto, turnoId)`. Así un reenvío no duplica nada.
- Un minuto puede llegar más de una vez con un valor mayor si seguía sumando
  cuando se entregó.
- Cualquier respuesta 2xx marca los minutos como entregados. Con 4xx/5xx o sin
  red se reintenta en el próximo ciclo; nada se pierde.
- El botón *Probar conexión central* manda `{"nodoId": N, "minutos": [], "prueba": true}`.

## Consulta del nodo (central/SCADA → nodo)

Protegidas con `X-Api-Key: {ClaveConsulta}` y sin autenticación básica:

- `GET /api/consulta/estado`: el mismo estado en vivo que ve el panel.
- `GET /api/consulta/minutos?desde=2026-09-23T06:00`: conteo por minuto desde
  esa hora (por defecto, la última hora; máximo 1 440 filas).
