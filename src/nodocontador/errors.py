from __future__ import annotations


def reason(exc: BaseException) -> str:
    """Texto útil de una excepción.

    TimeoutError y varios errores de socket llegan con str() vacío. Formatearlos
    tal cual dejaba mensajes como «SPH monitor:» o «No se pudo escribir la línea
    en la máquina:», que no distinguen un timeout de una conexión rechazada ni
    de un nombre que no resuelve. Con el nombre de la clase al menos se sabe qué
    modo de fallo fue.
    """
    return str(exc).strip() or type(exc).__name__
