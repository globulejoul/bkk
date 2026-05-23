"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const fmt = new Intl.NumberFormat('fr-FR');
const dateFmt = (s) => s ? new Date(s).toLocaleDateString('fr-FR') : '—';
const dateTimeFmt = (s) => s ? new Date(s).toLocaleString('fr-FR') : '—';

let tripChart = null;

// ── Tabs ─────────────────────────────────────────────

$$('.tabs button').forEach(b => {
  b.addEventListener('click', () => {
    $$('.tabs button').forEach(x => x.classList.remove('active'));
    $$('.tab').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    $('#tab-' + b.dataset.tab).classList.add('active');
    if (b.dataset.tab === 'trip') loadTripDetail();
    if (b.dataset.tab === 'alerts') loadAlerts();
    if (b.dataset.tab === 'runs') loadRuns();
  });
});

// ── Run-now button ──────────────────────────────────

$('#run-now').addEventListener('click', async () => {
  const btn = $('#run-now');
  btn.disabled = true;
  const r = await fetch('/api/run-now', { method: 'POST' });
  if (r.status === 409) {
    alert('Un check est déjà en cours.');
    btn.disabled = false;
    return;
  }
  // Poll until the run finishes
  let elapsed = 0;
  const tick = () => {
    elapsed += 5;
    const min = Math.floor(elapsed / 60);
    const sec = elapsed % 60;
    btn.textContent = `⏳ ${min}:${String(sec).padStart(2, '0')}`;
  };
  tick();
  const timer = setInterval(tick, 5000);

  const poll = setInterval(async () => {
    try {
      const runs = await fetch('/api/runs?limit=1').then(r => r.json());
      if (runs.length && runs[0].status !== 'running') {
        clearInterval(poll);
        clearInterval(timer);
        const run = runs[0];
        const dur = run.finished_at && run.started_at
          ? Math.round((new Date(run.finished_at) - new Date(run.started_at)) / 1000)
          : elapsed;
        btn.textContent = run.status === 'ok'
          ? `✓ ${run.trips_checked} périodes, ${run.alerts_generated} alertes (${dur}s)`
          : `✗ Erreur (${dur}s)`;
        btn.disabled = false;
        loadOverview();
        setTimeout(() => { btn.textContent = '↻ Check'; }, 8000);
      }
    } catch(e) { /* ignore poll errors */ }
  }, 5000);
});

// ── Overview ────────────────────────────────────────

async function loadOverview() {
  const trips = await fetch('/api/trips').then(r => r.json());
  const grid = $('#trips-grid');
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
}

