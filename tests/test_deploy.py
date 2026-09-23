from pathlib import Path


def test_nginx_protects_operations_and_allows_keyed_queries():
    config = Path("deploy/nginx-NodoPIContadorProducion.conf").read_text("utf-8")

    assert 'auth_basic "NodoPIContador";' in config
    assert "auth_basic_user_file /etc/nginx/NodoPIContadorProducion.htpasswd;" in config
    assert "location = /api/consulta/estado" in config
    assert "location = /api/consulta/minutos" in config
    assert config.count("auth_basic off;") == 2


def test_installer_creates_web_credentials():
    installer = Path("deploy/install.sh").read_text("utf-8")
    service = Path("deploy/NodoPIContadorProducion.service").read_text("utf-8")

    assert "apache2-utils" in installer
    assert "python3-lgpio" in installer
    assert "GPIOZERO_PIN_FACTORY=lgpio" in service
    assert "RuntimeDirectory=NodoPIContadorProducion-gpio" in service
    assert "WorkingDirectory=/run/NodoPIContadorProducion-gpio" in service
    assert installer.index("systemctl stop NodoPIContadorProducion.service") < installer.index('pip" install --force-reinstall')
    assert "restore_app_on_error" in installer
    assert "web-admin-password" in installer
    assert "NodoPIContadorProducion.htpasswd" in installer


def test_lightweight_updater_does_not_modify_system_packages():
    updater = Path("deploy/update-app.sh").read_text("utf-8")
    automatic = Path("deploy/update.sh").read_text("utf-8")

    assert "apt-get" not in updater
    assert updater.index("systemctl stop NodoPIContadorProducion.service") < updater.index('pip" install --force-reinstall')
    assert "systemctl restart NodoPIContadorProducion.service" in updater
    assert '"$REPOSITORY/deploy/update-app.sh"' in automatic
    assert '"$REPOSITORY/deploy/install.sh"' not in automatic


def test_healthcheck_waits_for_the_slow_armv6_startup():
    """Un solo `sleep 3` no alcanza: importar aiohttp en la Zero W tarda ~10 s."""
    updater = Path("deploy/update-app.sh").read_text("utf-8")
    automatic = Path("deploy/update.sh").read_text("utf-8")

    assert "sleep 3" not in updater
    assert "sleep 3" not in automatic
    assert "wait_for_app()" in updater
    assert 'sleep "$HEALTHCHECK_INTERVAL"' in updater
    assert "HEALTHCHECK_ATTEMPTS=${CONTADOR_HEALTHCHECK_ATTEMPTS:-45}" in updater
    assert "HEALTHCHECK_INTERVAL=${CONTADOR_HEALTHCHECK_INTERVAL:-2}" in updater
    # La espera aborta en cuanto el servicio se cae, sin agotar el tiempo total.
    assert "systemctl is-active --quiet NodoPIContadorProducion.service" in updater


def test_deployed_commit_is_recorded_only_after_a_successful_healthcheck():
    """Si el commit se marcara antes, un fallo dejaria el timer reinstalando en bucle."""
    updater = Path("deploy/update-app.sh").read_text("utf-8")
    automatic = Path("deploy/update.sh").read_text("utf-8")

    assert updater.index("wait_for_app\n") < updater.index("deployed-commit")
    assert automatic.index("/api/contador") < automatic.index('> "$DEPLOYED_FILE"')
