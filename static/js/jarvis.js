'use strict';

const API  = (path) => fetch(`/api${path}`).then(r => r.json());
const POST = (path, body) => fetch(`/api${path}`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)}).then(r => r.json());
const DEL  = (path) => fetch(`/api${path}`, {method:'DELETE'}).then(r => r.json());

let allSignals=[], allThreats=[], allNews=[], equityChart=null;

// ── Formatters ──────────────────────────────────────────────────────────────
const fmt2   = v => v != null ? Number(v).toFixed(2) : 'N/A';
const fmtPct = v => v != null ? `${v>=0?'+':''}${Number(v).toFixed(2)}%` : 'N/A';
const fmtPrice = v => {
  if (v==null) return 'N/A';
  v=Number(v);
  return v>1000?`$${v.toLocaleString('en',{maximumFractionDigits:0})}`:v>1?`$${v.toFixed(2)}`:`$${v.toFixed(6)}`;
};
const fmtVol = v => {
  if (!v) return '';
  v=Number(v);
  return v>1e9?`${(v/1e9).toFixed(1)}B`:v>1e6?`${(v/1e6).toFixed(1)}M`:v>1e3?`${(v/1e3).toFixed(0)}K`:`${v}`;
};
const timeAgo = iso => {
  if (!iso) return '';
  const diff = Date.now()-new Date(iso).getTime();
  const m = Math.floor(diff/60000);
  if (m<1) return 'just now';
  if (m<60) return `${m}m ago`;
  const h=Math.floor(m/60);
  if (h<24) return `${h}h ago`;
  return `${Math.floor(h/24)}d ago`;
};
const sevColor = {Critical:'danger',High:'warning',Medium:'primary',Low:'success'};
const sentIcon = {positive:'bi-arrow-up-circle-fill text-success',negative:'bi-arrow-down-circle-fill text-danger',neutral:'bi-dash-circle text-secondary'};

// ── SIGNALS ─────────────────────────────────────────────────────────────────
async function loadSignals() {
  const data = await API('/signals?limit=150');
  allSignals = data;
  renderSignals();
}

