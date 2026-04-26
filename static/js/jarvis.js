'use strict';

const API  = (p) => fetch(`/api${p}`).then(r=>r.json());
const POST = (p,b) => fetch(`/api${p}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(r=>r.json());
const DEL  = (p)   => fetch(`/api${p}`,{method:'DELETE'}).then(r=>r.json());

let allSignals=[], allThreats=[], allNews=[], equityChart=null;

/* ── Formatters ─────────────────────────────────────────────────────────── */
const fmt2   = v => v!=null ? Number(v).toFixed(2) : 'N/A';
const fmtPct = v => v!=null ? `${v>=0?'+':''}${Number(v).toFixed(2)}%` : 'N/A';
const fmtPrice = v => {
  if(v==null) return 'N/A'; v=Number(v);
  return v>1000?`$${v.toLocaleString('en',{maximumFractionDigits:0})}`:v>1?`$${v.toFixed(2)}`:`$${v.toFixed(6)}`;
};
const timeAgo = iso => {
  if(!iso) return '';
  const m=Math.floor((Date.now()-new Date(iso).getTime())/60000);
  if(m<1) return 'just now'; if(m<60) return `${m}m ago`;
  const h=Math.floor(m/60); if(h<24) return `${h}h ago`;
  return `${Math.floor(h/24)}d ago`;
};
const sevColor = {Critical:'danger',High:'warning',Medium:'primary',Low:'success'};

/* ── Job Indicators ──────────────────────────────────────────────────────── */
async function refreshJobIndicators() {
  try {
    const jobs = await API('/jobs/status');
    const el = document.getElementById('job-indicators');
    if(!el) return;
    el.innerHTML = Object.entries(jobs).map(([name,info])=>{
      const c={ok:'success',running:'warning',error:'danger',idle:'secondary'}[info.status]||'secondary';
      const icon={ok:'✓',running:'⏳',error:'✗',idle:'·'}[info.status]||'·';
      return `<span class="badge bg-${c}" title="${name}: ${info.last||'never'}${info.error?' — '+info.error:''}">${icon} ${name}</span>`;
    }).join('');
  } catch(e){}
}

/* ── Regime Badge ────────────────────────────────────────────────────────── */
async function refreshRegimeBadge() {
  try {
    const r = await API('/regime');
    const el = document.getElementById('regime-badge');
    if(!el) return;
    const c = {low:'success','medium':'info','medium-high':'warning',high:'danger'}[r.risk]||'secondary';
    el.innerHTML = `<span class="badge bg-${c}">${r.label||'Unknown'}</span>`;
  } catch(e){}
}

/* ── SIGNALS ──────────────────────────────────────────────────────────────── */
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
  if(sort==='score')      filtered.sort((a,b)=>(b.composite_score||b.confidence||0)-(a.composite_score||a.confidence||0));
  else if(sort==='confidence') filtered.sort((a,b)=>(b.confidence||0)-(a.confidence||0));
  else filtered.sort((a,b)=>new Date(b.generated_at)-new Date(a.generated_at));
  document.getElementById('signal-count').textContent=`${filtered.length} signals`;
  const grid=document.getElementById('signals-grid');
  if(!filtered.length){grid.innerHTML='<div class="col-12 text-center text-muted py-5">No signals</div>';return;}
  grid.innerHTML=filtered.map(s=>{
    const score=s.composite_score||s.confidence||0;
    const dir=(s.direction||'Long').toLowerCase();
    const conf=s.confidence||0;
    const confCls=conf>=75?'high':conf>=55?'medium':'low';
    const rr=s.entry_price&&s.target_price&&s.stop_loss&&s.entry_price>s.stop_loss
      ?((s.target_price-s.entry_price)/(s.entry_price-s.stop_loss)).toFixed(1):'N/A';
    const statusBadge={Active:'bg-success',Executed:'bg-primary',Expired:'bg-secondary',Rejected:'bg-danger',Closed:'bg-dark border border-secondary'}[s.status]||'bg-secondary';
    const scorePct=Math.round(score);
    const earningsBadge=s.earnings_risk?'<span class="badge bg-warning text-dark ms-1" title="Earnings risk">📅</span>':'';
    return `<div class="col-xl-3 col-lg-4 col-md-6">
      <div class="card signal-card ${dir} h-100">
        <div class="card-header d-flex justify-content-between align-items-center py-2">
          <div>
            <span class="fw-bold">${s.asset_symbol}</span>
            <span class="badge ${dir==='long'?'bg-success':'bg-primary'} ms-1">${s.direction}</span>
            <span class="badge ${statusBadge} ms-1">${s.status}</span>
            ${earningsBadge}
          </div>
          <small class="text-muted">${timeAgo(s.generated_at)}</small>
        </div>
        <div class="card-body py-2 px-3">
          <div class="d-flex justify-content-between align-items-center mb-1">
            <small class="text-muted">Composite Score</small>
            <span class="badge ${scorePct>=70?'bg-success':scorePct>=50?'bg-warning text-dark':'bg-danger'}">${scorePct}%</span>
          </div>
          <div class="conf-bar ${confCls} mb-2" style="width:${conf}%"></div>
          <div class="small mb-2 text-muted">${s.asset_name||''} · ${s.asset_class||''} · ${s.timeframe||''} · <span class="text-warning">LLM:${conf}%</span></div>
          <div class="d-flex justify-content-between small mb-1"><span>Entry</span><span class="fw-bold text-info">${fmtPrice(s.entry_price)}</span></div>
          <div class="d-flex justify-content-between small mb-1"><span>Target</span><span class="fw-bold text-success">${fmtPrice(s.target_price)}</span></div>
          <div class="d-flex justify-content-between small mb-2"><span>Stop</span><span class="fw-bold text-danger">${fmtPrice(s.stop_loss)}</span></div>
          <div class="d-flex justify-content-between small mb-2"><span class="text-muted">R:R</span><span class="badge bg-dark border border-secondary">${rr}</span></div>
          <p class="small text-muted mb-1" style="font-size:.72rem;line-height:1.4;max-height:72px;overflow:hidden">${(s.reasoning||'').slice(0,200)}${(s.reasoning||'').length>200?'…':''}</p>
          ${s.key_risks?`<p class="small text-warning mb-0" style="font-size:.7rem"><i class="bi bi-exclamation-triangle-fill"></i> ${s.key_risks.slice(0,100)}</p>`:''}
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
  if(!confirm('Submit bracket order for this signal?')) return;
  const res=await POST(`/signals/${id}/execute`,{});
  alert(res.error||`Order submitted!`);
  loadSignals();
}
async function deleteSignal(id) {
  if(!confirm('Delete this signal?')) return;
  await DEL(`/signals/${id}`); loadSignals();
}
async function clearExpiredSignals() {
  if(!confirm('Delete all Expired and Rejected signals?')) return;
  const res=await DEL('/signals/clear/expired');
  alert(`Deleted ${res.deleted||0} signals`); loadSignals();
}

