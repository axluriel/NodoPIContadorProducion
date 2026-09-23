#!/bin/sh
set -eu

REPOSITORY_FILE=/etc/NodoPIContador/update-repository
DEPLOYED_FILE=/var/lib/NodoPIContador/deployed-commit
LOCK_FILE=/run/lock/nodocontador-update.lock

if [ ! -s "$REPOSITORY_FILE" ]; then
  echo "No existe $REPOSITORY_FILE; ejecute deploy/install.sh." >&2
  exit 1
fi

REPOSITORY=$(sed -n '1p' "$REPOSITORY_FILE")
case "$REPOSITORY" in
  /*) ;;
  *) echo "La ruta del repositorio no es absoluta: $REPOSITORY" >&2; exit 1 ;;
esac

if [ ! -d "$REPOSITORY/.git" ]; then
  echo "No existe un repositorio Git en $REPOSITORY." >&2
  exit 1
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Ya hay una actualización de NodoPIContador en curso."
  exit 0
fi

REPOSITORY_OWNER=$(stat -c %U "$REPOSITORY")
OWNER_HOME=$(getent passwd "$REPOSITORY_OWNER" | cut -d: -f6)
if [ -z "$OWNER_HOME" ]; then
  echo "No se encontró el HOME de $REPOSITORY_OWNER." >&2
  exit 1
fi

as_owner() {
  runuser -u "$REPOSITORY_OWNER" -- env HOME="$OWNER_HOME" "$@"
}

if ! as_owner git -C "$REPOSITORY" diff --quiet --ignore-submodules -- || \
   ! as_owner git -C "$REPOSITORY" diff --cached --quiet --ignore-submodules --; then
  echo "Actualización omitida: hay cambios locales rastreados en $REPOSITORY." >&2
  exit 1
fi

as_owner git -C "$REPOSITORY" fetch --quiet origin main
REMOTE_COMMIT=$(as_owner git -C "$REPOSITORY" rev-parse origin/main)
DEPLOYED_COMMIT=$(sed -n '1p' "$DEPLOYED_FILE" 2>/dev/null || true)

if [ "$REMOTE_COMMIT" = "$DEPLOYED_COMMIT" ]; then
  echo "NodoPIContador ya está desplegado en $REMOTE_COMMIT."
  exit 0
fi

# La acción de GitHub debe terminar satisfactoriamente antes de desplegar.
CHECK_RESULT=$(as_owner gh api \
  "repos/axluriel/NodoPIContadorProducion/commits/$REMOTE_COMMIT/check-runs" \
  --jq '[.check_runs[] | select(.name == "Pruebas Python")][0].conclusion // "pending"' \
  2>/dev/null || printf '%s' unavailable)
if [ "$CHECK_RESULT" != success ]; then
  echo "Commit $REMOTE_COMMIT aún no aprobado por GitHub Actions ($CHECK_RESULT)."
  exit 0
fi

LOCAL_COMMIT=$(as_owner git -C "$REPOSITORY" rev-parse HEAD)
if [ "$LOCAL_COMMIT" != "$REMOTE_COMMIT" ]; then
  as_owner git -C "$REPOSITORY" merge --ff-only "$REMOTE_COMMIT"
fi

# update-app.sh solo retorna cuando /api/contador ya respondio, por lo que
# aqui basta con reconfirmar el estado sin volver a esperar el arranque.
"$REPOSITORY/deploy/update-app.sh"
systemctl is-active --quiet NodoPIContadorProducion.service
systemctl is-active --quiet nginx.service
curl --fail --silent --show-error --max-time 10 \
  http://127.0.0.1:5000/api/contador >/dev/null

printf '%s\n' "$REMOTE_COMMIT" > "$DEPLOYED_FILE"
chown nodocontador:nodocontador-netadmin "$DEPLOYED_FILE"
chmod 0640 "$DEPLOYED_FILE"
echo "NodoPIContador actualizado correctamente a $REMOTE_COMMIT."
