#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Ejecute este instalador con sudo." >&2
  exit 1
fi

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
APP_DIR=/opt/NodoPIContadorProducion

apt-get update
apt-get install -y python3-venv python3-aiohttp python3-gpiozero python3-lgpio nginx apache2-utils network-manager polkitd openssl curl gh
install -d -m 0755 "$APP_DIR"

# NodoPIContador ocupa el servidor predeterminado del puerto 80. El paquete nginx
# crea este enlace en una instalación nueva y provocaría dos default_server.
rm -f /etc/nginx/sites-enabled/default

if ! getent group nodocontador-netadmin >/dev/null; then
  groupadd --system nodocontador-netadmin
fi
if ! getent passwd nodocontador >/dev/null; then
  useradd --system --gid nodocontador-netadmin --home-dir /var/lib/NodoPIContador --shell /usr/sbin/nologin nodocontador
fi
if getent group gpio >/dev/null; then
  usermod -a -G gpio nodocontador
fi

# Repara instalaciones antiguas donde systemd creó el directorio persistente
# con otro propietario (por ejemplo, cuando se utilizaba DynamicUser).
install -d -o nodocontador -g nodocontador-netadmin -m 0750 /var/lib/NodoPIContador
chown -R nodocontador:nodocontador-netadmin /var/lib/NodoPIContador
if [ -f /var/lib/NodoPIContador/contador-settings.json ]; then
  chmod 0640 /var/lib/NodoPIContador/contador-settings.json
fi

install -d -m 0700 /etc/NodoPIContador
if [ ! -s /etc/NodoPIContador/network-admin-token ]; then
  umask 077
  openssl rand -hex 24 > /etc/NodoPIContador/network-admin-token
fi
chmod 0600 /etc/NodoPIContador/network-admin-token
# El umask de los secretos no debe dejar el entorno virtual inaccesible para
# el usuario de servicio en una instalación nueva.
umask 022

if [ ! -s /etc/nginx/NodoPIContadorProducion.htpasswd ] || [ ! -s /etc/NodoPIContador/web-admin-password ]; then
  WEB_PASSWORD=$(openssl rand -hex 12)
  printf '%s\n' "$WEB_PASSWORD" > /etc/NodoPIContador/web-admin-password
  printf '%s\n' "$WEB_PASSWORD" | htpasswd -iB -c /etc/nginx/NodoPIContadorProducion.htpasswd axel
fi
chmod 0600 /etc/NodoPIContador/web-admin-password
chown root:www-data /etc/nginx/NodoPIContadorProducion.htpasswd
chmod 0640 /etc/nginx/NodoPIContadorProducion.htpasswd

install -m 0644 "$PROJECT_DIR/deploy/49-nodocontador-network.rules" /etc/polkit-1/rules.d/49-nodocontador-network.rules

# No se deben reemplazar módulos dentro del entorno virtual mientras el proceso
# los está ejecutando. Se detiene solamente la aplicación justo antes del pip;
# nginx permanece arriba y volverá a responder al finalizar. Si algo falla, el
# trap intenta recuperar la versión que ya estaba instalada.
APP_WAS_ACTIVE=0
if systemctl is-active --quiet NodoPIContadorProducion.service 2>/dev/null; then
  APP_WAS_ACTIVE=1
  systemctl stop NodoPIContadorProducion.service
fi
restore_app_on_error() {
  status=$?
  trap - EXIT
  if [ "$status" -ne 0 ] && [ "$APP_WAS_ACTIVE" -eq 1 ]; then
    systemctl start NodoPIContadorProducion.service 2>/dev/null || true
  fi
  exit "$status"
}
trap restore_app_on_error EXIT

if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  python3 -m venv --system-site-packages "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --force-reinstall --no-deps "$PROJECT_DIR"

install -m 0644 "$PROJECT_DIR/deploy/NodoPIContadorProducion.service" /etc/systemd/system/NodoPIContadorProducion.service
install -m 0644 "$PROJECT_DIR/deploy/nginx-NodoPIContadorProducion.conf" /etc/nginx/sites-available/NodoPIContadorProducion
ln -sfn /etc/nginx/sites-available/NodoPIContadorProducion /etc/nginx/sites-enabled/NodoPIContadorProducion
install -m 0755 "$PROJECT_DIR/deploy/update.sh" /usr/local/sbin/nodocontador-update
install -m 0644 "$PROJECT_DIR/deploy/NodoPIContadorProducion-update.service" /etc/systemd/system/NodoPIContadorProducion-update.service
install -m 0644 "$PROJECT_DIR/deploy/NodoPIContadorProducion-update.timer" /etc/systemd/system/NodoPIContadorProducion-update.timer
printf '%s\n' "$PROJECT_DIR" > /etc/NodoPIContador/update-repository

if [ ! -s /var/lib/NodoPIContador/deployed-commit ] && \
   git -c safe.directory="$PROJECT_DIR" -C "$PROJECT_DIR" rev-parse --verify HEAD >/dev/null 2>&1; then
  git -c safe.directory="$PROJECT_DIR" -C "$PROJECT_DIR" rev-parse HEAD > /var/lib/NodoPIContador/deployed-commit
  chown nodocontador:nodocontador-netadmin /var/lib/NodoPIContador/deployed-commit
  chmod 0640 /var/lib/NodoPIContador/deployed-commit
fi

systemctl daemon-reload
systemctl enable NodoPIContadorProducion.service nginx.service NodoPIContadorProducion-update.timer
nginx -t
systemctl restart NodoPIContadorProducion.service nginx.service
systemctl restart NodoPIContadorProducion-update.timer
trap - EXIT

echo "NodoPIContadorProducion instalado. Abra http://$(hostname -I | awk '{print $1}')/"
echo "Usuario web: axel"
echo "Contraseña web: sudo cat /etc/NodoPIContador/web-admin-password"
echo "Token de administración de red: sudo cat /etc/NodoPIContador/network-admin-token"