document.getElementById('sig-filter-status').addEventListener('change',renderSignals);
document.getElementById('sig-filter-class').addEventListener('change',renderSignals);
document.getElementById('sig-sort').addEventListener('change',renderSignals);

/* ── POSITIONS ────────────────────────────────────────────────────────────── */
async function loadPositions() {
  try {
    const data=await API('/positions/with-signals');
    const acct=data.account||{};
    const plCls=(acct.unrealized_pl||0)>=0?'text-success':'text-danger';
    document.getElementById('account-summary').innerHTML=`
      <div class="col-auto"><div class="card px-3 py-2"><div class="small text-muted">Equity</div><div class="fw-bold text-info">$${(acct.equity||0).toLocaleString('en',{maximumFractionDigits:2})}</div></div></div>
      <div class="col-auto"><div class="card px-3 py-2"><div class="small text-muted">Cash</div><div class="fw-bold text-success">$${(acct.cash||0).toLocaleString('en',{maximumFractionDigits:2})}</div></div></div>
      <div class="col-auto"><div class="card px-3 py-2"><div class="small text-muted">Market Value</div><div class="fw-bold">$${(acct.market_value||0).toLocaleString('en',{maximumFractionDigits:2})}</div></div></div>
      <div class="col-auto"><div class="card px-3 py-2"><div class="small text-muted">Unrealized P&L</div><div class="fw-bold ${plCls}">$${(acct.unrealized_pl||0).toFixed(2)} (${(acct.unrealized_plpc||0).toFixed(2)}%)</div></div></div>
      <div class="col-auto"><div class="card px-3 py-2"><div class="small text-muted">Buying Power</div><div class="fw-bold">$${(acct.buying_power||0).toLocaleString('en',{maximumFractionDigits:2})}</div></div></div>
      <div class="col-auto"><div class="card px-3 py-2"><div class="small text-muted">Day Trades</div><div class="fw-bold">${acct.day_trade_count||0}</div></div></div>`;
    const tbody=document.getElementById('positions-body');
    const positions=data.positions||[];
    if(!positions.length){tbody.innerHTML='<tr><td colspan="9" class="text-center text-muted py-4">No open positions</td></tr>';return;}
    tbody.innerHTML=positions.map(p=>{
      const plpc=p.unrealized_plpc||0; const plCls=plpc>=0?'pl-positive':'pl-negative';
      const s=p.signal;
      // Signal context row
      let sigRow='';
      if(s){
        const sc=s.composite_score||s.confidence||0;
        const scBadge=sc>=70?'bg-success':sc>=50?'bg-warning text-dark':'bg-danger';
        const rr=s.rr?`<span class="badge bg-dark border border-secondary ms-2">R:R ${s.rr}</span>`:'';
        const prog=s.progress_pct!=null?`<div class="mt-1"><div class="small text-muted d-flex justify-content-between"><span>Trade Progress</span><span>${s.progress_pct}% to target</span></div><div class="progress mt-1" style="height:4px"><div class="progress-bar ${s.progress_pct>=100?'bg-success':s.progress_pct>=0?'bg-info':'bg-danger'}" style="width:${Math.max(0,Math.min(100,s.progress_pct||0))}%"></div></div></div>`:'';
        const timeAgoSig=s.generated_at?timeAgo(s.generated_at):'';
        sigRow=`<tr class="signal-detail-row">
          <td colspan="9" class="py-0">
            <div class="signal-context-panel px-3 py-2">
              <div class="row g-2 align-items-start">
                <div class="col-lg-4">
                  <div class="d-flex align-items-center gap-2 mb-1">
                    <span class="badge ${s.direction==='Long'?'bg-success':'bg-primary'}">${s.direction}</span>
                    <span class="badge ${scBadge}">Score ${sc.toFixed(0)}%</span>
                    <span class="badge bg-secondary">${s.timeframe||''}</span>
                    ${rr}
                    <span class="text-muted small ms-auto">${timeAgoSig}</span>
                  </div>
                  <div class="d-flex gap-3 small">
                    <div><span class="text-muted">Entry</span><br><span class="text-info fw-bold">${fmtPrice(s.entry_price)}</span></div>
                    <div><span class="text-muted">Target</span><br><span class="text-success fw-bold">${fmtPrice(s.target_price)}</span></div>
                    <div><span class="text-muted">Stop</span><br><span class="text-danger fw-bold">${fmtPrice(s.stop_loss)}</span></div>
                  </div>
                  ${prog}
                </div>
                <div class="col-lg-5">
                  <div class="small text-muted mb-1"><i class="bi bi-chat-text-fill text-info me-1"></i>Reasoning</div>
                  <div class="small" style="line-height:1.4;color:#ccc">${(s.reasoning||'No reasoning recorded').slice(0,300)}${(s.reasoning||'').length>300?'…':''}</div>
                </div>
                <div class="col-lg-3">
                  ${s.key_risks?`<div class="small text-muted mb-1"><i class="bi bi-exclamation-triangle-fill text-warning me-1"></i>Key Risks</div><div class="small text-warning" style="line-height:1.4">${s.key_risks.slice(0,150)}</div>`:''}
                  ${s.momentum?`<div class="small text-muted mt-2">Momentum: <span class="text-info">${s.momentum}</span></div>`:''}
                  <div class="small text-muted mt-1">Source: ${s.signal_source||'watchlist'}</div>
                </div>
              </div>
            </div>
          </td>
        </tr>`;
      } else {
        sigRow=`<tr class="signal-detail-row"><td colspan="9" class="py-1"><div class="signal-context-panel px-3 py-2"><span class="text-muted small"><i class="bi bi-info-circle me-1"></i>No signal record — position may have been entered manually or signal expired.</span></div></td></tr>`;
      }
      return `<tr class="position-row" style="cursor:pointer" onclick="toggleSignalRow(this)">
        <td class="fw-bold">${p.symbol} <i class="bi bi-chevron-down text-muted" style="font-size:.65rem"></i></td>
        <td><span class="badge ${p.asset_class==='Crypto'?'bg-warning text-dark':'bg-primary'}">${p.asset_class}</span></td>
        <td>${Number(p.qty).toLocaleString()}</td>
        <td>${fmtPrice(p.avg_entry)}</td>
        <td>${fmtPrice(p.current_price)}</td>
        <td>$${Number(p.market_value).toLocaleString('en',{maximumFractionDigits:2})}</td>
        <td class="${plCls}">$${Number(p.unrealized_pl).toFixed(2)}</td>
        <td class="${plCls} fw-bold">${fmtPct(plpc)}</td>
        <td><button class="btn btn-outline-danger btn-sm py-0 px-1" style="font-size:.7rem" onclick="event.stopPropagation();closePosition('${p.symbol}')"><i class="bi bi-x-circle"></i></button></td>
      </tr>${sigRow}`;
    }).join('');
  } catch(e) {
    document.getElementById('positions-body').innerHTML=`<tr><td colspan="9" class="text-danger py-3">${e.message}</td></tr>`;
  }
}