function renderSignals() {
  const status = document.getElementById('sig-filter-status').value;
  const cls    = document.getElementById('sig-filter-class').value;
  const sort   = document.getElementById('sig-sort').value;
  let filtered = allSignals.filter(s=>(!status||s.status===status)&&(!cls||s.asset_class===cls));
  filtered.sort((a,b)=>{
    if (sort==='score')      return (b.composite_score||b.confidence||0)-(a.composite_score||a.confidence||0);
    if (sort==='confidence') return (b.confidence||0)-(a.confidence||0);
    return new Date(b.generated_at)-new Date(a.generated_at);
  });
  document.getElementById('signal-count').textContent=`${filtered.length} signals`;
  const grid=document.getElementById('signals-grid');
  if (!filtered.length){grid.innerHTML='<div class="col-12 text-muted text-center py-5">No signals found</div>';return;}
  grid.innerHTML=filtered.map(s=>{
    const dir=(s.direction||'Long').toLowerCase();
    const score=s.composite_score||s.confidence||0;
    const conf=s.confidence||0;
    const scoreCls=score>=75?'high':score>=55?'medium':'low';
    const rr=s.entry_price&&s.target_price&&s.stop_loss&&s.entry_price>s.stop_loss
              ?((s.target_price-s.entry_price)/(s.entry_price-s.stop_loss)).toFixed(1):'?';
    const statusBadge={Active:'bg-success',Executed:'bg-primary',Expired:'bg-secondary',Rejected:'bg-danger',Closed:'bg-dark border border-secondary'}[s.status]||'bg-secondary';
    const earnBadge=s.earnings_risk?`<span class="badge bg-warning text-dark ms-1" title="Earnings this week">⚠️ Earnings</span>`:'';
    return `
    <div class="col-xl-3 col-lg-4 col-md-6">
      <div class="card signal-card ${dir} h-100">
        <div class="card-header d-flex justify-content-between align-items-center py-2">
          <div class="d-flex align-items-center gap-1 flex-wrap">
            <span class="fw-bold">${s.asset_symbol}</span>
            <span class="badge ${dir==='long'?'bg-success':'bg-primary'}">${s.direction}</span>
            <span class="badge ${statusBadge}">${s.status}</span>
            ${earnBadge}
          </div>
          <small class="text-muted">${timeAgo(s.generated_at)}</small>
        </div>
        <div class="card-body py-2 px-3">
          <div class="conf-bar ${scoreCls} mb-1" style="width:${score}%"></div>
          <div class="small mb-2 text-muted">
            ${s.asset_name||''} · ${s.asset_class||''} · ${s.timeframe||''}
            <span class="text-warning ms-1">${score.toFixed(0)}% score</span>
            ${score!==conf?`<span class="text-muted ms-1">(LLM: ${conf}%)</span>`:''}
          </div>
          <div class="d-flex justify-content-between small mb-1"><span>Entry</span><span class="fw-bold text-info">${fmtPrice(s.entry_price)}</span></div>
          <div class="d-flex justify-content-between small mb-1"><span>Target</span><span class="fw-bold text-success">${fmtPrice(s.target_price)}</span></div>
          <div class="d-flex justify-content-between small mb-2"><span>Stop</span><span class="fw-bold text-danger">${fmtPrice(s.stop_loss)}</span></div>
          <div class="d-flex justify-content-between small mb-2">
            <span class="text-muted">R:R</span><span class="badge bg-dark border border-secondary">${rr}x</span>
          </div>
          <p class="small text-muted mb-2" style="font-size:.72rem;line-height:1.4;max-height:80px;overflow:hidden">${(s.reasoning||'').slice(0,220)}${(s.reasoning||'').length>220?'…':''}</p>
          ${s.key_risks?`<p class="small text-warning mb-1" style="font-size:.7rem"><i class="bi bi-exclamation-triangle-fill"></i> ${s.key_risks.slice(0,120)}</p>`:''}
        </div>
        <div class="card-footer py-1 d-flex gap-1">
          ${s.status==='Active'?`<button class="btn btn-success btn-sm flex-fill py-0" style="font-size:.72rem" onclick="executeSignal('${s.id}')"><i class="bi bi-play-fill"></i> Execute</button>`:''}
          <button class="btn btn-outline-danger btn-sm py-0" style="font-size:.72rem" onclick="deleteSignal('${s.id}')"><i class="bi bi-trash"></i></button>
        </div>
      </div>
    </div>`;
  }).join('');
}

async function executeSignal(id) {
  if (!confirm('Submit bracket order?')) return;
  const res=await POST(`/signals/${id}/execute`,{});
  alert(res.error||`Order submitted`);
  loadSignals();
}
async function deleteSignal(id) {
  if (!confirm('Delete signal?')) return;
  await DEL(`/signals/${id}`);
  loadSignals();
}
async function clearExpiredSignals() {
  if (!confirm('Clear all Expired + Rejected signals?')) return;
  const res=await DEL('/signals/clear/expired');
  alert(`Deleted ${res.deleted||0} signals`);
  loadSignals();
}

document.getElementById('sig-filter-status').addEventListener('change', renderSignals);
document.getElementById('sig-filter-class').addEventListener('change', renderSignals);
document.getElementById('sig-sort').addEventListener('change', renderSignals);

