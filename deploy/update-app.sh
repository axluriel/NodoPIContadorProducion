#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Ejecute esta actualización con sudo." >&2
  exit 1
fi

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
APP_DIR=/opt/NodoPIContadorProducion

if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  echo "NodoPIContador no está instalado; ejecute primero sudo ./deploy/install.sh." >&2
  exit 1
fi

HEALTHCHECK_ATTEMPTS=${CONTADOR_HEALTHCHECK_ATTEMPTS:-45}
HEALTHCHECK_INTERVAL=${CONTADOR_HEALTHCHECK_INTERVAL:-2}

APP_WAS_ACTIVE=0
if systemctl is-active --quiet NodoPIContadorProducion.service 2>/dev/null; then
  APP_WAS_ACTIVE=1
  systemctl stop NodoPIContadorProducion.service
fi

# El arranque en la Zero W (ARMv6) tarda cerca de diez segundos solo en importar
# Python y aiohttp, asi que la verificacion debe reintentar en vez de comprobar
# una unica vez. Si el servicio se cae durante la espera se aborta de inmediato.
wait_for_app() {
  attempt=0
  while [ "$attempt" -lt "$HEALTHCHECK_ATTEMPTS" ]; do
    if ! systemctl is-active --quiet NodoPIContadorProducion.service; then
      echo "NodoPIContadorProducion.service dejo de estar activo durante la verificacion." >&2
      return 1
    fi
    if curl --fail --silent --show-error --max-time 5 \
        http://127.0.0.1:5000/api/contador >/dev/null 2>&1; then
      return 0
    fi
    attempt=$((attempt + 1))
    sleep "$HEALTHCHECK_INTERVAL"
  done
  echo "La aplicacion no respondio en /api/contador tras $((HEALTHCHECK_ATTEMPTS * HEALTHCHECK_INTERVAL)) s." >&2
  return 1
}

restore_app_on_error() {
  status=$?
  trap - EXIT
  if [ "$status" -ne 0 ] && [ "$APP_WAS_ACTIVE" -eq 1 ]; then
    systemctl start NodoPIContadorProducion.service 2>/dev/null || true
  fi
  exit "$status"
}
trap restore_app_on_error EXIT

# Las dependencias del sistema ya fueron preparadas por install.sh. Esta ruta
# únicamente reemplaza el paquete de la aplicación y sus archivos de despliegue.
"$APP_DIR/.venv/bin/pip" install --force-reinstall --no-deps "$PROJECT_DIR"

install -m 0644 "$PROJECT_DIR/deploy/NodoPIContadorProducion.service" /etc/systemd/system/NodoPIContadorProducion.service
install -m 0644 "$PROJECT_DIR/deploy/nginx-NodoPIContadorProducion.conf" /etc/nginx/sites-available/NodoPIContadorProducion
ln -sfn /etc/nginx/sites-available/NodoPIContadorProducion /etc/nginx/sites-enabled/NodoPIContadorProducion
install -m 0755 "$PROJECT_DIR/deploy/update.sh" /usr/local/sbin/nodocontador-update
install -m 0644 "$PROJECT_DIR/deploy/NodoPIContadorProducion-update.service" /etc/systemd/system/NodoPIContadorProducion-update.service
install -m 0644 "$PROJECT_DIR/deploy/NodoPIContadorProducion-update.timer" /etc/systemd/system/NodoPIContadorProducion-update.timer

systemctl daemon-reload
nginx -t
systemctl restart NodoPIContadorProducion.service
systemctl reload nginx.service

wait_for_app

if git -c safe.directory="$PROJECT_DIR" -C "$PROJECT_DIR" rev-parse --verify HEAD >/dev/null 2>&1; then
  git -c safe.directory="$PROJECT_DIR" -C "$PROJECT_DIR" rev-parse HEAD > /var/lib/NodoPIContador/deployed-commit
  chown nodocontador:nodocontador-netadmin /var/lib/NodoPIContador/deployed-commit
  chmod 0640 /var/lib/NodoPIContador/deployed-commit
fi

trap - EXIT
echo "Aplicación NodoPIContador actualizada sin modificar paquetes del sistema."
