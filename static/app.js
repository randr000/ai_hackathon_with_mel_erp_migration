/* Frontend for the ERP migration assistant.
 * Plain ES-module-free JS: no build step, so the served file is the source.
 *
 * The session lives in the browser, not on the server. This app runs both as a
 * local process and as Vercel serverless functions, and serverless instances
 * are ephemeral — anything kept in server memory can vanish between two clicks.
 * So every request carries `state.session` and the server replays it: the
 * inputs (charts, journal, human decisions) travel with each call, and the
 * server recomputes the derived data (mappings, validation).
 */

const state = {
  // The session payload echoed back by the server on every response.
  session: null,
  mappings: [],
  filter: "all",
  targets: [],
  // Which step-2 stat is currently showing, or null for the default table.
  view: null,
  voiceEnabled: false,
};

/* ---------- helpers ---------- */

function el(id) {
  return document.getElementById(id);
}

function setMsg(id, text, kind) {
  const node = el(id);
  node.textContent = text || "";
  node.className = "msg" + (kind ? " " + kind : "");
}

function pct(value) {
  return `${Math.round(value * 100)}%`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

/** POST JSON, attaching the session when we have one. */
async function post(path, body = {}) {
  const payload = state.session ? { ...body, session: state.session } : { ...body };
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const parsed = await response.json();
      if (parsed.detail) detail = parsed.detail;
    } catch (_) {
      /* not JSON; keep the status text */
    }
    throw new Error(detail);
  }
  const type = response.headers.get("content-type") || "";
  return type.includes("application/json") ? response.json() : response;
}

/** Store whatever session the server returned. */
function remember(data) {
  if (data && data.session) state.session = data.session;
  return data;
}

/* ---------- step navigation ---------- */

function showStep(n) {
  document.querySelectorAll(".panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `step-${n}`);
  });
  document.querySelectorAll(".step").forEach((button) => {
    button.classList.toggle("active", button.dataset.step === String(n));
  });
}

document.querySelectorAll(".step").forEach((button) => {
  button.addEventListener("click", () => showStep(button.dataset.step));
});

/* ---------- voice ---------- */

async function refreshVoiceStatus() {
  const node = el("voice-status");
  try {
    const response = await fetch("/api/health");
    const health = await response.json();
    const v = health.voice || {};
    state.voiceEnabled = !!v.enabled;
    if (v.enabled) {
      node.className = "voice-status on";
      node.textContent =
        `ElevenLabs ready · voice ${v.voice_id} · model ${v.model_id} · ` +
        `${v.cached_clips} cached clip(s)`;
    } else {
      node.className = "voice-status off";
      node.textContent =
        "ElevenLabs not configured — add ELEVENLABS_API_KEY to this deployment's environment variables, then redeploy.";
    }
  } catch (error) {
    node.className = "voice-status off";
    node.textContent = `Voice status unavailable: ${error.message}`;
  }
}

async function speak(text, sourceNumber) {
  if (!state.voiceEnabled) {
    setMsg("migrate-msg", "ElevenLabs is not configured for this deployment.", "error");
    return;
  }
  const body = sourceNumber ? { source_number: sourceNumber } : { text };
  try {
    const response = await post("/api/voice/speak", body);
    const blob = await response.blob();
    const player = el("player");
    player.src = URL.createObjectURL(blob);
    player.hidden = false;
    await player.play();
  } catch (error) {
    setMsg("migrate-msg", `Speech failed: ${error.message}`, "error");
  }
}

/* ---------- step 1: load ---------- */

async function loadCharts() {
  const source = el("source-file").files[0];
  const target = el("target-file").files[0];
  const txn = el("txn-file").files[0];

  if (!source || !target) {
    setMsg("load-msg", "Select both a source and a target chart of accounts.", "error");
    return;
  }

  const form = new FormData();
  form.append("source", source);
  form.append("target", target);
  if (txn) form.append("transactions", txn);

  setMsg("load-msg", "Parsing and mapping…");
  try {
    // Multipart, so this one bypasses post(): no session exists yet on first load.
    const response = await fetch("/api/load", { method: "POST", body: form });
    if (!response.ok) {
      const parsed = await response.json().catch(() => ({}));
      throw new Error(parsed.detail || `HTTP ${response.status}`);
    }
    const data = remember(await response.json());
    setMsg("load-msg", "Loaded and mapped.", "ok");
    renderSummary("summary-1", data.summary);
    applyMappings(data);
    loadTargets();
    showStep(2);
  } catch (error) {
    setMsg("load-msg", error.message, "error");
  }
}

