// cx dashboard —— 纯前端，无构建步骤。fetch /api/* 拿数据，Chart.js 画图。
(() => {
  "use strict";

  const HIGH_SHARE = 0.20; // 跟 cx/render.py 的 _HIGH_SHARE 保持一致
  const MED_SHARE = 0.08;

  const $ = (sel) => document.querySelector(sel);
  const fmt = (n) => Number(n || 0).toLocaleString("en-US");

  function setupTabs() {
    document.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
        document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
        btn.classList.add("active");
        $(`#tab-${btn.dataset.tab}`).classList.add("active");
      });
    });
  }

  async function getJSON(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${url} -> HTTP ${res.status}`);
    return res.json();
  }

  // --- Model tab --------------------------------------------------------
  let modelChart = null;

  function renderModelTab(data) {
    $("#cx-version").textContent = data.cx_version ? `v${data.cx_version}` : "";
    const models = data.models || [];
    const whole = data.total ? data.total.total_tokens : 0;

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

  // --- Debug tab --------------------------------------------------------
  // 新记录持续从 /api/debug?after=<lastTs> 轮询进来，只插入没见过的，不整表重绘。
  const DEBUG_POLL_MS = 3000;
  const DEBUG_MAX_ROWS = 500;
  let debugLastTs = "";
  let debugPollTimer = null;

  function esc(s) {
    const div = document.createElement("div");
    div.textContent = s == null ? "" : String(s);
    return div.innerHTML;
  }

  function buildDebugRow(e) {
    const row = document.createElement("div");
    row.className = "log-row" + (e.is_error ? " error" : "");
    const agent = e.agent ? `<span class="agent">[${esc(e.agent)}]</span>` : "";
    const stop = e.stop_reason ? `<span class="stop">stop=${esc(e.stop_reason)}</span>` : "";
    const tools = (e.tool_uses || []).length
      ? `<span class="tools">tools=${esc(e.tool_uses.join(","))}</span>` : "";
    row.innerHTML = `
      <span class="ts">${esc(e.timestamp)}</span>
      <span class="model">${esc(e.model)}</span>
      ${agent}${stop}${tools}
      <span class="req-id">${esc(e.request_id)}</span>
      ${e.is_error && e.error_text ? `<div class="err-text">${esc(e.error_text)}</div>` : ""}`;
    return row;
  }

  function prependDebugRow(e) {
    const box = $("#debug-log");
    const empty = box.querySelector(".empty");
    if (empty) empty.remove();
    box.insertBefore(buildDebugRow(e), box.firstChild);
    while (box.children.length > DEBUG_MAX_ROWS) box.removeChild(box.lastChild);
  }

  function renderDebugTab(data) {
    const box = $("#debug-log");
    const entries = data.entries || []; // 服务端按时间倒序返回，entries[0] 最新
    box.innerHTML = "";
    if (!entries.length) {
      box.innerHTML = `<div class="empty">没有找到该项目的会话记录</div>`;
    } else {
      for (const e of entries) box.appendChild(buildDebugRow(e));
      debugLastTs = entries[0].timestamp;
    }
    if (debugPollTimer) clearInterval(debugPollTimer);
    debugPollTimer = setInterval(pollDebugTab, DEBUG_POLL_MS);
  }

  async function pollDebugTab() {
    try {
      const data = await getJSON(`/api/debug?after=${encodeURIComponent(debugLastTs)}&limit=200`);
      // 服务端在 after 模式下按时间正序返回；逐条插到最前面，最新的自然停在最上面
      for (const e of data.entries || []) {
        prependDebugRow(e);
        debugLastTs = e.timestamp;
      }
    } catch (e) {
      // 轮询失败静默重试，不打断已渲染的内容
    }
  }

  async function loadAll() {
    const [model, config, doctor, debug] = await Promise.all([
      getJSON("/api/model"),
      getJSON("/api/config"),
      getJSON("/api/doctor"),
      getJSON("/api/debug"),
    ]);
    renderModelTab(model);
    renderConfigTab(config);
    renderDoctorTab(doctor);
    renderDebugTab(debug);
  }

  setupTabs();
  loadAll().catch((e) => {
    document.querySelector("main").innerHTML = `<div class="empty">加载失败: ${e.message}</div>`;
  });
})();