// ── POSITIONS ────────────────────────────────────────────────────────────────
async function loadPositions() {
  try {
    const data=await API('/positions');
    const acct=data.account||{};
    const plCls=(acct.unrealized_pl||0)>=0?'text-success':'text-danger';
    document.getElementById('account-summary').innerHTML=`
      <div class="col-auto"><div class="card px-3 py-2"><div class="small text-muted">Equity</div><div class="fw-bold text-info">${fmtPrice(acct.equity)}</div></div></div>
      <div class="col-auto"><div class="card px-3 py-2"><div class="small text-muted">Cash</div><div class="fw-bold text-success">${fmtPrice(acct.cash)}</div></div></div>
      <div class="col-auto"><div class="card px-3 py-2"><div class="small text-muted">Market Value</div><div class="fw-bold">${fmtPrice(acct.market_value)}</div></div></div>
      <div class="col-auto"><div class="card px-3 py-2"><div class="small text-muted">Unrealized P&L</div><div class="fw-bold ${plCls}">${fmtPrice(acct.unrealized_pl)} (${fmtPct(acct.unrealized_plpc)})</div></div></div>
      <div class="col-auto"><div class="card px-3 py-2"><div class="small text-muted">Day Trades</div><div class="fw-bold">${acct.day_trade_count||0}/3</div></div></div>
      <div class="col-auto"><div class="card px-3 py-2"><div class="small text-muted">Buying Power</div><div class="fw-bold">${fmtPrice(acct.buying_power)}</div></div></div>
    `;
    const tbody=document.getElementById('positions-body');
    const positions=data.positions||[];
    if (!positions.length){tbody.innerHTML='<tr><td colspan="9" class="text-center text-muted py-4">No open positions</td></tr>';return;}
    tbody.innerHTML=positions.map(p=>{
      const plpc=p.unrealized_plpc||0;
      const plCls2=plpc>=0?'pl-positive':'pl-negative';
      return `<tr>
        <td class="fw-bold">${p.symbol}</td>
        <td><span class="badge ${p.asset_class==='Crypto'?'bg-warning text-dark':'bg-primary'}">${p.asset_class}</span></td>
        <td>${Number(p.qty).toLocaleString()}</td>
        <td>${fmtPrice(p.avg_entry)}</td>
        <td>${fmtPrice(p.current_price)}</td>
        <td>$${Number(p.market_value).toLocaleString('en',{maximumFractionDigits:2})}</td>
        <td class="${plCls2}">$${Number(p.unrealized_pl).toFixed(2)}</td>
        <td class="${plCls2} fw-bold">${fmtPct(plpc)}</td>
        <td><button class="btn btn-outline-danger btn-sm py-0 px-1" style="font-size:.7rem" onclick="closePosition('${p.symbol}')"><i class="bi bi-x-circle"></i> Close</button></td>
      </tr>`;
    }).join('');
    loadEquityCurve(24);
  } catch(e) {
    document.getElementById('positions-body').innerHTML=`<tr><td colspan="9" class="text-danger py-3">${e.message}</td></tr>`;
  }
}

async function closePosition(symbol) {
  if (!confirm(`Close ${symbol}?`)) return;
  const res=await POST(`/positions/${symbol}/close`,{});
  alert(res.error||`${symbol} closed`);
  loadPositions();
}

async function loadOrders() {
  try {
    const orders=await API('/alpaca/orders');
    const el=document.getElementById('orders-list');
    if (!orders.length){el.innerHTML='<span class="text-muted">No open orders</span>';return;}
    el.innerHTML=orders.map(o=>`
      <div class="d-flex justify-content-between align-items-center border-bottom border-secondary py-1">
        <span><b>${o.symbol}</b> ${o.side} ${o.qty} @ ${o.type} <span class="badge bg-secondary">${o.status}</span></span>
        <button class="btn btn-outline-danger btn-sm py-0 px-1" onclick="cancelOrder('${o.id}')">Cancel</button>
      </div>`).join('');
  } catch(e){document.getElementById('orders-list').textContent=e.message;}
}
async function cancelOrder(id){
  await DEL(`/alpaca/orders/${id}`);
  loadOrders();
}

// ── EQUITY CURVE ─────────────────────────────────────────────────────────────
async function loadEquityCurve(hours=24) {
  ['eq-24h','eq-7d','eq-30d'].forEach(id=>{
    const btn=document.getElementById(id);
    if(btn) btn.classList.remove('active');
  });
  const btnMap={24:'eq-24h',168:'eq-7d',720:'eq-30d'};
  if(document.getElementById(btnMap[hours])) document.getElementById(btnMap[hours]).classList.add('active');

  try {
    const data=await API(`/portfolio/equity?hours=${hours}`);
    const canvas=document.getElementById('equity-chart');
    const noData=document.getElementById('equity-no-data');
    if (!data||!data.length){canvas.style.display='none';noData.style.display='block';return;}
    canvas.style.display='block';noData.style.display='none';
    const labels=data.map(d=>new Date(d.time).toLocaleTimeString('en',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}));
    const equities=data.map(d=>d.equity);
    const first=equities[0]||0;
    const last=equities[equities.length-1]||0;
    const trend=last>=first?'rgba(25,135,84,1)':'rgba(220,53,69,1)';
    const trendBg=last>=first?'rgba(25,135,84,0.15)':'rgba(220,53,69,0.15)';
    if (equityChart) equityChart.destroy();
    equityChart=new Chart(canvas,{
      type:'line',
      data:{labels,datasets:[{label:'Equity',data:equities,borderColor:trend,backgroundColor:trendBg,fill:true,tension:0.3,pointRadius:0}]},
      options:{responsive:true,plugins:{legend:{display:false}},scales:{x:{display:false},y:{ticks:{callback:v=>`$${v.toLocaleString()}`}}}}
    });
  } catch(e){console.error('Equity chart error:',e);}
}