function buildTripCard(t) {
  const card = document.createElement('div');
  card.className = 'trip-card' + (t.current_best === null ? ' no-data' : '');
  card.addEventListener('click', () => {
    $('#trip-select').value = t.trip_name;
    $$('.tabs button').forEach(b => {
      if (b.dataset.tab === 'trip') b.click();
    });
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
    <div class="name">${t.trip_name}</div>
    <div class="dates">${dates}</div>
    <div class="price-main ${priceClass}">
      ${priceTxt}${t.current_best != null ? '<span class="currency">€</span>' : ''}
    </div>
    <div class="price-stats">
      <span><span class="stat-label">bas</span> ${t.all_time_low != null ? Math.round(t.all_time_low) + '€' : '—'}</span>
      <span><span class="stat-label">moy 30j</span> ${t.avg_30d != null ? Math.round(t.avg_30d) + '€' : '—'}</span>
      <span><span class="stat-label">haut</span> ${t.all_time_high != null ? Math.round(t.all_time_high) + '€' : '—'}</span>
    </div>
    ${t.threshold ? `
      <div class="threshold">
        <span class="dim">Seuil d'alerte</span>
        <span class="target">≤ ${t.threshold}€</span>
      </div>
    ` : ''}
  `;
  return card;
}

// ── Trip detail ─────────────────────────────────────

$('#trip-select').addEventListener('change', loadTripDetail);

async function loadTripDetail() {
  const name = $('#trip-select').value;
  if (!name) return;

  const [history, breakdown] = await Promise.all([
    fetch(`/api/trips/${encodeURIComponent(name)}/history`).then(r => r.json()),
    fetch(`/api/trips/${encodeURIComponent(name)}/breakdown`).then(r => r.json()),
  ]);

  // Chart
  const ctx = $('#trip-chart').getContext('2d');
  if (tripChart) tripChart.destroy();

  // Find threshold from trips list
  const trips = await fetch('/api/trips').then(r => r.json());
  const tConf = trips.find(x => x.trip_name === name) || {};

  const datasets = [{
    label: 'Prix min (€)',
    data: history.map(p => ({ x: p.check_date, y: p.price_eur, meta: p })),
    borderColor: '#e4a42e',
    backgroundColor: '#e4a42e22',
    fill: true,
    tension: 0.3,
    pointRadius: 3,
    pointHoverRadius: 6,
  }];
  if (tConf.threshold && history.length) {
    datasets.push({
      label: 'Seuil',
      data: history.map(p => ({ x: p.check_date, y: tConf.threshold })),
      borderColor: '#7abcc4',
      borderDash: [6, 4],
      pointRadius: 0,
      fill: false,
    });
  }

  tripChart = new Chart(ctx, {
    type: 'line',
    data: { datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: '#f0e8db', font: { family: 'JetBrains Mono' } } },
        tooltip: {
          callbacks: {
            label: (item) => {
              const m = item.raw.meta;
              if (!m) return Math.round(item.parsed.y) + '€';
              return [
                Math.round(item.parsed.y) + '€',
                'Origines: ' + (m.origins || '—'),
                'Destinations: ' + (m.destinations || '—'),
              ];
            },
          },
        },
      },
      scales: {
        x: { type: 'time', time: { unit: 'day' },
             ticks: { color: '#b0a08a', font: { family: 'JetBrains Mono' } },
             grid: { color: '#3d3225' } },
        y: { ticks: { color: '#b0a08a', font: { family: 'JetBrains Mono' },
                      callback: v => v + '€' },
             grid: { color: '#3d3225' } },
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
      <td>${b.origin}</td>
      <td>${b.destination}</td>
      <td class="price-cell">${Math.round(b.best_eur)}€</td>
      <td>${b.airlines || '—'}</td>
      <td>${dateFmt(b.last_seen)}</td>
    `;
    tbody.appendChild(tr);
  });
  if (!breakdown.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="dim">Pas encore de données.</td></tr>';
  }
}

// ── Alerts ──────────────────────────────────────────

async function loadAlerts() {
  const alerts = await fetch('/api/alerts').then(r => r.json());
  const list = $('#alerts-list');
  list.innerHTML = '';
  if (!alerts.length) {
    list.innerHTML = '<p class="dim">Aucune alerte pour le moment.</p>';
    return;
  }
  alerts.forEach(a => {
    const p = a.payload || {};
    const card = document.createElement('div');
    let cls = 'alert-card';
    if (a.kind === 'rise') cls += ' rise';
    else if (p.hit_threshold) cls += ' threshold';
    card.className = cls;
    const kindLabel = a.kind === 'rise' ? '📈 Hausse'
      : p.hit_threshold ? '🎯 Seuil atteint' : '📉 Nouveau bas';
    card.innerHTML = `
      <div class="alert-header">
        <div>
          <div class="kind">${kindLabel} • ${dateTimeFmt(a.sent_at)}</div>
          <div class="trip-name">${a.trip_name}</div>
        </div>
        <div class="price">${Math.round(a.price_eur)}€</div>
      </div>
      <div class="alert-meta">
        ${p.airlines || ''} • ${p.origin || '?'} → ${p.destination || '?'}
        ${p.outbound_date ? '• ' + p.outbound_date + ' → ' + p.return_date : ''}
      </div>
    `;
    list.appendChild(card);
  });
}

// ── Runs log ─────────────────────────────────────────

async function loadRuns() {
  const runs = await fetch('/api/runs').then(r => r.json());
  const tbody = $('#runs-table tbody');
  tbody.innerHTML = '';
  runs.forEach(r => {
    const dur = r.finished_at && r.started_at
      ? Math.round((new Date(r.finished_at) - new Date(r.started_at)) / 1000) + 's'
      : (r.status === 'running' ? '…' : '—');
    const statusColor = r.status === 'ok' ? 'var(--teal)'
      : r.status === 'error' ? 'var(--rose)' : 'var(--amber)';
    tbody.innerHTML += `
      <tr>
        <td>${dateTimeFmt(r.started_at)}</td>
        <td>${dur}</td>
        <td style="color:${statusColor}">${r.status}</td>
        <td>${r.trips_checked ?? '—'}</td>
        <td>${r.alerts_generated ?? '—'}</td>
        <td class="dim">${r.error || ''}</td>
      </tr>
    `;
  });
}

// ── Init ─────────────────────────────────────────────

loadOverview();
setInterval(loadOverview, 60000); // refresh every minute
