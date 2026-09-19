"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const fmt = new Intl.NumberFormat('fr-FR');

// Le serveur écrit des horodatages naïfs (heure de Paris, sans offset) : le
// navigateur les interpréterait dans SON fuseau, ce qui décale l'affichage de
// 5-6 h depuis la Thaïlande. Un horodatage naïf est donc figé tel quel ; un
// horodatage porteur d'un offset (ou une date seule) est ramené à Paris / UTC.
const _hasTZ = (s) => /(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(s);
const _asDate = (s) => new Date(s.includes('T') && !_hasTZ(s) ? s + 'Z' : s);
const _tzOpts = (s) => ({ timeZone: _hasTZ(s) ? 'Europe/Paris' : 'UTC' });
const dateFmt = (s) => s
  ? _asDate(String(s)).toLocaleDateString('fr-FR', _tzOpts(String(s))) : '—';
const dateTimeFmt = (s) => s
  ? _asDate(String(s)).toLocaleString('fr-FR', _tzOpts(String(s))) : '—';
// Libellés des compagnies interrogées par les sondes. Déclaré ici, avec
// les autres helpers, parce que renderAlerts s'en sert bien avant la
// section admin où il vivait.
const CARRIER_LABEL = { AF: 'Air France', KL: 'KLM' };
const sourceLabel = (s) => {
  if (!s) return '—';
  if (s === 'duffel') return 'Compagnies';
  if (s === 'duffel_ow') return 'Compagnies';
  if (s.startsWith('google') || s.startsWith('fast')) return 'Google Flights';
  return esc(s);
};
const esc = (s) => {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
};
// Valeurs de config injectées dans des attributs : esc() seul renverrait ''
// pour le nombre 0 (âge d'un enfant, seuil à 0), d'où la conversion préalable.
const attr = (v) => (v === null || v === undefined || v === '') ? '' : esc(String(v));

// Au-dela, le prix affiche sur une carte periode n est plus courant.
const TRIP_STALE_HOURS = 24;
let tripChart = null;
let _totalPax = 1; // nombre total de voyageurs, chargé au démarrage
let _overviewDirty = false; // un rafraîchissement a été sauté (onglet masqué)

// ── Color interpolation helper ─────────────────────
function lerpColor(a, b, t) {
  // a, b are hex strings like "#00714c", t is 0..1
  const ar = parseInt(a.slice(1,3),16), ag = parseInt(a.slice(3,5),16), ab = parseInt(a.slice(5,7),16);
  const br = parseInt(b.slice(1,3),16), bg_ = parseInt(b.slice(3,5),16), bb = parseInt(b.slice(5,7),16);
  const r = Math.round(ar + (br - ar) * t);
  const g = Math.round(ag + (bg_ - ag) * t);
  const bl = Math.round(ab + (bb - ab) * t);
  return `rgb(${r},${g},${bl})`;
}

function scoreColor(score) {
  // 0=red, 50=orange, 100=green
  if (score <= 50) return lerpColor('#d35b17', '#c2a25b', score / 50);
  return lerpColor('#c2a25b', '#00714c', (score - 50) / 50);
}

// ── Tabs ─────────────────────────────────────────────

// Sémantique tab/tablist posée en JS : le balisage vit dans index.html.
const _tabsNav = $('.tabs');
if (_tabsNav) _tabsNav.setAttribute('role', 'tablist');

$$('.tabs button').forEach(b => {
  const panel = $('#tab-' + b.dataset.tab);
  b.setAttribute('role', 'tab');
  b.setAttribute('aria-selected', b.classList.contains('active') ? 'true' : 'false');
  if (panel) {
    b.setAttribute('aria-controls', panel.id);
    panel.setAttribute('role', 'tabpanel');
  }
  b.addEventListener('click', () => {
    $$('.tabs button').forEach(x => {
      x.classList.remove('active');
      x.setAttribute('aria-selected', 'false');
    });
    $$('.tab').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    b.setAttribute('aria-selected', 'true');
    $('#tab-' + b.dataset.tab).classList.add('active');
    // L'onglet masqué (display:none) rend les canvas 0×0 : on rejoue le
    // rafraîchissement sauté pendant l'absence au lieu de laisser des
    // sparklines vides jusqu'au tick suivant.
    if (b.dataset.tab === 'overview' && _overviewDirty) loadOverview();
    if (b.dataset.tab === 'trip') loadTripDetail();
    if (b.dataset.tab === 'alerts') loadAlerts();
    if (b.dataset.tab === 'runs') loadRuns();
    if (b.dataset.tab === 'hotels') loadHotels();
    if (b.dataset.tab === 'admin') loadAdmin();
  });
});

// ── Fetch helper ────────────────────────────────────

// Sans test de r.ok, une 500 renvoyait {detail} : les boucles .forEach()
// échouaient en silence et la page semblait figée.
async function api(url, options) {
  const r = await fetch(url, options);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

// ── Mot de passe admin ──────────────────────────────
// Le tableau de bord est consultable librement ; seules les routes qui
// écrivent la config ou déclenchent un run le demandent. Conservé en
// sessionStorage : redemandé à chaque nouvel onglet, jamais persisté.
const ADMIN_PWD_KEY = 'bkk-admin-pwd';

function getAdminPwd() {
  try { return sessionStorage.getItem(ADMIN_PWD_KEY) || ''; }
  catch (e) { return ''; }
}

function setAdminPwd(pwd) {
  try { sessionStorage.setItem(ADMIN_PWD_KEY, pwd); } catch (e) { /* mode privé */ }
}

function clearAdminPwd() {
  try { sessionStorage.removeItem(ADMIN_PWD_KEY); } catch (e) { /* ignore */ }
}

function adminHeaders(extra) {
  const h = Object.assign({}, extra || {});
  const pwd = getAdminPwd();
  if (pwd) h['X-Admin-Password'] = pwd;
  return h;
}

/** Appel protégé : redemande le mot de passe une fois sur 401. */
async function adminFetch(url, options) {
  const opts = Object.assign({}, options);
  opts.headers = adminHeaders(opts.headers);
  let r = await fetch(url, opts);
  if (r.status === 401) {
    clearAdminPwd();
    const pwd = window.prompt('Mot de passe administrateur');
    if (!pwd) return r;
    setAdminPwd(pwd);
    opts.headers = adminHeaders(options && options.headers);
    r = await fetch(url, opts);
    if (r.status === 401) clearAdminPwd();
  }
  return r;
}

// ── Run-now button ──────────────────────────────────

// Au-delà, on considère que le run n'a jamais démarré (config invalide,
// lock déjà pris) : sans ça la sonde concluait sur le run PRÉCÉDENT.
const RUN_START_TIMEOUT_MS = 30000;

function startPolling(startedAt, previousRunId) {
  const btn = $('#run-now');
  btn.disabled = true;

  const t0 = startedAt ? _asDate(String(startedAt)).getTime() : Date.now();
  const tick = () => {
    const elapsed = Math.round((Date.now() - t0) / 1000);
    const min = Math.floor(elapsed / 60);
    const sec = elapsed % 60;
    btn.textContent = `⏳ ${min}:${String(sec).padStart(2, '0')}`;
  };
  tick();
  const timer = setInterval(tick, 1000);
  const askedAt = Date.now();

  const stop = (label) => {
    clearInterval(poll);
    clearInterval(timer);
    btn.textContent = label;
    btn.disabled = false;
    setTimeout(() => { btn.textContent = '↻ Check'; }, 8000);
  };

  const poll = setInterval(async () => {
    try {
      const runs = await api('/api/runs?limit=1');
      const run = runs.length ? runs[0] : null;
      // previousRunId non défini = run déjà en cours au chargement.
      const isNewRun = previousRunId === undefined
        || (run && run.id !== previousRunId);
      if (run && isNewRun && run.status !== 'running') {
        const dur = run.finished_at && run.started_at
          ? Math.round((_asDate(String(run.finished_at))
                        - _asDate(String(run.started_at))) / 1000)
          : Math.round((Date.now() - t0) / 1000);
        // 'partial' = au moins une période collectée, mais pas toutes.
        // L'afficher comme une erreur ferait croire à un check raté
        // alors que les prix ont bien été relevés et persistés.
        const echec = run.status === 'error' || run.status === 'timeout';
        stop(echec
          ? `✗ Erreur (${dur}s)`
          : `${run.status === 'partial' ? '⚠' : '✓'} ${run.trips_checked} périodes, ${run.alerts_generated} alertes (${dur}s)`);
        loadOverview();
      } else if (!isNewRun && Date.now() - askedAt > RUN_START_TIMEOUT_MS) {
        stop('✗ run non démarré (voir logs)');
      }
    } catch(e) { /* ignore poll errors */ }
  }, 5000);
}

async function checkRunningState() {
  try {
    const runs = await api('/api/runs?limit=1');
    if (runs.length && runs[0].status === 'running') {
      startPolling(runs[0].started_at);
    }
  } catch(e) {}
}

$('#run-now').addEventListener('click', async () => {
  const btn = $('#run-now');
  btn.disabled = true;
  btn.textContent = '⏳ 0:00';
  // L'id du dernier run AVANT le POST : la ligne run_log n'est créée qu'après
  // config.load(), donc tant qu'elle n'apparaît pas rien n'a tourné.
  let previousRunId;
  try {
    const runs = await api('/api/runs?limit=1').catch(() => []);
    previousRunId = runs.length ? runs[0].id : null;
    const r = await adminFetch('/api/run-now', { method: 'POST' });
    if (r.status === 401) { stop('🔒 mot de passe requis'); return; }
    if (r.status === 409) {
      // Un run tourne déjà : sa ligne existe, on la suit telle quelle.
      btn.textContent = 'Check en cours...';
      const cur = await api('/api/runs?limit=1').catch(() => []);
      startPolling(cur.length ? cur[0].started_at : null);
      return;
    }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    startPolling(null, previousRunId);
  } catch (e) {
    btn.textContent = '✗ erreur réseau';
    btn.disabled = false;
    setTimeout(() => { btn.textContent = '↻ Check'; }, 8000);
  }
});

// ── Overview ────────────────────────────────────────

async function loadOverview() {
  _overviewDirty = false;
  const grid = $('#trips-grid');
  let trips, cfgSum;
  try {
    [trips, cfgSum] = await Promise.all([
      api('/api/trips'),
      api('/api/config-summary'),
    ]);
  } catch (e) {
    // Config momentanément invalide ou API en erreur : le dire plutôt que
    // laisser une grille vide sans explication.
    grid.innerHTML = '<p class="dim">Erreur de chargement (config invalide ?) — voir les logs.</p>';
    $('#last-run').textContent = 'erreur de chargement';
    return;
  }
  _totalPax = (cfgSum.adults || 1) + (cfgSum.children ? cfgSum.children.length : 0);
  grid.innerHTML = '';

  let latestRun = null;
  trips.forEach(t => {
    if (t.last_check_at && (!latestRun || t.last_check_at > latestRun)) {
      latestRun = t.last_check_at;
    }
  });
  $('#last-run').textContent = latestRun
    ? `dernier run: ${dateTimeFmt(latestRun)}`
    : 'aucun run encore';

  // Populate trip selector
  const sel = $('#trip-select');
  const prev = sel.value;
  sel.innerHTML = '';
  trips.forEach(t => {
    const o = document.createElement('option');
    o.value = t.trip_name;
    o.textContent = t.trip_name;
    sel.appendChild(o);
  });
  if (prev) sel.value = prev;

  trips.forEach(t => grid.appendChild(buildTripCard(t)));

  // Load sparklines + stats indicators async for each card
  trips.forEach(t => {
    loadCardSparkline(t.trip_name);
    loadCardIndicators(t.trip_name);
  });
}

function buildTripCard(t) {
  const card = document.createElement('div');
  card.className = 'trip-card' + (t.current_best === null ? ' no-data' : '');
  card.dataset.trip = t.trip_name;
  // Une div cliquable n'est ni focalisable ni activable au clavier sans ça.
  card.tabIndex = 0;
  card.setAttribute('role', 'button');
  const open = () => {
    $('#trip-select').value = t.trip_name;
    $$('.tabs button').forEach(b => {
      if (b.dataset.tab === 'trip') b.click();
    });
  };
  card.addEventListener('click', open);
  card.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
  });

  const dates = `${dateFmt(t.outbound_window?.[0])} → ${dateFmt(t.return_window?.[1])}`;
  let priceClass = 'none';
  let priceTxt = '— —';
  if (t.current_best != null) {
    priceTxt = fmt.format(Math.round(t.current_best));
    if (t.threshold && t.current_best <= t.threshold) priceClass = 'good';
    else if (t.all_time_low && t.current_best > t.all_time_low * 1.15) priceClass = 'bad';
    else priceClass = '';
  }

  card.innerHTML = `
    <div class="name">${esc(t.trip_name)}</div>
    <div class="dates">${dates}</div>
    <div class="price-main ${priceClass}">
      ${priceTxt}${t.current_best != null ? '<span class="currency">€</span>' : ''}
    </div>
    ${t.current_best != null && _totalPax > 1 ? `<div class="price-per-pax">${Math.round(t.current_best / _totalPax)}€/pers.</div>` : ''}
    <div class="price-stats">
      <span><span class="stat-label">bas</span> ${t.all_time_low != null ? Math.round(t.all_time_low) + '€' : '—'}</span>
      <span><span class="stat-label">moy 30j</span> ${t.avg_30d != null ? Math.round(t.avg_30d) + '€' : '—'}</span>
      <span><span class="stat-label">haut</span> ${t.all_time_high != null ? Math.round(t.all_time_high) + '€' : '—'}</span>
    </div>
    ${t.threshold ? `
      <div class="threshold">
        <span class="dim">Seuil d'alerte</span>
        <span class="target">≤ ${attr(t.threshold)}€</span>
      </div>
    ` : ''}
    <div class="sparkline-wrap"><canvas class="sparkline"></canvas></div>
    <div class="card-indicators">
      <span class="trend-badge"></span>
      <span class="score-badge"></span>
    </div>
  `;
  // Une période désactivée n'est plus interrogée : la masquer ferait croire
  // à une disparition, on la garde visible mais explicitement en pause.
  // Un run qui ne ramene rien laisse la carte sur un releve ancien :
  // sans ce reperage, un prix vieux de plusieurs jours passe pour courant.
  const stamp = t.last_captured_at || t.last_check_at;
  if (t.enabled !== false && stamp) {
    const ageH = (Date.now() - _asDate(String(stamp)).getTime()) / 3600000;
    if (ageH > TRIP_STALE_HOURS) {
      const vieux = document.createElement('div');
      vieux.className = 'hotel-status bad';
      vieux.textContent = 'dernier releve il y a ' + Math.round(ageH) + ' h';
      card.appendChild(vieux);
    }
  }
  if (t.enabled === false) {
    card.style.opacity = '0.55';
    const pause = document.createElement('div');
    pause.className = 'hotel-status dim';
    pause.textContent = 'en pause';
    card.appendChild(pause);
  }
  return card;
}