// ── MARKET ───────────────────────────────────────────────────────────────────
async function loadMarket() {
  try {
    const data=await API('/market/full');
    const filter=document.getElementById('market-filter').value;
    let eq=data.equities||[];
    if(filter==='positive') eq=eq.filter(a=>(a.change_percent||0)>0);
    if(filter==='negative') eq=eq.filter(a=>(a.change_percent||0)<0);
    document.getElementById('equities-body').innerHTML=eq.map(a=>{
      const chg=a.change_percent||0;
      const cls=chg>0?'text-success':chg<0?'text-danger':'text-muted';
      return `<tr><td class="fw-bold">${a.symbol}</td><td class="text-muted small">${(a.name||'').slice(0,20)}</td><td>${fmtPrice(a.price)}</td><td class="${cls} fw-bold">${fmtPct(chg)}</td><td class="text-muted small">${fmtVol(a.volume)}</td></tr>`;
    }).join('')||'<tr><td colspan="5" class="text-muted text-center py-3">No data yet</td></tr>';
    document.getElementById('crypto-body').innerHTML=(data.crypto||[]).map(a=>{
      const chg=a.change_percent||0;
      const cls=chg>0?'text-success':chg<0?'text-danger':'text-muted';
      return `<tr><td class="fw-bold">${a.symbol}</td><td>${fmtPrice(a.price)}</td><td class="${cls} fw-bold">${fmtPct(chg)}</td></tr>`;
    }).join('')||'<tr><td colspan="3" class="text-muted text-center py-3">No data yet</td></tr>';
    loadRegime();
  } catch(e){console.error('Market load error:',e);}
}

async function loadRegime() {
  try {
    const r=await API('/regime');
    const risk=r.risk||'unknown';
    const color={low:'success',medium:'warning','medium-high':'warning',high:'danger',unknown:'secondary'}[risk]||'secondary';
    const badge=`<span class="badge bg-${color} me-2">${r.label||'Unknown'}</span>`;
    document.getElementById('regime-badge').innerHTML=badge+`<span class="text-muted small">${r.recommendation||''}</span>`;
    document.getElementById('regime-detail').innerHTML=`
      ${badge}
      <span class="text-muted small">SPY $${r.spy_last||'?'} | RSI ${r.spy_rsi||'?'} | ADX ${r.spy_adx||'?'} | EMA21 $${r.spy_ema21||'?'} | Drawdown ${r.spy_drawdown_pct||'?'}%</span>
      <span class="ms-3 text-info small">${r.recommendation||''}</span>
    `;
  } catch(e){}
}

document.getElementById('market-filter').addEventListener('change', loadMarket);