function toggleSignalRow(row) {
  const next = row.nextElementSibling;
  if(!next || !next.classList.contains('signal-detail-row')) return;
  const panel = next.querySelector('.signal-context-panel');
  const icon  = row.querySelector('.bi-chevron-down,.bi-chevron-up');
  if(next.style.display==='none'||!next.style.display){
    next.style.display=''; 
    if(icon){icon.classList.remove('bi-chevron-down');icon.classList.add('bi-chevron-up');}
  } else {
    next.style.display='none';
    if(icon){icon.classList.remove('bi-chevron-up');icon.classList.add('bi-chevron-down');}
  }
}

async function closePosition(sym) {
  if(!confirm(`Close ${sym}?`)) return;
  const res=await POST(`/positions/${sym}/close`,{});
  alert(res.error||`${sym} closed`); loadPositions();
}

async function loadOrders() {
  try {
    const orders=await API('/alpaca/orders');
    const el=document.getElementById('orders-list');
    if(!orders.length){el.innerHTML='<span class="text-muted">No open orders</span>';return;}
    el.innerHTML=orders.map(o=>`<span class="badge bg-dark border border-secondary me-2">${o.symbol} ${o.side} x${o.qty} [${o.status}]
      <button class="btn btn-link btn-sm p-0 text-danger ms-1" onclick="cancelOrder('${o.id}')">✕</button></span>`).join('');
  } catch(e){document.getElementById('orders-list').innerHTML=`<span class="text-danger">${e.message}</span>`;}
}

async function cancelOrder(id) {
  await DEL(`/alpaca/orders/${id}`); loadOrders();
}