// ── Sparklines (overview cards) ─────────────────────

async function loadCardSparkline(tripName) {
  try {
    const history = await api(
      `/api/trips/${encodeURIComponent(tripName)}/history?days=30`);
    if (!history || history.length < 2) return;

    const card = document.querySelector(`.trip-card[data-trip="${CSS.escape(tripName)}"]`);
    if (!card) return;
    const canvas = card.querySelector('.sparkline');
    if (!canvas) return;
    // Onglet masqué (display:none) : le canvas mesure 0×0 et le tracé serait
    // perdu. On laisse la carte sans sparkline, le prochain passage redessine.
    if (!canvas.offsetWidth) return;

    const prices = history.map(h => h.price_eur);
    const min = Math.min(...prices);
    const max = Math.max(...prices);
    const range = max - min || 1;

    const ctx = canvas.getContext('2d');
    const w = canvas.width = canvas.offsetWidth * 2;
    const h = canvas.height = canvas.offsetHeight * 2;
    ctx.scale(2, 2);
    const cw = w / 2, ch = h / 2;
    const pad = 2;

    // Fill gradient
    const grad = ctx.createLinearGradient(0, 0, 0, ch);
    grad.addColorStop(0, 'rgba(194,162,91,0.15)');
    grad.addColorStop(1, 'rgba(194,162,91,0)');

    ctx.beginPath();
    ctx.moveTo(pad, ch - pad);
    for (let i = 0; i < prices.length; i++) {
      const x = pad + (i / (prices.length - 1)) * (cw - pad * 2);
      const y = pad + (1 - (prices[i] - min) / range) * (ch - pad * 2);
      ctx.lineTo(x, y);
    }
    ctx.lineTo(cw - pad, ch - pad);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    // Line
    ctx.beginPath();
    for (let i = 0; i < prices.length; i++) {
      const x = pad + (i / (prices.length - 1)) * (cw - pad * 2);
      const y = pad + (1 - (prices[i] - min) / range) * (ch - pad * 2);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = '#c2a25b';
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // Last point dot
    const lastX = cw - pad;
    const lastY = pad + (1 - (prices[prices.length - 1] - min) / range) * (ch - pad * 2);
    ctx.beginPath();
    ctx.arc(lastX, lastY, 2.5, 0, Math.PI * 2);
    ctx.fillStyle = '#c2a25b';
    ctx.fill();
  } catch (e) {
    // No sparkline data — silent
  }
}

// ── Trip detail ─────────────────────────────────────

let _chartDays = 60;

$('#trip-select').addEventListener('change', loadTripDetail);
$$('#chart-period button').forEach(btn => {
  btn.addEventListener('click', () => {
    $$('#chart-period button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    _chartDays = parseInt(btn.dataset.days, 10);
    loadTripDetail();
  });
});

// Deux chargements rapprochés (select + boutons de période) s'entrelaçaient :
// le second appelait new Chart() sur un canvas que le premier n'avait pas
// encore libéré (« Canvas is already in use »).
let _tripSeq = 0;

async function loadTripDetail() {
  const name = $('#trip-select').value;
  if (!name) return;
  const seq = ++_tripSeq;

  const daysParam = _chartDays > 0 ? `?days=${_chartDays}` : '?days=9999';
  const enc = encodeURIComponent(name);
  let history, routeHistory, breakdown, trips;
  try {
    [history, routeHistory, breakdown, trips] = await Promise.all([
      api(`/api/trips/${enc}/history${daysParam}`),
      api(`/api/trips/${enc}/history-by-route${daysParam}`),
      api(`/api/trips/${enc}/breakdown`),
      api('/api/trips'),
    ]);
  } catch (e) {
    if (seq !== _tripSeq) return;
    $('#breakdown-table').querySelector('tbody').innerHTML =
      '<tr><td colspan="7" class="dim">Erreur de chargement.</td></tr>';
    return;
  }
  if (seq !== _tripSeq) return;

  // Chart with one line per route
  const ctx = $('#trip-chart').getContext('2d');
  const tConf = trips.find(x => x.trip_name === name) || {};

  // Group route history by route key
  const byRoute = {};
  routeHistory.forEach(r => {
    const key = `${r.origin} → ${r.destination}`;
    if (!byRoute[key]) byRoute[key] = [];
    byRoute[key].push({ x: r.captured_at, y: r.price_eur });
  });

  // Color palette for routes
  const routeColors = [
    '#c2a25b', '#00714c', '#d35b17', '#5b8fc2', '#8b5bc2',
    '#c25b8f', '#5bc2a2', '#c2975b', '#5b5bc2', '#c25b5b',
  ];

  const datasets = [];

  // Best overall (thicker, filled)
  if (history.length) {
    datasets.push({
      label: 'Meilleur prix global',
      data: history.map(p => ({ x: p.captured_at || p.check_date, y: p.price_eur })),
      borderColor: '#c2a25b',
      backgroundColor: '#c2a25b12',
      fill: true,
      tension: 0.3,
      pointRadius: 4,
      pointHoverRadius: 7,
      borderWidth: 2.5,
    });
  }

  // Per-route lines
  const routeKeys = Object.keys(byRoute).sort();
  routeKeys.forEach((route, i) => {
    datasets.push({
      label: route,
      data: byRoute[route],
      borderColor: routeColors[i % routeColors.length],
      backgroundColor: 'transparent',
      fill: false,
      tension: 0.3,
      pointRadius: 2,
      pointHoverRadius: 5,
      borderWidth: 1.5,
      borderDash: [4, 2],
    });
  });

  // Threshold line
  if (tConf.threshold && history.length) {
    datasets.push({
      label: 'Seuil',
      data: history.map(p => ({ x: p.captured_at || p.check_date, y: tConf.threshold })),
      borderColor: '#00714c',
      borderDash: [6, 4],
      pointRadius: 0,
      borderWidth: 1,
      fill: false,
    });
  }

  if (tripChart) tripChart.destroy();
  tripChart = new Chart(ctx, {
    type: 'line',
    data: { datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'nearest', intersect: false },
      plugins: {
        legend: {
          labels: { color: '#464d2c', font: { family: 'JetBrains Mono', size: 10 },
                    boxWidth: 12, padding: 8 },
        },
        tooltip: {
          callbacks: {
            label: (item) => `${item.dataset.label}: ${Math.round(item.parsed.y)}€`,
          },
        },
      },
      scales: {
        x: { type: 'time',
             time: { tooltipFormat: 'dd/MM HH:mm', displayFormats: { hour: 'dd/MM HH:mm', day: 'dd/MM' } },
             ticks: { color: '#a8a8a2', font: { family: 'JetBrains Mono', size: 10 }, maxTicksLimit: 12 },
             grid: { color: '#cfcdcb' } },
        y: { ticks: { color: '#a8a8a2', font: { family: 'JetBrains Mono' },
                      callback: v => v + '€' },
             grid: { color: '#cfcdcb' } },
      },
    },
  });

  // Breakdown table
  const tbody = $('#breakdown-table tbody');
  tbody.innerHTML = '';
  breakdown.forEach((b, i) => {
    const tr = document.createElement('tr');
    if (i === 0) tr.classList.add('best-row');
    tr.innerHTML = `
      <td>${esc(b.origin)}</td>
      <td>${esc(b.destination)}</td>
      <td class="price-cell">${Math.round(b.best_eur)}€</td>
      <td>${esc(b.airlines) || '—'}</td>
      <td>${sourceLabel(b.source)}</td>
      <td>${b.outbound_date && b.return_date ? dateFmt(b.outbound_date) + ' → ' + dateFmt(b.return_date) : '—'}</td>
      <td>${dateFmt(b.last_seen)}</td>
    `;
    tbody.appendChild(tr);
  });
  if (!breakdown.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="dim">Pas encore de données.</td></tr>';
  }

  // Heatmap
  loadHeatmap(name, seq);
  loadProbeHeatmap(name, seq);

  // Stats
  loadTripStats(name, seq);
}

// ── Heatmap ────────────────────────────────────────

// Construit la table d'une grille aller x retour. Partagee par le
// calendrier de marche et celui des sondes : deux sources differentes,
// une seule forme — dupliquer le rendu les aurait fait diverger.
function buildHeatmapTable(data, opts) {
  opts = opts || {};
  const outDates = data.outbound_dates || [];
  const retDates = data.return_dates || [];
  const prices = data.prices || [];
  const lastSeen = opts.lastSeen || {};
  if (!outDates.length || !retDates.length) return null;

  let count = 0;
  let minPrice = Infinity, maxPrice = -Infinity;
  let minOut = -1, minRet = -1;
  for (let i = 0; i < outDates.length; i++) {
    for (let j = 0; j < retDates.length; j++) {
      const p = prices[i] && prices[i][j];
      if (p != null && p > 0) {
        count++;
        if (p < minPrice) { minPrice = p; minOut = i; minRet = j; }
        if (p > maxPrice) maxPrice = p;
      }
    }
  }
  if (!count) return null;
  const range = maxPrice - minPrice || 1;

  const table = document.createElement('table');
  table.className = 'hm-table';

  const thead = document.createElement('thead');
  let headerRow = '<tr><th class="hm-corner">Aller \ Retour</th>';
  retDates.forEach(d => {
    headerRow += `<th class="hm-col-header">${esc(dateFmt(d))}</th>`;
  });
  thead.innerHTML = headerRow + '</tr>';
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  for (let i = 0; i < outDates.length; i++) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<th class="hm-row-header">${esc(dateFmt(outDates[i]))}</th>`;
    for (let j = 0; j < retDates.length; j++) {
      const td = document.createElement('td');
      const p = prices[i] && prices[i][j];
      if (p != null && p > 0) {
        const t = (p - minPrice) / range; // 0 = moins cher (vert), 1 = plus cher
        td.className = 'hm-cell';
        td.style.backgroundColor = lerpColor('#00714c', '#d35b17', t);
        td.textContent = Math.round(p) + ' €';
        let title = dateFmt(outDates[i]) + ' → ' + dateFmt(retDates[j])
          + ' : ' + Math.round(p) + ' €';
        // Les cellules d'une sonde sont relevees a des dates differentes :
        // sans cet horodatage, une cellule vieille d'une semaine se lit
        // comme un prix du jour, et les cellules anciennes paraissent
        // systematiquement moins cheres (les prix montent a l'approche).
        const seen = lastSeen[outDates[i] + '>' + retDates[j]];
        if (seen) title += ' · relevé le ' + dateFmt(seen);
        td.title = title;
        if (i === minOut && j === minRet) td.classList.add('hm-cheapest');
      } else {
        td.className = 'hm-cell hm-empty';
        td.textContent = '—';
      }
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  return table;
}

async function loadHeatmap(tripName, seq) {
  const container = $('#heatmap-grid');
  container.innerHTML = '';
  try {
    const data = await api(`/api/trips/${encodeURIComponent(tripName)}/heatmap`);
    // Un chargement plus recent a pris la main : ne pas ecraser son rendu.
    if (seq !== undefined && seq !== _tripSeq) return;
    const table = data ? buildHeatmapTable(data) : null;
    if (!table) {
      container.innerHTML = '<div class="hm-no-data">Pas de données</div>';
      return;
    }
    container.appendChild(table);
  } catch (e) {
    container.innerHTML = '<div class="hm-no-data">Pas de données</div>';
  }
}

// ── Calendrier d'une sonde ─────────────────────────

let _probeTrip = null;

async function loadProbeHeatmap(tripName, seq) {
  const wrap = $('#probe-heatmap');
  const select = $('#probe-heatmap-select');
  const grid = $('#probe-heatmap-grid');
  if (!wrap || !select || !grid) return;
  _probeTrip = tripName;
  wrap.hidden = true;
  grid.innerHTML = '';
  try {
    const list = await api(`/api/trips/${encodeURIComponent(tripName)}/probes`);
    if (seq !== undefined && seq !== _tripSeq) return;
    // Aucune sonde n'a encore de releve : on masque toute la section
    // plutot que d'afficher un cadre vide sans explication.
    if (!Array.isArray(list) || !list.length) return;

    select.innerHTML = list.map(p => {
      const label = esc(p.probe)
        + (p.carriers ? ' — ' + esc(p.carriers) : '')
        + ' (' + (p.cells || 0) + ' cellule' + ((p.cells || 0) > 1 ? 's' : '') + ')';
      return `<option value="${attr(p.probe)}">${label}</option>`;
    }).join('');
    select.style.display = list.length > 1 ? '' : 'none';
    wrap.hidden = false;
    await renderProbeHeatmap(tripName, list[0].probe, seq);
  } catch (e) {
    wrap.hidden = true;
  }
}

async function renderProbeHeatmap(tripName, probe, seq) {
  const grid = $('#probe-heatmap-grid');
  grid.innerHTML = '';
  try {
    const data = await api(`/api/trips/${encodeURIComponent(tripName)}`
      + `/probe-heatmap?probe=${encodeURIComponent(probe)}`);
    if (seq !== undefined && seq !== _tripSeq) return;
    const table = data
      ? buildHeatmapTable(data, { lastSeen: data.last_seen }) : null;
    if (!table) {
      grid.innerHTML = '<div class="hm-no-data">Pas encore de relevé</div>';
      return;
    }
    grid.appendChild(table);
  } catch (e) {
    grid.innerHTML = '<div class="hm-no-data">Pas encore de relevé</div>';
  }
}

// ── Trip stats ─────────────────────────────────────

// /api/trips/*/stats renvoie trend = {direction, change_pct, recommendation}.
// L'ancien code comparait cet objet à une chaîne ('falling') puis à 0 : deux
// tests toujours faux, d'où un « stable » permanent. Le seuil de ±2 % est
// appliqué côté serveur, on ne rebranche donc que sur direction.
function trendInfo(trend) {
  const dir = (trend && trend.direction) || 'stable';
  if (dir === 'falling') {
    return { arrow: '↘', text: 'en baisse', cls: 'falling', color: 'var(--green)' };
  }
  if (dir === 'rising') {
    return { arrow: '↗', text: 'en hausse', cls: 'rising', color: 'var(--rose)' };
  }
  return { arrow: '→', text: 'stable', cls: 'stable', color: 'var(--text-dim)' };
}

function trendPct(trend) {
  const pct = trend && trend.change_pct;
  if (!pct) return '';
  return ` (${pct > 0 ? '+' : ''}${pct} %)`;
}

async function loadTripStats(tripName, seq) {
  const container = $('#stats-content');
  container.innerHTML = '';
  try {
    const stats = await api(`/api/trips/${encodeURIComponent(tripName)}/stats`);
    // Un chargement plus récent a pris la main : ne pas écraser son rendu.
    if (seq !== undefined && seq !== _tripSeq) return;

    if (!stats || (stats.trend == null && stats.buy_score == null)) {
      container.innerHTML = '<div class="stats-no-data">Pas de données statistiques disponibles.</div>';
      return;
    }

    let html = '<div class="stats-grid">';

    // Trend
    if (stats.trend != null) {
      const ti = trendInfo(stats.trend);
      const reco = (stats.trend && stats.trend.recommendation) || stats.recommendation;
      html += `
        <div class="stats-trend">
          <span class="trend-arrow" style="color:${ti.color}">${ti.arrow}</span>
          <div>
            <div class="trend-text">Tendance : <strong>${esc(ti.text)}</strong>${esc(trendPct(stats.trend))}</div>
            ${reco ? `<div class="dim" style="font-size:0.75rem;margin-top:0.15rem">${esc(reco)}</div>` : ''}
          </div>
        </div>`;
    }

    // Buy score
    if (stats.buy_score != null) {
      const score = Math.max(0, Math.min(100, Math.round(stats.buy_score)));
      const color = scoreColor(score);
      html += `
        <div class="stats-score">
          <div class="dim" style="font-size:0.7rem;text-transform:uppercase;letter-spacing:0.05em">Score d'achat</div>
          <div class="score-bar-wrap">
            <div class="score-bar-track">
              <div class="score-bar-fill" style="width:${score}%;background:${color}"></div>
            </div>
            <span class="score-label" style="color:${color}">${score}/100</span>
          </div>
        </div>`;
    }

    // Score history sparkline
    if (stats.score_history && stats.score_history.length >= 2) {
      html += `
        <div class="stats-score-history">
          <div class="dow-title">Évolution score d'achat</div>
          <div class="score-history-wrap"><canvas id="score-history-chart"></canvas></div>
        </div>`;
    }

    // Le bloc \u00AB Prix moyen par jour de la semaine \u00BB a \u00E9t\u00E9 retir\u00E9 : l'API
    // moyenne par jour du RELEV\u00C9 (et toutes routes confondues), pas par jour
    // de vol \u2014 avec un cron toutes les 6 h la variation n'est que du bruit.
    // Le rendu ne s'affichait d'ailleurs jamais (objets compar\u00E9s \u00E0 0).

    html += '</div>';
    container.innerHTML = html;

    // Render score history chart after DOM injection
    if (stats.score_history && stats.score_history.length >= 2) {
      _renderScoreHistory(stats.score_history);
    }
  } catch (e) {
    container.innerHTML = '<div class="stats-no-data">Pas de données statistiques disponibles.</div>';
  }
}

let _scoreChart = null;
function _renderScoreHistory(data) {
  const canvas = document.getElementById('score-history-chart');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  if (_scoreChart) _scoreChart.destroy();

  _scoreChart = new Chart(ctx, {
    type: 'line',
    data: {
      datasets: [{
        label: 'Score d\'achat',
        data: data.map(d => ({ x: d.date, y: d.score })),
        borderColor: '#c2a25b',
        backgroundColor: '#c2a25b18',
        fill: true,
        tension: 0.3,
        pointRadius: 3,
        pointHoverRadius: 6,
        pointBackgroundColor: data.map(d => scoreColor(d.score)),
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (item) => {
              const d = data[item.dataIndex];
              return `Score: ${d.score}/100 (${Math.round(d.price)}€)`;
            },
          },
        },
      },
      scales: {
        x: { type: 'time', time: { unit: 'day' },
             ticks: { color: '#a8a8a2', font: { family: 'JetBrains Mono', size: 9 }, maxTicksLimit: 8 },
             grid: { display: false } },
        y: { min: 0, max: 100,
             ticks: { color: '#a8a8a2', font: { family: 'JetBrains Mono', size: 9 },
                      stepSize: 25, callback: v => v },
             grid: { color: '#cfcdcb' } },
      },
    },
  });
}

// ── Card indicators (overview) ────────────────────

async function loadCardIndicators(tripName) {
  try {
    const stats = await api(`/api/trips/${encodeURIComponent(tripName)}/stats`);

    const card = document.querySelector(`.trip-card[data-trip="${CSS.escape(tripName)}"]`);
    if (!card) return;

    const trendBadge = card.querySelector('.trend-badge');
    const scoreBadge = card.querySelector('.score-badge');

    // Trend badge
    if (stats && stats.trend != null && trendBadge) {
      const ti = trendInfo(stats.trend);
      trendBadge.className = 'trend-badge ' + ti.cls;
      trendBadge.textContent = ti.arrow + ' ' + ti.text + trendPct(stats.trend);
      if (stats.trend.recommendation) trendBadge.title = stats.trend.recommendation;
    } else if (trendBadge) {
      trendBadge.style.display = 'none';
    }

    // Score badge
    if (stats && stats.buy_score != null && scoreBadge) {
      const score = Math.max(0, Math.min(100, Math.round(stats.buy_score)));
      const color = scoreColor(score);
      scoreBadge.style.backgroundColor = color;
      scoreBadge.textContent = 'Score ' + score + '/100';
    } else if (scoreBadge) {
      scoreBadge.style.display = 'none';
    }
  } catch (e) {
    // Stats not available — hide indicators silently
    const card = document.querySelector(`.trip-card[data-trip="${CSS.escape(tripName)}"]`);
    if (card) {
      const indicators = card.querySelector('.card-indicators');
      if (indicators) indicators.style.display = 'none';
    }
  }
}

// ── Alerts ──────────────────────────────────────────

async function loadAlerts() {
  const list = $('#alerts-list');
  let alerts;
  try {
    alerts = await api('/api/alerts');
  } catch (e) {
    list.innerHTML = '<p class="dim">Erreur de chargement des alertes.</p>';
    return;
  }
  list.innerHTML = '';
  if (!alerts.length) {
    list.innerHTML = '<p class="dim">Aucune alerte pour le moment.</p>';
    return;
  }
  alerts.forEach(a => {
    const p = a.payload || {};
    const isHotel = a.kind === 'hotel_low';
    const isProbe = a.kind === 'probe_low';
    const card = document.createElement('div');
    let cls = 'alert-card';
    if (a.kind === 'rise') cls += ' rise';
    else if (p.hit_threshold) cls += ' threshold';
    card.className = cls;
    // Une alerte hôtel n'a ni compagnie ni origine/destination : sans branche
    // dédiée elle s'affichait en « 📉 Nouveau bas • ? → ? ».
    // Une alerte de sonde, elle, a la même forme qu'une alerte de marché
    // mais ne dit PAS la même chose : c'est le plus bas d'une seule
    // compagnie. Sans libellé distinct, elle se lit comme un mouvement du
    // marché qui n'a pas eu lieu.
    const carrierLabel = CARRIER_LABEL[p.carrier] || p.carrier || 'compagnie';
    const kindLabel = isHotel ? (p.hit_threshold ? '🏨 Seuil atteint' : '🏨 Hôtel')
      : isProbe ? `✈️ Plus bas ${esc(carrierLabel)}`
      : a.kind === 'rise' ? '📈 Hausse'
      : p.hit_threshold ? '🎯 Seuil atteint' : '📉 Nouveau bas';
    let meta;
    if (isHotel) {
      const providers = Array.isArray(p.providers_seen) ? p.providers_seen : [];
      meta = `${p.checkin ? esc(dateFmt(p.checkin)) + ' → ' + esc(dateFmt(p.checkout)) : '—'}`
        + `${p.nights ? ' · ' + esc(String(p.nights)) + ' nuits' : ''}`
        + `${p.source ? ' · ' + esc(p.source) : ''}`
        + `${providers.length ? '<br>' + esc(providers.join(', ')) : ''}`;
    } else {
      meta = `${esc(p.airlines) || ''} • ${esc(p.origin) || '?'} → ${esc(p.destination) || '?'}`
        + `${p.outbound_date ? ' • ' + esc(p.outbound_date) + ' → ' + esc(p.return_date) : ''}`;
    }
    card.innerHTML = `
      <div class="alert-header">
        <div>
          <div class="kind">${kindLabel} • ${dateTimeFmt(a.sent_at)}</div>
          <div class="trip-name">${esc(a.trip_name)}</div>
        </div>
        <div class="price">${Math.round(a.price_eur)}€</div>
      </div>
      <div class="alert-meta">${meta}</div>
    `;
    list.appendChild(card);
  });
}

// ── Runs log ─────────────────────────────────────────

async function loadRuns() {
  const tbody = $('#runs-table tbody');
  let runs;
  try {
    runs = await api('/api/runs');
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="6" class="dim">Erreur de chargement.</td></tr>';
    return;
  }
  tbody.innerHTML = '';
  runs.forEach(r => {
    const dur = r.finished_at && r.started_at
      ? Math.round((_asDate(String(r.finished_at)) - _asDate(String(r.started_at))) / 1000) + 's'
      : (r.status === 'running' ? '…' : '—');
    const statusColor = r.status === 'ok' ? 'var(--teal)'
      : r.status === 'error' ? 'var(--rose)' : 'var(--gold)';
    tbody.innerHTML += `
      <tr>
        <td>${dateTimeFmt(r.started_at)}</td>
        <td>${dur}</td>
        <td style="color:${statusColor}">${r.status}</td>
        <td>${r.trips_checked ?? '—'}</td>
        <td>${r.alerts_generated ?? '—'}</td>
        <td class="dim">${esc(r.error) || ''}</td>
      </tr>
    `;
  });
}

// ── Infos (weather + FX) ─────────────────────────────

let weatherChart = null;
let fxChart = null;

async function loadInfos() {
  loadWeather();
  loadFx();
}

async function loadWeather() {
  try {
    const r = await fetch(
      'https://api.open-meteo.com/v1/forecast?latitude=13.75&longitude=100.52' +
      '&daily=temperature_2m_max,temperature_2m_min,apparent_temperature_max' +
      '&current=temperature_2m,apparent_temperature,weathercode' +
      '&timezone=Asia/Bangkok&past_days=7&forecast_days=7'
    );
    const data = await r.json();
    const cur = data.current || {};
    const daily = data.daily || {};

    const icon = weatherIcon(cur.weathercode);
    const feel = cur.apparent_temperature != null ? ` (ressenti ${Math.round(cur.apparent_temperature)}°)` : '';
    $('#weather-current').innerHTML = `
      <div><span class="temp-big">${icon} ${Math.round(cur.temperature_2m)}°C</span></div>
      <div>Bangkok maintenant${esc(feel)}</div>
    `;

    // daily.time est en Asia/Bangkok : la date UTC pointait sur la veille
    // entre 19 h et 2 h (heure de Paris). sv-SE donne le format AAAA-MM-JJ.
    const today = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Bangkok' })
      .format(new Date());
    const times = daily.time || [];
    const todayIdx = times.indexOf(today);
    const labels = times.map(d =>
      new Date(d).toLocaleDateString('fr-FR',
        {weekday: 'short', day: 'numeric', timeZone: 'UTC'})
    );

    // Point sizes: bigger for today
    const pointRadii = times.map(d => d === today ? 6 : 2);
    const pointBg = (color) => times.map(d => d === today ? color : color + '88');

    const ctx = $('#weather-chart').getContext('2d');
    if (weatherChart) weatherChart.destroy();

    // Vertical line plugin for today
    const todayLinePlugin = {
      id: 'todayLine',
      afterDraw(chart) {
        if (todayIdx < 0) return;
        const meta = chart.getDatasetMeta(0);
        if (!meta.data[todayIdx]) return;
        const x = meta.data[todayIdx].x;
        const ctx = chart.ctx;
        const top = chart.chartArea.top;
        const bottom = chart.chartArea.bottom;
        ctx.save();
        ctx.strokeStyle = '#464d2c44';
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(x, top);
        ctx.lineTo(x, bottom);
        ctx.stroke();
        ctx.restore();
        // "Auj." label
        ctx.save();
        ctx.fillStyle = '#464d2c';
        ctx.font = '10px JetBrains Mono';
        ctx.textAlign = 'center';
        ctx.fillText('auj.', x, top - 4);
        ctx.restore();
      }
    };

    weatherChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [
          {
            label: 'Max °C',
            data: daily.temperature_2m_max || [],
            borderColor: '#d35b17',
            backgroundColor: '#d35b1718',
            fill: true,
            tension: 0.4,
            pointRadius: pointRadii,
            pointBackgroundColor: pointBg('#d35b17'),
          },
          {
            label: 'Min °C',
            data: daily.temperature_2m_min || [],
            borderColor: '#00714c',
            backgroundColor: '#00714c18',
            fill: true,
            tension: 0.4,
            pointRadius: pointRadii,
            pointBackgroundColor: pointBg('#00714c'),
          },
          {
            label: 'Ressenti max',
            data: daily.apparent_temperature_max || [],
            borderColor: '#d35b1766',
            borderDash: [4, 3],
            fill: false,
            tension: 0.4,
            pointRadius: 0,
          },
        ],
      },
      plugins: [todayLinePlugin],
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { color: '#464d2c', font: { family: 'JetBrains Mono', size: 10 } } } },
        scales: {
          x: { ticks: {
            color: (ctx) => times[ctx.index] === today ? '#464d2c' : '#a8a8a2',
            font: (ctx) => ({ family: 'JetBrains Mono', size: 9, weight: times[ctx.index] === today ? 'bold' : 'normal' }),
            maxRotation: 45,
          }, grid: { display: false } },
          y: { ticks: { color: '#a8a8a2', font: { family: 'JetBrains Mono', size: 10 }, callback: v => v + '°' }, grid: { color: '#cfcdcb' } },
        },
      },
    });
  } catch (e) {
    $('#weather-current').textContent = 'Erreur chargement météo';
  }
}

function weatherIcon(code) {
  if (code == null) return '';
  if (code <= 1) return '☀️';
  if (code <= 3) return '⛅';
  if (code <= 48) return '☁️';
  if (code <= 67) return '🌧️';
  if (code <= 77) return '🌨️';
  if (code <= 82) return '🌧️';
  if (code <= 86) return '🌨️';
  return '⛈️';
}

async function loadFx() {
  try {
    const r = await fetch('/api/fx-history?months=6');
    const data = await r.json();
    if (data.error) throw new Error(data.error);
    const dates = data.dates;
    const rates = data.rates;
    const latest = rates[rates.length - 1];
    const oldest = rates[0];
    const diff = ((latest / oldest - 1) * 100).toFixed(1);
    const sign = diff >= 0 ? '+' : '';

    $('#fx-current').innerHTML = `
      <div><span class="rate-big">1€ = ${latest.toFixed(2)} ฿</span></div>
      <div>${sign}${diff}% sur 6 mois</div>
      <div class="fx-conversions">20฿ = ${(20/latest).toFixed(2)}€ · 100฿ = ${(100/latest).toFixed(2)}€ · 1000฿ = ${(1000/latest).toFixed(1)}€</div>
    `;

    const ctx = $('#fx-chart').getContext('2d');
    if (fxChart) fxChart.destroy();
    fxChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: dates,
        datasets: [{
          label: 'EUR/THB',
          data: rates,
          borderColor: '#c2a25b',
          backgroundColor: '#c2a25b18',
          fill: true,
          tension: 0.3,
          pointRadius: 0,
          pointHoverRadius: 4,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { type: 'time', time: { unit: 'month' },
               ticks: { color: '#a8a8a2', font: { family: 'JetBrains Mono', size: 10 }, maxTicksLimit: 6 },
               grid: { display: false } },
          y: { ticks: { color: '#a8a8a2', font: { family: 'JetBrains Mono', size: 10 }, callback: v => v + '฿' },
               grid: { color: '#cfcdcb' } },
        },
      },
    });
  } catch (e) {
    $('#fx-current').textContent = 'Erreur chargement taux de change';
  }
}

// ── Hotels ─────────────────────────────────────────────

let hotelChart = null;

async function loadHotels() {
  try {
    const hotels = await api('/api/hotels');
    const grid = $('#hotels-grid');
    grid.innerHTML = '';

    if (!hotels.length) {
      grid.innerHTML = '<p class="dim">Aucun hôtel configuré. Ajoutez-en dans l\'onglet Admin.</p>';
      $('#hotel-detail').style.display = 'none';
      return;
    }

    // Populate selector
    const sel = $('#hotel-select');
    const prev = sel.value;
    sel.innerHTML = '';
    hotels.forEach(h => {
      const o = document.createElement('option');
      o.value = h.hotel_name;
      o.textContent = h.hotel_name;
      sel.appendChild(o);
    });
    if (prev) sel.value = prev;

    hotels.forEach(h => grid.appendChild(buildHotelCard(h)));
    $('#hotel-detail').style.display = 'block';
    loadHotelDetail();
  } catch (e) {
    console.error('loadHotels error:', e);
    $('#hotels-grid').innerHTML =
      '<p class="dim">Erreur de chargement des hôtels — voir les logs.</p>';
  }
}

// Un relevé plus vieux que ça signale une collecte en panne.
const HOTEL_STALE_HOURS = 18;

function hotelStatus(h) {
  if (h.enabled === false) return { cls: 'dim', text: 'en pause' };
  if (h.consecutive_failures > 0) {
    const n = h.consecutive_failures;
    return {
      cls: 'bad',
      text: `collecte en échec (${n}×)` + (h.last_error ? ' — ' + h.last_error : ''),
    };
  }
  const stamp = h.last_captured_at || h.last_check_at;
  if (stamp) {
    const ageH = (Date.now() - new Date(stamp).getTime()) / 3600000;
    if (ageH > HOTEL_STALE_HOURS) {
      return { cls: 'bad', text: `dernier relevé il y a ${Math.round(ageH)} h` };
    }
  }
  return null;
}

function buildHotelCard(h) {
  const card = document.createElement('div');
  card.className = 'trip-card' + (h.current_best === null ? ' no-data' : '');
  // Une div cliquable n'est ni focalisable ni activable au clavier sans ça.
  card.tabIndex = 0;
  card.setAttribute('role', 'button');
  const open = () => {
    // Les <option> ont pour value h.hotel_name : toute autre valeur
    // laissait le select vide et le détail ne se chargeait jamais.
    $('#hotel-select').value = h.hotel_name;
    loadHotelDetail();
  };
  card.addEventListener('click', open);
  card.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
  });

  let priceTxt = '— —', priceClass = 'none';
  if (h.current_best != null) {
    priceTxt = fmt.format(Math.round(h.current_best));
    if (h.threshold && h.current_best <= h.threshold) priceClass = 'good';
    else priceClass = '';
  }

  const nights = h.nights || (h.checkin && h.checkout
    ? Math.round((new Date(h.checkout) - new Date(h.checkin)) / 86400000) : '?');
  card.innerHTML = `
    <div class="name">🏨 ${esc(h.hotel_name)}</div>
    <div class="dates">${h.checkin ? dateFmt(h.checkin) + ' → ' + dateFmt(h.checkout) : 'Dates non définies'} · ${nights} nuits</div>
    <div class="price-main ${priceClass}">
      ${priceTxt}${h.current_best != null ? '<span class="currency">€</span>' : ''}
    </div>
    <div class="price-stats">
      <span><span class="stat-label">bas</span> ${h.lowest_price_eur != null ? Math.round(h.lowest_price_eur) + '€' : '—'}</span>
      <span><span class="stat-label">moy 30j</span> ${h.avg_30d != null ? Math.round(h.avg_30d) + '€' : '—'}</span>
    </div>
    ${h.threshold ? `<div class="threshold"><span class="dim">Seuil</span><span class="target">≤ ${attr(h.threshold)}€</span></div>` : ''}
    <div class="hotel-status"></div>
  `;
  const st = hotelStatus(h);
  const statusEl = card.querySelector('.hotel-status');
  if (st) {
    statusEl.textContent = st.text;
    statusEl.classList.add(st.cls);
  } else {
    statusEl.remove();
  }
  return card;
}

$('#hotel-select').addEventListener('change', loadHotelDetail);

async function loadHotelDetail() {
  const hotelName = $('#hotel-select').value;
  if (!hotelName) return;

  let history, breakdown;
  try {
    [history, breakdown] = await Promise.all([
      api(`/api/hotels/${encodeURIComponent(hotelName)}/history`),
      api(`/api/hotels/${encodeURIComponent(hotelName)}/breakdown`),
    ]);
  } catch (e) {
    $('#hotel-breakdown-table').querySelector('tbody').innerHTML =
      '<tr><td colspan="4" class="dim">Erreur de chargement.</td></tr>';
    return;
  }

  // Chart
  const ctx = $('#hotel-chart').getContext('2d');
  if (hotelChart) hotelChart.destroy();

  hotelChart = new Chart(ctx, {
    type: 'line',
    data: {
      datasets: [{
        label: 'Prix (€)',
        data: history.map(p => ({ x: p.captured_at || p.check_date, y: p.price_eur })),
        borderColor: '#c2a25b',
        backgroundColor: '#c2a25b18',
        fill: true,
        tension: 0.3,
        pointRadius: 3,
        pointHoverRadius: 6,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#464d2c', font: { family: 'JetBrains Mono' } } } },
      scales: {
        x: { type: 'time', time: { unit: 'day' },
             ticks: { color: '#a8a8a2', font: { family: 'JetBrains Mono' } },
             grid: { color: '#cfcdcb' } },
        y: { ticks: { color: '#a8a8a2', font: { family: 'JetBrains Mono' },
                      callback: v => v + '€' },
             grid: { color: '#cfcdcb' } },
      },
    },
  });

  // Breakdown table
  const tbody = $('#hotel-breakdown-table tbody');
  tbody.innerHTML = '';
  breakdown.forEach((b, i) => {
    const tr = document.createElement('tr');
    if (i === 0) tr.classList.add('best-row');
    tr.innerHTML = `
      <td>${esc(b.source)}</td>
      <td class="price-cell">${Math.round(b.best_eur)}€</td>
      <td>${esc(b.currency)}</td>
      <td>${dateFmt(b.last_seen)}</td>
    `;
    tbody.appendChild(tr);
  });
  if (!breakdown.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="dim">Pas encore de données.</td></tr>';
  }
}

// ── Admin ──────────────────────────────────────────────

let _adminConfig = null;

async function loadAdmin() {
  try {
    const ra = await adminFetch('/api/admin/config');
    if (!ra.ok) throw new Error(ra.status === 401
      ? 'Mot de passe administrateur requis'
      : `HTTP ${ra.status}`);
    _adminConfig = await ra.json();
    // Etat des sondes : non bloquant, la config doit s'afficher meme si
    // cette route echoue.
    try {
      const rp = await adminFetch('/api/probes');
      _probeStatus = rp.ok ? await rp.json() : [];
    } catch (e) {
      _probeStatus = [];
    }
    renderOrigins();
    renderDestinations();
    renderTravelers();
    renderTrips();
    renderHotelsAdmin();
    renderProbesAdmin();
    adminMessage('');
  } catch (e) {
    adminMessage('Erreur chargement config');
  }
}

function renderOrigins() {
  const container = $('#admin-origins');
  container.innerHTML = '';
  (_adminConfig.origins || []).forEach((o, i) => {
    const tag = document.createElement('span');
    tag.className = 'tag';
    tag.textContent = o;
    const btn = document.createElement('button');
    btn.className = 'tag-remove';
    btn.textContent = '\u00d7';
    btn.setAttribute('aria-label', `Retirer le d\u00e9part ${o}`);
    btn.title = 'Retirer';
    btn.addEventListener('click', () => { _adminConfig.origins.splice(i, 1); renderOrigins(); });
    tag.appendChild(btn);
    container.appendChild(tag);
  });
}

function renderDestinations() {
  const container = $('#admin-destinations');
  container.innerHTML = '';
  (_adminConfig.destinations || []).forEach((d, i) => {
    const tag = document.createElement('span');
    tag.className = 'tag';
    tag.textContent = d;
    const btn = document.createElement('button');
    btn.className = 'tag-remove';
    btn.textContent = '\u00d7';
    btn.setAttribute('aria-label', `Retirer la destination ${d}`);
    btn.title = 'Retirer';
    btn.addEventListener('click', () => { _adminConfig.destinations.splice(i, 1); renderDestinations(); });
    tag.appendChild(btn);
    container.appendChild(tag);
  });
}

// Un code IATA fait exactement 3 lettres : « LYON », « CD G » ou « 123 »
// étaient acceptés et partaient dans chaque combinaison de recherche.
const IATA_RE = /^[A-Z]{3}$/;

function adminMessage(msg) {
  const status = $('#admin-status');
  status.textContent = msg;
  status.style.color = msg ? 'var(--rose)' : '';
}

function addOrigin() {
  const input = $('#add-origin');
  const val = input.value.trim().toUpperCase();
  if (!val) return;
  if (!IATA_RE.test(val)) {
    adminMessage(`Code IATA invalide : « ${val} » (3 lettres attendues, ex. CDG)`);
    return;
  }
  adminMessage('');
  if (_adminConfig.origins.includes(val)) return;
  _adminConfig.origins.push(val);
  renderOrigins();
  input.value = '';
}

function removeOrigin(i) {
  _adminConfig.origins.splice(i, 1);
  renderOrigins();
}

function addDestination() {
  const input = $('#add-dest');
  const val = input.value.trim().toUpperCase();
  if (!val) return;
  if (!IATA_RE.test(val)) {
    adminMessage(`Code IATA invalide : « ${val} » (3 lettres attendues, ex. BKK)`);
    return;
  }
  adminMessage('');
  if (_adminConfig.destinations.includes(val)) return;
  _adminConfig.destinations.push(val);
  renderDestinations();
  input.value = '';
}

function removeDestination(i) {
  _adminConfig.destinations.splice(i, 1);
  renderDestinations();
}

function renderTravelers() {
  const container = $('#admin-travelers');
  if (!_adminConfig) return;
  const adults = _adminConfig.adults || 1;
  const children = _adminConfig.children || [];

  let childrenHtml = '';
  children.forEach((age, i) => {
    childrenHtml += `
      <div class="child-row">
        <label for="child-age-${i}" class="child-label">Enfant ${i + 1}</label>
        <div class="input-unit">
          <input type="number" id="child-age-${i}" name="child-age-${i}" min="0" max="17" value="${attr(age)}"
                 data-child-idx="${i}">
          <span class="unit">ans</span>
        </div>
        <button class="tag-remove" data-remove-child="${i}" title="Retirer"
                aria-label="Retirer l'enfant ${i + 1}">\u00d7</button>
      </div>`;
  });

  const maxFly = _adminConfig.max_fly_duration_hours || 18;
  container.innerHTML = `
    <div class="travelers-row">
      <div class="travelers-field">
        <label for="admin-adults">Adultes</label>
        <input type="number" id="admin-adults" name="admin-adults" min="1" max="9" value="${attr(adults)}">
      </div>
      <div class="travelers-field">
        <span class="travelers-field-title">Enfants</span>
        <div class="children-list">
          ${childrenHtml || '<span class="dim" style="font-size:0.75rem">Aucun enfant</span>'}
        </div>
        <button id="btn-add-child" class="btn-small">+ Ajouter un enfant</button>
      </div>
      <div class="travelers-field">
        <label for="admin-max-fly">Durée vol max</label>
        <div class="input-unit">
          <input type="number" id="admin-max-fly" name="admin-max-fly" min="6" max="48" value="${attr(maxFly)}">
          <span class="unit">h</span>
        </div>
      </div>
    </div>`;

  container.querySelector('#admin-adults').addEventListener('change', (e) => {
    _adminConfig.adults = parseInt(e.target.value, 10) || 1;
  });
  container.querySelector('#admin-max-fly').addEventListener('change', (e) => {
    _adminConfig.max_fly_duration_hours = parseInt(e.target.value, 10) || 18;
  });
  container.querySelectorAll('input[data-child-idx]').forEach(input => {
    input.addEventListener('change', () => {
      _adminConfig.children[parseInt(input.dataset.childIdx, 10)] = parseInt(input.value, 10) || 0;
    });
  });
  container.querySelectorAll('button[data-remove-child]').forEach(btn => {
    btn.addEventListener('click', () => {
      _adminConfig.children.splice(parseInt(btn.dataset.removeChild, 10), 1);
      renderTravelers();
    });
  });
  container.querySelector('#btn-add-child').addEventListener('click', () => {
    if (!_adminConfig.children) _adminConfig.children = [];
    _adminConfig.children.push(10);
    renderTravelers();
  });
}

function renderHotelsAdmin() {
  const container = $('#admin-hotels');
  if (!_adminConfig) return;
  const htls = _adminConfig.hotels || [];
  container.innerHTML = '';

  if (!htls.length) {
    container.innerHTML = '<span class="dim" style="font-size:0.8rem">Aucun hôtel configuré</span>';
    return;
  }

  htls.forEach((h, idx) => {
    const card = document.createElement('div');
    card.className = 'trip-edit-card' + (h.enabled === false ? ' trip-disabled' : '');
    card.innerHTML = `
      <div class="trip-edit-header">
        <label class="toggle" title="${h.enabled !== false ? 'Désactiver' : 'Activer'}">
          <input type="checkbox" ${h.enabled !== false ? 'checked' : ''} data-hotel-toggle="${idx}" aria-label="Activer ${esc(h.name)}">
          <span class="toggle-slider"></span>
        </label>
        <span class="trip-edit-name">${esc(h.name)}</span>
        <button class="tag-remove" data-remove-hotel="${idx}" title="Supprimer"
                aria-label="Supprimer l'h\u00f4tel ${esc(h.name)}">\u00d7</button>
      </div>
      <div class="trip-edit-row">
        <label for="hotel-${idx}-entity">Entity ID</label>
        <input type="text" id="hotel-${idx}-entity" name="hotel-${idx}-entity"
               value="${attr(h.entity_id)}" data-hotel="${idx}" data-field="entity_id"
               style="font-size:0.7rem;width:220px">
      </div>
      <div class="trip-edit-row trip-date-row">
        <label for="hotel-${idx}-checkin">Check-in</label>
        <input type="date" id="hotel-${idx}-checkin" name="hotel-${idx}-checkin"
               value="${attr(h.checkin)}" data-hotel="${idx}" data-field="checkin">
        <span class="date-sep">check-out</span>
        <input type="date" id="hotel-${idx}-checkout" name="hotel-${idx}-checkout"
               value="${attr(h.checkout)}" data-hotel="${idx}" data-field="checkout">
      </div>
      <div class="trip-edit-row">
        <label for="hotel-${idx}-threshold">Seuil alerte</label>
        <div class="input-unit">
          <input type="number" id="hotel-${idx}-threshold" name="hotel-${idx}-threshold"
                 value="${attr(h.price_threshold)}" placeholder="4500" data-hotel="${idx}" data-field="price_threshold">
          <span class="unit">\u20ac</span>
        </div>
      </div>
    `;
    // Event listeners
    card.querySelectorAll('input[data-field]').forEach(input => {
      input.addEventListener('change', () => {
        const ht = _adminConfig.hotels[input.dataset.hotel];
        const f = input.dataset.field;
        if (f === 'entity_id') ht.entity_id = input.value;
        else if (f === 'checkin') ht.checkin = input.value;
        else if (f === 'checkout') ht.checkout = input.value;
        else if (f === 'price_threshold') ht.price_threshold = input.value ? parseInt(input.value, 10) : null;
      });
    });
    card.querySelector('input[data-hotel-toggle]').addEventListener('change', (e) => {
      _adminConfig.hotels[idx].enabled = e.target.checked;
      card.classList.toggle('trip-disabled', !e.target.checked);
    });
    card.querySelector('button[data-remove-hotel]').addEventListener('click', () => {
      _adminConfig.hotels.splice(idx, 1);
      renderHotelsAdmin();
    });
    container.appendChild(card);
  });
}

// ── Sondes compagnies ──────────────────────────────────
// Etat serveur (quota consomme, cle presente, couverture) : la config
// seule ne dit pas si la sonde tourne reellement.
let _probeStatus = [];


function renderProbesAdmin() {
  const container = $('#admin-probes');
  if (!_adminConfig) return;
  const probes = _adminConfig.probes || [];
  const tripNames = (_adminConfig.trips || []).map(t => t.name);
  container.innerHTML = '';

  if (!probes.length) {
    container.innerHTML = '<span class="dim" style="font-size:0.8rem">Aucune sonde configurée</span>';
    return;
  }

  probes.forEach((p, idx) => {
    const st = _probeStatus.find(s => s.name === p.name) || {};
    const chosen = p.trips || [];
    const selectedTrip = chosen.length ? chosen[0] : '';
    // Une sonde peut viser plusieurs periodes en YAML ; le select n'en
    // montre qu'une, donc y toucher les ecraserait en silence.
    const multi = chosen.length > 1;
    // Une periode renommee laisse la sonde orpheline : sans cette option
    // explicite, le select affichait « Toutes » alors que la config dit
    // autre chose, et choisir « Toutes » ne declenchait aucun change.
    const orphan = selectedTrip && !tripNames.includes(selectedTrip);
    const card = document.createElement('div');
    card.className = 'trip-edit-card' + (p.enabled === false ? ' trip-disabled' : '');

    // La cle vit en variable d'environnement : sans elle la sonde se
    // court-circuite en silence, ce qui est indistinguable d'une panne.
    let quota = '';
    if (st.key_present === false) {
      quota = `<span style="color:var(--rose)">Clé absente : définir ${esc(st.key_env || p.key_env || 'AFKL_API_KEY')} dans .env</span>`;
    } else if (st.quota_limit) {
      const pct = Math.round(100 * (st.quota_used || 0) / st.quota_limit);
      quota = `Quota du jour : ${st.quota_used || 0} / ${st.quota_limit} (${pct} %) — seau « ${esc(st.quota_bucket || '')} »`;
    }
    const cover = (st.trips || [])
      .filter(t => t.cells)
      .map(t => `${esc(t.trip_name)} : ${t.cells} cellule(s)`
         + (t.best_eur != null ? `, meilleur ${Math.round(t.best_eur)} €` : ''))
      .join(' · ');
    const failing = (st.trips || []).filter(t => t.last_error);

    card.innerHTML = `
      <div class="trip-edit-header">
        <label class="toggle" title="${p.enabled !== false ? 'Désactiver' : 'Activer'}">
          <input type="checkbox" ${p.enabled !== false ? 'checked' : ''} data-probe-toggle="${idx}" aria-label="Activer ${esc(p.name)}">
          <span class="toggle-slider"></span>
        </label>
        <span class="trip-edit-name">${esc(p.name)}</span>
        <button class="tag-remove" data-remove-probe="${idx}" title="Supprimer"
                aria-label="Supprimer la sonde ${esc(p.name)}">×</button>
      </div>
      <div class="trip-edit-row">
        <label for="probe-${idx}-carrier">Compagnie</label>
        <select id="probe-${idx}-carrier" name="probe-${idx}-carrier" data-probe="${idx}" data-field="travel_host">
          ${Object.entries(CARRIER_LABEL).map(([code, label]) =>
            `<option value="${code}" ${p.travel_host === code ? 'selected' : (code === 'AF' && p.travel_host !== 'KL' ? 'selected' : '')}>${esc(label)}</option>`).join('')}
        </select>
      </div>
      <div class="trip-edit-row">
        <label for="probe-${idx}-origins">Départs</label>
        <input type="text" id="probe-${idx}-origins" name="probe-${idx}-origins"
               value="${attr((p.origins || []).join(', '))}" placeholder="CDG"
               data-probe="${idx}" data-field="origins" style="width:130px">
        <span class="date-sep">vers</span>
        <label for="probe-${idx}-dests" class="sr-only">Destinations</label>
        <input type="text" id="probe-${idx}-dests" name="probe-${idx}-dests"
               value="${attr((p.destinations || []).join(', '))}" placeholder="BKK"
               data-probe="${idx}" data-field="destinations" style="width:130px">
      </div>
      <div class="trip-edit-row">
        <label for="probe-${idx}-trip">Période</label>
        <select id="probe-${idx}-trip" name="probe-${idx}-trip" data-probe="${idx}" data-field="trips" ${multi ? 'disabled' : ''}>
          ${multi ? `<option selected>${chosen.length} périodes — éditer config.yml</option>` : `
          <option value="" ${selectedTrip ? '' : 'selected'}>Toutes</option>
          ${orphan ? `<option value="${attr(selectedTrip)}" selected>${esc(selectedTrip)} — période inconnue</option>` : ''}
          ${tripNames.map(n => `<option value="${attr(n)}" ${n === selectedTrip ? 'selected' : ''}>${esc(n)}</option>`).join('')}`}
        </select>
      </div>
      <div class="trip-edit-row">
        <label for="probe-${idx}-mode">Dates</label>
        <select id="probe-${idx}-mode" name="probe-${idx}-mode" data-probe="${idx}" data-field="date_mode">
          <option value="grid" ${p.date_mode === 'median' ? '' : 'selected'}>Grille tournante</option>
          <option value="median" ${p.date_mode === 'median' ? 'selected' : ''}>Date médiane seule</option>
        </select>
        <label for="probe-${idx}-cells" style="margin-left:0.6rem">Cellules / run</label>
        <input type="number" id="probe-${idx}-cells" name="probe-${idx}-cells" min="1" max="12"
               value="${attr(p.cells_per_run == null ? 4 : p.cells_per_run)}"
               data-probe="${idx}" data-field="cells_per_run" style="width:70px">
      </div>
      <div class="trip-edit-row">
        <label for="probe-${idx}-pax">Passagers</label>
        <select id="probe-${idx}-pax" name="probe-${idx}-pax" data-probe="${idx}" data-field="passengers">
          <option value="adults" ${p.passengers === 'family' ? '' : 'selected'}>Adultes seuls (comparable aux autres sources)</option>
          <option value="family" ${p.passengers === 'family' ? 'selected' : ''}>Famille complète (prix réel, non comparable)</option>
        </select>
      </div>
      ${quota ? `<div class="trip-edit-row dim" style="font-size:0.75rem">${quota}</div>` : ''}
      ${cover ? `<div class="trip-edit-row dim" style="font-size:0.75rem">${cover}</div>` : ''}
      ${failing.length ? `<div class="trip-edit-row" style="font-size:0.75rem;color:var(--rose)">Dernière erreur : ${esc(failing[0].last_error)}</div>` : ''}
    `;

    card.querySelectorAll('[data-field]').forEach(el => {
      el.addEventListener('change', () => {
        const pr = _adminConfig.probes[el.dataset.probe];
        const f = el.dataset.field;
        if (f === 'origins' || f === 'destinations') {
          pr[f] = el.value.split(',').map(s => s.trim().toUpperCase()).filter(Boolean);
        } else if (f === 'trips') {
          // Liste vide = toutes les periodes ; l'admin n'en expose qu'une,
          // le YAML reste libre d'en lister plusieurs.
          pr.trips = el.value ? [el.value] : [];
        } else if (f === 'cells_per_run') {
          pr.cells_per_run = parseInt(el.value, 10) || 4;
        } else {
          pr[f] = el.value;
        }
      });
    });
    card.querySelector('input[data-probe-toggle]').addEventListener('change', (e) => {
      _adminConfig.probes[idx].enabled = e.target.checked;
      card.classList.toggle('trip-disabled', !e.target.checked);
    });
    card.querySelector('button[data-remove-probe]').addEventListener('click', () => {
      _adminConfig.probes.splice(idx, 1);
      renderProbesAdmin();
    });
    container.appendChild(card);
  });
}

function addProbe() {
  const nameInput = $('#add-probe-name');
  const carrier = $('#add-probe-carrier').value;
  const originInput = $('#add-probe-origin');
  const destInput = $('#add-probe-dest');
  const name = nameInput.value.trim();
  const origin = originInput.value.trim().toUpperCase();
  const dest = destInput.value.trim().toUpperCase();
  if (!name || !origin || !dest) return;
  if (!_adminConfig.probes) _adminConfig.probes = [];
  // Bornes serveur repliquees ici : sans elles, la 5e sonde ou un nom
  // deja pris faisait echouer la sauvegarde ENTIERE en 422, emportant
  // les modifications sans rapport faites dans le meme ecran.
  if (_adminConfig.probes.length >= 4) {
    adminMessage('4 sondes maximum'); return;
  }
  if (_adminConfig.probes.some(p => p.name === name)) {
    adminMessage(`Une sonde s'appelle déjà « ${name} »`); return;
  }
  _adminConfig.probes.push({
    name, adapter: 'afklm', travel_host: carrier,
    key_env: 'AFKL_API_KEY',
    origins: [origin], destinations: [dest], trips: [],
    date_mode: 'grid', cells_per_run: 4, min_interval_s: 1.2,
    cabin: 'ECONOMY', passengers: 'adults', enabled: true,
  });
  renderProbesAdmin();
  nameInput.value = '';
  originInput.value = '';
  destInput.value = '';
}