// ── THREATS ──────────────────────────────────────────────────────────────────
async function loadThreats() {
  const data=await API('/threats?limit=80');
  allThreats=data;
  renderThreats();
}
function renderThreats() {
  const sev=document.getElementById('threat-filter-sev').value;
  const reg=document.getElementById('threat-filter-region').value;
  let filtered=allThreats.filter(t=>(!sev||t.severity===sev)&&(!reg||t.region===reg));
  document.getElementById('threat-count').textContent=`${filtered.length} threats`;
  const grid=document.getElementById('threats-grid');
  if (!filtered.length){grid.innerHTML='<div class="col-12 text-muted text-center py-5">No active threats</div>';return;}
  grid.innerHTML=filtered.map(t=>`
    <div class="col-xl-3 col-lg-4 col-md-6">
      <div class="card h-100 border-${sevColor[t.severity]||'secondary'}">
        <div class="card-header py-2 d-flex justify-content-between">
          <span class="badge sev-${t.severity}">${t.severity}</span>
          <small class="text-muted">${t.country||''} · ${timeAgo(t.published_at)}</small>
        </div>
        <div class="card-body py-2">
          <p class="fw-bold mb-1 small">${t.source_url?`<a href="${t.source_url}" target="_blank" class="text-info text-decoration-none">${t.title}</a>`:t.title}</p>
          <p class="text-muted small mb-1" style="font-size:.72rem">${(t.description||'').slice(0,200)}</p>
          <div class="d-flex gap-1 flex-wrap">
            <span class="badge bg-dark border border-secondary small">${t.event_type||''}</span>
            <span class="badge bg-dark border border-secondary small">${t.region||''}</span>
          </div>
        </div>
        <div class="card-footer py-1 small text-muted">${timeAgo(t.published_at)} · ${t.source||''}</div>
      </div>
    </div>`).join('');
}
document.getElementById('threat-filter-sev').addEventListener('change', renderThreats);
document.getElementById('threat-filter-region').addEventListener('change', renderThreats);

// ── NEWS ─────────────────────────────────────────────────────────────────────
async function loadNews() {
  const data=await API('/news?limit=80');
  allNews=data;
  renderNews();
}
function renderNews() {
  const cat=document.getElementById('news-filter-cat').value;
  const sent=document.getElementById('news-filter-sent').value;
  let filtered=allNews.filter(n=>(!cat||n.category===cat)&&(!sent||n.sentiment===sent));
  const el=document.getElementById('news-list');
  if (!filtered.length){el.innerHTML='<div class="text-muted text-center py-5">No news</div>';return;}
  el.innerHTML=filtered.map(n=>{
    const sentClass={positive:'text-success',negative:'text-danger',neutral:'text-muted'}[n.sentiment]||'text-muted';
    const icon=sentIcon[n.sentiment]||'';
    const assets=(n.affected_assets||[]).filter(Boolean).slice(0,5).map(a=>`<span class="badge bg-dark border border-secondary">${a}</span>`).join(' ');
    return `
    <div class="border-bottom border-secondary py-2">
      <div class="d-flex justify-content-between align-items-start">
        <div class="flex-grow-1 me-3">
          <a href="${n.url||'#'}" target="_blank" class="text-info text-decoration-none small fw-bold">${n.title}</a>
          <p class="text-muted mb-1" style="font-size:.72rem">${(n.summary||'').slice(0,200)}</p>
          <div class="d-flex gap-2 align-items-center flex-wrap">
            <span class="badge bg-secondary">${n.category||''}</span>
            <span class="small ${sentClass}"><i class="bi ${icon}"></i> ${n.sentiment||''}</span>
            ${assets}
          </div>
        </div>
        <div class="text-end text-muted" style="font-size:.7rem;white-space:nowrap">
          <div>${n.source||''}</div>
          <div>${timeAgo(n.published_at)}</div>
        </div>
      </div>
    </div>`;
  }).join('');
}
document.getElementById('news-filter-cat').addEventListener('change', renderNews);
document.getElementById('news-filter-sent').addEventListener('change', renderNews);

