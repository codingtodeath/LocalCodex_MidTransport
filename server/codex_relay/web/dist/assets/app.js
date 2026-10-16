/* Codex 私有中继 · 管理网站
 * 同源、本地托管、无公网 CDN 依赖。
 * 私有数据一律经 /admin/api/* 取得；本文件不含任何秘密。
 */

(function () {
  "use strict";

  const state = {
    csrf: null,
    username: null,
    timer: null,
    view: null,
    polling: false,
  };

  const $ = (sel) => document.querySelector(sel);

  // --- HTTP ------------------------------------------------------------

  async function api(path, options = {}) {
    const opts = Object.assign({ credentials: "same-origin", headers: {} }, options);
    opts.headers = Object.assign({ Accept: "application/json" }, opts.headers || {});
    if (opts.body !== undefined && typeof opts.body !== "string") {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    if (opts.method && opts.method !== "GET" && state.csrf) {
      opts.headers["x-relay-csrf"] = state.csrf;
    }
    const controller = new AbortController();
    opts.signal = controller.signal;
    const deadline = window.setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(path, opts);
      let payload = null;
      const text = await response.text();
      if (text) {
        try {
          payload = JSON.parse(text);
        } catch (err) {
          throw new Error("监测服务返回了无法读取的响应，请稍后重试。");
        }
      }
      if (!response.ok) {
        const message =
          payload && payload.error && payload.error.message
            ? payload.error.message
            : "请求失败（HTTP " + response.status + "）";
        const error = new Error(message);
        error.status = response.status;
        error.code = payload && payload.error ? payload.error.code : null;
        throw error;
      }
      return payload;
    } catch (err) {
      if (err.name === "AbortError") throw new Error("连接超时，请检查网络后重试。");
      if (err instanceof TypeError) throw new Error("无法连接监测服务，请检查网络后重试。");
      throw err;
    } finally {
      window.clearTimeout(deadline);
    }
  }

  // --- 转义与格式化 ----------------------------------------------------

  function esc(value) {
    if (value === null || value === undefined) return "";
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function num(value) {
    if (value === null || value === undefined) return "未提供";
    return Number(value).toLocaleString("zh-CN");
  }

  /** 显示浏览器本地时区，并明确标注时区。 */
  function when(value) {
    if (!value) return '<span class="muted">未提供</span>';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return esc(value);
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "本地时区";
    const text = date.toLocaleString("zh-CN", { hour12: false });
    return '<span title="' + esc(value) + '">' + esc(text) + ' <span class="tz">' + esc(tz) + "</span></span>";
  }

  function relative(seconds) {
    if (seconds === null || seconds === undefined) return "未知";
    const s = Math.round(seconds);
    if (s < 60) return s + " 秒前";
    if (s < 3600) return Math.round(s / 60) + " 分钟前";
    if (s < 86400) return Math.round(s / 3600) + " 小时前";
    return Math.round(s / 86400) + " 天前";
  }

  function percent(value) {
    if (value === null || value === undefined) return "未提供";
    return (Number(value) * 100).toFixed(1) + "%";
  }

  const STATE_LABEL = {
    online: "在线",
    degraded: "降级",
    offline: "离线",
    unknown: "未知",
  };

  function badge(state) {
    const label = STATE_LABEL[state] || state || "未知";
    return '<span class="badge badge-' + esc(state || "unknown") + '">' + esc(label) + "</span>";
  }

  // --- 布局 ------------------------------------------------------------

  function layout(active, title) {
    if (state.view === active && $("#content")) return;
    state.view = active;
    const pages = [
      ["/", "overview", "概览"],
      ["/quota", "quota", "账号额度"],
      ["/requests", "requests", "请求流水"],
      ["/keys", "keys", "Key 管理"],
      ["/settings", "settings", "设置"],
    ];
    const nav = pages
      .map(
        (p) =>
          '<a class="nav-link' +
          (p[1] === active ? " active" : "") +
          '" href="' +
          p[0] +
          '">' +
          esc(p[2]) +
          "</a>"
      )
      .join("");
    document.body.innerHTML =
      '<header class="topbar">' +
      '<div class="brand"><span class="dot"></span>Codex 私有中继<small>Ubuntu Gateway</small></div>' +
      '<nav class="nav">' + nav + "</nav>" +
      '<button id="logout" class="btn btn-ghost">退出</button>' +
      "</header>" +
      '<main class="wrap"><h1 class="page-title">' + esc(title) + '</h1><p id="connection-status" class="muted small" role="status"></p><div id="content"></div></main>' +
      '<footer class="foot">API 等价估算费用 · 非实际账单 ｜ 本页数据来自本网关，不含 Codex 订阅凭证</footer>';

    const logout = $("#logout");
    if (logout) {
      logout.addEventListener("click", async function () {
        try {
          await api("/admin/api/logout", { method: "POST" });
        } catch (err) {
          /* 退出失败也要回到登录页 */
        }
        window.location.href = "/";
      });
    }
  }

  function toast(message, kind) {
    let node = document.getElementById("toast");
    if (!node) {
      node = document.createElement("div");
      node.id = "toast";
      document.body.appendChild(node);
    }
    node.className = "toast " + (kind || "info");
    node.textContent = message;
    node.style.display = "block";
    window.clearTimeout(node._timer);
    node._timer = window.setTimeout(function () {
      node.style.display = "none";
    }, 4000);
  }

  // --- 登录 ------------------------------------------------------------

  function renderLogin(message) {
    if (state.timer) window.clearInterval(state.timer);
    state.timer = null;
    state.view = null;
    document.body.innerHTML =
      '<div class="login-wrap"><form id="login-form" class="card login-card">' +
      "<h1>Codex 私有中继</h1>" +
      '<p class="muted">账号额度详情、请求流水和管理操作需要管理员登录。</p>' +
      (message ? '<p class="error-text">' + esc(message) + "</p>" : "") +
      '<label>用户名<input name="username" autocomplete="username" required></label>' +
      '<label>密码<input name="password" type="password" autocomplete="current-password" required></label>' +
      '<button class="btn btn-primary" type="submit">登录</button>' +
      '<p class="small"><a href="/">← 返回公开用量概览</a></p>' +
      "</form></div>";

    $("#login-form").addEventListener("submit", async function (event) {
      event.preventDefault();
      const form = event.target;
      const button = form.querySelector('button[type="submit"]');
      button.disabled = true;
      button.textContent = "正在登录…";
      try {
        const result = await api("/admin/api/login", {
          method: "POST",
          body: { username: form.username.value, password: form.password.value },
        });
        state.csrf = result.csrf_token;
        state.username = result.username;
        await boot();
      } catch (err) {
        // 统一错误，不区分账号是否存在。
        renderLogin(err.message);
      }
    });
  }

  // --- 概览 ------------------------------------------------------------

  async function renderOverview() {
    layout("overview", "概览");
    const data = await api("/admin/api/overview");
    const worker = data.worker || {};
    const today = data.today || {};
    const quota = data.quota || {};
    const costText = !today.request_count ? "尚无请求" : !today.estimable_count ? "不可估价" :
      esc(today.currency || "USD") + " " + esc(today.api_equivalent_cost) +
      (today.estimable_count < today.request_count ? "（部分请求）" : "");

    const tunnelOk = worker.probe_ok === true && worker.instance_consistent && worker.probe_fresh;
    const channelState = worker.heartbeat_fresh && tunnelOk ? "online" : worker.heartbeat_fresh ? "degraded" : worker.state;
    const rows = [
      ["中继通道", badge(channelState)],
      ["最后心跳", worker.last_seen_at ? when(worker.last_seen_at) + "（" + relative(worker.heartbeat_age_seconds) + "）" : "未收到心跳"],
      ["Worker 健康检查", worker.probe_at ? (tunnelOk ? '<span class="ok">已确认</span>' : '<span class="warn">未确认</span>') + "（" + when(worker.probe_at) + "）" : '<span class="muted">未探测</span>'],
      ["实例一致性", worker.instance_consistent ? '<span class="ok">一致</span>' : '<span class="warn">不一致或未知</span>'],
      ["本地适配器", worker.adapter_status === "running" ? '<span class="ok">运行中</span>' : worker.adapter_status === "error" ? '<span class="bad">初始化异常</span>' : '<span class="muted">未运行</span>'],
      ["认证状态", worker.auth_state === "ready" ? '<span class="ok">ready</span>' : worker.auth_state === "reauth_required" ? '<span class="bad">需要重新登录</span>' : '<span class="muted">unknown</span>'],
      ["最近一次真实推理成功", data.counters && data.counters.last_inference_success_at ? when(data.counters.last_inference_success_at) : "尚无记录"],
      ["当前活动请求", num(data.counters ? data.counters.active_requests : 0) + " / " + num(data.concurrency ? data.concurrency.global_limit : null)],
    ];

    const quotaSummary = quota.has_snapshot
      ? '<div class="kv"><span>额度状态</span><b>' + esc(quota.status) + "（" + esc(quota.freshness === "fresh" ? "新鲜" : quota.freshness === "stale" ? "已过期" : "不可用") + "）</b></div>" +
        '<div class="kv"><span>采集时间</span><b>' + when(quota.captured_at) + "</b></div>"
      : '<p class="muted">尚未收到成功额度快照。</p>';

    $("#content").innerHTML =
      '<section class="card"><h2>连接状态</h2><div class="grid">' +
      rows
        .map(
          (r) =>
            '<div class="kv"><span>' + esc(r[0]) + "</span><b>" + r[1] + "</b></div>"
        )
        .join("") +
      "</div>" +
      (worker.reasons && worker.reasons.length
        ? '<p class="muted small">判定说明：' + esc(worker.reasons.join("；")) + "</p>"
        : "") +
      "</section>" +
      '<section class="card"><h2>账号额度（摘要）</h2>' +
      quotaSummary +
      (quota.notes && quota.notes.length ? '<p class="muted small">' + esc(quota.notes.join(" ")) + "</p>" : "") +
      '<p><a class="btn btn-ghost" href="/quota">查看额度详情</a></p></section>' +
      '<section class="card"><h2>今日用量（UTC 统计时区 ' + esc(today.day || "") + '）</h2><div class="grid">' +
      [
        ["请求数", num(today.request_count)],
        ["已报告 Token 请求", num(today.reported_usage_count)],
        ["未知计量数", num(today.unknown_usage_count)],
        ["输入 Token", num(today.input_tokens)],
        ["缓存输入 Token", num(today.cached_input_tokens)],
        ["输出 Token", num(today.output_tokens)],
        ["总 Token", num(today.total_tokens)],
        ["计量覆盖率", today.usage_coverage === null || today.usage_coverage === undefined ? "无请求" : percent(today.usage_coverage)],
        ["可估价覆盖率", today.estimate_coverage === null || today.estimate_coverage === undefined ? "无请求" : percent(today.estimate_coverage)],
      ]
        .map((r) => '<div class="kv"><span>' + esc(r[0]) + "</span><b>" + r[1] + "</b></div>")
        .join("") +
      "</div>" +
      '<div class="cost"><div class="cost-label">API 等价估算费用 · 非实际账单</div>' +
      '<div class="cost-value">' + costText + "</div>" +
      '<div class="muted small">实际账单：' + (today.actual_billing === "not_integrated" ? "未接入" : esc(today.actual_billing)) + "。未知计量的请求不会被当作 0 计入完整费用。</div></div>" +
      "</section>";
  }

  // --- 额度 ------------------------------------------------------------

  function windowRow(window) {
    const used = Number(window.used_percent);
    // remaining_percent = max(0, min(100, 100 - used))；绝不修改原始记录。
    const remaining = Math.max(0, Math.min(100, 100 - used));
    const abnormal = used > 100;
    const minutes = window.window_minutes;
    const windowText =
      minutes === null || minutes === undefined
        ? "窗口长度未提供"
        : minutes >= 1440
        ? "窗口 " + (minutes / 1440).toFixed(minutes % 1440 === 0 ? 0 : 1) + " 天"
        : "窗口 " + minutes + " 分钟";
    return (
      '<div class="quota-window' + (abnormal ? " abnormal" : "") + '">' +
      '<div class="quota-head"><b>' +
      esc(window.display_name || window.bucket_id + " / " + window.window_id) +
      "</b><span class=\"muted small\">" + esc(window.bucket_id) + " · " + esc(window.window_id) + "</span></div>" +
      '<div class="bar"><div class="bar-fill" style="width:' + remaining.toFixed(2) + '%"></div></div>' +
      '<div class="quota-meta"><span>剩余 ' + remaining.toFixed(1) + "%</span><span>已用 " + esc(window.used_percent) + "%</span><span>" + esc(windowText) + "</span></div>" +
      '<div class="quota-meta"><span>重置时间：' + (window.resets_at ? when(window.resets_at) : "未提供") + "</span></div>" +
      (abnormal ? '<p class="warn small">上游返回 used_percent 超过 100，标记为异常；原始值未被修改。</p>' : "") +
      "</div>"
    );
  }

  async function renderQuota() {
    if (window.CodexRelayQuotaAnalysis?.isEditing() || window.CodexRelayKeys?.isEditing()) return;
    layout("quota", "账号额度");
    const data = await api("/admin/api/quota");
    const quota = data.quota || {};
    const windows = quota.windows || [];
    const credits = quota.credits;

    $("#content").innerHTML =
      '<section class="card" id="key-usage"></section>' +
      '<section class="card" id="quota-analysis"></section>' +
      '<section class="card"><h2>快照信息</h2><div class="grid">' +
      [
        ["Worker 状态", badge(data.worker_state)],
        ["账号别名", quota.account_alias ? esc(quota.account_alias) : "未提供"],
        ["身份核验方式", esc(quota.identity_check || "未提供")],
        ["套餐类型", quota.plan_type ? esc(quota.plan_type) : "未提供"],
        ["采集时间", quota.captured_at ? when(quota.captured_at) : "未提供"],
        ["数据新鲜度", quota.freshness === "fresh" ? '<span class="ok">新鲜</span>' : quota.freshness === "stale" ? '<span class="warn">已过期</span>' : '<span class="muted">不可用</span>'],
        ["最近查询结果", quota.last_attempt_category ? esc(quota.last_attempt_category) + (quota.last_attempt_error ? "（" + esc(quota.last_attempt_error) + "）" : "") : "无记录"],
        ["最近查询时间", quota.last_attempt_at ? when(quota.last_attempt_at) : "未提供"],
      ]
        .map((r) => '<div class="kv"><span>' + esc(r[0]) + "</span><b>" + r[1] + "</b></div>")
        .join("") +
      "</div>" +
      (quota.notes && quota.notes.length
        ? '<p class="warn small">' + esc(quota.notes.join(" ")) + "</p>"
        : "") +
      '<div class="actions"><button id="refresh-quota" class="btn btn-primary"' +
      (data.refreshable ? "" : " disabled") +
      ">刷新额度</button>" +
      (data.refreshable
        ? '<span class="muted small">命令将在下一次心跳下发，受 30 秒最短间隔约束。</span>'
        : '<span class="muted small">Worker 当前离线，无法即时刷新。</span>') +
      "</div></section>" +
      '<section class="card"><h2>额度窗口</h2>' +
      (windows.length
        ? windows.map(windowRow).join("")
        : '<p class="muted">上游未提供任何窗口数据。不伪造 0，也不固定为“5 小时 / 每周”。</p>') +
      "</section>" +
      '<section class="card"><h2>Credits</h2>' +
      (credits
        ? '<div class="grid">' +
          [
            ["可用", credits.available === null ? "未提供" : credits.available ? "是" : "否"],
            ["无限", credits.unlimited === null ? "未提供" : credits.unlimited ? "是" : "否"],
            ["余额", credits.balance === null || credits.balance === undefined ? "未提供" : esc(credits.balance)],
            ["单位", credits.unit ? esc(credits.unit) : "未提供"],
          ]
            .map((r) => '<div class="kv"><span>' + esc(r[0]) + "</span><b>" + r[1] + "</b></div>")
            .join("") +
          "</div>" +
          '<p class="muted small">credits 不是美元；本页不把其换算成“剩余美元”。</p>'
        : '<p class="muted">上游未提供 credits 信息（“未提供”不等于 0）。</p>') +
      "</section>" +
      '<section class="card"><h2>账号级汇总（独立口径）</h2>' +
      (data.account_usage
        ? '<div class="grid">' +
          Object.entries(data.account_usage.summary || {})
            .map(([k, v]) => '<div class="kv"><span>' + esc(k) + "</span><b>" + num(v) + "</b></div>")
            .join("") +
          "</div>"
        : '<p class="muted">未提供官方账号级汇总。</p>') +
      '<p class="muted small">' + esc(data.account_usage_note || "") + "</p></section>";

    await window.CodexRelayKeys?.mountUsage(api, toast);
    window.CodexRelayQuotaAnalysis?.mount(api);
    const button = $("#refresh-quota");
    if (button && data.refreshable) {
      button.addEventListener("click", async function () {
        button.disabled = true;
        try {
          const result = await api("/admin/api/quota/refresh", { method: "POST" });
          toast(result.created ? "已创建刷新命令。" : "未创建新命令：" + (result.reason || result.status), result.created ? "ok" : "info");
        } catch (err) {
          toast(err.message, "error");
        }
        window.setTimeout(function () {
          button.disabled = false;
        }, 3000);
      });
    }
  }

  // --- 请求流水 --------------------------------------------------------

  async function renderRequests() {
    layout("requests", "请求流水");
    $("#content").innerHTML =
      '<section class="card"><form id="filters" class="filters">' +
      '<label>Key<select name="client_key_id" id="request-key-filter"></select></label>' +
      '<label>日期（UTC+8）<input type="date" name="day"></label>' +
      '<label>模型<input name="model" placeholder="全部"></label>' +
      '<label>结果<select name="status"><option value="">全部</option>' +
      '<option value="completed">completed</option><option value="failed">failed</option>' +
      '<option value="incomplete">incomplete</option><option value="interrupted">interrupted</option>' +
      "</select></label>" +
      '<button class="btn" type="submit">筛选</button>' +
      '<a id="requests-export" class="btn btn-ghost" href="/admin/api/requests.csv">导出 CSV</a>' +
      "</form>" +
      '<p class="muted small">只展示元数据：不含 prompt、源码、模型正文或工具参数。</p>' +
      '<div id="table"></div>' +
      '<div class="actions"><button id="prev" class="btn btn-ghost">上一页</button>' +
      '<button id="next" class="btn btn-ghost">下一页</button>' +
      '<span id="page-info" class="muted small"></span></div></section><section class="card" id="connection-events"></section>';

    const pageState = { offset: 0, limit: 50, total: 0, filters: { client_key_id: window.CodexRelayKeys?.selected() || "" } };

    async function load() {
      const params = new URLSearchParams({ limit: String(pageState.limit), offset: String(pageState.offset) });
      Object.entries(pageState.filters).forEach(function ([k, v]) {
        if (v) params.set(k, v);
      });
      const data = await api("/admin/api/requests?" + params.toString());
      const exportParams=new URLSearchParams(pageState.filters);
      $("#requests-export").href="/admin/api/requests.csv?"+exportParams.toString();
      await window.CodexRelayKeys?.events(api, $("#connection-events"), pageState.filters.client_key_id);
      pageState.total = data.total;
      const rows = data.items || [];
      $("#table").innerHTML = rows.length
        ? '<div class="table-scroll"><table><thead><tr>' +
          ["时间", "客户端", "请求模型", "返回模型", "流式", "HTTP", "结果", "错误", "输入", "缓存", "输出", "总 Token", "计量", "估价", "费用"]
            .map((h) => "<th>" + esc(h) + "</th>")
            .join("") +
          "</tr></thead><tbody>" +
          rows
            .map(function (r) {
              return (
                "<tr>" +
                "<td>" + when(r.started_at) + "</td>" +
                "<td>" + esc(r.client_alias || "") + "</td>" +
                "<td>" + esc(r.requested_model || "") + "</td>" +
                "<td>" + esc(r.response_model || "") + "</td>" +
                "<td>" + (r.stream ? "是" : "否") + "</td>" +
                "<td>" + esc(r.http_status === null ? "" : r.http_status) + "</td>" +
                "<td>" + esc(r.response_status || "") + "</td>" +
                "<td>" + esc(r.error_code || "") + "</td>" +
                "<td>" + num(r.input_tokens) + "</td>" +
                "<td>" + num(r.cached_input_tokens) + "</td>" +
                "<td>" + num(r.output_tokens) + "</td>" +
                "<td>" + num(r.total_tokens) + "</td>" +
                "<td>" + esc(r.usage_status) + "</td>" +
                "<td>" + esc(r.estimate_status) + "</td>" +
                "<td>" + (r.estimated_cost ? esc(r.currency + " " + r.estimated_cost) : '<span class="muted">不可估价</span>') + "</td>" +
                "</tr>"
              );
            })
            .join("") +
          "</tbody></table></div>"
        : '<p class="muted">没有符合条件的记录。</p>';
      $("#page-info").textContent =
        "共 " + pageState.total + " 条，显示第 " + (pageState.total ? pageState.offset + 1 : 0) + "–" +
        Math.min(pageState.offset + pageState.limit, pageState.total) + " 条";
    }

    $("#filters").addEventListener("submit", function (event) {
      event.preventDefault();
      const form = event.target;
      pageState.filters = { day: form.day.value, model: form.model.value, status: form.status.value, client_key_id: form.client_key_id.value };
      window.CodexRelayKeys?.choose(form.client_key_id.value);
      pageState.offset = 0;
      load().catch(function (err) {
        toast(err.message, "error");
      });
    });
    $("#prev").addEventListener("click", function () {
      pageState.offset = Math.max(0, pageState.offset - pageState.limit);
      load().catch(function (err) { toast(err.message, "error"); });
    });
    $("#next").addEventListener("click", function () {
      if (pageState.offset + pageState.limit < pageState.total) {
        pageState.offset += pageState.limit;
        load().catch(function (err) { toast(err.message, "error"); });
      }
    });

    await window.CodexRelayKeys?.options(api, $("#request-key-filter"));
    pageState.filters.client_key_id=window.CodexRelayKeys?.selected() || "";
    await load();
  }

  // --- 设置 ------------------------------------------------------------

  async function renderSettings() {
    layout("settings", "设置");
    $("#content").innerHTML =
      '<section class="card"><h2>客户端 API Key</h2><a class="btn btn-primary" href="/keys">管理名称、美元余额和 Key</a></section>' +
      '<section class="card"><h2>价格版本</h2><div id="prices"></div>' +
      '<form id="price-form" class="filters">' +
      '<label>版本号<input name="price_version" placeholder="2026-11-01-v1" required></label>' +
      '<label>模型<input name="model" placeholder="模型 ID" required></label>' +
      '<label>输入 $/1M<input name="p_input" placeholder="2.00" required></label>' +
      '<label>缓存读 $/1M<input name="p_cached" placeholder="0.20"></label>' +
      '<label>输出 $/1M<input name="p_output" placeholder="8.00" required></label>' +
      '<label>生效时间<input type="datetime-local" name="effective_from" required></label>' +
      '<label>来源链接<input name="source_url" placeholder="可选"></label>' +
      '<button class="btn btn-primary" type="submit">新增规则</button></form>' +
      '<p class="muted small">规则不可变：新版本只影响生效后的请求，历史记录保留当时的 price_version。未配置价格的模型显示“不可估价”，不默认免费。</p></section>' +
      '<section class="card"><h2>运行参数（只读）</h2><div id="runtime"></div></section>';

    async function loadPrices() {
      const data = await api("/admin/api/prices");
      $("#prices").innerHTML =
        data.count
          ? '<p class="muted small">单价单位：美元 / 百万 Token。标准 API 等价参考价格；必要的缓存写入明细缺失时完整请求仍不可估价，历史流水不补算。</p><div class="table-scroll"><table><thead><tr><th>版本</th><th>模型</th><th>上下文</th><th>输入</th><th>缓存读</th><th>缓存写</th><th>输出</th><th>币种</th><th>生效时间</th><th>来源 / 核对时间</th><th>排除项</th></tr></thead><tbody>' +
            data.items
              .map(function (r) {
                return (
                  "<tr><td class=\"mono\">" + esc(r.price_version) + "</td><td>" + esc(r.model) + "</td>" +
                  "<td>" + esc(r.input_context_min) + "–" + (r.input_context_max === null ? "∞" : esc(r.input_context_max)) + "</td>" +
                  "<td>" + esc(r.price_input) + "</td><td>" + esc(r.price_cached_input === null ? "—" : r.price_cached_input) + "</td>" +
                  "<td>" + esc(r.price_cache_write === null ? "—" : r.price_cache_write) + "</td>" +
                  "<td>" + esc(r.price_output === null ? "—" : r.price_output) + "</td><td>" + esc(r.currency) + "</td>" +
                  "<td>" + when(r.effective_from) + "</td><td class=\"small\">" + (r.source_url && /^https:\/\//.test(r.source_url) ? '<a href="' + esc(r.source_url) + '" target="_blank" rel="noopener noreferrer">价格来源</a><br>' : "") + when(r.verified_at) + "</td><td class=\"small\">" + esc((r.excluded_costs || []).join(", ")) + "</td></tr>"
                );
              })
              .join("") +
            "</tbody></table></div>"
          : '<p class="muted">尚未配置价格规则；因此所有请求都显示“不可估价”。</p>';
    }

    await loadPrices();

    $("#price-form").addEventListener("submit", async function (event) {
      event.preventDefault();
      const form = event.target;
      const prices = { input: form.p_input.value, output: form.p_output.value };
      if (form.p_cached.value) prices.cached_input = form.p_cached.value;
      const effective = form.effective_from.value;
      try {
        await api("/admin/api/prices", {
          method: "POST",
          body: {
            price_version: form.price_version.value,
            model: form.model.value,
            effective_from: effective ? new Date(effective).toISOString() : "",
            currency: "USD",
            formula: "input_cached_output_v1",
            cache_write_policy: "not_applicable",
            prices_per_million: prices,
            excluded_costs: ["hosted_tools", "images", "audio", "subscription_fee", "tax", "fx"],
            source_url: form.source_url.value || null,
          },
        });
        toast("已新增价格规则。", "ok");
        form.reset();
        await loadPrices();
      } catch (err) {
        toast(err.message, "error");
      }
    });

    const overview = await api("/admin/api/overview");
    $("#runtime").innerHTML =
      '<div class="grid">' +
      [
        ["全局并发上限", num(overview.concurrency.global_limit)],
        ["单客户端并发上限", num(overview.concurrency.per_client_limit)],
        ["统计时区", esc(overview.today.timezone)],
        ["服务器时间", when(overview.server_time)],
      ]
        .map((r) => '<div class="kv"><span>' + esc(r[0]) + "</span><b>" + r[1] + "</b></div>")
        .join("") +
      "</div>" +
      '<p class="muted small">并发由 Gateway 和 Worker 共同限制。</p>';
  }

  // --- 引导 ------------------------------------------------------------

  const ROUTES = {
    "/": renderOverview,
    "/quota": renderQuota,
    "/requests": renderRequests,
    "/keys": async function () { layout("keys", "Key 管理"); await window.CodexRelayKeys.management(api, toast); },
    "/settings": renderSettings,
  };

  async function boot() {
    if (state.timer) window.clearInterval(state.timer);
    state.timer = null;
    try {
      const session = await api("/admin/api/session");
      state.csrf = session.csrf_token;
    } catch (err) {
      if (err.status === 401) {
        renderLogin(null);
      } else {
        state.view = null;
        document.body.innerHTML = '<main class="login-wrap"><section class="card login-card"><h1>监测服务暂时无法连接</h1><p class="error-text" role="alert">' + esc(err.message) + '</p><button id="retry-boot" class="btn btn-primary">重新连接</button></section></main>';
        $("#retry-boot").addEventListener("click", boot);
      }
      return;
    }
    const path = window.location.pathname.replace(/\/+$/, "") || "/";
    const render = ROUTES[path] || renderOverview;
    try {
      await render();
      connectionStatus(false);
    } catch (err) {
      if (err.status === 401) {
        renderLogin(null);
        return;
      }
      const content = $("#content");
      if (content) content.innerHTML = '<p class="error-text">' + esc(err.message) + "</p>";
      connectionStatus(true);
    }
    // 默认 5 秒轮询即可；不为仪表盘引入 WebSocket。
    if (path === "/" || path === "/quota") {
      state.timer = window.setInterval(async function () {
        if (state.polling || document.visibilityState === "hidden") return;
        state.polling = true;
        try {
          await render();
          connectionStatus(false);
        } catch (err) {
          if (err.status === 401) renderLogin("登录已过期，请重新登录管理员账号。");
          else connectionStatus(true);
        } finally {
          state.polling = false;
        }
      }, 5000);
    }
  }

  function connectionStatus(failed) {
    const node = $("#connection-status");
    if (!node) return;
    node.className = failed ? "warn small" : "muted small";
    const auto = window.location.pathname === "/" || window.location.pathname === "/quota";
    node.textContent = failed ? "连接暂时中断，当前内容可能未更新；请重试。" : "最近更新：" + new Date().toLocaleTimeString("zh-CN", { hour12: false }) + (auto ? " · 每 5 秒更新" : " · 可手动刷新");
  }

  window.addEventListener("DOMContentLoaded", boot);
})();
