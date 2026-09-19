/* Frontend for the ERP migration assistant.
 * Plain ES modules-free JS: no build step, so the served file is the source.
 * Every request goes through `api()` so error handling stays in one place.
 */

const state = {
  mappings: [],
  filter: "all",
  targets: [],
  // Which collection the step-2 stats are currently showing. One of the
  // summary labels ("Source accounts", "Target accounts", ...) or null for the
  // default mappings table.
  view: null,
};

/* ---------- helpers ---------- */

async function api(path, options = {}) {
  const opts = { ...options };
  if (opts.body && !(opts.body instanceof FormData)) {
    opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    opts.body = JSON.stringify(opts.body);
  }
  const response = await fetch(path, opts);
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const payload = await response.json();
      if (payload.detail) detail = payload.detail;
    } catch (_) {
      /* response was not JSON; keep the status text */
    }
    throw new Error(detail);
  }
  const type = response.headers.get("content-type") || "";
  return type.includes("application/json") ? response.json() : response;
}

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

let voiceEnabled = false;

async function refreshVoiceStatus() {
  try {
    const status = await api("/api/voice/status");
    voiceEnabled = status.enabled;
    const node = el("voice-status");
    if (status.enabled) {
      node.className = "voice-status on";
      node.textContent =
        `ElevenLabs ready · voice ${status.voice_id} · model ${status.model_id} · ` +
        `${status.cached_clips} cached clip(s)`;
    } else {
      node.className = "voice-status off";
      node.textContent =
        "ElevenLabs not configured — add ELEVENLABS_API_KEY to .env to enable spoken explanations.";
    }
  } catch (error) {
    el("voice-status").className = "voice-status off";
    el("voice-status").textContent = `Voice status unavailable: ${error.message}`;
  }
}

async function speak(text, sourceNumber) {
  if (!voiceEnabled) {
    setMsg("migrate-msg", "ElevenLabs is not configured. See README for setup.", "error");
    return;
  }
  const body = sourceNumber ? { source_number: sourceNumber } : { text };
  try {
    const response = await api("/api/voice/speak", { method: "POST", body });
    const blob = await response.blob();
    const player = el("player");
    player.src = URL.createObjectURL(blob);
    player.hidden = false;
    await player.play();
    refreshVoiceStatus(); // cached-clip count changed
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
    const summary = await api("/api/load", { method: "POST", body: form });
    setMsg("load-msg", "Loaded and mapped.", "ok");
    renderSummary("summary-1", summary);
    await loadMappings();
    await loadTargets();
    showStep(2);
  } catch (error) {
    setMsg("load-msg", error.message, "error");
  }
}

async function loadSamples() {
  setMsg("load-msg", "Loading sample data…");
  try {
    const summary = await api("/api/load-samples", { method: "POST" });
    setMsg("load-msg", "Sample data loaded.", "ok");
    renderSummary("summary-1", summary);
    await loadMappings();
    await loadTargets();
    showStep(2);
  } catch (error) {
    setMsg("load-msg", error.message, "error");
  }
}

/* The target list is needed for the reviewer's dropdown. Derived from mappings
 * would miss unused accounts, so it comes from the mapping candidates plus the
 * targets already referenced. */
async function loadTargets() {
  const seen = new Map();
  state.mappings.forEach((m) => {
    (m.candidates || []).forEach((c) => {
      seen.set(c.target_number, c.target_name);
    });
    if (m.target_number) seen.set(m.target_number, m.target_name);
  });
  state.targets = [...seen.entries()]
    .map(([number, name]) => ({ number, name }))
    .sort((a, b) => a.number.localeCompare(b.number));
}

/* ---------- summaries ---------- */

// The step-2 stats double as buttons that swap the content below them. `views`
// maps a stat label to a renderer that fills the mappings container.
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
      const extra = interactive ? ` class="stat interactive" data-view="${label}"` : ' class="stat"';
      return `<div${extra}>${label}: <b>${value}</b></div>`;
    })
    .join("");

  if (interactive) {
    el(nodeId).querySelectorAll(".stat[data-view]").forEach((stat) => {
      stat.addEventListener("click", () => showStep2View(stat.dataset.view));
    });
    // Restore the active highlight after a re-render.
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
  const data = await api("/api/source-accounts");
  el("mappings").innerHTML = accountTable(data.accounts);
}

async function renderTargetAccounts() {
  const data = await api("/api/target-accounts");
  el("mappings").innerHTML = accountTable(data.accounts);
}