function addHotel() {
  const nameInput = $('#add-hotel-name');
  const entityInput = $('#add-hotel-entity');
  const name = nameInput.value.trim();
  const entity = entityInput.value.trim();
  if (!name || !entity) return;
  if (!_adminConfig.hotels) _adminConfig.hotels = [];
  _adminConfig.hotels.push({ name, entity_id: entity, checkin: '', checkout: '', enabled: true });
  renderHotelsAdmin();
  nameInput.value = '';
  entityInput.value = '';
}

// Dates officielles vacances scolaires Zone A (Lyon) 2026-2027
const VACANCES_ZONE_A = {
  'Toussaint 2026':       ['2026-10-17', '2026-11-02'],
  'Noël 2026':            ['2026-12-19', '2027-01-04'],
  'Hiver 2027':           ['2027-02-13', '2027-03-01'],
  'Printemps 2027':       ['2027-04-10', '2027-04-26'],
  'Été 2027 (2-3 sem)':   ['2027-07-05', '2027-08-31'],
};

function renderTrips() {
  const container = $('#admin-trips');
  container.innerHTML = '';
  (_adminConfig.trips || []).forEach((trip, idx) => {
    const card = document.createElement('div');
    card.className = 'trip-edit-card';
    const ow = trip.outbound_window || ['', ''];
    const rw = trip.return_window || ['', ''];
    // Les dates officielles viennent de la config si elle les porte : la table
    // codée en dur ci-dessus ne couvre que 2026-2027 et indexe par nom exact.
    const vac = trip.vacation || VACANCES_ZONE_A[trip.name];
    const vacInfo = vac
      ? `<span class="dim trip-edit-vac">Vacances : ${dateFmt(vac[0])} \u2192 ${dateFmt(vac[1])}</span>`
      : '';
    const enabled = trip.enabled !== false;
    card.classList.toggle('trip-disabled', !enabled);
    card.innerHTML = `
      <div class="trip-edit-header">
        <label class="toggle" title="${enabled ? 'Désactiver' : 'Activer'} cette période">
          <input type="checkbox" ${enabled ? 'checked' : ''} data-trip-toggle="${idx}" aria-label="Activer ${esc(trip.name)}">
          <span class="toggle-slider"></span>
        </label>
        <span class="trip-edit-name">${esc(trip.name)}</span>
        ${vacInfo}
      </div>
      <div class="trip-edit-row trip-date-row">
        <label for="trip-${idx}-ow0">Aller entre le</label>
        <input type="date" id="trip-${idx}-ow0" name="trip-${idx}-ow0" data-trip="${idx}" data-field="ow0" value="${attr(ow[0])}">
        <span class="date-sep">et le</span>
        <input type="date" id="trip-${idx}-ow1" name="trip-${idx}-ow1" data-trip="${idx}" data-field="ow1" value="${attr(ow[1])}">
      </div>
      <div class="trip-edit-row trip-date-row">
        <label for="trip-${idx}-rw0">Retour entre le</label>
        <input type="date" id="trip-${idx}-rw0" name="trip-${idx}-rw0" data-trip="${idx}" data-field="rw0" value="${attr(rw[0])}">
        <span class="date-sep">et le</span>
        <input type="date" id="trip-${idx}-rw1" name="trip-${idx}-rw1" data-trip="${idx}" data-field="rw1" value="${attr(rw[1])}">
      </div>
      <div class="trip-edit-row">
        <label for="trip-${idx}-threshold">Seuil alerte</label>
        <div class="input-unit">
          <input type="number" id="trip-${idx}-threshold" name="trip-${idx}-threshold" data-trip="${idx}" data-field="threshold" value="${attr(trip.price_threshold)}" placeholder="800">
          <span class="unit">\u20ac</span>
        </div>
      </div>
      <div class="trip-edit-row">
        <label for="trip-${idx}-minn">Dur\u00e9e</label>
        <div class="input-unit">
          <input type="number" min="1" max="365" id="trip-${idx}-minn" name="trip-${idx}-minn" data-trip="${idx}" data-field="min_nights" value="${attr(trip.min_nights)}" placeholder="min">
        </div>
        <span class="date-sep">\u00e0</span>
        <div class="input-unit">
          <input type="number" min="1" max="365" id="trip-${idx}-maxn" name="trip-${idx}-maxn" data-trip="${idx}" data-field="max_nights" value="${attr(trip.max_nights)}" placeholder="max">
        </div>
        <span class="dim">nuits \u2014 vide = sans contrainte</span>
      </div>
    `;
    card.querySelectorAll('input[data-field]').forEach(input => {
      input.addEventListener('change', () => {
        const t = _adminConfig.trips[input.dataset.trip];
        const f = input.dataset.field;
        if (f === 'ow0') t.outbound_window[0] = input.value;
        else if (f === 'ow1') t.outbound_window[1] = input.value;
        else if (f === 'rw0') t.return_window[0] = input.value;
        else if (f === 'rw1') t.return_window[1] = input.value;
        else if (f === 'threshold') t.price_threshold = input.value ? parseInt(input.value, 10) : null;
        // Champ vide = pas de contrainte : on retire la clé plutôt que
        // d'écrire un null qui polluerait config.yml.
        else if (f === 'min_nights' || f === 'max_nights') {
          if (input.value) t[f] = parseInt(input.value, 10);
          else delete t[f];
        }
      });
    });
    card.querySelector('input[data-trip-toggle]').addEventListener('change', (e) => {
      _adminConfig.trips[idx].enabled = e.target.checked;
      card.classList.toggle('trip-disabled', !e.target.checked);
    });
    container.appendChild(card);
  });
}