// ── SCANNER ──────────────────────────────────────────────────────────────────
async function runScan() {
  const sym=(document.getElementById('scan-symbol').value||'').toUpperCase().trim();
  if (!sym){alert('Enter a symbol');return;}
  const tfs=['1H','4H','1D'].filter(tf=>document.getElementById(`tf-${tf.toLowerCase().replace('h','h')}`).checked);
  const tfsChecked=['tf-1h','tf-4h','tf-1d'].filter(id=>document.getElementById(id).checked).map(id=>id.replace('tf-','').toUpperCase());
  const genSig=document.getElementById('gen-signal-check').checked;
  const el=document.getElementById('scan-result');
  el.innerHTML=`<div class="text-muted"><span class="spinner-border spinner-border-sm me-2"></span>Analyzing ${sym}${genSig?' + generating signal':''}... (may take 30-60s)</div>`;
  try {
    const res=await POST('/analyze',{symbol:sym,timeframes:tfsChecked,generate_signal:genSig});
    const ta=res.ta||{};
    let html=`<div class="fw-bold mb-2">${sym} Analysis</div>`;
    for (const [tf, td] of Object.entries(ta)) {
      if (!td||td.error) continue;
      const price=td.price||{};
      const rsi=td.rsi;const macd=td.macd||{};const bias=td.bias||'';
      const biasClass={bullish:'text-success',bearish:'text-danger',neutral:'text-muted'}[bias]||'';
      html+=`<div class="card mb-2">
        <div class="card-header py-1 d-flex justify-content-between">
          <span class="fw-bold small">${tf}</span>
          <span class="small ${biasClass}">${bias.toUpperCase()}</span>
        </div>
        <div class="card-body py-2 small">
          <div class="row g-2">
            <div class="col-6">Price: <b>${fmtPrice(price.last)}</b></div>
            <div class="col-6">RSI: <b>${rsi?rsi.toFixed(1):'N/A'}</b></div>
            <div class="col-6">EMA20: ${fmtPrice(td.ema?.ema20)}</div>
            <div class="col-6">EMA50: ${fmtPrice(td.ema?.ema50)}</div>
            <div class="col-6">ATR: ${td.atr?.value?td.atr.value.toFixed(2):'N/A'}</div>
            <div class="col-6">ADX: ${td.adx?.toFixed(1)||'N/A'}</div>
            <div class="col-12 text-muted" style="font-size:.7rem">${td.summary||''}</div>
          </div>
        </div>
      </div>`;
    }
    if (res.signal) {
      const s=res.signal;
      html+=`<div class="card border-success mt-2">
        <div class="card-header py-1 bg-success bg-opacity-25 small fw-bold">Generated Signal</div>
        <div class="card-body py-2 small">
          <div class="row g-1">
            <div class="col-6">Direction: <b class="${s.direction==='Long'?'text-success':'text-primary'}">${s.direction||''}</b></div>
            <div class="col-6">Confidence: <b>${s.confidence||0}%</b></div>
            <div class="col-4">Entry: <b class="text-info">${fmtPrice(s.entry_price)}</b></div>
            <div class="col-4">Target: <b class="text-success">${fmtPrice(s.target_price)}</b></div>
            <div class="col-4">Stop: <b class="text-danger">${fmtPrice(s.stop_loss)}</b></div>
            <div class="col-12 text-muted mt-1">${(s.reasoning||'').slice(0,300)}</div>
          </div>
        </div>
      </div>`;
    }
    el.innerHTML=html;
  } catch(e){el.innerHTML=`<div class="text-danger">${e.message}</div>`;}
}

