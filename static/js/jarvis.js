'use strict';

const API = (path) => fetch(`/api${path}`).then(r => r.json());
const POST = (path, body) => fetch(`/api${path}`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)}).then(r => r.json());
const DEL  = (path) => fetch(`/api${path}`, {method:'DELETE'}).then(r => r.json());

let allSignals = [], allThreats = [], allNews = [];

// ── Formatters ─────────────────────────────────────────────────────────────────
const fmt2  = (v) => v != null ? Number(v).toFixed(2) : 'N/A';
const fmt4  = (v) => v != null ? Number(v).toFixed(4) : 'N/A';
const fmtPct= (v) => v != null ? `${v >= 0 ? '+' : ''}${Number(v).toFixed(2)}%` : 'N/A';
const fmtPrice = (v) => {
  if (v == null) return 'N/A';
  v = Number(v);
  return v > 1000 ? `$${v.toLocaleString('en',{maximumFractionDigits:0})}` :
         v > 1    ? `$${v.toFixed(2)}` :
                    `$${v.toFixed(6)}`;
};
const timeAgo = (iso) => {
  if (!iso) return '';
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff/60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m/60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h/24)}d ago`;
};
const sevColor = {'Critical':'danger','High':'warning','Medium':'primary','Low':'success'};
const sentIcon = {'positive':'bi-arrow-up-circle-fill text-success','negative':'bi-arrow-down-circle-fill text-danger','neutral':'bi-dash-circle text-secondary'};

// ── Signals Tab ───────────────────────────────────────────────────────────────
async function loadSignals() {
  const data = await API('/signals?limit=100');
  allSignals = data;
  renderSignals();
}

function renderSignals() {
  const status = document.getElementById('sig-filter-status').value;
  const cls    = document.getElementById('sig-filter-class').value;
  let filtered = allSignals.filter(s =>
    (!status || s.status === status) && (!cls || s.asset_class === cls)
  );
  document.getElementById('signal-count').textContent = `${filtered.length} signals`;
  const grid = document.getElementById('signals-grid');
  if (!filtered.length) { grid.innerHTML = '<div class="col-12 text-muted text-center py-5">No signals found</div>'; return; }
  grid.innerHTML = filtered.map(s => {
    const dir    = (s.direction || 'Long').toLowerCase();
    const conf   = s.confidence || 0;
    const confCls= conf >= 75 ? 'high' : conf >= 55 ? 'medium' : 'low';
    const rr     = s.entry_price && s.target_price && s.stop_loss
                   ? ((s.target_price - s.entry_price) / (s.entry_price - s.stop_loss)).toFixed(1)
                   : 'N/A';
    const statusBadge = {
      'Active':   'bg-success',
      'Executed': 'bg-primary',
      'Expired':  'bg-secondary',
      'Rejected': 'bg-danger',
      'Closed':   'bg-dark border border-secondary',
    }[s.status] || 'bg-secondary';
    return `
    <div class="col-xl-3 col-lg-4 col-md-6">
      <div class="card signal-card ${dir} h-100">
        <div class="card-header d-flex justify-content-between align-items-center py-2">
          <div>
            <span class="fw-bold">${s.asset_symbol}</span>
            <span class="badge ${dir === 'long' ? 'bg-success' : 'bg-primary'} ms-1">${s.direction}</span>
            <span class="badge ${statusBadge} ms-1">${s.status}</span>
          </div>
          <small class="text-muted">${timeAgo(s.generated_at)}</small>
        </div>
        <div class="card-body py-2 px-3">
          <div class="conf-bar ${confCls} mb-2" style="width:${conf}%"></div>
          <div class="small mb-2 text-muted">${s.asset_name || ''} · ${s.asset_class || ''} · ${s.timeframe || ''} · <span class="text-warning">${conf}% conf</span></div>
          <div class="d-flex justify-content-between small mb-1">
            <span>Entry</span><span class="fw-bold text-info">${fmtPrice(s.entry_price)}</span>
          </div>
          <div class="d-flex justify-content-between small mb-1">
            <span>Target</span><span class="fw-bold text-success">${fmtPrice(s.target_price)}</span>
          </div>
          <div class="d-flex justify-content-between small mb-2">
            <span>Stop</span><span class="fw-bold text-danger">${fmtPrice(s.stop_loss)}</span>
          </div>
          <div class="d-flex justify-content-between small mb-2">
            <span class="text-muted">R:R</span><span class="badge bg-dark border border-secondary">${rr}</span>
          </div>
          <p class="small text-muted mb-2" style="font-size:.72rem;line-height:1.4;max-height:80px;overflow:hidden">${(s.reasoning||'').slice(0,200)}${(s.reasoning||'').length>200?'…':''}</p>
          ${s.key_risks ? `<p class="small text-warning mb-2" style="font-size:.7rem"><i class="bi bi-exclamation-triangle-fill"></i> ${s.key_risks.slice(0,120)}</p>` : ''}
        </div>
        <div class="card-footer py-1 d-flex gap-1">
          ${s.status==='Active' ? `<button class="btn btn-success btn-sm flex-fill py-0" style="font-size:.72rem" onclick="executeSignal('${s.id}')"><i class="bi bi-play-fill"></i> Execute</button>` : ''}
          <button class="btn btn-outline-danger btn-sm py-0" style="font-size:.72rem" onclick="deleteSignal('${s.id}')"><i class="bi bi-trash"></i></button>
        </div>
      </div>
    </div>`;
  }).join('');
}

async function executeSignal(id) {
  if (!confirm('Submit bracket order for this signal?')) return;
  const res = await POST(`/signals/${id}/execute`, {});
  alert(res.error || `Order submitted: ${JSON.stringify(res.order || res)}`);
  loadSignals();
}

async function deleteSignal(id) {
  if (!confirm('Delete this signal?')) return;
  await DEL(`/signals/${id}`);
  loadSignals();
}

document.getElementById('sig-filter-status').addEventListener('change', renderSignals);
document.getElementById('sig-filter-class').addEventListener('change', renderSignals);

// ── Positions Tab ─────────────────────────────────────────────────────────────
async function loadPositions() {
  try {
    const data = await API('/positions');
    const acct = data.account || {};
    document.getElementById('account-summary').innerHTML = `
      <div class="col-auto"><div class="card px-3 py-2"><div class="small text-muted">Equity</div><div class="fw-bold text-info">$${(acct.equity||0).toLocaleString('en',{maximumFractionDigits:2})}</div></div></div>
      <div class="col-auto"><div class="card px-3 py-2"><div class="small text-muted">Cash</div><div class="fw-bold text-success">$${(acct.cash||0).toLocaleString('en',{maximumFractionDigits:2})}</div></div></div>
      <div class="col-auto"><div class="card px-3 py-2"><div class="small text-muted">Buying Power</div><div class="fw-bold">${(acct.buying_power||0).toLocaleString('en',{maximumFractionDigits:2})}</div></div></div>
      <div class="col-auto"><div class="card px-3 py-2"><div class="small text-muted">Day Trades</div><div class="fw-bold">${acct.day_trade_count||0}</div></div></div>
    `;
    const tbody = document.getElementById('positions-body');
    const positions = data.positions || [];
    if (!positions.length) { tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted py-4">No open positions</td></tr>'; return; }
    tbody.innerHTML = positions.map(p => {
      const plpc = p.unrealized_plpc || 0;
      const plCls = plpc >= 0 ? 'pl-positive' : 'pl-negative';
      const cls   = p.symbol.includes('/') ? 'Crypto' : 'Equity';
      return `<tr>
        <td class="fw-bold">${p.symbol}</td>
        <td><span class="badge ${cls==='Crypto'?'bg-warning text-dark':'bg-primary'}">${cls}</span></td>
        <td>${Number(p.qty).toLocaleString()}</td>
        <td>${fmtPrice(p.avg_entry)}</td>
        <td>$${Number(p.market_value).toLocaleString('en',{maximumFractionDigits:2})}</td>
        <td class="${plCls}">$${Number(p.unrealized_pl).toFixed(2)}</td>
        <td class="${plCls} fw-bold">${fmtPct(plpc)}</td>
        <td>
          <button class="btn btn-outline-danger btn-sm py-0 px-1" style="font-size:.7rem" onclick="closePosition('${p.symbol}')">
            <i class="bi bi-x-circle"></i> Close
          </button>
        </td>
      </tr>`;
    }).join('');
  } catch(e) {
    document.getElementById('positions-body').innerHTML = `<tr><td colspan="8" class="text-danger py-3">${e.message}</td></tr>`;
  }
}

async function closePosition(symbol) {
  if (!confirm(`Close position in ${symbol}?`)) return;
  const res = await POST(`/positions/${symbol}/close`, {});
  alert(res.error || `${symbol} position closed`);
  loadPositions();
}

// ── Threats Tab ───────────────────────────────────────────────────────────────
async function loadThreats() {
  const data = await API('/threats?limit=60');
  allThreats = data;
  renderThreats();
}

function renderThreats() {
  const grid = document.getElementById('threats-grid');
  if (!allThreats.length) { grid.innerHTML = '<div class="col-12 text-muted text-center py-5">No active threats</div>'; return; }
  grid.innerHTML = allThreats.map(t => `
    <div class="col-xl-3 col-lg-4 col-md-6">
      <div class="card h-100 border-${sevColor[t.severity]||'secondary'}">
        <div class="card-header py-2 d-flex justify-content-between">
          <span class="badge sev-${t.severity}">${t.severity}</span>
          <small class="text-muted">${t.country||''}</small>
        </div>
        <div class="card-body py-2">
          <p class="fw-bold mb-1 small">${t.source_url ? `<a href="${t.source_url}" target="_blank" class="text-info text-decoration-none">${t.title}</a>` : t.title}</p>
          <p class="text-muted small mb-1" style="font-size:.72rem">${(t.description||'').slice(0,200)}</p>
          <div class="d-flex gap-1 flex-wrap">
            <span class="badge bg-dark border border-secondary small">${t.event_type||''}</span>
            <span class="badge bg-dark border border-secondary small">${t.region||''}</span>
          </div>
        </div>
        <div class="card-footer py-1 small text-muted">${t.source} · ${timeAgo(t.published_at)}</div>
      </div>
    </div>
  `).join('');
}

// ── News Tab ──────────────────────────────────────────────────────────────────
async function loadNews() {
  const data = await API('/news?limit=80');
  allNews = data;
  renderNews();
}

function renderNews() {
  const cat  = document.getElementById('news-filter-cat').value;
  const sent = document.getElementById('news-filter-sent').value;
  let filtered = allNews.filter(n =>
    (!cat  || n.category  === cat) &&
    (!sent || n.sentiment === sent)
  );
  const container = document.getElementById('news-list');
  container.innerHTML = filtered.slice(0, 60).map(n => `
    <div class="card mb-2">
      <div class="card-body py-2 px-3 d-flex align-items-start gap-3">
        <i class="bi ${sentIcon[n.sentiment]||'bi-dash-circle text-secondary'} mt-1"></i>
        <div class="flex-grow-1">
          <div class="d-flex justify-content-between">
            <span class="fw-bold small">${n.url ? `<a href="${n.url}" target="_blank" class="text-info text-decoration-none">${n.title}</a>` : n.title}</span>
            <small class="text-muted ms-3 text-nowrap">${timeAgo(n.published_at)}</small>
          </div>
          <p class="small text-muted mb-1" style="font-size:.72rem">${(n.summary||'').slice(0,200)}</p>
          <div class="d-flex gap-1">
            <span class="badge bg-secondary small">${n.source}</span>
            <span class="badge bg-dark border border-secondary small">${n.category||''}</span>
            ${n.affected_assets ? `<span class="badge bg-dark border border-warning text-warning small">${n.affected_assets}</span>` : ''}
          </div>
        </div>
      </div>
    </div>
  `).join('');
}

document.getElementById('news-filter-cat').addEventListener('change', renderNews);
document.getElementById('news-filter-sent').addEventListener('change', renderNews);

// ── Scanner ───────────────────────────────────────────────────────────────────
async function runScanner() {
  const symbol = document.getElementById('scanner-symbol').value.trim().toUpperCase();
  const tfs    = document.getElementById('scanner-tfs').value.split(',');
  if (!symbol) { alert('Enter a symbol'); return; }
  document.getElementById('scanner-loading').classList.remove('d-none');
  document.getElementById('scanner-result').innerHTML = '';
  try {
    const data = await POST('/analyze', {symbol, timeframes: tfs});
    document.getElementById('scanner-loading').classList.add('d-none');
    if (data.error) { document.getElementById('scanner-result').innerHTML = `<div class="alert alert-danger">${data.error}</div>`; return; }
    document.getElementById('scanner-result').innerHTML = `<pre>${data.prompt_block || JSON.stringify(data.ta, null, 2)}</pre>`;
  } catch(e) {
    document.getElementById('scanner-loading').classList.add('d-none');
    document.getElementById('scanner-result').innerHTML = `<div class="alert alert-danger">${e.message}</div>`;
  }
}
document.getElementById('scanner-symbol').addEventListener('keydown', e => { if (e.key === 'Enter') runScanner(); });

// ── Jobs Tab ──────────────────────────────────────────────────────────────────
async function loadJobs() {
  const data = await API('/jobs/status');
  const schedules = {
    market:'every 15 min', threats:'every 15 min (offset 7m)',
    signals:'every 30 min', execute:'every 30 min (offset 3m)',
    positions:'every 5 min', telegram:'every 1 min'
  };
  document.getElementById('jobs-grid').innerHTML = Object.entries(data).map(([name, job]) => {
    const statusBadge = {'ok':'success','running':'warning','error':'danger','idle':'secondary'}[job.status]||'secondary';
    return `
    <div class="col-xl-2 col-lg-3 col-md-4 col-6">
      <div class="card h-100">
        <div class="card-body text-center py-3">
          <div class="job-dot ${job.status} mx-auto mb-2"></div>
          <div class="fw-bold text-capitalize mb-1">${name}</div>
          <div class="badge bg-${statusBadge} mb-2">${job.status}</div>
          <div class="small text-muted mb-3">${schedules[name]||''}</div>
          ${job.last ? `<div class="small text-muted mb-2">${timeAgo(job.last)}</div>` : ''}
          ${job.error ? `<div class="small text-danger mb-2" style="font-size:.68rem">${job.error.slice(0,80)}</div>` : ''}
          <button class="btn btn-outline-success btn-sm w-100" onclick="triggerJob('${name}')">
            <i class="bi bi-play-fill"></i> Run Now
          </button>
        </div>
      </div>
    </div>`;
  }).join('');

  // Update navbar indicators
  document.getElementById('job-indicators').innerHTML = Object.entries(data).map(([name, job]) =>
    `<span title="${name}: ${job.status}" class="job-dot ${job.status}"></span>`
  ).join('');
}

async function triggerJob(name) {
  await POST(`/jobs/${name}/trigger`, {});
  setTimeout(loadJobs, 1500);
}

// ── Settings Tab ──────────────────────────────────────────────────────────────
async function loadSettings() {
  const data = await API('/settings');
  document.getElementById('settings-body').innerHTML = data.map(c => `
    <tr>
      <td>${c.label}</td>
      <td><span class="badge bg-secondary">${c.platform}</span></td>
      <td><code class="small">${(c.api_key||'').slice(0,8)}***</code></td>
      <td class="small text-muted">${c.api_url||'—'}</td>
      <td class="small">${c.extra_field_1||'—'}</td>
      <td>${c.is_default ? '<i class="bi bi-star-fill text-warning"></i>' : ''}</td>
      <td>${c.is_active ? '<i class="bi bi-check-circle-fill text-success"></i>' : '<i class="bi bi-x-circle text-danger"></i>'}</td>
      <td class="d-flex gap-1">
        ${!c.is_default ? `<button class="btn btn-outline-warning btn-sm py-0" onclick="setDefault('${c.id}')"><i class="bi bi-star"></i></button>` : ''}
        <button class="btn btn-outline-danger btn-sm py-0" onclick="deleteSetting('${c.id}')"><i class="bi bi-trash"></i></button>
      </td>
    </tr>
  `).join('');
}

async function saveConfig() {
  const form = document.getElementById('config-form');
  const fd = new FormData(form);
  const body = {
    label: fd.get('label'), platform: fd.get('platform'),
    api_key: fd.get('api_key'), api_secret: fd.get('api_secret'),
    api_url: fd.get('api_url'), extra_field_1: fd.get('extra_field_1'),
    is_default: document.getElementById('cfg-is-default').checked,
    is_active:  document.getElementById('cfg-is-active').checked,
  };
  await POST('/settings', body);
  bootstrap.Modal.getInstance(document.getElementById('addConfigModal')).hide();
  form.reset();
  loadSettings();
}

async function setDefault(id) {
  await POST(`/settings/${id}/set-default`, {});
  loadSettings();
}

async function deleteSetting(id) {
  if (!confirm('Delete this config?')) return;
  await DEL(`/settings/${id}`);
  loadSettings();
}

// ── Bootstrap & Auto-refresh ───────────────────────────────────────────────────
function refreshAll() {
  loadSignals();
  loadJobs();
  document.getElementById('last-refresh').textContent = `Updated ${new Date().toLocaleTimeString()}`;
}

document.querySelectorAll('[data-bs-toggle="tab"]').forEach(tab => {
  tab.addEventListener('shown.bs.tab', e => {
    const target = e.target.getAttribute('href');
    if (target === '#tab-positions') loadPositions();
    if (target === '#tab-threats')   loadThreats();
    if (target === '#tab-news')      loadNews();
    if (target === '#tab-settings')  loadSettings();
    if (target === '#tab-jobs')      loadJobs();
  });
});

// Initial load
loadSignals();
loadJobs();

// Auto-refresh every 60s
setInterval(refreshAll, 60000);