/* ── EQUITY CURVE ─────────────────────────────────────────────────────────── */
async function loadEquityCurve(hours=24) {
  try {
    const data=await API(`/portfolio/equity?hours=${hours}`);
    const canvas=document.getElementById('equity-chart');
    const noData=document.getElementById('equity-no-data');
    if(!data.length){canvas.style.display='none';noData.style.display='block';return;}
    canvas.style.display='block'; noData.style.display='none';
    const labels=data.map(d=>new Date(d.time).toLocaleTimeString('en',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}));
    const equities=data.map(d=>d.equity);
    const first=equities[0]||0; const last=equities[equities.length-1]||0;
    const color=last>=first?'rgba(25,135,84,1)':'rgba(220,53,69,1)';
    const fillColor=last>=first?'rgba(25,135,84,0.15)':'rgba(220,53,69,0.15)';
    if(equityChart){equityChart.destroy();}
    equityChart=new Chart(canvas,{type:'line',data:{labels,datasets:[{label:'Equity',data:equities,borderColor:color,backgroundColor:fillColor,borderWidth:2,pointRadius:1,fill:true,tension:0.3}]},options:{responsive:true,plugins:{legend:{display:false}},scales:{x:{display:false},y:{ticks:{callback:v=>`$${v.toLocaleString('en',{maximumFractionDigits:0})}`}}}}});
  } catch(e){}
}

/* ── MARKET TAB ───────────────────────────────────────────────────────────── */
async function loadMarket() {
  try {
    const [mkt, regime] = await Promise.all([API('/market/full'), API('/regime')]);
    // Regime card
    const rc=document.getElementById('regime-detail');
    if(rc&&regime){
      const risk=regime.risk||'medium';
      const em={low:'🟢',medium:'🟡','medium-high':'🟠',high:'🔴'}[risk]||'⚪';
      rc.innerHTML=`${em} <b>${regime.label||'Unknown'}</b> &nbsp;|&nbsp; SPY $${regime.spy_last||'?'} &nbsp; RSI:${regime.spy_rsi||'?'} &nbsp; ADX:${regime.spy_adx||'?'} &nbsp; Drawdown:${regime.spy_drawdown_pct||'?'}% &nbsp;|&nbsp; <span class="text-info">${regime.recommendation||''}</span>`;
    }
    const filter=document.getElementById('market-filter').value;
    // Equities
    let eq=mkt.equities||[];
    if(filter==='positive') eq=eq.filter(a=>(a.change_percent||0)>0);
    if(filter==='negative') eq=eq.filter(a=>(a.change_percent||0)<0);
    document.getElementById('equities-body').innerHTML=eq.map(a=>{
      const chg=a.change_percent||0; const cc=chg>0?'text-success':chg<0?'text-danger':'text-muted';
      return `<tr><td class="fw-bold">${a.symbol}</td><td class="text-muted small">${(a.name||'').slice(0,20)}</td><td>${fmtPrice(a.price)}</td><td class="${cc} fw-bold">${fmtPct(chg)}</td><td class="text-muted small">${a.volume?Number(a.volume).toLocaleString('en',{notation:'compact'}):'—'}</td></tr>`;
    }).join('');
    // Crypto
    document.getElementById('crypto-body').innerHTML=(mkt.crypto||[]).map(a=>{
      const chg=a.change_percent||0; const cc=chg>0?'text-success':chg<0?'text-danger':'text-muted';
      return `<tr><td class="fw-bold">${a.symbol}</td><td>${fmtPrice(a.price)}</td><td class="${cc} fw-bold">${fmtPct(chg)}</td></tr>`;
    }).join('');
  } catch(e){ console.error('Market load error',e); }
}

document.getElementById('market-filter').addEventListener('change',loadMarket);

/* ── THREATS ──────────────────────────────────────────────────────────────── */
async function loadThreats() {
  allThreats=await API('/threats?limit=80');
  renderThreats();
}
function renderThreats() {
  const sev=document.getElementById('threat-filter-sev').value;
  const reg=document.getElementById('threat-filter-region').value;
  let filtered=allThreats.filter(t=>(!sev||t.severity===sev)&&(!reg||t.region===reg));
  document.getElementById('threat-count').textContent=`${filtered.length} threats`;
  const grid=document.getElementById('threats-grid');
  if(!filtered.length){grid.innerHTML='<div class="col-12 text-center text-muted py-5">No threats</div>';return;}
  grid.innerHTML=filtered.map(t=>`
    <div class="col-xl-3 col-lg-4 col-md-6">
      <div class="card h-100 border-${sevColor[t.severity]||'secondary'}">
        <div class="card-header py-2 d-flex justify-content-between">
          <span class="badge sev-${t.severity}">${t.severity}</span>
          <small class="text-muted">${t.country||''}</small>
        </div>
        <div class="card-body py-2">
          <p class="fw-bold mb-1 small">${t.source_url?`<a href="${t.source_url}" target="_blank" class="text-info text-decoration-none">${t.title}</a>`:t.title}</p>
          <p class="text-muted small mb-2" style="font-size:.72rem">${(t.description||'').slice(0,180)}</p>
          <div class="d-flex gap-1 flex-wrap">
            <span class="badge bg-dark border border-secondary small">${t.event_type||''}</span>
            <span class="badge bg-dark border border-secondary small">${t.region||''}</span>
          </div>
        </div>
        <div class="card-footer py-1 small text-muted">${timeAgo(t.published_at)} · ${t.source||''}</div>
      </div>
    </div>`).join('');
}
document.getElementById('threat-filter-sev').addEventListener('change',renderThreats);
document.getElementById('threat-filter-region').addEventListener('change',renderThreats);

