/* Private named-key management. Never store plaintext keys in browser storage. */
(function () {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const num = (v) => v == null ? "—" : Number(v).toLocaleString("zh-CN", {maximumFractionDigits:6});
  const local = (date) => new Date(date.getTime()+8*3600000).toISOString().slice(0,16);
  let selected = "";
  try { selected = sessionStorage.getItem("relay-selected-key") || ""; } catch (_) { /* optional */ }
  const range = {start:local(new Date(Date.now()-86400000)),end:local(new Date())};
  function choose(value) {
    selected=value || "";
    try { sessionStorage.setItem("relay-selected-key",selected); } catch (_) { /* optional */ }
    window.CodexRelayQuotaAnalysis?.invalidate();
  }
  async function options(api, node) {
    const data=await api("/admin/api/client-keys");
    if (!node.isConnected) return;
    if (!data.items.some((k)=>k.key_id===selected)) choose("");
    node.innerHTML='<option value="">全部 Key</option>'+data.items.map((k)=>'<option value="'+esc(k.key_id)+'"'+(selected===k.key_id?' selected':'')+'>'+esc(k.alias)+(k.revoked_at?'（已撤销）':'')+'</option>').join('');
  }
  const walletText=(k)=>k.revoked_at?'已撤销':k.unlimited?'无限':k.balance_blocked?'待核对计量':Number(k.balance_usd)<=0?'余额耗尽':'可用';
  function balanceText(k) {
    return k.unlimited ? '无限' : '$'+num(k.available_usd)+(Number(k.overdraft_usd)>0?' · 超支 $'+num(k.overdraft_usd):'');
  }
  async function management(api, toast) {
    $('#content').innerHTML='<section class="card"><h2>创建 API Key</h2><p class="muted small">按现有模型 Token 价格扣减 USD 余额。名称用于流水和额度筛选；Key 只在创建时显示一次。</p>'+
      '<form id="create-key" class="filters"><label>名称<input name="alias" maxlength="64" required placeholder="例如 laptop"></label><label>初始余额（USD）<input name="balance" type="number" min="0" max="1000000000" step="0.000001" value="0" required></label>'+
      '<label><input name="unlimited" type="checkbox"> 无限余额</label><button class="btn btn-primary" type="submit">创建 Key</button></form><div id="key-secret" aria-live="polite"></div></section>'+
      '<section class="card"><h2>Key 管理</h2><div class="actions"><button id="reload-keys" class="btn btn-ghost">刷新</button><a class="btn btn-ghost" href="/quota">查看额度和费用</a><a class="btn btn-ghost" href="/requests">查看请求与连接日志</a></div><div id="key-list"></div>'+
      '<p class="muted small">“设置余额”设置当前余额的绝对值，不是追加充值。费用在收到用量后结算；并发请求可能超支，超支金额会保留。余额耗尽后拒绝新请求，并关闭尚未结束的连接。计量缺失或冲突时，有限余额 Key 暂停使用；核对日志后重新设置余额可解除。</p></section>';
    const form=$('#create-key');
    form.unlimited.addEventListener('change',()=>{form.balance.disabled=form.unlimited.checked;});
    async function load() {
      const data=await api('/admin/api/client-keys');
      $('#key-list').innerHTML='<div class="table-scroll"><table><thead><tr><th>名称</th><th>余额 · USD</th><th>状态</th><th>操作</th></tr></thead><tbody>'+data.items.map((k)=>
        '<tr><td><b>'+esc(k.alias)+'</b><br><small class="mono muted">'+esc(k.key_id)+'</small></td><td>'+esc(balanceText(k))+'</td><td>'+esc(walletText(k))+'</td><td>'+(k.revoked_at?'':
        '<button class="btn btn-ghost" data-edit-key="'+esc(k.key_id)+'">编辑名称 / 余额</button> <button class="btn btn-danger" data-revoke-key="'+esc(k.key_id)+'">撤销</button>')+'</td></tr>').join('')+'</tbody></table></div><div id="key-editor"></div>';
      document.querySelectorAll('[data-edit-key]').forEach((button)=>button.addEventListener('click',()=>{
        const key=data.items.find((k)=>k.key_id===button.dataset.editKey);
        $('#key-editor').innerHTML='<h3>编辑 '+esc(key.alias)+'</h3><form id="edit-key" class="filters"><label>名称<input name="alias" maxlength="64" required value="'+esc(key.alias)+'"></label>'+
          '<label>当前余额（USD）<input name="balance" type="number" min="0" max="1000000000" step="0.000001" value="'+esc(key.unlimited?'0':key.available_usd)+'" required'+(key.unlimited?' disabled':'')+'></label>'+
          '<label><input name="unlimited" type="checkbox"'+(key.unlimited?' checked':'')+'> 无限余额</label><label><input name="change_balance" type="checkbox"> 重新设置余额（解除计量暂停）</label>'+
          '<button class="btn btn-primary" type="submit">保存</button><button id="cancel-key-edit" class="btn btn-ghost" type="button">取消</button></form>';
        const edit=$('#edit-key');
        edit.unlimited.addEventListener('change',()=>{edit.balance.disabled=edit.unlimited.checked;edit.change_balance.checked=true;});
        edit.balance.addEventListener('input',()=>{edit.change_balance.checked=true;});
        $('#cancel-key-edit').addEventListener('click',()=>{$('#key-editor').innerHTML='';});
        edit.addEventListener('submit',async(event)=>{
          event.preventDefault();const save=edit.querySelector('[type=submit]');save.disabled=true;
          const body={alias:edit.alias.value};
          if(edit.change_balance.checked) body.balance_usd=edit.unlimited.checked?null:edit.balance.value;
          try {await api('/admin/api/client-keys/'+encodeURIComponent(key.key_id),{method:'PATCH',body});toast('已保存。','ok');await load();}
          catch(error){save.disabled=false;toast(error.message,'error');}
        });
      }));
      document.querySelectorAll('[data-revoke-key]').forEach((button)=>button.addEventListener('click',async()=>{
        if(!window.confirm('撤销将拒绝此 Key 的新请求，并关闭正在传输的连接。确定继续？'))return;
        button.disabled=true;
        try{await api('/admin/api/client-keys/'+encodeURIComponent(button.dataset.revokeKey),{method:'DELETE'});await load();toast('已撤销。','ok');}
        catch(error){button.disabled=false;toast(error.message,'error');}
      }));
    }
    form.addEventListener('submit',async(event)=>{
      event.preventDefault();const button=form.querySelector('[type=submit]');button.disabled=true;
      try {
        const data=await api('/admin/api/client-keys',{method:'POST',body:{alias:form.alias.value,balance_usd:form.unlimited.checked?null:form.balance.value}});
        $('#key-secret').innerHTML='<div class="notice"><b>请立即保存此 Key：</b><code class="mono" id="created-key"></code><div class="actions"><button class="btn" id="copy-key">复制</button><button class="btn btn-ghost" id="clear-key">已保存，隐藏</button></div><p class="small">离开页面后无法再次查看。</p></div>';
        $('#created-key').textContent=data.plaintext_key;
        $('#copy-key').addEventListener('click',async()=>{try{await navigator.clipboard.writeText($('#created-key').textContent);toast('已复制。','ok');}catch(_){toast('请手动选择并复制 Key。','info');}});
        $('#clear-key').addEventListener('click',()=>{$('#key-secret').replaceChildren();});
        form.reset();form.balance.disabled=false;await load();
      }catch(error){toast(error.message,'error');}finally{button.disabled=false;}
    });
    $('#reload-keys').addEventListener('click',()=>load().catch((e)=>toast(e.message,'error')));
    await load();
  }
  async function mountUsage(api, toast) {
    const target=$('#key-usage');if(!target)return;
    target.innerHTML='<h2>Key 余额与 Token 费用</h2><form id="key-usage-form" class="filters"><label>Key<select id="quota-key-filter"></select></label><label>开始（UTC+8）<input name="start" type="datetime-local" required value="'+esc(range.start)+'"></label>'+
      '<label>结束（UTC+8）<input name="end" type="datetime-local" required value="'+esc(range.end)+'"></label><button class="btn" type="submit">查询费用</button><button class="btn btn-ghost" id="key-usage-today" type="button">今天</button><button class="btn btn-ghost" id="key-usage-week" type="button">近 7 天</button></form><div id="key-usage-result"></div><p class="muted small">下方账号额度百分比和重置时间属于整个账号。选择单个 Key 时仅筛选其 Token 费用，不把账号百分比记到该 Key。</p>';
    const form=$('#key-usage-form');let serial=0;
    await options(api,$('#quota-key-filter'));
    async function load() {
      const number=++serial;
      range.start=form.start.value;range.end=form.end.value;
      const first=new Date(range.start+'+08:00'),last=new Date(range.end+'+08:00');
      if(!Number.isFinite(first.getTime()) || !Number.isFinite(last.getTime()) || first>=last)throw Error('请选择有效起止时间。');
      const q=new URLSearchParams({client_key_id:selected,start:first.toISOString(),end:last.toISOString()});
      const data=await api('/admin/api/key-usage?'+q.toString());if(number!==serial || !target.isConnected)return;
      $('#key-usage-result').innerHTML='<div class="grid"><div class="kv"><span>当前余额 · '+esc(data.wallet?.alias || '全部 Key')+'</span><b>'+esc(data.wallet?balanceText(data.wallet):'各 Key 单独管理')+'</b></div>'+
        '<div class="kv"><span>所选区间费用 · USD</span><b>$'+esc(data.cost_usd)+'</b></div><div class="kv"><span>请求数 / 可估价</span><b>'+esc(num(data.requests))+' / '+esc(num(data.priced_requests))+'</b></div><div class="kv"><span>总 Token</span><b>'+esc(num(data.total_tokens))+'</b></div></div>'+
        (data.wallet?.balance_blocked?'<p class="warn">此 Key 存在未完成计量，暂停使用；请在 Key 管理页核对后设置余额。</p>':'')+
        (data.unpriced_requests?'<p class="warn small">'+esc(num(data.unpriced_requests))+' 个请求费用未确定，所示费用不含这些请求。</p>':'');
    }
    const safely=()=>load().catch((error)=>toast(error.message,'error'));
    $('#quota-key-filter').addEventListener('change',(event)=>{choose(event.target.value);safely();});
    form.addEventListener('submit',(event)=>{event.preventDefault();safely();});
    ['today','week'].forEach((name)=>$('#key-usage-'+name).addEventListener('click',()=>{
      const now=new Date();form.end.value=local(now);form.start.value=name==='today'?local(now).slice(0,10)+'T00:00':local(new Date(now.getTime()-7*86400000));safely();
    }));
    await load();
  }
  async function events(api, target, key) {
    const data=await api('/admin/api/connection-events?'+new URLSearchParams({client_key_id:key || '',limit:'100'}));
    if(!target.isConnected)return;
    target.innerHTML='<h2>近期连接日志 · '+esc(key?'所选 Key':'全部 Key')+'</h2><p class="muted small">'+esc(data.note)+'</p>'+
      (data.items.length?'<div class="table-scroll"><table><thead><tr><th>时间</th><th>Key</th><th>请求 ID</th><th>事件</th><th>HTTP</th><th>错误 / 结果</th><th>耗时 ms</th><th>断开信号</th></tr></thead><tbody>'+data.items.map((r)=>'<tr><td>'+esc(new Date(r.at).toLocaleString('zh-CN',{timeZone:'Asia/Hong_Kong',hour12:false}))+'</td><td>'+esc(r.client_alias || '鉴权前 / 未关联')+'</td><td class="mono">'+esc(r.request_id)+'</td><td>'+esc(r.event)+'</td><td>'+esc(r.http_status)+'</td><td>'+esc(r.error_code || r.outcome || '—')+'</td><td>'+esc(num(r.duration_ms))+'</td><td>'+esc(r.disconnect_observed?'已观测':r.response_complete?'响应完整':'未确定')+'</td></tr>').join('')+'</tbody></table></div>':'<p class="muted">暂无匹配的连接记录。</p>');
  }
  window.CodexRelayKeys={management,mountUsage,options,choose,events,selected:()=>selected,
    isEditing:()=>Boolean(document.activeElement?.closest?.('#key-usage-form'))};
})();