// FastAPI renvoie un `detail` tableau pour une erreur de validation
// Pydantic, et une chaîne pour celles levées par config.save_raw.
// Sans ce tri, l'admin affichait « Erreur : [object Object] ».
function formatDetail(detail, fallback) {
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail.map(e => {
      const champ = (e.loc || []).slice(1).join('.');
      return champ ? `${champ} : ${e.msg}` : e.msg;
    }).join(' ; ');
  }
  return fallback;
}

async function saveConfig() {
  const btn = $('#admin-save');
  const status = $('#admin-status');
  btn.disabled = true;
  status.textContent = 'Sauvegarde...';
  try {
    const r = await adminFetch('/api/admin/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(_adminConfig),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(formatDetail(err.detail, r.statusText));
    }
    status.textContent = 'Sauvegardé !';
    status.style.color = 'var(--green)';
    loadOverview();
    setTimeout(() => {
      status.textContent = '';
      status.style.color = '';
    }, 3000);
  } catch (e) {
    status.textContent = 'Erreur : ' + e.message;
    status.style.color = 'var(--rose)';
  } finally {
    btn.disabled = false;
  }
}

// Une notification qui n'arrive pas se diagnostiquait dans les logs du
// conteneur : ce bouton exerce la chaîne ntfy depuis l'interface.
async function testNotification() {
  const btn = $('#admin-test-notif');
  const status = $('#admin-test-notif-status');
  btn.disabled = true;
  status.style.color = '';
  status.textContent = 'Envoi...';
  try {
    const r = await adminFetch('/api/test-notification', { method: 'POST' });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(formatDetail(err.detail, `HTTP ${r.status}`));
    }
    // La route répond 200 même quand ntfy refuse : sans lire `sent`, un
    // topic erroné s'afficherait comme un succès.
    const data = await r.json().catch(() => ({}));
    if (data.sent === false) throw new Error('ntfy a refusé (topic ou token ?)');
    status.textContent = 'Notification envoyée';
    status.style.color = 'var(--green)';
  } catch (e) {
    status.textContent = 'Échec : ' + e.message;
    status.style.color = 'var(--rose)';
  } finally {
    btn.disabled = false;
    setTimeout(() => {
      status.textContent = '';
      status.style.color = '';
    }, 8000);
  }
}

