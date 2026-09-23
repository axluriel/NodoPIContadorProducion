const $ = id => document.getElementById(id);
const txt = (id, value) => { const element = $(id); if (element) element.textContent = value ?? '—'; };
const esc = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
const number = value => Number(value || 0).toLocaleString('es');

async function api(url, options) {
  const response = await fetch(url, {cache: 'no-store', ...options});
  const raw = await response.text();
  let data;
  try {
    data = raw ? JSON.parse(raw) : {};
  } catch {
    const detail = raw.replace(/\s+/g, ' ').trim().slice(0, 160);
    throw new Error(`${url} devolvió una respuesta no JSON (HTTP ${response.status})${detail ? `: ${detail}` : '.'}`);
  }
  if (!response.ok) throw new Error(data.message || `HTTP ${response.status}`);
  return data;
}
const jsonBody = (method, value) => ({method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(value)});

const badge = (id, text, kind) => {
  const element = $(id);
  element.textContent = text;
  element.className = `badge ${kind}${id === 'counter-state' ? ' counter-state' : ''}`;
};

function uptime(seconds) {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor(seconds % 86400 / 3600);
  const minutes = Math.floor(seconds % 3600 / 60);
  return `${days ? days + 'd ' : ''}${hours}h ${minutes}m`;
}
const clock = iso => iso ? new Date(iso).toLocaleTimeString('es', {hour:'2-digit', minute:'2-digit', second:'2-digit'}) : '—';
const stamp = iso => iso ? new Date(iso).toLocaleString('es', {day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit'}) : '—';
const duration = seconds => {
  if (seconds == null) return '—';
  const minutes = Math.floor(seconds / 60);
  return minutes >= 60 ? `${Math.floor(minutes / 60)} h ${minutes % 60} min` : `${minutes} min`;
};

const STATES = {
  PRODUCIENDO: ['PRODUCIENDO', 'good'], PARADA: ['LÍNEA PARADA', 'bad'],
  ESPERANDO: ['ESPERANDO PRIMERA PIEZA', 'warn'], DESHABILITADO: ['CONTADOR DESHABILITADO', 'warn'],
};

let lastConfig = null;

async function refreshCounter() {
  try {
    const data = await api('/api/contador');
    lastConfig = data.configuracion;
    txt('node-name', (data.nodo || 'NODO CONTADOR').toUpperCase());
    txt('line-name', data.linea || 'Contador de producción');
    txt('shift-units', number(data.turno.unidades));
    const [label, kind] = STATES[data.estado] || [data.estado, 'warn'];
    badge('counter-state', data.error ? `ERROR GPIO: ${data.error}` : label, data.error ? 'bad' : kind);
    const target = data.turno.meta;
    $('target-wrap').classList.toggle('hidden', !target);
    if (target) {
      $('target-bar').style.width = `${Math.min(100, data.turno.avance)}%`;
      txt('target-text', `${number(data.turno.unidades)} de ${number(target)} · ${data.turno.avance}%`);
    }
    txt('rate-minute', number(data.ritmo.ultimoMinuto));
    txt('rate-average', data.ritmo.promedio10Min.toLocaleString('es'));
    const detecting = data.sensor.detectando;
    $('sensor-dot').classList.toggle('on', !!detecting);
    txt('sensor-text', detecting == null ? 'Sin configurar' : detecting ? 'Detectando' : 'Libre');
    txt('sensor-info', `BCM${data.sensor.bcm} · pin físico ${data.sensor.fisico ?? '—'} · ${data.sensor.alto == null ? '—' : data.sensor.alto ? 'ALTO' : 'BAJO'}`);
    txt('last-piece', clock(data.ultimaPieza));
    txt('idle-text', data.segundosSinPiezas == null ? 'Sin piezas desde el arranque' : `hace ${Math.round(data.segundosSinPiezas)} s`);
    txt('shift-stop', duration(data.turno.segundosParada));
    txt('stop-since', data.paradaDesde ? `Parada desde ${clock(data.paradaDesde)}` : 'Sin parada en curso');
    txt('total-units', number(data.totalAcumulado));
    txt('filtered', `${number(data.filtrados)} pulsos filtrados`);
    txt('shift-title', `Turno ${data.turno.id} · desde ${stamp(data.turno.inicio)}`);
    const central = data.central;
    if (!central.configurada) badge('central-badge', 'SIN CENTRAL', 'warn');
    else badge('central-badge', central.minutosPendientes ? `CENTRAL · ${central.minutosPendientes} PEND.` : 'CENTRAL AL DÍA',
      central.estado.startsWith('No se pudo') ? 'bad' : 'good');
    $('central-badge').title = central.estado;
  } catch (error) {
    badge('counter-state', error.message, 'bad');
  }
}

async function refreshSystem() {
  try {
    const data = await api('/api/status');
    const system = data.system;
    txt('cpu', `${system.cpuPercent}%`);
    txt('memory', `${system.memoryPercent}%`);
    txt('temperature', system.temperatureC == null ? '—' : `${system.temperatureC} °C`);
    txt('uptime', uptime(system.uptimeSeconds));
    badge('gpio-badge', data.gpio.available ? (data.gpio.mock ? 'GPIO SIMULADO' : 'GPIO OK') : 'GPIO NO DISPONIBLE',
      data.gpio.available ? (data.gpio.mock ? 'warn' : 'good') : 'bad');
    $('simulate').classList.toggle('hidden', !data.gpio.mock);
    fillPins(data.gpio.pins);
  } catch (error) {
    badge('gpio-badge', 'SIN ESTADO', 'bad');
  }
}

let pinsFilled = false;
function fillPins(pins) {
  if (pinsFilled || !pins) return;
  pinsFilled = true;
  const options = pins.map(pin => [pin.bcm, `BCM${pin.bcm} · pin ${pin.physical}`]);
  $('sensor-pin').replaceChildren(...options.map(([value, label]) => new Option(label, value)));
  for (const id of ['target-pin', 'stop-pin'])
    $(id).replaceChildren(new Option('Sin salida', 0), ...options.map(([value, label]) => new Option(label, value)));
}

async function refreshProduction() {
  try {
    const data = await api(`/api/contador/produccion?horas=${$('hours').value}`);
    const hours = data.horas;
    const peak = Math.max(1, ...hours.map(hour => hour.unidades));
    $('bars').innerHTML = hours.map(hour => `<div style="height:${Math.max(2, 100 * hour.unidades / peak)}%" title="${esc(stamp(hour.hora))}: ${number(hour.unidades)} unidades"></div>`).join('')
      || '<p class="muted">Sin producción en el período.</p>';
    txt('bars-from', hours.length ? stamp(hours[0].hora) : '—');
    txt('bars-to', hours.length ? stamp(hours[hours.length - 1].hora) : '—');
    txt('bars-peak', hours.length ? `Máximo ${number(Math.max(...hours.map(hour => hour.unidades)))} u/h` : '—');
    $('shifts').innerHTML = data.turnos.map(shift => `<tr><td>${shift.id}</td><td>${stamp(shift.inicio)}</td><td>${shift.fin ? stamp(shift.fin) : '<strong>En curso</strong>'}</td><td>${number(shift.unidades)}</td><td>${esc(shift.motivoCierre || '')}</td></tr>`).join('');
    $('stops').innerHTML = data.paradas.map(stop => `<tr><td>${stop.turnoId}</td><td>${stamp(stop.desde)}</td><td>${stop.hasta ? stamp(stop.hasta) : '<strong>En curso</strong>'}</td><td>${duration(stop.segundos)}</td></tr>`).join('') || '<tr><td colspan="4">Sin paradas</td></tr>';
  } catch (error) { txt('bars-peak', error.message); }
}
$('hours').onchange = refreshProduction;

$('new-shift').onclick = async () => {
  if (!confirm('¿Cerrar el turno actual y empezar a contar desde cero?')) return;
  try {
    const data = await api('/api/contador/turno', {method:'POST'});
    txt('shift-message', data.message);
    await refreshCounter();
    await refreshProduction();
  } catch (error) { txt('shift-message', error.message); }
};
$('simulate').onclick = async () => {
  try { await api('/api/contador/simular', jsonBody('POST', {pulsos:1})); await refreshCounter(); }
  catch (error) { txt('shift-message', error.message); }
};

function setupDialog(openId, dialogId, closeIds) {
  $(openId).onclick = () => $(dialogId).showModal();
  for (const id of closeIds) $(id).onclick = () => $(dialogId).close();
}
setupDialog('open-config', 'config-dialog', ['close-config']);
setupDialog('open-network-config', 'network-dialog', ['close-network-config']);

$('open-config').addEventListener('click', () => {
  const c = lastConfig;
  if (!c) return;
  $('enabled').checked = c.Habilitado;
  $('node').value = c.NombreNodo;
  $('line').value = c.NombreLinea;
  $('sensor-pin').value = c.PinSensor;
  $('input-mode').value = c.ModoEntrada;
  $('active-high').value = String(c.ActivoEnAlto);
  $('units').value = c.UnidadesPorPulso;
  $('debounce').value = c.AntirreboteMs;
  $('min-pulse').value = c.PulsoMinimoMs;
  $('stop-seconds').value = c.SegundosParada;
  $('target').value = c.MetaTurno;
  $('target-pin').value = c.PinSalidaMeta;
  $('stop-pin').value = c.PinSalidaParada;
  $('shifts-input').value = (c.InicioTurnos || []).join(', ');
  $('central-url').value = c.UrlCentral;
  $('node-id').value = c.NodoId;
  $('sync-seconds').value = c.SegundosSincronizacion;
  $('central-key').value = c.ClaveNodo;
  $('query-key').value = c.ClaveConsulta;
  txt('settings-message', '');
});

function formSettings() {
  return {
    Habilitado: $('enabled').checked, NombreNodo: $('node').value, NombreLinea: $('line').value,
    PinSensor: Number($('sensor-pin').value), ModoEntrada: $('input-mode').value,
    ActivoEnAlto: $('active-high').value === 'true', UnidadesPorPulso: Number($('units').value),
    AntirreboteMs: Number($('debounce').value), PulsoMinimoMs: Number($('min-pulse').value),
    SegundosParada: Number($('stop-seconds').value), MetaTurno: Number($('target').value),
    PinSalidaMeta: Number($('target-pin').value), PinSalidaParada: Number($('stop-pin').value),
    InicioTurnos: $('shifts-input').value.split(/[,;\s]+/).map(value => value.trim()).filter(Boolean),
    UrlCentral: $('central-url').value, NodoId: Number($('node-id').value),
    SegundosSincronizacion: Number($('sync-seconds').value),
    ClaveNodo: $('central-key').value, ClaveConsulta: $('query-key').value,
  };
}

$('settings-form').onsubmit = async event => {
  event.preventDefault();
  try {
    const data = await api('/api/contador/configuracion', jsonBody('PUT', formSettings()));
    txt('settings-message', data.message);
    await refreshCounter();
  } catch (error) { txt('settings-message', error.message); }
};

$('test-central').onclick = async () => {
  txt('settings-message', 'Probando la central…');
  try {
    const data = await api('/api/contador/central/probar', jsonBody('POST', formSettings()));
    $('settings-message').innerHTML = `<strong class="${data.success ? 'test-ok' : 'test-error'}">${esc(data.message)}</strong><ul>${
      data.checks.map(check => `<li class="${check.ok ? 'test-ok' : 'test-error'}">${esc(check.name)}: ${esc(check.detail)}</li>`).join('')}</ul>`;
  } catch (error) { txt('settings-message', error.message); }
};

let networkToken = '';
async function networkApi(url, options = {}) {
  return api(url, {...options, headers:{...(options.headers || {}), 'X-Network-Token':networkToken}});
}

function networkLines(id) {
  return $(id).value.split(/\r?\n|,/).map(value => value.trim()).filter(Boolean);
}

function renderNetworkDevices(devices) {
  $('network-devices').replaceChildren(...devices.map(device => {
    const card = document.createElement('article');
    card.className = 'network-device';
    const heading = document.createElement('div');
    heading.className = 'heading';
    const name = document.createElement('strong');
    name.textContent = device.name;
    const state = document.createElement('span');
    state.className = `badge ${String(device.state || '').startsWith('100') ? 'good' : 'warn'}`;
    state.textContent = device.state || 'Sin estado';
    heading.append(name, state);
    const connection = document.createElement('span');
    connection.textContent = device.connection || 'Sin conexión';
    const addresses = document.createElement('code');
    addresses.textContent = (device.addresses || []).join(' · ') || 'Sin IPv4';
    const mac = document.createElement('small');
    mac.textContent = `${device.type || 'interfaz'} · ${device.mac || 'sin MAC'}${device.gateway ? ` · gateway ${device.gateway}` : ''}`;
    card.append(heading, connection, addresses, mac);
    return card;
  }));
}

async function refreshNetwork() {
  if (!networkToken) return;
  try {
    const data = await networkApi('/api/network/status');
    renderNetworkDevices(data.devices || []);
    badge('network-badge', 'ADMINISTRACIÓN ACTIVA', 'good');
    txt('network-message', data.pending ? `Cambio pendiente en ${data.pending.interface}.` : 'Estado de red actualizado.');
  } catch (error) {
    badge('network-badge', 'BLOQUEADO', 'bad');
    txt('network-message', error.message);
    $('network-controls').classList.add('hidden');
  }
}

$('network-unlock').onclick = async () => {
  networkToken = $('network-token').value.trim();
  if (!networkToken) { txt('network-message', 'Escribe el token administrativo.'); return; }
  try {
    const data = await networkApi('/api/network/status');
    $('network-controls').classList.remove('hidden');
    $('network-token').value = '';
    renderNetworkDevices(data.devices || []);
    badge('network-badge', 'ADMINISTRACIÓN ACTIVA', 'good');
    txt('network-message', 'Acceso administrativo habilitado para esta pestaña.');
  } catch (error) {
    networkToken = '';
    badge('network-badge', 'BLOQUEADO', 'bad');
    txt('network-message', error.message);
  }
};
$('network-refresh').onclick = refreshNetwork;

$('wifi-scan').onclick = async () => {
  txt('network-message', 'Buscando redes Wi-Fi…');
  try {
    const data = await networkApi('/api/network/scan', {method:'POST'});
    $('wifi-list').replaceChildren(new Option('Selecciona o escribe el SSID…', ''), ...(data.networks || []).map(network => new Option(`${network.active ? '✓ ' : ''}${network.ssid} · ${network.signal}% · ${network.security}`, network.ssid)));
    txt('network-message', `${(data.networks || []).length} redes encontradas.`);
  } catch (error) { txt('network-message', error.message); }
};
$('wifi-list').onchange = () => { if ($('wifi-list').value) $('wifi-ssid').value = $('wifi-list').value; };

async function confirmNetworkChange(data) {
  txt('network-message', `${data.message} Confirmando…`);
  const confirmation = await networkApi('/api/network/confirm', jsonBody('POST', {changeId:data.changeId}));
  txt('network-message', confirmation.message);
  await refreshNetwork();
}

$('wifi-form').onsubmit = async event => {
  event.preventDefault();
  if (!confirm(`¿Cambiar la conexión Wi-Fi a "${$('wifi-ssid').value}"? Si falla, se restaurará la red anterior en 60 segundos.`)) return;
  txt('network-message', 'Aplicando la nueva red Wi-Fi…');
  try {
    const data = await networkApi('/api/network/wifi', jsonBody('POST', {ssid:$('wifi-ssid').value, password:$('wifi-password').value, hidden:$('wifi-hidden').checked}));
    $('wifi-password').value = '';
    await confirmNetworkChange(data);
  } catch (error) { txt('network-message', `${error.message} Si se perdió la conexión, espera 60 segundos para la reversión.`); }
};

const updateEthernetFields = () => $('eth-manual').classList.toggle('hidden', $('eth-method').value !== 'manual');
$('eth-method').onchange = updateEthernetFields;
updateEthernetFields();
$('ethernet-form').onsubmit = async event => {
  event.preventDefault();
  if (!confirm('¿Aplicar esta configuración en eth0? Si se pierde la conexión, se restaurará el perfil anterior en 60 segundos.')) return;
  txt('network-message', 'Aplicando la configuración Ethernet…');
  try {
    const data = await networkApi('/api/network/ethernet', jsonBody('PUT', {method:$('eth-method').value, addresses:networkLines('eth-addresses'), gateway:$('eth-gateway').value, dns:networkLines('eth-dns')}));
    await confirmNetworkChange(data);
  } catch (error) { txt('network-message', `${error.message} Si se perdió la conexión, espera 60 segundos para la reversión.`); }
};

// El contador se consulta cada segundo: es una lectura en memoria y es lo que
// mira el operario. El resto cambia despacio y no vale la pena cargar la Zero W.
refreshSystem().then(refreshCounter);
refreshProduction();
setInterval(refreshCounter, 1000);
setInterval(refreshSystem, 10000);
setInterval(refreshProduction, 60000);