async function loadSamples() {
  setMsg("load-msg", "Loading sample data…");
  try {
    const data = remember(await post("/api/load-samples"));
    setMsg("load-msg", "Sample data loaded.", "ok");
    renderSummary("summary-1", data.summary);
    applyMappings(data);
    loadTargets();
    showStep(2);
  } catch (error) {
    setMsg("load-msg", error.message, "error");
  }
}

/** Store mappings from a response and re-render whatever view is active. */
function applyMappings(data) {
  state.mappings = data.mappings || state.mappings;
  renderSummary("summary-2", data.summary);
  if (state.view) {
    STEP2_VIEWS[state.view]();
  } else {
    renderMappings();
  }
}

async function refreshMappings() {
  const data = remember(await post("/api/mappings"));
  applyMappings(data);
}

/* The target list feeds the reviewer's dropdown. Derived from the mapping
 * candidates plus the targets already referenced. */
function loadTargets() {
  const seen = new Map();
  state.mappings.forEach((m) => {
    (m.candidates || []).forEach((c) => seen.set(c.target_number, c.target_name));
    if (m.target_number) seen.set(m.target_number, m.target_name);
  });
  state.targets = [...seen.entries()]
    .map(([number, name]) => ({ number, name }))
    .sort((a, b) => a.number.localeCompare(b.number));
}

/* ---------- summaries ---------- */

// The step-2 stats double as buttons that swap the content below them.
const STEP2_VIEWS = {
  "Source accounts": renderSourceAccounts,
  "Target accounts": renderTargetAccounts,
  "Transactions": renderTransactions,
  "Auto-mapped": () => renderMappingsView("auto"),
  "Needs review": () => renderMappingsView("needs_review"),
  "Confirmed": () => renderMappingsView("confirmed"),
  "Unmapped": () => renderMappingsView("unmapped"),
};

function renderSummary(nodeId, summary) {
  if (!summary) return;
  const interactive = nodeId === "summary-2";
  const stats = [
    ["Source accounts", summary.source_accounts],
    ["Target accounts", summary.target_accounts],
    ["Transactions", summary.transactions],
    ["Auto-mapped", summary.auto],
    ["Needs review", summary.needs_review],
    ["Confirmed", summary.confirmed],
    ["Unmapped", summary.unmapped],
  ];
  el(nodeId).innerHTML = stats
    .map(([label, value]) => {
      const cls = interactive ? ' class="stat interactive"' : ' class="stat"';
      const attr = interactive ? ` data-view="${label}"` : "";
      return `<div${cls}${attr}>${label}: <b>${value}</b></div>`;
    })
    .join("");

  if (interactive) {
    el(nodeId).querySelectorAll(".stat[data-view]").forEach((stat) => {
      stat.addEventListener("click", () => showStep2View(stat.dataset.view));
    });
    highlightActiveStat();
  }
}

function showStep2View(view) {
  // Clicking the active stat toggles back to the default mappings table.
  state.view = state.view === view ? null : view;
  if (state.view) {
    STEP2_VIEWS[state.view]();
  } else {
    renderMappings();
  }
  highlightActiveStat();
}

function highlightActiveStat() {
  document.querySelectorAll("#summary-2 .stat[data-view]").forEach((stat) => {
    stat.classList.toggle("active", stat.dataset.view === state.view);
  });
}

function renderMappingsView(status) {
  const container = el("mappings");
  const rows = state.mappings
    .filter((m) => (status === "unmapped" ? !m.target_number : m.status === status))
    .map((m) => mappingRow(m))
    .join("");
  container.innerHTML = rows
    ? `<table>
      <thead>
        <tr>
          <th>Source account</th><th>Target account</th><th>Status</th>
          <th>Reasoning</th><th>Override / listen</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`
    : `<p class="hint">No ${status.replace("_", " ")} mappings.</p>`;
  bindMappingControls(container);
}

async function renderSourceAccounts() {
  const data = remember(await post("/api/source-accounts"));
  el("mappings").innerHTML = accountTable(data.accounts);
}