// Admin button bindings
$('#btn-add-origin').addEventListener('click', addOrigin);
$('#btn-add-dest').addEventListener('click', addDestination);
$('#admin-save').addEventListener('click', saveConfig);
$('#btn-add-hotel').addEventListener('click', addHotel);
$('#btn-add-probe').addEventListener('click', addProbe);
$('#probe-heatmap-select').addEventListener('change', (e) => {
  if (_probeTrip) renderProbeHeatmap(_probeTrip, e.target.value, _tripSeq);
});
$('#admin-test-notif').addEventListener('click', testNotification);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && e.target.id === 'add-origin') addOrigin();
  if (e.key === 'Enter' && e.target.id === 'add-dest') addDestination();
});

// ── Init ─────────────────────────────────────────────

loadOverview();
loadIntro();
loadInfos();
checkRunningState();

// Le rafraîchissement était minuté à 60 s, onglet en arrière-plan compris :
// ~17 000 appels internes par jour pour un cron qui tourne toutes les 6 h.
// On espace à 5 min et on saute les ticks inutiles (onglet caché ou autre
// section), le rattrapage se fait au retour sur « Vue d'ensemble ».
const OVERVIEW_REFRESH_MS = 300000;
setInterval(() => {
  const overview = $('#tab-overview');
  if (document.hidden || !overview || !overview.classList.contains('active')) {
    _overviewDirty = true;
    return;
  }
  loadOverview();
}, OVERVIEW_REFRESH_MS);