// ── JOBS ─────────────────────────────────────────────────────────────────────
async function loadJobs() {
  try {
    const data=await API('/jobs/status');
    const jobMeta={
      market:   {icon:'bi-database',label:'Market Data',schedule:'Every 15 min'},
      threats:  {icon:'bi-shield-exclamation',label:'Threat News',schedule:'Every 15 min'},
      signals:  {icon:'bi-lightning-fill',label:'Signal Gen',schedule:'Every 30 min'},
      execute:  {icon:'bi-play-circle-fill',label:'Execute',schedule:'Every 30 min'},
      positions:{icon:'bi-briefcase-fill',label:'Position Mgmt',schedule:'Every 5 min'},
      telegram: {icon:'bi-telegram',label:'Telegram Bot',schedule:'Every 1 min'},
    };
    const statusInfo={
      ok:      {badge:'bg-success',icon:'bi-check-circle-fill',text:'OK'},
      running: {badge:'bg-warning text-dark',icon:'bi-arrow-repeat',text:'Running'},
      error:   {badge:'bg-danger',icon:'bi-x-circle-fill',text:'Error'},
      idle:    {badge:'bg-secondary',icon:'bi-pause-circle',text:'Idle'},
    };
    document.getElementById('jobs-grid').innerHTML=Object.entries(data).map(([name,info])=>{
      const meta=jobMeta[name]||{icon:'bi-cpu',label:name,schedule:''};
      const st=statusInfo[info.status||'idle']||statusInfo.idle;
      return `
      <div class="col-lg-4 col-md-6">
        <div class="card h-100">
          <div class="card-body py-2">
            <div class="d-flex justify-content-between align-items-center mb-2">
              <span class="fw-bold small"><i class="bi ${meta.icon} me-1"></i>${meta.label}</span>
              <span class="badge ${st.badge}"><i class="bi ${st.icon} me-1"></i>${st.text}</span>
            </div>
            <div class="small text-muted mb-1">Schedule: ${meta.schedule}</div>
            <div class="small text-muted mb-2">Last: ${info.last?timeAgo(info.last):'Never'}</div>
            ${info.error?`<div class="small text-danger text-truncate mb-2" title="${info.error}"><i class="bi bi-exclamation-triangle me-1"></i>${info.error.slice(0,80)}</div>`:''}
            <button class="btn btn-outline-success btn-sm w-100 py-0" onclick="triggerJob('${name}')">
              <i class="bi bi-play-fill"></i> Run Now
            </button>
          </div>
        </div>
      </div>`;
    }).join('');
  } catch(e){console.error('Jobs load error:',e);}
}

async function triggerJob(name) {
  const btn=event.target.closest('button');
  if(btn){btn.disabled=true;btn.innerHTML='<span class="spinner-border spinner-border-sm"></span>';}
  await POST(`/jobs/${name}/trigger`,{});
  setTimeout(()=>{loadJobs();},1000);
}

// ── SETTINGS ─────────────────────────────────────────────────────────────────
let allConfigs=[];

async function loadSettings() {
  allConfigs=await API('/settings');
  renderConfigs();
}

function renderConfigs() {
  const el=document.getElementById('configs-list');
  if (!allConfigs.length){el.innerHTML='<div class="text-muted small">No configurations yet. Add one on the left.</div>';return;}
  const grouped={};
  for (const c of allConfigs){
    const p=c.platform||'other';
    if (!grouped[p]) grouped[p]=[];
    grouped[p].push(c);
  }
  el.innerHTML=Object.entries(grouped).map(([plat,cfgs])=>`
    <div class="mb-3">
      <div class="small fw-bold text-uppercase text-muted mb-1">${plat.replace('_',' ')}</div>
      ${cfgs.map(c=>`
        <div class="card mb-1">
          <div class="card-body py-2 d-flex justify-content-between align-items-center">
            <div>
              <span class="fw-bold small me-2">${c.label}</span>
              ${c.is_default?'<span class="badge bg-success me-1">Default</span>':''}
              ${c.is_active?'<span class="badge bg-secondary">Active</span>':'<span class="badge bg-dark">Inactive</span>'}
              ${c.api_key?`<div class="text-muted font-monospace mt-1" style="font-size:.7rem">${c.api_key.slice(0,8)}…</div>`:''}
              ${c.notes?`<div class="text-muted mt-1" style="font-size:.7rem">${c.notes}</div>`:''}
            </div>
            <div class="d-flex gap-1">
              <button class="btn btn-outline-secondary btn-sm py-0 px-1" title="Set default" onclick="setDefault('${c.id}')"><i class="bi bi-star${c.is_default?'-fill text-warning':''}"></i></button>
              <button class="btn btn-outline-primary btn-sm py-0 px-1" onclick="editConfig('${c.id}')"><i class="bi bi-pencil"></i></button>
              <button class="btn btn-outline-danger btn-sm py-0 px-1" onclick="deleteConfig('${c.id}')"><i class="bi bi-trash"></i></button>
            </div>
          </div>
        </div>`).join('')}
    </div>`).join('');
}