/* ── NEWS ─────────────────────────────────────────────────────────────────── */
async function loadNews() {
  allNews=await API('/news?limit=80');
  renderNews();
}
function renderNews() {
  const cat=document.getElementById('news-filter-cat').value;
  const sent=document.getElementById('news-filter-sent').value;
  let filtered=allNews.filter(n=>(!cat||n.category===cat)&&(!sent||n.sentiment===sent));
  const sentIcon={positive:'bi-arrow-up-circle-fill text-success',negative:'bi-arrow-down-circle-fill text-danger',neutral:'bi-dash-circle text-secondary'};
  document.getElementById('news-list').innerHTML=filtered.map(n=>`
    <div class="d-flex align-items-start gap-2 py-2 border-bottom border-secondary">
      <i class="bi ${sentIcon[n.sentiment]||'bi-dash-circle text-muted'} mt-1 flex-shrink-0"></i>
      <div class="flex-grow-1">
        <div class="small fw-bold">${n.url?`<a href="${n.url}" target="_blank" class="text-info text-decoration-none">${n.title}</a>`:n.title}</div>
        <div class="small text-muted mt-1">${(n.summary||'').slice(0,160)}</div>
        <div class="d-flex gap-2 mt-1 flex-wrap">
          <span class="badge bg-dark border border-secondary" style="font-size:.65rem">${n.source||''}</span>
          <span class="badge bg-dark border border-secondary" style="font-size:.65rem">${n.category||''}</span>
          ${(n.affected_assets||[]).slice(0,3).map(a=>`<span class="badge bg-dark border border-warning text-warning" style="font-size:.65rem">${a}</span>`).join('')}
          <span class="text-muted" style="font-size:.65rem">${timeAgo(n.published_at)}</span>
        </div>
      </div>
    </div>`).join('');
}
document.getElementById('news-filter-cat').addEventListener('change',renderNews);
document.getElementById('news-filter-sent').addEventListener('change',renderNews);

/* ── SCANNER ──────────────────────────────────────────────────────────────── */
async function runScan() {
  const sym=document.getElementById('scan-symbol').value.toUpperCase().trim();
  if(!sym){alert('Enter a symbol');return;}
  const tfs=[...document.querySelectorAll('[id^="tf-"]:checked')].map(e=>e.value);
  const genSig=document.getElementById('gen-signal-check').checked;
  const el=document.getElementById('scan-result');
  el.innerHTML='<div class="text-warning"><i class="bi bi-hourglass-split"></i> Fetching OHLCV + running TA... (may take 10-30s)</div>';
  try {
    const data=await POST('/analyze',{symbol:sym,timeframes:tfs,generate_signal:genSig});
    const ta=data.ta||{};
    let html=`<div class="mb-3"><span class="badge bg-info text-dark me-2">${sym}</span> <span class="text-muted small">Analyzed ${tfs.join(', ')}</span></div>`;
    // TA summary per timeframe
    for(const [tf,td] of Object.entries(ta)){
      if(!td||td.error) continue;
      const bias=td.bias||'neutral'; const bc=bias==='bullish'?'success':bias==='bearish'?'danger':'secondary';
      const p=td.price||{}; const rsi=td.rsi; const macd=td.macd||{};
      html+=`<div class="card mb-2">
        <div class="card-header py-1 d-flex justify-content-between">
          <span class="small fw-bold">${tf}</span>
          <span class="badge bg-${bc}">${bias.toUpperCase()}</span>
        </div>
        <div class="card-body py-2 small">
          <div class="row g-2">
            <div class="col-6"><b>Price:</b> ${fmtPrice(p.last)} &nbsp; <span class="text-muted">EMA20:${fmtPrice(p.ema20)} EMA50:${fmtPrice(p.ema50)}</span></div>
            <div class="col-6"><b>RSI:</b> <span class="${rsi>70?'text-danger':rsi<30?'text-success':'text-muted'}">${rsi!=null?rsi.toFixed(1):'N/A'}</span> &nbsp; <b>ATR:</b> ${td.atr?.pct!=null?td.atr.pct.toFixed(2)+'%':'N/A'}</div>
            <div class="col-6"><b>MACD:</b> ${macd.value!=null?macd.value.toFixed(4):'N/A'} signal:${macd.signal!=null?macd.signal.toFixed(4):'N/A'}</div>
            <div class="col-6"><b>Volume:</b> ${td.volume?.surge?'<span class="text-success">SURGE</span>':td.volume?.dry?'<span class="text-warning">DRY</span>':'Normal'}</div>
          </div>
        </div>
      </div>`;
    }
    if(data.signal){
      const sig=data.signal;
      if(sig.error){
        html+=`<div class="alert alert-warning mt-2">LLM error: ${sig.error}</div>`;
      } else {
        html+=`<div class="card mt-3 border-success">
          <div class="card-header py-2 bg-success bg-opacity-10"><span class="fw-bold"><i class="bi bi-lightning-fill text-success"></i> Generated Signal</span></div>
          <div class="card-body small">
            <div class="row g-2">
              <div class="col-6"><b>Direction:</b> <span class="${sig.direction==='Long'?'text-success':'text-primary'}">${sig.direction}</span></div>
              <div class="col-6"><b>Confidence:</b> ${sig.confidence}%</div>
              <div class="col-6"><b>Entry:</b> ${fmtPrice(sig.entry_price)}</div>
              <div class="col-6"><b>Target:</b> <span class="text-success">${fmtPrice(sig.target_price)}</span></div>
              <div class="col-12"><b>Stop:</b> <span class="text-danger">${fmtPrice(sig.stop_loss)}</span></div>
              <div class="col-12 text-muted">${sig.reasoning||''}</div>
              ${sig.key_risks?`<div class="col-12 text-warning"><i class="bi bi-exclamation-triangle-fill"></i> ${sig.key_risks}</div>`:''}
            </div>
          </div>
        </div>`;
      }
    }
    el.innerHTML=html;
  } catch(e){el.innerHTML=`<div class="text-danger">Error: ${e.message}</div>`;}
}