async function renderTargetAccounts() {
  const data = remember(await post("/api/target-accounts"));
  el("mappings").innerHTML = accountTable(data.accounts);
}

async function renderTransactions() {
  const data = remember(await post("/api/transactions"));
  const rows = (data.transactions || [])
    .map((t) => `
      <tr>
        <td>${escapeHtml(t.transaction_id)}</td>
        <td>${escapeHtml(t.date)}</td>
        <td><strong>${escapeHtml(t.account_number)}</strong></td>
        <td>${escapeHtml(t.account_name)}</td>
        <td style="text-align:right">${Number(t.amount).toLocaleString(undefined, { minimumFractionDigits: 2 })}</td>
        <td>${escapeHtml(t.memo || "")}</td>
      </tr>`)
    .join("");
  el("mappings").innerHTML = rows
    ? `<table>
      <thead>
        <tr><th>ID</th><th>Date</th><th>Account</th><th>Name</th><th style="text-align:right">Amount</th><th>Memo</th></tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`
    : '<p class="hint">No transactions loaded.</p>';
}

function accountTable(accounts) {
  const rows = (accounts || [])
    .map((a) => `
      <tr>
        <td><strong>${escapeHtml(a.account_number)}</strong></td>
        <td>${escapeHtml(a.account_name)}</td>
        <td>${escapeHtml(a.type || "")}</td>
        <td>${escapeHtml(a.detail_type || "")}</td>
      </tr>`)
    .join("");
  return rows
    ? `<table>
      <thead>
        <tr><th>Number</th><th>Name</th><th>Type</th><th>Detail type</th></tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`
    : '<p class="hint">No accounts loaded.</p>';
}

/* ---------- step 2: mappings ---------- */

function statusBadge(mapping) {
  if (!mapping.target_number) return '<span class="badge unmapped">unmapped</span>';
  const map = { auto: "auto", needs_review: "review", confirmed: "confirmed" };
  const label = mapping.status === "needs_review" ? "needs review" : mapping.status;
  return `<span class="badge ${map[mapping.status] || ""}">${label}</span>`;
}

function candidateOptions(mapping) {
  const options = mapping.candidates && mapping.candidates.length
    ? mapping.candidates
    : state.targets.map((t) => ({ target_number: t.number, target_name: t.name, score: 0 }));

  return (
    '<option value="">— select —</option>' +
    options
      .map((c) => {
        const selected = c.target_number === mapping.target_number ? " selected" : "";
        return `<option value="${escapeHtml(c.target_number)}"${selected}>` +
          `${escapeHtml(c.target_number)} · ${escapeHtml(c.target_name)}` +
          `${c.score ? ` (${c.score.toFixed(2)})` : ""}</option>`;
      })
      .join("")
  );
}

