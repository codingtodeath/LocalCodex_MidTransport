/* Same-origin private interval comparison. Inputs use UTC+8 on every device. */
(function () {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const num = (v) => v == null ? "—" : Number(v).toLocaleString("zh-CN", {maximumFractionDigits:6});
  const local = (date) => new Date(date.getTime()+8*3600000).toISOString().slice(0,16);
  const when = (v) => v ? new Date(v).toLocaleString("zh-CN", {timeZone:"Asia/Hong_Kong",hour12:false}) : "—";
  const money = (low, high, currency) => low == null || high == null ? "不可估价" :
    (currency || "")+" "+num(low)+(low === high ? "" : " ～ "+num(high));
  const issues = {
    key_quota_not_attributable:"当前费用仅属于所选 Key；账号百分比无法拆分到该 Key，因此不计算兑换比例。",
    not_enough_snapshots:"区间内不足两条真实额度快照，请扩大区间。",
    account_changed_or_unknown:"区间跨账号／工作空间或身份未知，请选择同一账号的区间。",
    identity_unverified:"部分快照未完成身份核验。",
    external_usage_not_confirmed:"请确认所选区间没有在中继外使用此账号，才能显示费用与额度的比例。",
    requests_cross_boundary:"有请求跨越实际区间边界；其消耗无法拆分，请把起止时间调整到请求之间。",
    no_contained_requests:"实际比较区间内没有完整的中继请求。",
    incomplete_cost_coverage:"部分请求缺少计量或价格；仅显示可估价部分，不计算完整费用比例。",
    mixed_currencies:"价格包含不同币种，不能直接相加。",
    snapshot_gap:"额度快照存在超过 15 分钟的采样缺口，不计算完整比例。",
    reset_or_cycle_changed:"区间跨额度重置、比例回退或周期不明确，请缩短区间。",
    window_missing_or_invalid:"部分快照缺少此额度窗口或数值异常。",
    no_observed_quota_increase:"未观测到额度百分点增加；可能是上游整数百分比精度限制，不能除以 0。"
  };
  const settings = {start:local(new Date(Date.now()-3*3600000)),end:local(new Date()),confirmed:false,
                    result:null,dirty:false,busy:false,serial:0};

  function resultMarkup(data) {
    if (!data) return '<p class="muted">选择一段仅通过中继使用账号的时间，点击“分析区间”。</p>';
    const cost=data.cost;
    const notes=new Set(data.issues || []);
    (data.windows || []).forEach((w)=>(w.issues || []).forEach((i)=>notes.add(i)));
    let html='<p class="small">实际比较区间（UTC+8）：<b>'+esc(when(data.effective_start))+' → '+esc(when(data.effective_end))+'</b><br>'+
      '已对齐区间内第一条和最后一条真实快照，共 '+esc(num(data.snapshot_count))+' 条；不插值。'+
      (data.effective_start ? ' 所选起止边缘分别舍去 '+esc(num(data.start_trim_seconds))+' / '+esc(num(data.end_trim_seconds))+' 秒。':'')+'</p>';
    if (cost) {
      html+='<div class="analysis-metrics"><div class="analysis-metric"><span>Token 估算费用 · 非账单</span><strong>'+esc(money(cost.min,cost.max,cost.currency))+'</strong><small>可估价 '+esc(num(cost.priced_requests))+' / '+esc(num(cost.requests))+' 个区间内请求</small></div>'+
        '<div class="analysis-metric"><span>区间 Token 用量 · 已报告</span><strong>'+esc(num(cost.total_tokens))+'</strong><small>输入 '+esc(num(cost.input_tokens))+' · 缓存读取 '+esc(num(cost.cached_input_tokens))+' · 输出 '+esc(num(cost.output_tokens))+'</small></div></div>';
      if (cost.crossing_requests) html+='<p class="warn small">另有 '+esc(num(cost.crossing_requests))+' 个跨边界或未结束请求未计入完整区间费用。</p>';
      if (cost.uncertain_write_requests) html+='<p class="warn small">'+esc(num(cost.uncertain_write_requests))+' 个请求未报告缓存写入量，费用按全部普通输入到全部缓存写入的边界给出范围。</p>';
    }
    (data.windows || []).forEach((w)=>{
      html+='<article class="analysis-window"><h3>'+esc(w.label)+'</h3><div class="analysis-metrics">'+
        '<div class="analysis-metric"><span>已观测额度消耗</span><strong>'+esc(num(w.observed_used_points))+' <small>百分点</small></strong><small>已用 '+esc(num(w.start_used_percent))+'% → '+esc(num(w.end_used_percent))+'%；采样覆盖 '+esc(num(w.coverage_seconds/60))+' / '+esc(num(w.duration_seconds/60))+' 分钟</small></div>'+
        '<div class="analysis-metric"><span>每消耗 1 个百分点的估算费用</span><strong>'+esc(w.ratio_available ? money(w.cost_per_point_min,w.cost_per_point_max,cost?.currency) : '暂不可估算')+'</strong><small>区间 Token 费用 ÷ 该窗口已观测消耗百分点</small></div></div></article>';
    });
    if (notes.size) html+='<ul class="analysis-notes">'+[...notes].map((i)=>'<li>'+esc(issues[i] || '此区间存在未确认的数据条件。')+'</li>').join('')+'</ul>';
    if ((data.models || []).length) html+='<div class="table-scroll"><table><thead><tr><th>模型</th><th>请求数</th><th>输入 Token</th><th>缓存读取</th><th>输出 Token</th><th>Token 估算费用</th></tr></thead><tbody>'+
      data.models.map((m)=>'<tr><td>'+esc(m.model)+'</td><td>'+esc(num(m.requests))+'</td><td>'+esc(num(m.input_tokens))+'</td><td>'+esc(num(m.cached_input_tokens))+'</td><td>'+esc(num(m.output_tokens))+'</td><td>'+esc(money(m.cost_min,m.cost_max,m.currency))+'</td></tr>').join('')+'</tbody></table></div>';
    html+='<p class="muted small">'+esc(data.price_basis)+'<br>'+esc(data.note)+' 多个额度窗口分别计算，不能把百分点相加；输入 Token 已包含缓存，输出已包含推理 Token。</p>';
    return html;
  }

  function mount(api) {
    const target=$("#quota-analysis");
    if (!target) return;
    target.innerHTML='<h2>额度消耗与 Token 费用</h2><p class="muted small">选择你没有直接使用此账号的区间，比较账号额度变化与中继请求费用。时间统一为 UTC+8，最多 31 天。</p>'+
      '<form id="quota-analysis-form"><div class="filters"><label>开始时间（UTC+8）<input id="analysis-start" type="datetime-local" required value="'+esc(settings.start)+'"></label>'+
      '<label>结束时间（UTC+8）<input id="analysis-end" type="datetime-local" required value="'+esc(settings.end)+'"></label><button id="analysis-submit" class="btn btn-primary" type="submit">分析区间</button></div>'+
      '<div class="actions analysis-presets"><button class="btn btn-ghost" type="button" data-analysis-range="1h">最近 1 小时</button><button class="btn btn-ghost" type="button" data-analysis-range="3h">最近 3 小时</button><button class="btn btn-ghost" type="button" data-analysis-range="today">今天</button><button class="btn btn-ghost" type="button" data-analysis-range="yesterday">昨天</button><button class="btn btn-ghost" type="button" data-analysis-range="7d">近 7 天</button></div>'+
      '<label class="analysis-confirm"><input id="analysis-confirm" type="checkbox"'+(settings.confirmed?' checked':'')+'> 我确认该区间没有在中继外使用此账号</label></form>'+
      '<div id="quota-analysis-result" aria-live="polite">'+(settings.dirty?'<p class="muted">条件已修改，请点击“分析区间”。</p>':resultMarkup(settings.result))+'</div>';
    function changed() {
      settings.start=$("#analysis-start").value;settings.end=$("#analysis-end").value;
      settings.confirmed=$("#analysis-confirm").checked;settings.dirty=true;settings.result=null;settings.serial++;
      $("#quota-analysis-result").innerHTML='<p class="muted">条件已修改，请点击“分析区间”。</p>';
    }
    ["analysis-start","analysis-end","analysis-confirm"].forEach((id)=>$('#'+id).addEventListener('input',changed));
    target.querySelectorAll('[data-analysis-range]').forEach((button)=>button.addEventListener('click',()=>{
      const now=new Date(),range=button.dataset.analysisRange,day=local(now).slice(0,10)+'T00:00';
      const midnight=new Date(day+'+08:00');
      let start=new Date(now.getTime()-(range==='1h'?1:range==='7d'?168:3)*3600000),end=now;
      if (range==='today') start=midnight;
      if (range==='yesterday') {start=new Date(midnight.getTime()-86400000);end=midnight;}
      $("#analysis-start").value=local(start);$("#analysis-end").value=local(end);changed();
    }));
    $("#quota-analysis-form").addEventListener('submit',async(event)=>{
      event.preventDefault();if(settings.busy)return;changed();
      const first=new Date(settings.start+'+08:00'),last=new Date(settings.end+'+08:00');
      if(!Number.isFinite(first.getTime()) || !Number.isFinite(last.getTime()) || first>=last || last-first>31*86400000) {
        $("#quota-analysis-result").innerHTML='<p class="warn">请选择有效的起止时间，区间最多 31 天。</p>';return;
      }
      settings.busy=true;const serial=++settings.serial;$("#analysis-submit").disabled=true;
      $("#quota-analysis-result").innerHTML='<p class="muted">正在读取区间快照与请求用量…</p>';
      const query=new URLSearchParams({start:first.toISOString(),end:last.toISOString(),external_usage_excluded:String(settings.confirmed)});
      const key=window.CodexRelayKeys?.selected();if(key)query.set('client_key_id',key);
      try {
        const data=await api('/admin/api/quota-analysis?'+query.toString());
        if(serial===settings.serial && $("#quota-analysis-result")) {settings.result=data;settings.dirty=false;$("#quota-analysis-result").innerHTML=resultMarkup(data);}
      } catch(error) {
        if(serial===settings.serial && $("#quota-analysis-result")) $("#quota-analysis-result").innerHTML='<p class="warn">'+esc(error.message)+'</p>';
      } finally {settings.busy=false;if($("#analysis-submit"))$("#analysis-submit").disabled=false;}
    });
  }
  window.CodexRelayQuotaAnalysis={mount,invalidate:()=>{settings.result=null;settings.dirty=true;settings.serial++;if($('#quota-analysis-result'))$('#quota-analysis-result').innerHTML='<p class="muted">Key 已修改，请重新分析区间。</p>';},isEditing:()=>settings.busy || Boolean(document.activeElement?.closest?.('#quota-analysis-form'))};
})();
