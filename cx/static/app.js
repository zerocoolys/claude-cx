// cx dashboard —— 纯前端，无构建步骤。fetch /api/* 拿数据，Chart.js 画图。
(() => {
  "use strict";

  const HIGH_SHARE = 0.20; // 跟 cx/render.py 的 _HIGH_SHARE 保持一致
  const MED_SHARE = 0.08;

  const $ = (sel) => document.querySelector(sel);
  const fmt = (n) => Number(n || 0).toLocaleString("en-US");

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

  function setupTabs() {
    document.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
        document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
        btn.classList.add("active");
        $(`#tab-${btn.dataset.tab}`).classList.add("active");
        $("#page-title").textContent = btn.querySelector(".nav-label").textContent;
      });
    });
  }

  async function getJSON(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${url} -> HTTP ${res.status}`);
    return res.json();
  }

  // --- Sessions tab: 活跃会话列表 ------------------------------------------
  // 卡片式列表，每 4 秒整体重拉重渲染；展开的时间线面板状态靠 expandedSessionId
  // 记住，重渲染后如果还在列表里就保持展开并重新拉一次它的 timeline。
  const SESSIONS_POLL_MS = 4000;
  let sessionsPollTimer = null;
  let expandedSessionId = null;

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

  // 卡片拆成两层：session-main（每次轮询整体重绘）+ session-timeline（独立管理，
  // 轮询重绘 main 时绝不touch，展开的日志才不会跟着列表一起消失又重建造成跳动。
  function sessionMainHTML(s) {
    const cwdName = (s.cwd || "").split("/").filter(Boolean).pop() || s.cwd || "(unknown)";
    const branch = s.branch ? `<span class="scope-tag scope-project">${esc(s.branch)}</span>` : "";
    const toolChip = s.last_tool ? `<span class="chip">tool: ${esc(s.last_tool)}</span>` : "";
    const stopChip = s.last_stop_reason
      ? `<span class="chip">stop: ${esc(s.last_stop_reason)}</span>` : "";
    const agentChip = s.last_agent ? `<span class="chip">agent: ${esc(s.last_agent)}</span>` : "";
    return `
      <div class="session-head">
        <span class="status-dot${s.active ? " pulse" : " idle"}"></span>
        <span class="session-title">${esc(cwdName)}</span>
        ${branch}
        <span class="session-id">${esc(s.session_id.slice(-8))}</span>
        <span class="session-time hint">${esc(relTime(s.last))}</span>
      </div>
      <div class="session-body">
        <div class="session-stats">
          <span class="stat">messages <b>${fmt(s.messages)}</b></span>
          <span class="stat">tokens <b>${fmt(s.total_tokens)}</b></span>
        </div>
        ${buildSparklineSVG(s.token_series)}
      </div>
      <div class="session-chips">${toolChip}${stopChip}${agentChip}</div>
      ${buildAgentBadges(s.agents)}`;
  }

  function bindCardHead(card, sessionId) {
    card.querySelector(".session-head").addEventListener("click", () => toggleSessionTimeline(sessionId));
  }

  function toggleSessionTimeline(sessionId) {
    const box = document.getElementById(`timeline-${sessionId}`);
    if (expandedSessionId === sessionId) {
      expandedSessionId = null;
      if (box) { box.classList.remove("open"); box.innerHTML = ""; }
      return;
    }
    if (expandedSessionId) {
      const prevBox = document.getElementById(`timeline-${expandedSessionId}`);
      if (prevBox) { prevBox.classList.remove("open"); prevBox.innerHTML = ""; }
    }
    expandedSessionId = sessionId;
    loadSessionTimeline(sessionId);
  }

  function buildSessionCard(s) {
    const card = document.createElement("div");
    card.className = "session-card" + (s.active ? " active" : "");
    card.dataset.sessionId = s.session_id;
    card.innerHTML =
      `<div class="session-main">${sessionMainHTML(s)}</div>` +
      `<div class="session-timeline" id="timeline-${esc(s.session_id)}"></div>`;
    bindCardHead(card, s.session_id);
    return card;
  }

  function updateSessionCard(card, s) {
    card.classList.toggle("active", !!s.active);
    card.querySelector(".session-main").innerHTML = sessionMainHTML(s);
    bindCardHead(card, s.session_id);
  }

  let lastSessionsData = [];
  const sessionCardEls = new Map(); // session_id -> 卡片 DOM，跨轮询复用，避免整表重建

  function renderSessionsList(sessions) {
    lastSessionsData = sessions;
    const list = $("#sessions-list");
    if (!sessions.length) {
      list.innerHTML = `<div class="empty">没有找到该项目的会话记录</div>`;
      sessionCardEls.clear();
      expandedSessionId = null;
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

    if (expandedSessionId && !seen.has(expandedSessionId)) {
      expandedSessionId = null;
    }
  }

  async function loadSessionTimeline(sessionId) {
    const box = document.getElementById(`timeline-${sessionId}`);
    if (!box) return;
    box.classList.add("open");
    // 已经有内容（比如轮询期间重新拉取）就先保留旧内容，等新数据到了再一次性替换，
    // 不要先清空再显示"加载中"，那一下清空就是日志窗口"消失"的跳动来源。
    if (!box.children.length) box.innerHTML = `<div class="empty">加载中…</div>`;
    try {
      const data = await getJSON(`/api/sessions/${encodeURIComponent(sessionId)}/timeline`);
      if (expandedSessionId !== sessionId) return; // 拉取期间用户切换/收起了，丢弃结果
      box.innerHTML = "";
      const entries = data.entries || [];
      if (!entries.length) {
        box.innerHTML = `<div class="empty">没有调用记录</div>`;
        return;
      }
      for (const e of entries) box.appendChild(buildDebugLine(e));
      box.scrollTop = box.scrollHeight;
    } catch (err) {
      if (expandedSessionId !== sessionId) return;
      box.innerHTML = `<div class="empty">加载失败: ${esc(err.message)}</div>`;
    }
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
      if (expandedSessionId) loadSessionTimeline(expandedSessionId);
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

  function isNearBottom(box) {
    return box.scrollHeight - box.scrollTop - box.clientHeight < 24;
  }

  function buildDebugLine(e) {
    const line = document.createElement("div");
    line.className = "term-line" + (e.is_error ? " error" : "");
    const agent = e.agent ? `<span class="agent">[${esc(e.agent)}]</span> ` : "";
    const stop = e.stop_reason ? ` <span class="stop">stop=${esc(e.stop_reason)}</span>` : "";
    const tools = (e.tool_uses || []).length
      ? ` <span class="tools">tools=${esc(e.tool_uses.join(","))}</span>` : "";
    line.innerHTML =
      `<span class="prompt">&gt;</span> <span class="ts">${esc(e.timestamp)}</span> ` +
      `${agent}<span class="model">${esc(e.model)}</span>${stop}${tools} ` +
      `<span class="req-id">${esc(e.request_id)}</span>` +
      (e.is_error && e.error_text ? `<span class="err-text">${esc(e.error_text)}</span>` : "");
    return line;
  }

  function appendDebugLine(e) {
    const box = $("#debug-log");
    const empty = box.querySelector(".empty");
    if (empty) empty.remove();
    const follow = debugFollow && isNearBottom(box);
    box.appendChild(buildDebugLine(e));
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
      box.scrollTop = box.scrollHeight;
    }
    if (debugPollTimer) clearInterval(debugPollTimer);
    debugPollTimer = setInterval(pollDebugTab, DEBUG_POLL_MS);
  }

  async function pollDebugTab() {
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

  function setupTerminalControls() {
    const box = $("#debug-log");
    const followToggle = $("#follow-toggle");
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
