/* Aggregate-only public dashboard. No credentials, private API or external CDN. */
(function () {
  "use strict";
  const $ = (selector) => document.querySelector(selector);
  const ranges = ["today", "yesterday", "7d", "30d"];
  const state = { range: "today", metric: "quota", window: "0", granularity: "hour", data: null, controller: null, serial: 0 };
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const num = (v) => v == null ? "—" : Number(v).toLocaleString("zh-CN", {maximumFractionDigits: 2});
  const date = (v, detailed=false) => new Date(v).toLocaleString("zh-CN", {timeZone:"Asia/Hong_Kong", month:"2-digit", day:"2-digit", ...(detailed ? {hour:"2-digit",minute:"2-digit",hour12:false} : {})});
  let processingLoading = false;
  function processingStatus(data) {
    const node=$("#processing-status");
    const fresh=data && Math.abs(Date.now()-new Date(data.observed_at).getTime())<10000;
    const ok=fresh && data.available && Number.isInteger(data.active_requests) && data.active_requests>=0 && Number.isInteger(data.limit) && data.limit>0;
    node.className='processing-status'+(ok ? '' : ' unavailable');
    node.textContent=ok ? `正在处理：${data.active_requests} / ${data.limit}` : '正在处理：暂不可确认';
  }
  async function loadProcessing() {
    if(processingLoading || document.visibilityState==='hidden') return;
    processingLoading=true;
    const controller=new AbortController(), timeout=window.setTimeout(()=>controller.abort(),2500);
    try {
      const response=await fetch('/public/api/overview?live=true',{signal:controller.signal,credentials:'omit',cache:'no-store',headers:{Accept:'application/json'}});
      if(!response.ok) throw new Error('processing unavailable');
      processingStatus((await response.json()).processing);
    } catch(err) { processingStatus(null); }
    finally { window.clearTimeout(timeout);processingLoading=false; }
  }
  function quotaWindow() { return state.data.quota_windows.find((w) => w.id === state.window); }
  function relayStatus() {
    const relay=state.data.relay || {},labels={online:'中继在线',degraded:'中继通道未确认',offline:'中继离线',unknown:'状态待确认'};
    const node=$("#relay-status"),status=Object.hasOwn(labels,relay.state) ? relay.state : 'unknown';
    node.className='relay-status '+status;
    node.textContent='● '+labels[status];
    node.title='心跳：'+(relay.heartbeat_ok ? '正常':'未确认')+'；中继通道：'+(relay.tunnel_ok ? '正常':'未确认');
  }
  function quotaCards() {
    const windows=state.data.quota_windows;
    if(!windows.length) { $("#quota-cards").innerHTML='<article class="quota-card"><h3>当前额度</h3><p class="muted">尚无已确认的额度快照，稍后自动更新。</p></article>';return; }
    $("#quota-cards").innerHTML=windows.map((w)=>{
      const remaining=Math.max(0,Math.min(100,Number(w.remaining_percent))),stale=!w.fresh || w.reset_pending_confirmation;
      const reset=w.resets_at ? new Date(w.resets_at) : null;
      const fullDate=reset && !Number.isNaN(reset.getTime()) ? reset.toLocaleString('zh-CN',{timeZone:'Asia/Hong_Kong',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}) : '上游未提供';
      const diff=reset ? Math.max(0,reset.getTime()-Date.now()) : 0,days=Math.floor(diff/86400000),hours=Math.floor(diff%86400000/3600000),minutes=Math.floor(diff%3600000/60000);
      const countdown=w.reset_pending_confirmation ? '预计重置时间已到，等待新快照确认' : !reset ? '重置时间未知' : `距离重置约 ${days ? days+' 天 ':''}${hours} 小时 ${minutes} 分钟`;
      return `<article class="quota-card"><div class="quota-card-head"><h3>${esc(w.label)}额度</h3><span class="freshness${stale?' stale':''}">${stale?'待更新':'最新快照'}</span></div><div class="quota-value">${esc(num(remaining))}<small>% 剩余</small></div><div class="quota-progress" role="progressbar" aria-label="${esc(w.label)}剩余额度" aria-valuenow="${remaining}" aria-valuemin="0" aria-valuemax="100"><div style="width:${remaining}%"></div></div><div class="quota-detail"><span>已用 ${esc(num(w.current_used_percent))}%</span><span>剩余 ${esc(num(remaining))}%</span></div><p class="quota-caption">采集于 ${esc(date(state.data.quota_captured_at,true))}${!w.fresh?' · 旧快照，仅供参考':''}${w.current_used_percent>100?' · 上游已用比例超过 100%，请查看详情':''}</p></article>`+
        `<article class="quota-card reset-card"><div class="quota-card-head"><h3>${esc(w.label)} · 预计重置时间</h3><span class="clock-symbol">◷</span></div><div class="reset-date">${esc(fullDate)}</div><span class="timezone-tag">Asia/Hong_Kong · UTC+8</span><p class="reset-countdown${w.reset_pending_confirmation?' pending':''}">${esc(countdown)}</p><p class="quota-caption">上游预计时间；到时不会自动把旧快照改为 100%。</p></article>`;
    }).join('');
  }
  function cards() {
    const data = state.data, totals = data.totals, window = quotaWindow();
    const valid = window ? window.hourly_used_points.filter((p) => p !== null).length : 0;
    const quotaNote = window ? `${window.label} · 当前已用 ${num(window.current_used_percent)}%${window.fresh ? "" : "（旧快照）"}` : "尚无已确认的额度快照";
    const card = (label, value, unit, note, accent=false, tag="") => `<article class="metric"><div class="metric-label">${esc(label)}${tag ? `<span class="metric-tag">${esc(tag)}</span>` : ""}</div><div class="metric-value${accent ? " accent" : ""}">${esc(value)}${unit ? `<small>${esc(unit)}</small>` : ""}</div><div class="metric-note">${esc(note)}</div></article>`;
    $("#metrics").innerHTML = card("期间额度消耗 · 已观测", num(window?.observed_used_points), "百分点", quotaNote, true, valid ? `${valid} 小时有采样` : "数据不足") +
      card("期间 Token 用量", num(totals.total_tokens), "", `输入 ${num(totals.input_tokens)} · 输出 ${num(totals.output_tokens)}`) +
      card("期间请求数", num(totals.requests), "次", totals.unknown_requests ? `${num(totals.unknown_requests)} 次用量未报告或有冲突` : "上游已报告用量均已计入") +
      card("保留流水累计 Token", num(data.recorded_total.total_tokens), "", data.recorded_total.since ? `${date(data.recorded_total.since)} 起 · ${num(data.recorded_total.requests)} 次请求` : "尚无请求流水");
  }
  function series() {
    const window = quotaWindow();
    const hourly = state.data.hourly.map((b, i) => ({at:b.at, requests:b.requests, tokens:b.total_tokens,
      value:state.metric === "quota" ? (window?.hourly_used_points[i] ?? null) : state.metric === "tokens" ? b.total_tokens : b.requests,
      coverage:window?.coverage_minutes[i] ?? 0}));
    if (state.granularity === "hour") return hourly;
    const days = [];
    for (let i=0; i<hourly.length; i+=24) {
      const rows = hourly.slice(i,i+24), values = rows.filter((r) => r.value !== null);
      days.push({at:rows[0].at, requests:rows.reduce((sum,r)=>sum+r.requests,0),tokens:rows.reduce((sum,r)=>sum+r.tokens,0),
        value:values.length ? values.reduce((sum,r)=>sum+r.value,0) : null, coverage:rows.reduce((sum,r)=>sum+r.coverage,0)});
    }
    return days;
  }
  function chart() {
    const rows = series(), window = quotaWindow(), unit = state.metric === "quota" ? "百分点" : state.metric === "tokens" ? "Token" : "次";
    $("#window-label").hidden = state.metric !== "quota";
    $("#chart-description").textContent = `${state.granularity === "hour" ? "每小时" : "每天"} · ${unit} · UTC+8`;
    const valid = rows.filter((r) => r.value !== null);
    if (!valid.length) {
      $("#chart").innerHTML = '<p class="empty">此范围内暂无可计算的额度增量。<br>请等待更多快照，或切换到 Token 用量 / 请求数。</p>';
    } else {
      const max = Math.max(...valid.map((r)=>r.value)), ceiling = max ? max*1.15 : 1;
      const W=1080,H=285,left=64,right=16,top=28,bottom=38,width=W-left-right,height=H-top-bottom,step=width/rows.length;
      let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc($("#chart-description").textContent)}用量柱状图；详细值见下方数据表"><text class="axis-unit" x="${left}" y="12">${esc(unit)}</text>`;
      for(let i=0;i<=4;i++) {
        const y=top+height*i/4,v=ceiling*(1-i/4);
        svg+=`<line x1="${left}" y1="${y}" x2="${W-right}" y2="${y}" stroke="#edf0f6" stroke-dasharray="3 4"/><text x="${left-12}" y="${y+4}" text-anchor="end">${esc(num(v))}</text>`;
      }
      rows.forEach((r,i)=>{
        const x=left+step*i+step*.17,w=Math.max(.8,step*.66),h=r.value === null ? 3 : r.value ? Math.max(1.5,height*r.value/ceiling) : 1;
        const tooltip=`${date(r.at,true)}\n${r.value === null ? "数据不足" : num(r.value)+" "+unit}\n${num(r.requests)} 次请求 · ${num(r.tokens)} Token${state.metric === "quota" ? "\n采样覆盖约 "+num(r.coverage)+" 分钟" : ""}`;
        svg+=`<rect class="${r.value === null ? "no-data" : "bar"}" x="${x}" y="${top+height-h}" width="${w}" height="${h}" rx="${step>10?2:0}"><title>${esc(tooltip)}</title></rect>`;
        const every=Math.max(1,Math.ceil(rows.length/8));
        if(i%every===0) {
          const label=state.granularity === "hour" && rows.length<=24 ? date(r.at,true).split(" ").pop() : date(r.at,state.granularity === "hour");
          svg+=`<text x="${left+step*(i+.5)}" y="${H-12}" text-anchor="${i===0 ? "start":"middle"}">${esc(label)}</text>`;
        }
      });
      $("#chart").innerHTML=svg+'</svg>';
    }
    $("#chart-note").textContent=state.metric === "quota" ? `${window?.label || "额度窗口"} · 灰色空档表示无有效增量，蓝色数据可能仅覆盖部分时段。` : "仅统计本网关流水；缺失或冲突的 Token 用量不计入。";
    $("#data-table").innerHTML=`<table><caption>${esc($("#chart-description").textContent)}</caption><thead><tr><th>时间（UTC+8）</th><th>${esc(unit)}</th><th>请求数</th><th>Token</th>${state.metric === "quota" ? "<th>采样覆盖（分钟）</th>" : ""}</tr></thead><tbody>` + rows.map((r)=>`<tr><td>${esc(date(r.at,state.granularity === "hour"))}</td><td>${r.value === null ? "数据不足" : esc(num(r.value))}</td><td>${esc(num(r.requests))}</td><td>${esc(num(r.tokens))}</td>${state.metric === "quota" ? `<td>${esc(num(r.coverage))}</td>` : ""}</tr>`).join("")+"</tbody></table>";
  }
  function render() {
    const options=state.data.quota_windows;
    if(!options.some((w)=>w.id===state.window)) state.window=options[0]?.id || "0";
    $("#window").innerHTML=options.length ? options.map((w)=>`<option value="${esc(w.id)}"${w.id===state.window ? " selected":""}>${esc(w.label)}</option>`).join("") : '<option value="0">尚无快照</option>';
    $("#date-range").textContent=`${date(state.data.start)} — ${date(new Date(new Date(state.data.end).getTime()-1))} · UTC+8`;
    relayStatus(); cards(); quotaCards(); chart();
  }
  async function load() {
    if(state.controller) state.controller.abort();
    const controller=new AbortController(),serial=++state.serial;
    state.controller=controller;
    $("#refresh").disabled=true;
    const timeout=window.setTimeout(()=>controller.abort(),10000);
    try {
      const response=await fetch('/public/api/overview?range='+encodeURIComponent(state.range),{signal:controller.signal,credentials:"omit",headers:{Accept:"application/json"}});
      if(!response.ok) throw new Error('汇总服务暂时不可用（HTTP '+response.status+'）');
      const data=await response.json();
      if(serial!==state.serial) return;
      state.data=data; render();
      $("#connection").className="";
      $("#connection").textContent='汇总数据更新于 '+date(data.generated_at,true)+' · 每 10 秒获取；正在处理每 2 秒更新';
    } catch(err) {
      if(serial!==state.serial) return;
      $("#relay-status").className='relay-status unknown';
      $("#relay-status").textContent='● 连接状态暂不可确认';
      $("#relay-status").title='监测数据获取失败，请刷新重试';
      $("#connection").className="error";
      $("#connection").textContent=(err.name==='AbortError' ? '连接超时' : '无法获取汇总数据')+'；'+(state.data ? '暂时保留上次数据。' : '请点击刷新重试。');
      if(!state.data) { $("#metrics").innerHTML='<article class="metric">暂时没有可展示的数据</article>'; $("#chart").innerHTML='<p class="empty">暂时无法连接汇总服务，请稍后刷新。</p>'; }
    } finally {
      window.clearTimeout(timeout);
      if(serial===state.serial) {state.controller=null;$("#refresh").disabled=false;}
    }
  }
  function boot() {
    document.querySelectorAll('[data-range]').forEach((button)=>button.addEventListener('click',()=>{
      if(!ranges.includes(button.dataset.range)) return;
      state.range=button.dataset.range;
      // Do not present old-range figures under a newly selected range on fetch failure.
      state.data=null;$("#metrics").innerHTML='<article class="metric skeleton">正在加载所选范围…</article>';
      $("#chart").innerHTML='<p class="empty">正在加载…</p>';$("#data-table").innerHTML="";
      document.querySelectorAll('[data-range]').forEach((b)=>{b.classList.toggle('selected',b===button);b.setAttribute('aria-pressed',b===button ? 'true':'false');});
      load();
    }));
    ['metric','window','granularity'].forEach((name)=>$('#'+name).addEventListener('change',(event)=>{state[name]=event.target.value;if(state.data){cards();chart();}}));
    $("#refresh").addEventListener('click',load);
    $("#table-toggle").addEventListener('click',()=>{const table=$("#data-table"),button=$("#table-toggle");table.hidden=!table.hidden;button.setAttribute('aria-expanded',String(!table.hidden));button.textContent=table.hidden ? '查看数据表':'收起数据表';});
    load();
    loadProcessing();
    window.setInterval(loadProcessing,2000);
    document.addEventListener('visibilitychange',()=>{if(document.visibilityState!=='hidden') loadProcessing();});
    window.setInterval(()=>{if(document.visibilityState!=='hidden' && !state.controller) load();},10000);
  }
  window.addEventListener('DOMContentLoaded',boot);
})();