/* ── JOBS TAB ─────────────────────────────────────────────────────────────── */
async function loadJobs() {
  try {
    const [jobs, cache, llm] = await Promise.all([
      API('/jobs/status'),
      API('/cache/stats').catch(()=>({})),
      API('/llm/health').catch(()=>({}))
    ]);
    const jobNames={market:'Market Data',threats:'Threat News',signals:'Signal Gen',execute:'Execute',positions:'Positions',telegram:'Telegram'};
    const schedules={market:'every 15m',threats:'every 15m +7m',signals:'every 30m',execute:'every 30m +3m',positions:'every 5m',telegram:'every 1m'};
    const grid=document.getElementById('jobs-grid');
    grid.innerHTML=Object.entries(jobs).map(([name,info])=>{
      const sc={ok:'success',running:'warning',error:'danger',idle:'secondary'}[info.status]||'secondary';
      const icon={ok:'bi-check-circle-fill',running:'bi-hourglass-split',error:'bi-x-circle-fill',idle:'bi-pause-circle'}[info.status]||'bi-pause-circle';
      return `<div class="col-lg-4 col-md-6">
        <div class="card h-100 border-${sc}">
          <div class="card-body py-3">
            <div class="d-flex justify-content-between align-items-center mb-2">
              <span class="fw-bold"><i class="bi ${icon} text-${sc} me-2"></i>${jobNames[name]||name}</span>
              <span class="badge bg-${sc}">${info.status}</span>
            </div>
            <div class="small text-muted mb-1">Schedule: ${schedules[name]||'—'}</div>
            <div class="small text-muted mb-2">Last run: ${info.last?timeAgo(info.last):'Never'}</div>
            ${info.error?`<div class="alert alert-danger py-1 small mb-2">${info.error}</div>`:''}
            <button class="btn btn-outline-primary btn-sm w-100" onclick="triggerJob('${name}')"><i class="bi bi-play-fill"></i> Run Now</button>
          </div>
        </div>
      </div>`;
    }).join('');
    // Append cache + LLM status cards
    const llmOk=llm.ok!==false;
    const cacheRows=cache.symbols?`<div class="small text-muted">Symbols: ${cache.symbols} · Bars: ${(cache.total_bars||0).toLocaleString()}</div>`:'<div class="small text-muted">No cache data yet</div>';
    grid.innerHTML+=`
      <div class="col-lg-4 col-md-6">
        <div class="card h-100 border-${llmOk?'success':'danger'}">
          <div class="card-body py-3">
            <div class="d-flex justify-content-between mb-2">
              <span class="fw-bold"><i class="bi bi-robot me-2 text-${llmOk?'success':'danger'}"></i>LM Studio</span>
              <span class="badge bg-${llmOk?'success':'danger'}">${llmOk?'Online':'Offline'}</span>
            </div>
            <div class="small text-muted mb-1">${llm.model||llm.url||'No config'}</div>
            <div class="small text-muted">${llm.error||''}</div>
          </div>
        </div>
      </div>
      <div class="col-lg-4 col-md-6">
        <div class="card h-100 border-info">
          <div class="card-body py-3">
            <div class="d-flex justify-content-between mb-2">
              <span class="fw-bold"><i class="bi bi-database me-2 text-info"></i>OHLCV Cache</span>
              <button class="btn btn-outline-info btn-sm py-0" onclick="triggerBackfill()">Backfill</button>
            </div>
            ${cacheRows}
            <div class="small text-muted mt-1">yfinance fallback: active</div>
          </div>
        </div>
      </div>`;
  } catch(e){ document.getElementById('jobs-grid').innerHTML=`<div class="col-12 text-danger">${e.message}</div>`; }
}

async function triggerJob(name) {
  const res=await POST(`/jobs/${name}/trigger`,{});
  if(res.ok) { setTimeout(loadJobs,1500); }
}

async function triggerBackfill() {
  const res=await POST('/cache/backfill',{});
  alert(res.message||'Backfill started');
}

