// cx dashboard —— 纯前端，无构建步骤。fetch /api/* 拿数据，Chart.js 画图。
(() => {
  "use strict";

  const HIGH_SHARE = 0.20; // 跟 cx/render.py 的 _HIGH_SHARE 保持一致
  const MED_SHARE = 0.08;

  const $ = (sel) => document.querySelector(sel);
  const fmt = (n) => Number(n || 0).toLocaleString("en-US");
  // < $0.01 时按 4 位小数显示，避免小额调用全部显示成 $0.00。
  const fmtUsd = (n) => `$${Number(n || 0).toFixed(Number(n) < 0.01 ? 4 : 2)}`;

  function esc(s) {
    const div = document.createElement("div");
    div.textContent = s == null ? "" : String(s);
    return div.innerHTML;
  }

  // 相对时间：给 Sessions tab 的卡片标题用。时间戳是 ISO 8601 UTC。
  function relTime(ts) {
    if (!ts) return "";
    const then = new Date(ts).getTime();
    if (Number.isNaN(then)) return "";
    const diffSec = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (diffSec < 5) return "刚刚";
    if (diffSec < 60) return `${diffSec}秒前`;
    if (diffSec < 3600) return `${Math.floor(diffSec / 60)}分钟前`;
    if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}小时前`;
    return `${Math.floor(diffSec / 86400)}天前`;
  }

  function switchToTab(name) {
    const btn = document.querySelector(`.tab-btn[data-tab="${name}"]`);
    if (!btn) return;
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    btn.classList.add("active");
    $(`#tab-${name}`).classList.add("active");
    $("#page-title").textContent = btn.querySelector(".nav-label").textContent;
  }

  function setupTabs() {
    document.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => switchToTab(btn.dataset.tab));
    });
  }

  async function getJSON(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${url} -> HTTP ${res.status}`);
    return res.json();
  }

  // --- Sessions tab: 活跃会话列表 ------------------------------------------
  // 卡片式列表，每 4 秒整体重拉重渲染。点击卡片不再原地展开时间线（那份状态
  // 每次轮询都要重建一次，跟 Debug tab 是两套重复的日志视图）——改成跳转到
  // Debug tab 并把它筛到这个 session，日志展示统一到一个地方。
  const SESSIONS_POLL_MS = 4000;
  let sessionsPollTimer = null;

  function buildSparklineSVG(series) {
    if (!series || series.length < 2) return "";
    const values = series.map((p) => p.total);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    const points = series.map((p, i) => {
      const x = (i / (series.length - 1)) * 100;
      const y = 28 - ((p.total - min) / span) * 26;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
    return `<svg class="sparkline" viewBox="0 0 100 30" preserveAspectRatio="none">
      <polyline points="${points}" fill="none" stroke="currentColor" stroke-width="2" />
    </svg>`;
  }

  function buildAgentBadges(agents) {
    if (!agents || !agents.length) return "";
    const items = agents.map((a) =>
      `<span class="agent-badge">${esc(a.agent)} × ${fmt(a.messages)}</span>`).join("");
    return `<div class="agent-badges">${items}</div>`;
  }

  // 只有用了不止一个模型的会话才值得展示分模型明细，单模型会话跟总量重复没有信息量。
  function buildModelBadges(models) {
    if (!models || models.length < 2) return "";
    const items = models.map((m) =>
      `<span class="model-badge">${esc(m.model)} × ${fmt(m.total_tokens)}</span>`).join("");
    return `<div class="model-badges">${items}</div>`;
  }

  function buildCostStat(s) {
    if (!s.models || !s.models.length) return "";
    // cost_has_unpriced_model: 会话里有档位没有公开定价（如 fable），金额是下限，
    // 不是全量——前缀 "~" 提示这一点，跟 /usage 里未知模型直接跳过不一样。
    const prefix = s.cost_has_unpriced_model ? "~" : "";
    return `<span class="stat">花费 <b title="${s.cost_has_unpriced_model ? "部分模型无公开定价，此为下限估算" : "按公开定价估算"}">${prefix}${fmtUsd(s.estimated_cost_usd)}</b></span>`;
  }

  function buildCacheStat(models) {
    const totals = (models || []).reduce((a, m) => ({
      input: a.input + (m.input_tokens || 0),
      read: a.read + (m.cache_read_tokens || 0),
    }), { input: 0, read: 0 });
    const denom = totals.input + totals.read;
    if (!denom) return "";
    const pct = Math.round((totals.read / denom) * 100);
    return `<span class="stat">cache 命中 <b>${pct}%</b></span>`;
  }

  // worktree 目录一旦被复用（同一目录后来 checkout 了别的分支/任务），目录名
  // 就跟当前会话实际在做的事对不上了。标题优先级：人写的 custom_title
  // （sidebar 任务列表那个名字，来自 jsonl 里的 custom-title 记录）> 分支名
  // > 目录名兜底。Sessions 卡片和 Debug tab 的 session 下拉框共用同一套标题
  // 规则，不然同一个 session 在两处显示成不一样的名字。
  function sessionDisplayTitle(s) {
    const cwdName = (s.cwd || "").split("/").filter(Boolean).pop() || s.cwd || "(unknown)";
    return s.custom_title || s.branch || cwdName;
  }

  function sessionMainHTML(s) {
    const title = sessionDisplayTitle(s);
    const branch = s.branch ? `<span class="scope-tag scope-project">${esc(s.branch)}</span>` : "";
    const toolChip = s.last_tool ? `<span class="chip">tool: ${esc(s.last_tool)}</span>` : "";
    const stopChip = s.last_stop_reason
      ? `<span class="chip">stop: ${esc(s.last_stop_reason)}</span>` : "";
    const agentChip = s.last_agent ? `<span class="chip">agent: ${esc(s.last_agent)}</span>` : "";
    const effortChip = s.last_effort ? `<span class="chip">effort: ${esc(s.last_effort)}</span>` : "";
    const errorChip = s.error_count
      ? `<span class="chip chip-error">${fmt(s.error_count)} 次出错</span>` : "";
    const versionHint = s.cli_version ? `<span class="hint">CLI v${esc(s.cli_version)}</span>` : "";
    const cacheStat = buildCacheStat(s.models);
    return `
      <div class="session-head">
        <span class="status-dot${s.active ? " pulse" : " idle"}"></span>
        <span class="session-title" title="${esc(s.cwd || "")}">${esc(title)}</span>
        ${branch}
        <span class="session-id">${esc(s.session_id.slice(-8))}</span>
        <span class="session-time hint">${esc(relTime(s.last))}</span>
      </div>
      <div class="session-body">
        <div class="session-stats">
          <span class="stat">messages <b>${fmt(s.messages)}</b></span>
          <span class="stat">tokens <b>${fmt(s.total_tokens)}</b></span>
          ${buildCostStat(s)}
          ${cacheStat}
          ${versionHint}
        </div>
        ${buildSparklineSVG(s.token_series)}
      </div>
      <div class="session-chips">${errorChip}${toolChip}${stopChip}${agentChip}${effortChip}</div>
      ${buildModelBadges(s.models)}
      ${buildAgentBadges(s.agents)}`;
  }

  function bindCardHead(card, sessionId) {
    card.querySelector(".session-head").addEventListener("click", () => openSessionInDebug(sessionId));
  }

  function buildSessionCard(s) {
    const card = document.createElement("div");
    card.className = "session-card" + (s.active ? " active" : "");
    card.dataset.sessionId = s.session_id;
    card.innerHTML = `<div class="session-main">${sessionMainHTML(s)}</div>`;
    bindCardHead(card, s.session_id);
    return card;
  }

  function updateSessionCard(card, s) {
    card.classList.toggle("active", !!s.active);
    card.querySelector(".session-main").innerHTML = sessionMainHTML(s);
    bindCardHead(card, s.session_id);
  }

  const sessionCardEls = new Map(); // session_id -> 卡片 DOM，跨轮询复用，避免整表重建
  let lastSessionsData = []; // Debug tab 的 session 下拉框要用完整列表找标题，跳转时也要用

  function renderSessionsList(sessions) {
    lastSessionsData = sessions;
    const list = $("#sessions-list");
    if (!sessions.length) {
      list.innerHTML = `<div class="empty">没有找到该项目的会话记录</div>`;
      sessionCardEls.clear();
      renderSessionSelect(sessions);
      return;
    }
    if (list.querySelector(".empty")) list.innerHTML = "";

    const seen = new Set();
    let prevEl = null;
    for (const s of sessions) {
      seen.add(s.session_id);
      let card = sessionCardEls.get(s.session_id);
      if (card) {
        updateSessionCard(card, s);
      } else {
        card = buildSessionCard(s);
        sessionCardEls.set(s.session_id, card);
      }
      const wantNext = prevEl ? prevEl.nextSibling : list.firstChild;
      if (wantNext !== card) list.insertBefore(card, wantNext);
      prevEl = card;
    }
    for (const [id, el] of sessionCardEls) {
      if (!seen.has(id)) {
        el.remove();
        sessionCardEls.delete(id);
      }
    }
    renderSessionSelect(sessions);
  }

  function renderSessionsTab(data) {
    renderSessionsList(data.sessions || []);
    if (sessionsPollTimer) clearInterval(sessionsPollTimer);
    sessionsPollTimer = setInterval(pollSessionsTab, SESSIONS_POLL_MS);
  }

  async function pollSessionsTab() {
    try {
      const data = await getJSON("/api/sessions");
      renderSessionsList(data.sessions || []);
    } catch (e) {
      // 轮询失败静默重试
    }
  }

  // --- Model tab --------------------------------------------------------
  let modelChart = null;

  function renderModelStats(models, whole) {
    const stats = $("#model-stats");
    const sessions = models.reduce((a, m) => a + (m.sessions || 0), 0);
    const messages = models.reduce((a, m) => a + (m.messages || 0), 0);
    const hot = models.filter((m) => whole && m.total_tokens / whole >= HIGH_SHARE).length;
    const tiles = [
      { label: "总 token", value: fmt(whole) },
      { label: "模型数", value: fmt(models.length) },
      { label: "会话数", value: fmt(sessions) },
      { label: "消息数", value: fmt(messages) },
      { label: "高消耗模型", value: fmt(hot), hot: hot > 0 },
    ];
    stats.innerHTML = tiles.map((t) => `
      <div class="stat-tile">
        <div class="label">${t.label}</div>
        <div class="value${t.hot ? " hot" : ""}">${t.value}</div>
      </div>`).join("");
  }

  function renderModelTab(data) {
    $("#cx-version").textContent = data.cx_version ? `v${data.cx_version}` : "";
    const models = data.models || [];
    const whole = data.total ? data.total.total_tokens : 0;
    renderModelStats(models, whole);

    const tbody = $("#model-table tbody");
    tbody.innerHTML = "";
    if (!models.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="empty">没有找到该项目的会话记录</td></tr>`;
    }
    for (const m of models) {
      const share = whole ? m.total_tokens / whole : 0;
      const tr = document.createElement("tr");
      if (share >= HIGH_SHARE) tr.className = "hot";
      tr.innerHTML = `
        <td>${m.model}</td>
        <td class="num">${fmt(m.sessions)}</td>
        <td class="num">${fmt(m.messages)}</td>
        <td class="num">${fmt(m.input_tokens)}</td>
        <td class="num">${fmt(m.output_tokens)}</td>
        <td class="num">${fmt(m.cache_creation_tokens)}</td>
        <td class="num">${fmt(m.cache_read_tokens)}</td>
        <td class="num">${fmt(m.total_tokens)}</td>`;
      tbody.appendChild(tr);
    }

    const ctx = $("#model-chart").getContext("2d");
    const labels = models.map((m) => m.model);
    const datasets = [
      { label: "input", data: models.map((m) => m.input_tokens), backgroundColor: "#5b9bd5" },
      { label: "output", data: models.map((m) => m.output_tokens), backgroundColor: "#6fcf78" },
      { label: "cache write", data: models.map((m) => m.cache_creation_tokens), backgroundColor: "#e0c341" },
      { label: "cache read", data: models.map((m) => m.cache_read_tokens), backgroundColor: "#c77dd1" },
    ];
    if (modelChart) modelChart.destroy();
    modelChart = new Chart(ctx, {
      type: "bar",
      data: { labels, datasets },
      options: {
        responsive: true,
        scales: { x: { stacked: true }, y: { stacked: true } },
        plugins: { legend: { labels: { color: "#e6e8ec" } } },
      },
    });
  }

  // --- Config tab ---------------------------------------------------------
  function renderConfigTab(data) {
    $("#cwd").textContent = data.cwd || "";

    const stbody = $("#sources-table tbody");
    stbody.innerHTML = "";
    for (const s of data.sources || []) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><span class="scope-tag scope-${s.scope}">${s.scope}</span></td>
        <td>${s.path}</td>
        <td>${s.exists ? "✓" : "·"}</td>
        <td>${(s.keys || []).join(", ")}</td>`;
      stbody.appendChild(tr);
    }

    const prov = $("#provenance");
    prov.innerHTML = "";
    const entries = Object.entries(data.provenance || {});
    if (!entries.length) {
      prov.innerHTML = `<div class="empty">没有可显示的配置项</div>`;
    }
    for (const [key, rows] of entries) {
      const div = document.createElement("div");
      div.className = "prov-key";
      const lines = rows.map((r) =>
        `<div class="prov-entry"><span class="scope-tag scope-${r.scope}">${r.scope}</span> ${r.path} = ${JSON.stringify(r.value)}</div>`
      ).join("");
      div.innerHTML = `<div class="key">${key}</div>${lines}`;
      prov.appendChild(div);
    }
  }

  // --- Doctor tab -----------------------------------------------------------
  function renderDoctorTab(data) {
    const summary = $("#doctor-summary");
    const c = data.summary || { error: 0, warn: 0, info: 0, ignored: 0 };
    summary.innerHTML = `
      <div class="sev-error">error<b>${c.error}</b></div>
      <div class="sev-warn">warn<b>${c.warn}</b></div>
      <div class="sev-info">info<b>${c.info}</b></div>
      ${c.ignored ? `<div>ignored<b>${c.ignored}</b></div>` : ""}`;

    const box = $("#doctor-findings");
    box.innerHTML = "";
    const findings = (data.findings || []).filter((f) => !f.ignored);
    if (!findings.length) {
      box.innerHTML = `<div class="empty">✓ 未发现问题</div>`;
      return;
    }
    for (const f of findings) {
      const div = document.createElement("div");
      div.className = `finding ${f.severity}`;
      div.innerHTML = `
        <div class="title">${f.title} <span class="meta">${f.id}</span></div>
        <div class="meta">${f.detail}</div>
        <div class="meta">位置 ${f.where} · 修复 ${f.fix}</div>`;
      box.appendChild(div);
    }
  }

  // --- Debug tab: web-terminal ------------------------------------------
  // 新记录持续从 /api/debug?after=<lastTs> 轮询进来，像终端一样追加到底部；
  // 只有用户本来就贴在底部时才自动滚动（"跟随最新"），避免打断向上翻看。
  const DEBUG_POLL_MS = 3000;
  const DEBUG_MAX_LINES = 1000;
  let debugLastTs = "";
  let debugPollTimer = null;
  let debugFollow = true;

  // 筛到单个 session 时（Sessions tab 点击卡片跳转过来，或者手动选下拉框），
  // "" 代表全部 session，走 /api/debug 的增量轮询；选中具体 session 后改用
  // /api/sessions/<id>/timeline，每轮询周期整份重拉——数据量有 limit 兜底，
  // 重拉的代价可以接受，换来的是不用维护第二套增量游标逻辑。
  let debugSessionFilter = "";

  // 下拉框只列 active session（用户明确要的口径）；但如果正在筛选的 session
  // 中途从 active 掉出去了（消息结束、超过 5 分钟没动静），继续把它的选项
  // 保留在下拉框里并标注"已结束"，不然筛选状态会在下一次轮询时突然消失。
  function renderSessionSelect(sessions) {
    const select = $("#session-select");
    if (!select) return;
    const active = sessions.filter((s) => s.active);
    const options = [{ id: "", label: "全部 session" }];
    for (const s of active) {
      options.push({ id: s.session_id, label: `${sessionDisplayTitle(s)} · ${s.session_id.slice(-8)}` });
    }
    if (debugSessionFilter && !active.some((s) => s.session_id === debugSessionFilter)) {
      const stale = sessions.find((s) => s.session_id === debugSessionFilter);
      options.push({
        id: debugSessionFilter,
        label: stale
          ? `${sessionDisplayTitle(stale)} · ${debugSessionFilter.slice(-8)}（已结束）`
          : `${debugSessionFilter.slice(-8)}（已结束）`,
      });
    }
    select.innerHTML = options.map((o) =>
      `<option value="${esc(o.id)}"${o.id === debugSessionFilter ? " selected" : ""}>${esc(o.label)}</option>`
    ).join("");
  }

  function openSessionInDebug(sessionId) {
    debugSessionFilter = sessionId;
    renderSessionSelect(lastSessionsData);
    switchToTab("debug");
    startDebugSource();
  }

  function isNearBottom(box) {
    return box.scrollHeight - box.scrollTop - box.clientHeight < 24;
  }

  // 展开状态跟 DOM 节点本身绑死会有个问题：单 session 模式每次轮询都会把
  // Debug tab 整个重新拉取、整个重建 DOM（见 loadSessionDebug），用户刚点开
  // 看的工具详情就随着重建一起消失了。用 request_id 记住"哪些条目展开过"，
  // 重建时按这个 Set 直接把 .open 加回去，跟具体 DOM 节点脱钩。
  const openEntryKeys = new Set();
  const entryKey = (e) => e.request_id || `${e.timestamp}-${e.model}`;

  // 一条记录里除了摘要行（模型/工具名/耗时等）之外的详细内容——工具入参、
  // 助手输出文字——点击摘要行才展开，避免默认就把日志窗口塞满。
  function buildDetailPanel(e) {
    const parts = [];
    for (const call of e.tool_calls || []) {
      parts.push(
        `<div class="detail-tool"><span class="name">${esc(call.name)}</span>` +
        `<pre>${esc(call.input)}</pre>` +
        (call.truncated ? `<div class="detail-truncated">（入参过长，已截断）</div>` : "") +
        `</div>`
      );
    }
    if (e.text) parts.push(`<div class="detail-text">${esc(e.text)}</div>`);
    return parts.join("");
  }

  function buildDebugLine(e) {
    const wrap = document.createElement("div");
    const key = entryKey(e);
    wrap.dataset.entryKey = key;
    wrap.dataset.cost = e.cost_usd || 0;
    wrap.className = "term-entry" + (openEntryKeys.has(key) ? " open" : "");

    const hasDetail = (e.tool_calls && e.tool_calls.length) || e.text;
    const line = document.createElement("div");
    line.className = "term-line" + (e.is_error ? " error" : "") + (hasDetail ? " has-detail" : "");
    const toggle = hasDetail ? `<span class="toggle">▸</span>` : `<span class="toggle"></span>`;
    const agent = e.agent ? `<span class="agent">[${esc(e.agent)}]</span> ` : "";
    const stop = e.stop_reason ? ` <span class="stop">stop=${esc(e.stop_reason)}</span>` : "";
    const tools = (e.tool_uses || []).length
      ? ` <span class="tools">tools=${esc(e.tool_uses.join(","))}</span>` : "";
    const effort = e.effort ? ` <span class="effort">effort=${esc(e.effort)}</span>` : "";
    const cacheMiss = e.cache_miss_reason
      ? ` <span class="cache-miss">cache_miss=${esc(e.cache_miss_reason)}</span>` : "";
    const tokens = e.total_tokens
      ? ` <span class="tokens">tokens=${fmt(e.total_tokens)}</span>` : "";
    const cost = e.cost_usd
      ? ` <span class="cost">${fmtUsd(e.cost_usd)}</span>` : "";
    line.innerHTML =
      `${toggle}<span class="prompt">&gt;</span> <span class="ts">${esc(e.timestamp)}</span> ` +
      `${agent}<span class="model">${esc(e.model)}</span>${stop}${tools}${effort}${cacheMiss}${tokens}${cost} ` +
      `<span class="req-id">${esc(e.request_id)}</span>` +
      (e.is_error && e.error_text ? `<span class="err-text">${esc(e.error_text)}</span>` : "");
    if (hasDetail) {
      line.addEventListener("click", () => {
        const isOpen = wrap.classList.toggle("open");
        if (isOpen) openEntryKeys.add(key); else openEntryKeys.delete(key);
      });
    }
    wrap.appendChild(line);

    if (hasDetail) {
      const detail = document.createElement("div");
      detail.className = "term-detail";
      detail.innerHTML = buildDetailPanel(e);
      wrap.appendChild(detail);
    }
    return wrap;
  }

  // cost 过滤：只影响显示（display:none），不影响已拉取的数据，阈值改了
  // 不用重新请求接口就能立刻生效。0 = 不过滤。
  let debugCostThreshold = 0;

  function applyCostFilterTo(entry) {
    const cost = Number(entry.dataset.cost || 0);
    entry.classList.toggle("cost-hidden", cost < debugCostThreshold);
  }

  function applyCostFilter() {
    const box = $("#debug-log");
    for (const entry of box.querySelectorAll(".term-entry")) applyCostFilterTo(entry);
  }

  function appendDebugLine(e) {
    const box = $("#debug-log");
    const empty = box.querySelector(".empty");
    if (empty) empty.remove();
    const follow = debugFollow && isNearBottom(box);
    const entry = buildDebugLine(e);
    applyCostFilterTo(entry);
    box.appendChild(entry);
    while (box.children.length > DEBUG_MAX_LINES) box.removeChild(box.firstChild);
    if (follow) box.scrollTop = box.scrollHeight;
  }

  function renderDebugTab(data) {
    const box = $("#debug-log");
    // 服务端首屏按时间倒序返回；终端要按时间正序从上往下追加，最新的停在底部。
    const entries = (data.entries || []).slice().reverse();
    box.innerHTML = "";
    if (!entries.length) {
      box.innerHTML = `<div class="empty">没有找到该项目的会话记录</div>`;
    } else {
      for (const e of entries) {
        box.appendChild(buildDebugLine(e));
        debugLastTs = e.timestamp;
      }
      applyCostFilter();
      box.scrollTop = box.scrollHeight;
    }
    if (debugPollTimer) clearInterval(debugPollTimer);
    debugPollTimer = setInterval(pollDebugTab, DEBUG_POLL_MS);
  }

  async function pollDebugTab() {
    if (debugSessionFilter) return; // 已经切到单 session 模式，这个全量增量轮询该停了
    try {
      const data = await getJSON(`/api/debug?after=${encodeURIComponent(debugLastTs)}&limit=200`);
      // 服务端在 after 模式下按时间正序返回，直接依次追加即可
      for (const e of data.entries || []) {
        appendDebugLine(e);
        debugLastTs = e.timestamp;
      }
    } catch (e) {
      // 轮询失败静默重试，不打断已渲染的内容
    }
  }

  // 单 session 模式：每轮询周期整份重拉这个 session 的完整时间线并重建 DOM——
  // openEntryKeys 保证重建不影响已展开的详情，见 buildDebugLine 顶部的注释。
  async function loadSessionDebug(sessionId) {
    const box = $("#debug-log");
    try {
      const data = await getJSON(`/api/sessions/${encodeURIComponent(sessionId)}/timeline?limit=1000`);
      if (debugSessionFilter !== sessionId) return; // 拉取期间用户切换了筛选，丢弃结果
      const entries = data.entries || [];
      box.innerHTML = "";
      if (!entries.length) {
        box.innerHTML = `<div class="empty">这个 session 还没有调用记录</div>`;
      } else {
        for (const e of entries) box.appendChild(buildDebugLine(e));
        applyCostFilter();
        if (debugFollow) box.scrollTop = box.scrollHeight;
      }
    } catch (err) {
      if (debugSessionFilter !== sessionId) return;
      box.innerHTML = `<div class="empty">加载失败: ${esc(err.message)}</div>`;
    }
  }

  // Debug tab 数据源的统一入口：按 debugSessionFilter 决定走全量增量轮询还是
  // 单 session 整拉，切换前先停掉另一套的定时器，避免两个轮询同时写 DOM。
  async function startDebugSource() {
    if (debugPollTimer) clearInterval(debugPollTimer);
    const box = $("#debug-log");
    if (debugSessionFilter) {
      box.innerHTML = `<div class="empty">加载中…</div>`;
      await loadSessionDebug(debugSessionFilter);
      debugPollTimer = setInterval(() => loadSessionDebug(debugSessionFilter), DEBUG_POLL_MS);
    } else {
      try {
        renderDebugTab(await getJSON("/api/debug"));
      } catch (err) {
        box.innerHTML = `<div class="empty">加载失败: ${esc(err.message)}</div>`;
      }
    }
  }

  function setupTerminalControls() {
    const box = $("#debug-log");
    const followToggle = $("#follow-toggle");
    const costFilter = $("#cost-filter");
    const sessionSelect = $("#session-select");
    costFilter.addEventListener("input", () => {
      debugCostThreshold = Number(costFilter.value) || 0;
      applyCostFilter();
    });
    sessionSelect.addEventListener("change", () => {
      debugSessionFilter = sessionSelect.value;
      startDebugSource();
    });
    followToggle.addEventListener("change", () => {
      debugFollow = followToggle.checked;
      if (debugFollow) box.scrollTop = box.scrollHeight;
    });
    // 用户手动往上滚时自动关闭"跟随最新"，滚回底部再自动打开
    box.addEventListener("scroll", () => {
      const atBottom = isNearBottom(box);
      if (atBottom !== debugFollow) {
        debugFollow = atBottom;
        followToggle.checked = atBottom;
      }
    });
    $("#clear-terminal").addEventListener("click", () => {
      box.innerHTML = `<div class="empty">已清屏，等待新日志…</div>`;
    });
  }

  async function loadAll() {
    const [sessions, model, config, doctor, debug] = await Promise.all([
      getJSON("/api/sessions"),
      getJSON("/api/model"),
      getJSON("/api/config"),
      getJSON("/api/doctor"),
      getJSON("/api/debug"),
    ]);
    renderSessionsTab(sessions);
    renderModelTab(model);
    renderConfigTab(config);
    renderDoctorTab(doctor);
    renderDebugTab(debug);
  }

  setupTabs();
  setupTerminalControls();
  loadAll().catch((e) => {
    document.querySelector("main").innerHTML = `<div class="empty">加载失败: ${e.message}</div>`;
  });
})();