document.addEventListener('visibilitychange', () => {
  const overview = $('#tab-overview');
  if (!document.hidden && _overviewDirty
      && overview && overview.classList.contains('active')) {
    loadOverview();
  }
});

async function loadIntro() {
  try {
    const [cfg, trips] = await Promise.all([
      api('/api/config-summary'),
      api('/api/trips'),
    ]);
    const cron = parseCron(cfg.schedule_cron);
    const kids = cfg.children && cfg.children.length
      ? ` + ${cfg.children.length} enfant${cfg.children.length > 1 ? 's' : ''} (${cfg.children.join(' et ')} ans)`
      : '';
    const pax = (cfg.adults === 1 ? '1 adulte' : cfg.adults + ' adultes') + kids;
    const nbPeriodes = trips.length;
    // L'intro annonçait « Dates ±3j » alors que les fenêtres sont libres dans
    // config.yml (jusqu'à 27 jours) : on les mesure au lieu de les supposer.
    $('#intro').textContent =
      `Check auto ${cron} pour ${pax} · ` +
      `${nbPeriodes} périodes vacances Zone A · ` +
      `Départs ${cfg.origins.join(', ')} → ${cfg.destinations.join(', ')} · ` +
      `${windowLabel(trips)} · ` +
      `Vols < ${cfg.max_fly_duration_hours}h`;
  } catch(e) {}
}

// Largeur réelle des fenêtres de dates aller, en jours (bornes incluses).
function windowLabel(trips) {
  const widths = trips.map(t => {
    const w = t.outbound_window;
    if (!w || !w[0] || !w[1]) return 0;
    return Math.round((_asDate(w[1]) - _asDate(w[0])) / 86400000) + 1;
  }).filter(n => n > 0);
  if (!widths.length) return 'Fenêtres de dates libres';
  const min = Math.min(...widths), max = Math.max(...widths);
  return min === max
    ? `Fenêtres de ${min} j`
    : `Fenêtres de ${min} à ${max} j`;
}

function parseCron(expr) {
  if (!expr) return '';
  const parts = expr.split(' ');
  const min = parts[0], hour = parts[1];
  if (hour.includes(',')) return `${hour.split(',').length}x/jour (${hour.replace(/,/g,'h, ')}h)`;
  if (hour.includes('*/')) return `toutes les ${hour.replace('*/','')}h`;
  if (hour === '*') return 'toutes les heures';
  return `à ${hour}h${min !== '0' ? min : ''}`;
}