/* ── SETTINGS ─────────────────────────────────────────────────────────────── */
const PLATFORM_DEFS = {
  alpaca_paper:  {label:'Alpaca Paper',  fields:{api_key:'API Key (PKTEST...)',api_secret:'Secret Key',api_url:'https://paper-api.alpaca.markets',extra_field_1:'paper',extra_field_2:'Chat/Notes'},desc:'Alpaca paper trading — free tier'},
  alpaca_live:   {label:'Alpaca Live',   fields:{api_key:'API Key (PK...)',api_secret:'Secret Key',api_url:'https://api.alpaca.markets',extra_field_1:'live',extra_field_2:'Notes'},desc:'Alpaca live trading — real money'},
  lmstudio:      {label:'LM Studio',     fields:{api_url:'http://localhost:1234/v1',extra_field_1:'Model name (e.g. mistral-7b)',extra_field_2:'Notes'},desc:'Local LM Studio — OpenAI-compatible endpoint'},
  openai:        {label:'OpenAI',        fields:{api_key:'API Key (sk-...)',api_url:'https://api.openai.com/v1',extra_field_1:'Model (gpt-4o / gpt-4-turbo)',extra_field_2:'Notes'},desc:'OpenAI cloud LLM'},
  anthropic:     {label:'Anthropic',     fields:{api_key:'API Key (sk-ant-...)',api_url:'https://api.anthropic.com',extra_field_1:'Model (claude-3-5-sonnet-20241022)',extra_field_2:'Notes'},desc:'Anthropic Claude'},
  groq:          {label:'Groq',          fields:{api_key:'API Key',api_url:'https://api.groq.com/openai/v1',extra_field_1:'Model (llama-3.1-70b-versatile)',extra_field_2:'Notes'},desc:'Groq — ultra-fast inference'},
  deepseek:      {label:'DeepSeek',      fields:{api_key:'API Key',api_url:'https://api.deepseek.com/v1',extra_field_1:'Model (deepseek-reasoner / deepseek-chat)',extra_field_2:'Notes'},desc:'DeepSeek — strong reasoning model'},
  ollama:        {label:'Ollama',        fields:{api_url:'http://localhost:11434/v1',extra_field_1:'Model (llama3.2 / mistral / phi4)',extra_field_2:'Notes'},desc:'Ollama — local model runner'},
  telegram:      {label:'Telegram',      fields:{api_key:'Bot Token (from @BotFather)',extra_field_1:'Default Chat ID',extra_field_2:'Notes'},desc:'Telegram bot for alerts & commands'},
  coinbase:      {label:'Coinbase Adv.', fields:{api_key:'API Key',api_secret:'API Secret',api_url:'https://api.coinbase.com',extra_field_1:'passphrase (if CDP)',extra_field_2:'Notes'},desc:'Coinbase Advanced Trade'},
  kraken:        {label:'Kraken',        fields:{api_key:'API Key',api_secret:'Private Key',api_url:'https://api.kraken.com',extra_field_1:'Notes'},desc:'Kraken exchange'},
  binance:       {label:'Binance',       fields:{api_key:'API Key',api_secret:'Secret Key',api_url:'https://api.binance.com',extra_field_1:'testnet? (yes/no)',extra_field_2:'Notes'},desc:'Binance spot/futures'},
  interactive:   {label:'IBKR',          fields:{api_url:'http://localhost:5000/v1/api',extra_field_1:'Account ID',extra_field_2:'Notes'},desc:'Interactive Brokers — Client Portal API'},
  tradier:       {label:'Tradier',       fields:{api_key:'Access Token',api_url:'https://api.tradier.com/v1',extra_field_1:'Account ID',extra_field_2:'sandbox (yes/no)'},desc:'Tradier broker — equities + options'},
  tradovate:     {label:'Tradovate',     fields:{api_key:'Username',api_secret:'Password',api_url:'https://live.tradovateapi.com/v1',extra_field_1:'CID',extra_field_2:'Notes'},desc:'Tradovate — futures'},
};

function updatePlatformFields() {
  const sel=document.getElementById('cfg-platform').value;
  const def=PLATFORM_DEFS[sel]||{};
  const f=def.fields||{};
  if(f.api_key!==undefined){document.getElementById('cfg-key').placeholder=f.api_key;}
  if(f.extra_field_1!==undefined){document.getElementById('cfg-extra1').placeholder=f.extra_field_1;document.getElementById('extra1-label').textContent=f.extra_field_1.split('(')[0].trim()||'Extra Field 1';}
  if(f.api_url!==undefined&&!document.getElementById('cfg-url').value){document.getElementById('cfg-url').placeholder=f.api_url;}
}
document.getElementById('cfg-platform').addEventListener('change',updatePlatformFields);