function renderMappings() {
  state.view = null;
  const container = el("mappings");
  const filtered = state.mappings.filter((m) =>
    state.filter === "all"
      ? true
      : state.filter === "unmapped"
        ? !m.target_number
        : m.status === state.filter
  );

  if (!filtered.length) {
    container.innerHTML = '<p class="hint">No mappings in this view.</p>';
    highlightActiveStat();
    return;
  }

  container.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Source account</th>
          <th>Target account</th>
          <th>Status</th>
          <th>Reasoning</th>
          <th>Override / listen</th>
        </tr>
      </thead>
      <tbody>${filtered.map(mappingRow).join("")}</tbody>
    </table>`;

  bindMappingControls(container);
  highlightActiveStat();
}

function mappingRow(m) {
  const flagged = m.status === "needs_review";
  const reasons = (m.reasons || []).map((r) => `<li>${escapeHtml(r)}</li>`).join("");
  return `
    <tr class="${flagged ? "flag" : ""}">
      <td>
        <strong>${escapeHtml(m.source_number)}</strong><br />
        ${escapeHtml(m.source_name)}
        <div class="reasons">${escapeHtml(m.source_type || "")}</div>
      </td>
      <td>
        <div class="chosen">
          ${m.target_number
            ? `${escapeHtml(m.target_number)} · ${escapeHtml(m.target_name)}`
            : "<em>no confident match</em>"}
        </div>
        <div class="reasons">${pct(m.confidence)} · ${escapeHtml(m.method)}</div>
      </td>
      <td>${statusBadge(m)}</td>
      <td><ul class="reasons">${reasons}</ul></td>
      <td>
        <select data-source="${escapeHtml(m.source_number)}">${candidateOptions(m)}</select>
        <div style="margin-top:6px; display:flex; gap:6px;">
          <button class="small" data-speak="${escapeHtml(m.source_number)}">Explain</button>
          ${m.status === "needs_review" && m.target_number
            ? `<button class="small" data-approve="${escapeHtml(m.source_number)}">✓ Accept top</button>`
            : ""}
        </div>
      </td>
    </tr>`;
}

function bindMappingControls(container) {
  container.querySelectorAll("select[data-source]").forEach((select) => {
    select.addEventListener("change", () => confirmMapping(select.dataset.source, select.value));
  });
  container.querySelectorAll("button[data-speak]").forEach((button) => {
    button.addEventListener("click", () => speak(null, button.dataset.speak));
  });
  container.querySelectorAll("button[data-approve]").forEach((button) => {
    button.addEventListener("click", () => approveTop(button.dataset.approve));
  });
}

async function approveTop(sourceNumber) {
  try {
    const data = remember(
      await post("/api/approve-top", sourceNumber ? { source_number: sourceNumber } : {})
    );
    applyMappings(data);
  } catch (error) {
    setMsg("load-msg", error.message, "error");
  }
}

async function confirmMapping(sourceNumber, targetNumber) {
  if (!targetNumber) return;
  try {
    const data = remember(
      await post("/api/confirm", { source_number: sourceNumber, target_number: targetNumber })
    );
    applyMappings(data);
  } catch (error) {
    setMsg("load-msg", error.message, "error");
  }
}

document.querySelectorAll('input[name="filter"]').forEach((radio) => {
  radio.addEventListener("change", () => {
    state.filter = radio.value;
    renderMappings();
  });
});

el("btn-clear-confirm").addEventListener("click", async () => {
  try {
    const data = remember(await post("/api/confirm/clear"));
    applyMappings(data);
  } catch (error) {
    setMsg("load-msg", error.message, "error");
  }
});

/* ---------- downloads ---------- */

/* Exports are POSTs because the session travels in the request body, so they
 * cannot be plain <a href> links. Fetch the file, then hand the blob to a
 * synthetic link so the browser's own download behaviour still applies. */
async function downloadCsv(path, filename) {
  try {
    const response = await post(path);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  } catch (error) {
    setMsg("migrate-msg", `Download failed: ${error.message}`, "error");
  }
}

el("export-mappings").addEventListener("click", (event) => {
  event.preventDefault();
  downloadCsv("/api/export/mappings.csv", "account_mappings.csv");
});

el("export-migrated").addEventListener("click", (event) => {
  event.preventDefault();
  downloadCsv("/api/export/migrated.csv", "migrated_transactions.csv");
});

/* ---------- step 3: migrate ---------- */

el("btn-migrate").addEventListener("click", async () => {
  setMsg("migrate-msg", "Migrating…");
  try {
    const data = remember(await post("/api/migrate"));
    applyMappings({ mappings: state.mappings, summary: data.summary });
    setMsg(
      "migrate-msg",
      `Migrated ${data.summary.migrated_lines} lines` +
        (data.unmapped_lines ? `, ${data.unmapped_lines} still unmapped.` : "."),
      data.unmapped_lines ? "error" : "ok"
    );
    renderSummary("summary-3", data.summary);
    renderReport(data.report);
    showStep(4);
  } catch (error) {
    setMsg("migrate-msg", error.message, "error");
  }
});

/* ---------- step 4: report ---------- */

function renderReport(report) {
  const verdict = report.passed
    ? '<div class="verdict pass">✓ Validation passed — the migration is sound.</div>'
    : '<div class="verdict fail">✕ Validation failed — do not migrate this data yet.</div>';

  const checks = (report.checks || [])
    .map((check) => {
      const klass = check.passed ? "pass" : check.severity === "warning" ? "warn" : "fail";
      const icon = check.passed ? "✓" : check.severity === "warning" ? "!" : "✕";
      return `
        <div class="check ${klass}">
          <div class="icon">${icon}</div>
          <div class="body">
            <div class="name">${escapeHtml(check.name)}</div>
            <div class="detail">${escapeHtml(check.detail)}</div>
          </div>
        </div>`;
    })
    .join("");

  const totals = report.totals || {};
  const totalRow = `
    <div class="summary">
      <div class="stat">Source total: <b>${Number(totals.source_total ?? 0).toLocaleString()}</b></div>
      <div class="stat">Migrated total: <b>${Number(totals.migrated_total ?? 0).toLocaleString()}</b></div>
      <div class="stat">Difference: <b>${Number(totals.difference ?? 0).toLocaleString()}</b></div>
    </div>`;

  el("report").innerHTML = verdict + totalRow + checks;
}

el("btn-speak-report").addEventListener("click", () => {
  const checks = document.querySelectorAll("#report .check");
  if (!checks.length) {
    setMsg("migrate-msg", "Run a migration first.", "error");
    return;
  }
  const verdict = document.querySelector("#report .verdict").textContent.trim();
  const details = [...checks].map((node) =>
    `${node.querySelector(".name").textContent}. ${node.querySelector(".detail").textContent}`
  );
  speak([verdict, ...details].join(" "));
});

/* ---------- voice commands (speech-to-text) ---------- */

let mediaRecorder = null;
let recordedChunks = [];
let isRecording = false;

async function startRecording() {
  if (!state.voiceEnabled) {
    el("voice-hint").textContent = "Voice input is disabled (no API key on the server).";
    return;
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    el("voice-hint").textContent = "Your browser doesn't support microphone access.";
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    recordedChunks = [];
    const mimeType = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "";
    mediaRecorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    mediaRecorder.ondataavailable = (event) => {
      if (event.data.size) recordedChunks.push(event.data);
    };
    mediaRecorder.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      handleRecordingComplete();
    };
    mediaRecorder.start();
    isRecording = true;
    el("btn-mic").classList.add("recording");
    el("btn-mic").textContent = "● Release to send";
  } catch (error) {
    el("voice-hint").textContent = `Microphone error: ${error.message}`;
  }
}

function stopRecording() {
  if (!isRecording || !mediaRecorder) return;
  isRecording = false;
  el("btn-mic").classList.remove("recording");
  el("btn-mic").textContent = "🎤 Talk";
  mediaRecorder.stop();
}

async function handleRecordingComplete() {
  const blob = new Blob(recordedChunks, { type: "audio/webm" });
  if (!blob.size) {
    el("voice-hint").textContent = "Nothing was recorded.";
    return;
  }
  el("voice-hint").textContent = "Listening…";
  const base64 = arrayBufferToBase64(await blob.arrayBuffer());

  try {
    const result = remember(
      await post("/api/voice/command", { audio: base64, filename: "clip.webm" })
    );
    el("voice-hint").textContent = `"${result.transcript}"`;
    if (result.reply) {
      speak(result.reply);
      applyCommandSideEffects(result);
    }
  } catch (error) {
    el("voice-hint").textContent = `Voice command failed: ${error.message}`;
  }
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

function applyCommandSideEffects(result) {
  if (result.intent === "download_mappings") {
    downloadCsv("/api/export/mappings.csv", "account_mappings.csv");
  } else if (result.intent === "download_migrated") {
    downloadCsv("/api/export/migrated.csv", "migrated_transactions.csv");
  } else if (["approve_top", "approve", "map", "clear"].includes(result.intent)) {
    refreshMappings();
  } else if (result.intent === "migrate" || result.intent === "validate") {
    refreshMappings();
    showStep(4);
  }
}

el("btn-mic").addEventListener("mousedown", startRecording);
el("btn-mic").addEventListener("mouseup", stopRecording);
el("btn-mic").addEventListener("touchstart", (e) => { e.preventDefault(); startRecording(); });
el("btn-mic").addEventListener("touchend", stopRecording);

/* ---------- folder tracking ---------- */

function refreshFolderHint() {
  if (state.session && state.session.last_folder) {
    el("folder-hint").textContent = `Last folder: ${state.session.last_folder}`;
  }
}

/* ---------- wiring ---------- */

el("btn-load").addEventListener("click", loadCharts);
el("btn-samples").addEventListener("click", loadSamples);

refreshVoiceStatus();
refreshFolderHint();
showStep(1);