async function renderTransactions() {
  const data = await api("/api/transactions");
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

async function loadMappings() {
  const data = await api("/api/mappings");
  state.mappings = data.mappings;
  renderSummary("summary-2", data.summary);
  // Re-render whatever view is active, not always the default table. This
  // keeps the user in "needs review" (or any stat view) after accepting a
  // mapping instead of snapping back to "all".
  if (state.view) {
    STEP2_VIEWS[state.view]();
  } else {
    renderMappings();
  }
}

function statusBadge(mapping) {
  if (!mapping.target_number) return '<span class="badge unmapped">unmapped</span>';
  const map = {
    auto: "auto",
    needs_review: "review",
    confirmed: "confirmed",
  };
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
      <td>
        <ul class="reasons">${reasons}</ul>
      </td>
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
    await api("/api/approve-top", {
      method: "POST",
      body: sourceNumber ? { source_number: sourceNumber } : {},
    });
    await loadMappings();
  } catch (error) {
    setMsg("load-msg", error.message, "error");
  }
}

async function confirmMapping(sourceNumber, targetNumber) {
  if (!targetNumber) return;
  try {
    await api("/api/confirm", {
      method: "POST",
      body: { source_number: sourceNumber, target_number: targetNumber },
    });
    await loadMappings();
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
    const summary = await api("/api/confirm/clear", { method: "POST" });
    renderSummary("summary-2", summary);
    await loadMappings();
  } catch (error) {
    setMsg("load-msg", error.message, "error");
  }
});

/* ---------- step 3: migrate ---------- */

el("btn-migrate").addEventListener("click", async () => {
  setMsg("migrate-msg", "Migrating…");
  try {
    const result = await api("/api/migrate", { method: "POST" });
    setMsg(
      "migrate-msg",
      `Migrated ${result.summary.migrated_lines} lines` +
        (result.unmapped_lines ? `, ${result.unmapped_lines} still unmapped.` : "."),
      result.unmapped_lines ? "error" : "ok"
    );
    renderSummary("summary-3", result.summary);
    renderReport(result.report);
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
  if (!voiceEnabled) {
    el("voice-hint").textContent = "Voice input is disabled (no API key).";
    return;
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    el("voice-hint").textContent = "Your browser doesn't support microphone access.";
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    recordedChunks = [];
    // Prefer webm/opus, which Scribe accepts; fall back to whatever is offered.
    const mimeType = MediaRecorder.isTypeSupported("audio/webm")
      ? "audio/webm"
      : "";
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

  // The transcript is sent as JSON; base64 keeps it a simple string.
  const bytes = await blob.arrayBuffer();
  const base64 = arrayBufferToBase64(bytes);

  try {
    const result = await api("/api/voice/command", {
      method: "POST",
      body: { audio: base64, filename: "clip.webm" },
    });
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

/* After a spoken command, refresh the UI the command may have changed, and if
 * the command was "download", trigger the browser download. */
function applyCommandSideEffects(result) {
  if (result.intent === "download_mappings") {
    window.location.href = "/api/export/mappings.csv";
  } else if (result.intent === "download_migrated") {
    window.location.href = "/api/export/migrated.csv";
  } else if (
    ["approve_top", "approve", "map", "clear", "migrate", "validate"].includes(
      result.intent
    )
  ) {
    loadMappings();
    if (result.intent === "migrate" || result.intent === "validate") {
      api("/api/report").then((report) => renderReport(report)).catch(() => {});
    }
  }
}

el("btn-mic").addEventListener("mousedown", startRecording);
el("btn-mic").addEventListener("mouseup", stopRecording);
el("btn-mic").addEventListener("touchstart", (e) => { e.preventDefault(); startRecording(); });
el("btn-mic").addEventListener("touchend", stopRecording);

/* ---------- folder tracking ---------- */

async function refreshFolderHint() {
  try {
    const { folder } = await api("/api/folder");
    if (folder) {
      el("folder-hint").textContent = `Last folder: ${folder}`;
    }
  } catch (_) {
    /* folder tracking is best-effort */
  }
}

/* ---------- wiring ---------- */

el("btn-load").addEventListener("click", loadCharts);
el("btn-samples").addEventListener("click", loadSamples);

/* Uploaded file inputs and downloads update the "last used folder" suggestion,
 * but browsers do not expose the full path of a file input for privacy. We can
 * only infer it on the download side, so we surface what the server remembers
 * and let the user set it explicitly via the API if they want. */
refreshFolderHint();
refreshVoiceStatus();
showStep(1);