async function loadSettings() {
  const configs=await API('/settings');
  const el=document.getElementById('configs-list');
  if(!configs.length){el.innerHTML='<div class="text-muted small p-3">No configurations yet. Add your API credentials on the left.</div>';return;}
  const grouped={};
  configs.forEach(c=>{(grouped[c.platform]=grouped[c.platform]||[]).push(c);});
  el.innerHTML=Object.entries(grouped).map(([platform,cfgs])=>`
    <div class="card mb-3">
      <div class="card-header py-2 small fw-bold">${PLATFORM_DEFS[platform]?.label||platform}</div>
      <div class="list-group list-group-flush">
        ${cfgs.map(c=>`<div class="list-group-item bg-dark py-2">
          <div class="d-flex justify-content-between align-items-start">
            <div>
              <span class="fw-bold small">${c.label}</span>
              ${c.is_default?'<span class="badge bg-success ms-1" style="font-size:.6rem">DEFAULT</span>':''}
              ${!c.is_active?'<span class="badge bg-secondary ms-1" style="font-size:.6rem">INACTIVE</span>':''}
              <div class="small text-muted mt-1">${c.api_key?'Key: '+c.api_key.slice(0,8)+'…':''} ${c.api_url?'| URL: '+c.api_url.slice(0,30):''}${c.extra_field_1?' | '+c.extra_field_1:''}</div>
              ${c.notes?`<div class="small text-muted">${c.notes}</div>`:''}
            </div>
            <div class="d-flex gap-1 ms-2 flex-shrink-0">
              ${!c.is_default?`<button class="btn btn-outline-success btn-sm py-0 px-1" style="font-size:.7rem" onclick="setDefault('${c.id}')">★</button>`:''}
              <button class="btn btn-outline-primary btn-sm py-0 px-1" style="font-size:.7rem" onclick="editConfig(${JSON.stringify(c).replace(/"/g,'&quot;')})">✎</button>
              <button class="btn btn-outline-danger btn-sm py-0 px-1" style="font-size:.7rem" onclick="deleteConfig('${c.id}')">✕</button>
            </div>
          </div>
        </div>`).join('')}
      </div>
    </div>`).join('');
}

function editConfig(c) {
  document.getElementById('cfg-edit-id').value=c.id;
  document.getElementById('cfg-label').value=c.label||'';
  document.getElementById('cfg-platform').value=c.platform||'alpaca_paper';
  document.getElementById('cfg-key').value=c.api_key||'';
  document.getElementById('cfg-secret').value=c.api_secret||'';
  document.getElementById('cfg-url').value=c.api_url||'';
  document.getElementById('cfg-extra1').value=c.extra_field_1||'';
  document.getElementById('cfg-notes').value=c.notes||'';
  document.getElementById('cfg-active').checked=c.is_active!==false;
  document.getElementById('cfg-default').checked=c.is_default===true;
  updatePlatformFields();
}

function clearConfigForm() {
  ['cfg-edit-id','cfg-label','cfg-key','cfg-secret','cfg-url','cfg-extra1','cfg-notes'].forEach(id=>{document.getElementById(id).value='';});
  document.getElementById('cfg-active').checked=true;
  document.getElementById('cfg-default').checked=false;
}

async function saveConfig() {
  const id=document.getElementById('cfg-edit-id').value;
  const body={
    label:document.getElementById('cfg-label').value,
    platform:document.getElementById('cfg-platform').value,
    api_key:document.getElementById('cfg-key').value,
    api_secret:document.getElementById('cfg-secret').value,
    api_url:document.getElementById('cfg-url').value,
    extra_field_1:document.getElementById('cfg-extra1').value,
    notes:document.getElementById('cfg-notes').value,
    is_active:document.getElementById('cfg-active').checked,
    is_default:document.getElementById('cfg-default').checked,
  };
  if(!body.label||!body.platform){alert('Label and Platform are required');return;}
  const res=id?await fetch(`/api/settings/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json())
               :await POST('/settings',body);
  if(res.error){alert('Error: '+res.error);return;}
  clearConfigForm(); loadSettings();
}

async function setDefault(id) {
  await POST(`/settings/${id}/set-default`,{}); loadSettings();
}

async function deleteConfig(id) {
  if(!confirm('Delete this config?')) return;
  await DEL(`/settings/${id}`); loadSettings();
}

/* ── GLOBAL INIT + REFRESH ───────────────────────────────────────────────── */
async function refreshAll() {
  const active=document.querySelector('.nav-link.active')?.getAttribute('href')?.replace('#tab-','');
  document.getElementById('last-refresh').textContent='Updated '+new Date().toLocaleTimeString();
  refreshJobIndicators();
  refreshRegimeBadge();
  if(active==='signals'||!active) loadSignals();
  else if(active==='positions'){loadPositions();loadEquityCurve(24);}
  else if(active==='market')   loadMarket();
  else if(active==='threats')  loadThreats();
  else if(active==='news')     loadNews();
  else if(active==='jobs')     loadJobs();
  else if(active==='settings') loadSettings();
}

// Tab change — load relevant data
document.querySelectorAll('[data-bs-toggle="tab"]').forEach(el=>{
  el.addEventListener('shown.bs.tab',e=>{
    const tab=e.target.getAttribute('href')?.replace('#tab-','');
    if(tab==='signals')   loadSignals();
    else if(tab==='positions'){loadPositions();loadEquityCurve(24);}
    else if(tab==='market')   loadMarket();
    else if(tab==='threats')  loadThreats();
    else if(tab==='news')     loadNews();
    else if(tab==='scanner')  {} 
    else if(tab==='jobs')     loadJobs();
    else if(tab==='settings'){loadSettings();updatePlatformFields();}
  });
});

// Initial load
loadSignals();
refreshJobIndicators();
refreshRegimeBadge();

// Auto-refresh every 60s
setInterval(refreshAll, 60000);