function editConfig(id) {
  const c=allConfigs.find(x=>x.id===id);
  if (!c) return;
  document.getElementById('cfg-edit-id').value=c.id;
  document.getElementById('cfg-label').value=c.label||'';
  document.getElementById('cfg-platform').value=c.platform||'other';
  document.getElementById('cfg-key').value=c.api_key||'';
  document.getElementById('cfg-secret').value=c.api_secret||'';
  document.getElementById('cfg-url').value=c.api_url||'';
  document.getElementById('cfg-extra1').value=c.extra_field_1||'';
  document.getElementById('cfg-notes').value=c.notes||'';
  document.getElementById('cfg-active').checked=c.is_active;
  document.getElementById('cfg-default').checked=c.is_default;
}

function clearConfigForm() {
  document.getElementById('cfg-edit-id').value='';
  ['cfg-label','cfg-key','cfg-secret','cfg-url','cfg-extra1','cfg-notes'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('cfg-active').checked=true;
  document.getElementById('cfg-default').checked=false;
}

async function saveConfig() {
  const editId=document.getElementById('cfg-edit-id').value;
  const body={
    label:         document.getElementById('cfg-label').value.trim(),
    platform:      document.getElementById('cfg-platform').value,
    api_key:       document.getElementById('cfg-key').value.trim(),
    api_secret:    document.getElementById('cfg-secret').value.trim(),
    api_url:       document.getElementById('cfg-url').value.trim(),
    extra_field_1: document.getElementById('cfg-extra1').value.trim(),
    notes:         document.getElementById('cfg-notes').value.trim(),
    is_active:     document.getElementById('cfg-active').checked,
    is_default:    document.getElementById('cfg-default').checked,
  };
  if (!body.label){alert('Label is required');return;}
  if (editId) await fetch(`/api/settings/${editId}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  else await POST('/settings',body);
  clearConfigForm();
  loadSettings();
}

async function deleteConfig(id) {
  if (!confirm('Delete this config?')) return;
  await DEL(`/settings/${id}`);
  loadSettings();
}

async function setDefault(id) {
  await POST(`/settings/${id}/set-default`,{});
  loadSettings();
}

// ── Job indicators in navbar ──────────────────────────────────────────────────
async function updateJobIndicators() {
  try {
    const data=await API('/jobs/status');
    const colors={ok:'#198754',running:'#ffc107',error:'#dc3545',idle:'#6c757d'};
    document.getElementById('job-indicators').innerHTML=Object.entries(data).map(([name,info])=>`
      <span title="${name}: ${info.status}${info.error?' — '+info.error:''}" style="width:8px;height:8px;border-radius:50%;background:${colors[info.status||'idle']};display:inline-block"></span>
    `).join('');
  } catch(e){}
}

// ── Init & Refresh ────────────────────────────────────────────────────────────
const REFRESH_INTERVALS = {
  signals:   60000,
  positions: 60000,
  market:    120000,
  threats:   120000,
  news:      120000,
  jobs:      15000,
};

function init() {
  loadSignals();
  loadJobs();
  updateJobIndicators();
  loadSettings();
  
  // Tab-based lazy loading
  document.querySelectorAll('a[data-bs-toggle="tab"]').forEach(tab=>{
    tab.addEventListener('shown.bs.tab', e=>{
      const target=e.target.getAttribute('href');
      if (target==='#tab-positions') {loadPositions();loadOrders();}
      if (target==='#tab-market')    loadMarket();
      if (target==='#tab-threats')   loadThreats();
      if (target==='#tab-news')      loadNews();
      if (target==='#tab-jobs')      loadJobs();
      if (target==='#tab-settings')  loadSettings();
    });
  });
  
  // Auto refresh
  setInterval(loadSignals,   REFRESH_INTERVALS.signals);
  setInterval(loadJobs,      REFRESH_INTERVALS.jobs);
  setInterval(updateJobIndicators, 15000);
  
  // Refresh visible tab data
  setInterval(()=>{
    const active=document.querySelector('.tab-pane.active');
    if (!active) return;
    const id=active.id;
    if (id==='tab-positions') loadPositions();
    if (id==='tab-market')    loadMarket();
    if (id==='tab-threats')   loadThreats();
    if (id==='tab-news')      loadNews();
  }, 60000);
  
  // Clock
  setInterval(()=>{
    const el=document.getElementById('last-refresh');
    if(el) el.textContent=new Date().toLocaleTimeString();
  }, 1000);
}

document.addEventListener('DOMContentLoaded', init);
