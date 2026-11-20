/* ExitPilot web app. Plain JavaScript, no build step. Talks to exitpilot/webapp.py.
   Same tabs and content as the Streamlit app (app.py): Report, Market study, Dependency map, Inventory,
   Common JSON, and Phase 2 below them. What the AI wrote is highlighted.
   Everything that comes from the analysed files or from the AI is escaped before it is shown. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n, d = 0) => (n === null || n === undefined ? "n/a" : Number(n).toFixed(d));
const show = (v) => (v === null || v === undefined || v === "" ? "-" : v);

const state = { status: null, example: null, files: [], useAi: true, data: null, market: null, migration: null, showSupport: false, mapObj: null };

const AI_ICON = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0l1.6 4.4L14 6 9.6 7.6 8 12 6.4 7.6 2 6l4.4-1.6zM13 10l.8 2.2L16 13l-2.2.8L13 16l-.8-2.2L10 13l2.2-.8z"/></svg>';
const aiBadge = (text = "Written by AI") => `<span class="badge-ai">${AI_ICON}${esc(text)}</span>`;
const plainBadge = (text) => `<span class="badge-plain">${esc(text)}</span>`;

// ------------------------------------------------------------------ API
async function api(path, options = {}) {
  const res = await fetch(path, options);
  const isJson = (res.headers.get("Content-Type") || "").includes("application/json");
  const body = isJson ? await res.json() : null;
  if (!res.ok) throw new Error((body && body.error) || `${res.status} ${res.statusText}`);
  return body;
}
const postJson = (path, payload) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });

function toast(message, isError = false) {
  const t = $("#toast");
  t.textContent = message;
  t.classList.toggle("error", isError);
  t.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.add("hidden"), isError ? 7000 : 3500);
}

// ------------------------------------------------------------------ loading overlay
const loading = {
  timer: null, start: 0, stepTimer: null,
  show(title, steps, note = "") {
    $("#loading-title").textContent = title;
    $("#loading-note").textContent = note;
    $("#loading-steps").innerHTML = steps.map((s) => `<li>${esc(s)}</li>`).join("");
    $("#loading").classList.remove("hidden");
    this.start = Date.now();
    const items = $$("#loading-steps li");
    let i = 0;
    const advance = () => { items.forEach((li, k) => { li.className = k < i ? "done" : k === i ? "now" : ""; }); };
    advance();
    this.stepTimer = setInterval(() => { if (i < items.length - 1) { i += 1; advance(); } }, 1400);
    this.timer = setInterval(() => {
      const s = Math.round((Date.now() - this.start) / 1000);
      $("#loading-elapsed").textContent = s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
    }, 500);
  },
  hide() {
    clearInterval(this.timer); clearInterval(this.stepTimer);
    $("#loading").classList.add("hidden");
    $("#loading-elapsed").textContent = "0 s";
  },
};

// ------------------------------------------------------------------ home
async function loadStatus() {
  try {
    state.status = await api("/api/status");
  } catch (e) {
    toast("Could not reach the ExitPilot server: " + e.message, true);
    return;
  }
  const llm = state.status.llm;
  const pill = $("#ai-status");
  pill.classList.toggle("online", llm.online);
  $(".label", pill).textContent = llm.online ? `AI: ${llm.label}${llm.model ? " · " + llm.model : ""}` : (llm.available ? "AI: cached answers only" : "AI offline");
  pill.title = llm.status;
  $("#ai-toggle-model").textContent = llm.model ? `(${llm.label} · ${llm.model})` : `(${llm.label})`;
  $("#ai-toggle-status").textContent = llm.status;
  if (!llm.available) { $("#use-ai").checked = false; }

  const box = $("#examples");
  box.innerHTML = state.status.examples.map((ex) => `
    <button type="button" class="example" role="radio" aria-checked="false" data-name="${esc(ex.name)}">
      <b>${esc(ex.name)}</b><span>${esc(ex.description || "Example project")}</span>
    </button>`).join("") || `<p class="muted small">No example projects in this installation. Upload files instead.</p>`;
  $$(".example", box).forEach((b) => b.addEventListener("click", () => selectExample(b.dataset.name)));
  const preferred = state.status.examples.find((e) => e.name === "loja-mista") || state.status.examples[0];
  if (preferred) selectExample(preferred.name);
}

function selectExample(name) {
  state.example = name; state.files = [];
  $$(".example").forEach((b) => { const on = b.dataset.name === name; b.classList.toggle("selected", on); b.setAttribute("aria-checked", on); });
  $("#dropzone").classList.remove("selected");
  $("#dropzone-text").innerHTML = "<b>Drop a .zip</b> (best) or single files: Terraform, docker-compose, .env, requirements, code";
  updateStart();
}

function selectFiles(fileList) {
  const files = [...(fileList || [])];
  if (!files.length) return;
  state.files = files; state.example = null;
  $$(".example").forEach((b) => { b.classList.remove("selected"); b.setAttribute("aria-checked", "false"); });
  $("#dropzone").classList.add("selected");
  const size = files.reduce((a, f) => a + f.size, 0);
  $("#dropzone-text").innerHTML = `<b>${esc(files.length === 1 ? files[0].name : files.length + " files")}</b> · ${(size / 1e6).toFixed(1)} MB`;
  updateStart();
}

function updateStart() {
  const ready = Boolean(state.example || state.files.length);
  $("#analyse").disabled = !ready;
  $("#start-hint").textContent = ready ? (state.files.length ? "Your files are analysed on this machine." : "") : "Choose an example or upload files first.";
}

const toBase64 = (buf) => { let s = ""; const b = new Uint8Array(buf); for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode(...b.subarray(i, i + 0x8000)); return btoa(s); };

async function analyse() {
  const useAi = $("#use-ai").checked;
  state.useAi = useAi;
  const steps = ["Reading Terraform", "Finding third-party services", "Classifying and scoring", "Checking security and waste"];
  if (useAi) steps.push("AI is analysing each dependency", "AI is writing the report");
  const what = state.files.length ? (state.files.length === 1 ? state.files[0].name : `${state.files.length} files`) : state.example;
  loading.show("Reading the infrastructure and writing the report", steps,
    useAi ? "With a local model this can take a few minutes. Cached projects are instant." : "");
  try {
    let data;
    if (state.files.length) {
      const files = await Promise.all(state.files.map(async (f) => ({ name: f.name, data: toBase64(await f.arrayBuffer()) })));
      data = await api(`/api/analyse?ai=${useAi ? 1 : 0}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ files }) });
    } else {
      data = await api(`/api/analyse?ai=${useAi ? 1 : 0}&example=${encodeURIComponent(state.example)}`, { method: "POST" });
    }
    state.data = data; state.market = data.market; state.migration = null; state.showSupport = false;
    renderResults();
    showView("results");
  } catch (e) {
    toast(`Analysis of ${what} failed: ${e.message}`, true);
  } finally {
    loading.hide();
  }
}

function showView(name) {
  $("#home").classList.toggle("hidden", name !== "home");
  $("#results").classList.toggle("hidden", name !== "results");
  $("#new-analysis").classList.toggle("hidden", name !== "results");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// ------------------------------------------------------------------ small UI pieces
const metric = (labelText, value, help = "", extra = "") =>
  `<div class="metric" ${help ? `title="${esc(help)}"` : ""}><div class="k-label">${esc(labelText)}</div><div class="k-value">${value}</div>${extra}</div>`;
const delta = (n, inverse = false) => {
  if (n === null || n === undefined || n === 0) return '<div class="delta">± 0</div>';
  const good = inverse ? n < 0 : n > 0;
  return `<div class="delta ${good ? "up" : "down"}">${n > 0 ? "▲" : "▼"} ${Math.abs(n)}</div>`;
};
function table(rows, columns, opts = {}) {
  if (!rows || !rows.length) return `<div class="empty">${esc(opts.empty || "Nothing to show.")}</div>`;
  const cols = columns || Object.keys(rows[0]).map((k) => [k, k]);
  return `<div class="tbl-wrap"><table class="tbl"><thead><tr>${cols.map(([, h]) => `<th>${esc(h)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${cols.map(([k]) => `<td>${opts.render && opts.render[k] ? opts.render[k](r[k], r) : esc(show(r[k]))}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}
const lvlCell = (n) => `<span class="lvl ${n === null || n === undefined ? "lvl-x" : "lvl-" + n}">${n ?? "?"}</span>`;
function zonePill(zone) {
  if (zone === "EU") return '<span class="pill good">EU</span>';
  if (zone === "Outside Europe") return '<span class="pill bad">Outside Europe</span>';
  if (zone === "Europe (non-EU)") return '<span class="pill warn">Europe (non-EU)</span>';
  return `<span class="pill neutral">${esc(zone || "Unknown")}</span>`;
}

// ------------------------------------------------------------------ Markdown (the report), with AI sections highlighted
function inline(text) {
  let s = esc(text);
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  s = s.replace(/(^|[\s(])_([^_]+)_(?=[\s).,:;]|$)/g, "$1<i>$2</i>");
  s = s.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return s;
}
function markdownBlocks(md) {
  const lines = md.split("\n"); const out = []; let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^#{1,4} /.test(line)) { const level = line.match(/^#+/)[0].length; out.push({ type: "h", level, text: line.slice(level + 1) }); i++; continue; }
    if (/^---+\s*$/.test(line)) { out.push({ type: "hr" }); i++; continue; }
    if (line.trim().startsWith("|")) {
      const rows = []; while (i < lines.length && lines[i].trim().startsWith("|")) { rows.push(lines[i]); i++; }
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
      const body = rows.filter((r, k) => !(k === 1 && /^[\s|:-]+$/.test(r)));
      out.push({ type: "table", head: cells(body[0]), rows: body.slice(1).map(cells) }); continue;
    }
    if (/^\s*[-*] /.test(line)) { const items = []; while (i < lines.length && /^\s*[-*] /.test(lines[i])) { items.push(lines[i].replace(/^\s*[-*] /, "")); i++; } out.push({ type: "ul", items }); continue; }
    if (/^\s*\d+\. /.test(line)) { const items = []; while (i < lines.length && /^\s*\d+\. /.test(lines[i])) { items.push(lines[i].replace(/^\s*\d+\. /, "")); i++; } out.push({ type: "ol", items }); continue; }
    if (!line.trim()) { i++; continue; }
    const para = []; while (i < lines.length && lines[i].trim() && !/^(#{1,4} |\||\s*[-*] |\s*\d+\. |---)/.test(lines[i])) { para.push(lines[i]); i++; }
    out.push({ type: "p", text: para.join(" ") });
  }
  return out;
}
function blockHtml(b) {
  switch (b.type) {
    case "h": return `<h${Math.min(b.level + 1, 4)}>${inline(b.text)}</h${Math.min(b.level + 1, 4)}>`;
    case "hr": return "<hr>";
    case "ul": return `<ul>${b.items.map((x) => `<li>${inline(x)}</li>`).join("")}</ul>`;
    case "ol": return `<ol>${b.items.map((x) => `<li>${inline(x)}</li>`).join("")}</ol>`;
    case "table": return `<div class="tbl-wrap"><table class="tbl"><thead><tr>${b.head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${b.rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
    default: return `<p>${inline(b.text)}</p>`;
  }
}
function isAiSection(title, aiText) {
  if (/\bAI\b/.test(title)) return true;                                  // "AI analysis...", "suggested by the AI"
  return aiText && /^(1\. Executive summary|3\. Biggest lock-in points|6\. Next steps)/.test(title);
}
function renderMarkdown(md, aiText) {
  const blocks = markdownBlocks(md.replace(/^# .*\n/, ""));          // the H1 title is already at the top
  const sections = []; let cur = { title: "", level: 0, blocks: [] };
  for (const b of blocks) {
    if (b.type === "h" && b.level <= 3) { sections.push(cur); cur = { title: b.text, level: b.level, blocks: [b] }; }
    else cur.blocks.push(b);
  }
  sections.push(cur);
  return sections.filter((s) => s.blocks.length).map((s) => {
    const ai = isAiSection(s.title, aiText);
    return `<section class="md-sec ${ai ? "ai" : ""}">${ai ? `<div class="md-badge">${aiBadge("Written by AI")}</div>` : ""}${s.blocks.map(blockHtml).join("")}</section>`;
  }).join("");
}

// ------------------------------------------------------------------ results: header
function renderResults() {
  const d = state.data, h = d.header, e = d.exposure || {};
  $("#r-title").textContent = `Lock-in report: ${d.project}`;
  $("#r-eyebrow").textContent = d.ai ? `AI by ${d.ai.provider}${d.ai.model ? " · " + d.ai.model : ""}` : "Without AI";

  $("#metrics").innerHTML =
    metric("Sovereignty score", h.score_rounded === null ? "n/a" : `${h.score_rounded}<span class="of">/100</span>`, "S = 100 x (1 - N / (3 x R)). 100 = no lock-in.") +
    metric("Lock-in", esc(h.lock_in)) +
    metric("Controlled outside the EU", e.dependencies ? `${e.controlled_outside_eu}<span class="of">/${e.dependencies}</span>` : "n/a",
      "Dependencies controlled by a company headquartered outside the EU, wherever its servers are.") +
    metric("Main resources", esc(h.main_resources), "Resources and services that enter the score (support configuration is left out).") +
    metric("Not fully analysed", esc(h.not_fully_analysed));
  $("#by-provider").innerHTML = (h.by_provider || []).map((p) => metric(p.provider, p.score === null ? "n/a" : `${p.score}<span class="of">/100</span>`)).join("");
  $("#by-provider").classList.toggle("hidden", !(h.by_provider || []).length);

  const sec = d.security || [], waste = d.suspected_waste || [];
  const crit = sec.filter((f) => f.severity === "critical").length;
  $("#alerts").innerHTML =
    (sec.length ? `<div class="alert bad">🔓 <b>${sec.length} security problem(s)</b>${crit ? ` (${crit} critical)` : ""}: ${esc(sec.slice(0, 3).map((f) => f.title).join("; "))}${sec.length > 3 ? " ..." : ""}. See <i>Security</i> in the report: the migration plan lists them so they are not carried over.</div>` : "") +
    (waste.length ? `<div class="alert info">🧹 <b>${waste.length} suspected waste item(s)</b> look unused (${h.waste_left_out} can be left out of the migration). See <i>Suspected waste</i> in the report or the Inventory tab. Nothing is deleted automatically.</div>` : "");

  const r = d.report;
  const box = $("#summary");
  box.className = "card summary " + (r.ai_text ? "ai" : "");
  box.innerHTML = `<div class="summary-head"><h2 style="margin:0">Executive summary</h2>${r.ai_text ? aiBadge("Written by AI") : plainBadge("Template text (AI not used)")}</div>
    <p class="summary-text">${esc(r.executive_summary || "")}</p>`;
  $("#ai-caption").textContent = r.ai_text
    ? `🤖 Report text written by AI (${d.ai ? d.ai.provider : "AI"}), numbers computed by ExitPilot. Sections written by the AI are highlighted.`
    : "Report text from template (AI not used). All numbers computed by ExitPilot.";

  renderReport(); renderMarket(); renderMap(); renderInventory(); renderJson(); renderPhase2();
  selectTab("report");
}

// ------------------------------------------------------------------ Report tab
function renderReport() {
  const r = state.data.report;
  $("#tab-report").innerHTML = `<div class="md">${renderMarkdown(r.markdown || "", r.ai_text)}</div>
    <button class="ghost" type="button" data-download="report.md">Download report (.md)</button>`;
  bindDownloads($("#tab-report"));
}

// ------------------------------------------------------------------ Market study tab
function renderMarket() {
  const m = state.market, d = state.data;
  if (!m) { $("#tab-market").innerHTML = '<div class="empty">No market study.</div>'; return; }
  const cities = state.status?.cities || [m.city];
  const w = m.weights;
  const names = { price: "Price", proximity: "Proximity", coverage: "Managed services", automation: "Automated migration" };
  const helpAuto = "Weight for providers ExitPilot already generates and checks the migration for (phase 2: today Leaseweb). A ready, validated migration is cheaper and less risky. Set it to 0 for a study that ignores ExitPilot's own automation.";
  const hasWaste = (d.suspected_waste || []).length > 0;
  const recAi = m.recommendation_source === "ai";
  const canAsk = state.useAi && state.status?.llm?.available;
  const b = m.best_offer, c = m.cheapest, cl = m.closest;
  const skip = state.marketSkip ?? true;

  $("#tab-market").innerHTML = `
    <h2>Where to go: European providers for this infrastructure</h2>
    <p class="muted small">Compares European providers on price (cheapest plan that fits each machine), distance from your company to their nearest EU data centre, and the managed services your infrastructure uses. Indicative public list prices, EUR excl. VAT, compute only.</p>
    <div class="controls card">
      <div>
        <label for="mk-city">Your company is in</label>
        <select id="mk-city">${cities.map((x) => `<option ${x === m.city ? "selected" : ""}>${esc(x)}</option>`).join("")}</select>
        <label class="check"><input type="checkbox" id="mk-basic" ${m.include_basic ? "checked" : ""}> Include test/dev plans (no fixed SLA)</label>
        <label class="check"><input type="checkbox" id="mk-waste" ${skip ? "checked" : ""} ${hasWaste ? "" : "disabled"}> Leave suspected waste out</label>
      </div>
      <div class="weights">${Object.keys(names).map((k) => `
        <div ${k === "automation" ? `title="${esc(helpAuto)}"` : ""}><label for="w-${k}">${names[k]} <output id="o-${k}">${fmt(w[k])}</output></label>
        <input type="range" min="0" max="100" step="1" id="w-${k}" data-weight="${k}" value="${w[k]}"></div>`).join("")}</div>
    </div>

    ${canAsk && !recAi ? '<button class="ghost" type="button" id="mk-ai">Ask the AI to explain the choice</button>' : ""}
    <article class="alert ${m.best ? "good" : "warn"} ${recAi ? "ai" : ""}" style="margin-top:12px">
      ${recAi ? `<div style="margin-bottom:6px">${aiBadge("Written by AI")}</div>` : ""}${esc(m.recommendation)}
    </article>

    <div class="metrics three">
      ${metric("Best overall" + (b ? ` (score ${fmt(b.score, 1)})` : ""), esc(b ? b.name : "n/a"))}
      ${metric("Cheapest" + (c ? ` (EUR ${fmt(c.monthly_eur, 2)}/month)` : ""), esc(c ? c.name : "n/a"))}
      ${metric("Closest" + (cl ? ` (${fmt(cl.distance_km)} km, ${cl.nearest_dc})` : ""), esc(cl ? cl.name : "n/a"))}
    </div>

    ${table(m.offers.map((o) => ({
      provider: `${o.name} (${o.hq})`, score: o.score, eur: o.monthly_eur, dc: o.nearest_dc, km: o.distance_km, rtt: o.rtt_ms,
      missing: (o.missing_labels || []).join(", ") || "-", orderable: o.orderable ? "yes" : "no", tf: o.terraform || "-",
      gen: o.phase2_automated ? "yes" : "no", _best: o.provider === m.best,
    })), [["provider", "provider"], ["score", "score"], ["eur", "EUR/month"], ["dc", "nearest EU data centre"], ["km", "km"],
         ["rtt", "min. round trip (ms)"], ["missing", "managed services missing"], ["orderable", "orderable today"],
         ["tf", "Terraform provider"], ["gen", "ExitPilot generates Terraform"]],
       { render: { provider: (v, r) => `${esc(v)}${r._best ? ' <span class="pill good">recommended</span>' : ""}`, eur: (v) => (v === null ? "n/a" : fmt(v, 2)) } })}
    <p class="muted small">score = 100 x (wp x P + wd x D + wc x C + wa x A) / (wp + wd + wc + wa), where P = cheapest cost / this cost, D = (nearest km + 50) / (this km + 50), C = managed services covered / needed, A = 1 if ExitPilot already generates its Terraform, else 0. Providers whose plans cannot be ordered today are listed but never recommended. Round trip: theoretical minimum over fibre (about 1 ms per 100 km).</p>

    <details class="expander"><summary>Machines to host (${m.machines.length}) and the plan chosen at each provider</summary>
      ${table(m.machines.map((x) => ({ machine: x.source, vcpu: x.vcpu, ram: x.ram_gb, known: x.size_known ? "yes" : "no (assumed 2 vCPU / 4 GB)" })),
        [["machine", "machine"], ["vcpu", "vCPU"], ["ram", "RAM (GB)"], ["known", "size known"]])}
      ${table(m.offers.flatMap((o) => (o.lines || []).map((ln) => ({ provider: o.name, machine: ln.source, plan: ln.plan, vcpu: ln.vcpu, ram: ln.ram_gb, eur: ln.eur_month }))),
        [["provider", "provider"], ["machine", "machine"], ["plan", "plan"], ["vcpu", "vCPU"], ["ram", "RAM (GB)"], ["eur", "EUR/month"]])}
    </details>

    <p style="margin-top:18px"><b>Today vs. the European options</b> <span class="muted small">(<span style="color:#c62828">●</span> red: where your infrastructure runs today, <span style="color:#1e88e5">●</span> blue: your company, <span style="color:#2e7d32">●</span> green: recommended provider, <span style="color:#9e9e9e">●</span> grey: other providers)</span></p>
    <div id="mk-map" class="map"></div>

    <details class="expander"><summary>Notes and sources (checked ${esc(m.checked)})</summary>
      <ul>${m.offers.map((o) => `<li><b>${esc(o.name)}</b>: ${esc(o.note)} ${o.source ? `<a href="${esc(o.source)}" target="_blank" rel="noopener noreferrer">Price list</a>` : ""}</li>`).join("")}</ul>
    </details>
    <button class="ghost" type="button" data-download="market.md">Download the market study (.md)</button>`;

  drawMap(m.map || []);
  const refresh = () => { clearTimeout(renderMarket.t); renderMarket.t = setTimeout(() => updateMarket(false), 250); };
  $("#mk-city").addEventListener("change", refresh);
  $("#mk-basic").addEventListener("change", refresh);
  $("#mk-waste").addEventListener("change", () => { state.marketSkip = $("#mk-waste").checked; refresh(); });
  $$("input[data-weight]").forEach((inp) => inp.addEventListener("input", () => { $(`#o-${inp.dataset.weight}`).textContent = inp.value; refresh(); }));
  const ask = $("#mk-ai");
  if (ask) ask.addEventListener("click", () => updateMarket(true));
  bindDownloads($("#tab-market"));
}

function drawMap(points) {
  const box = $("#mk-map");
  if (!box) return;
  if (typeof L === "undefined") { box.innerHTML = '<p class="muted small" style="padding:16px">Map not available (no internet to load the map library). ' + esc(points.map((p) => p.label).join(" · ")) + "</p>"; return; }
  if (state.mapObj) { state.mapObj.remove(); state.mapObj = null; }
  const map = L.map(box, { scrollWheelZoom: false, attributionControl: true });
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors', maxZoom: 12,
  }).addTo(map);
  const layers = points.map((p) => L.circleMarker([p.lat, p.lon], {
    radius: Math.max(5, Math.round(Math.sqrt(p.size) / 22)), color: p.color, fillColor: p.color, fillOpacity: .8, weight: 1.5,
  }).bindTooltip(esc(p.label)).addTo(map));
  state.mapObj = map;
  state.mapBounds = layers.length ? L.featureGroup(layers).getBounds().pad(0.25) : null;
  fitMap();
}

function fitMap() {
  // The map is drawn while its tab may be hidden (size 0): fit it again whenever it becomes visible.
  const map = state.mapObj;
  if (!map) return;
  setTimeout(() => {
    map.invalidateSize();
    if (state.mapBounds) map.fitBounds(state.mapBounds, { maxZoom: 6 }); else map.setView([50, 5], 4);
  }, 60);
}

async function updateMarket(askAi) {
  const weights = Object.fromEntries($$("input[data-weight]").map((i) => [i.dataset.weight, Number(i.value)]));
  const payload = { id: state.data.id, city: $("#mk-city").value, weights, include_basic: $("#mk-basic").checked, skip_waste: $("#mk-waste").checked };
  if (askAi) loading.show("The AI is writing the recommendation", ["Comparing providers", "Writing the recommendation", "Checking the answer"]);
  try {
    const m = await postJson(askAi ? "/api/market/ai" : "/api/market", payload);
    state.market = m;
    renderMarket();
    if (askAi && m.ai_rejected) toast("The AI answer did not pass validation (wrong provider or numbers that are not in the facts). Showing the recommendation computed by ExitPilot.", true);
  } catch (e) {
    toast(e.message, true);
  } finally {
    if (askAi) loading.hide();
  }
}

// ------------------------------------------------------------------ Dependency map tab
async function renderMap() {
  const d = state.data;
  const legend = (d.legend || []).map(([c, t]) => `<span><span style="color:${esc(c)};font-size:1.2em">■</span> ${esc(t)}</span>`).join("");
  $("#tab-map").innerHTML = `
    <label class="check"><input type="checkbox" id="map-support" ${state.showSupport ? "checked" : ""}> Show support configuration (network, permissions...)</label>
    <div class="graph" id="graph"><p class="muted small">Drawing the map…</p></div>
    <div class="legend">${legend}</div>
    <p class="muted small">Arrows: direct dependency. Dashed lines: linked through support configuration.</p>`;
  $("#map-support").addEventListener("change", (e) => { state.showSupport = e.target.checked; renderMap(); });
  const dot = state.showSupport ? d.dot.full : d.dot.main;
  const box = $("#graph");
  try {
    if (typeof Viz === "undefined") throw new Error("graph library not loaded (no internet?)");
    const viz = await Viz.instance();
    const svg = viz.renderSVGElement(dot.replace('bgcolor="transparent"', 'bgcolor="transparent" color="#778DA9" fontcolor="#E0E1DD"')
      .replace(/node \[/, 'edge [color="#778DA9"]; node ['));
    box.innerHTML = ""; box.appendChild(svg);
  } catch (e) {
    box.innerHTML = `<p class="muted small">Map not available (${esc(e.message)}). Graph source:</p><pre class="code">${esc(dot)}</pre>`;
  }
}

// ------------------------------------------------------------------ Inventory tab
function renderInventory() {
  const d = state.data, inv = d.inventory, sec = d.security || [], waste = d.suspected_waste || [];
  const lvlR = { level: (v) => lvlCell(v) };
  $("#tab-inventory").innerHTML = `
    <h3>Main resources</h3>
    ${table(inv.main, [["resource", "resource"], ["provider", "provider"], ["category", "category"], ["level", "level"], ["confidence", "confidence"], ["classified_by", "classified by"], ["region", "region"], ["file", "file"]], { render: lvlR })}
    ${inv.services.length ? `<h3>Third-party services the application uses</h3>
      <p class="muted small">Zone = where the company is headquartered. Evidence = where we found it (config, code, dependency, image or observed traffic). Secret values are never stored.</p>
      ${table(inv.services, [["service", "service"], ["company", "company"], ["country", "headquarters"], ["zone", "zone"], ["category", "category"], ["level", "level"], ["verified", "verified"], ["evidence", "evidence"]],
        { render: { ...lvlR, zone: (v) => zonePill(v), verified: (v) => (v ? "yes" : '<span class="pill warn">no</span>') } })}` : ""}
    ${sec.length ? `<h3>Security: exposed to the internet</h3>
      ${table(sec, [["severity", "severity"], ["address", "item"], ["title", "problem"], ["detail", "detail"], ["fix", "fix"], ["file", "file"]],
        { render: { severity: (v) => `<span class="pill ${v === "critical" ? "bad" : "warn"}">${esc(v)}</span>` } })}` : ""}
    ${waste.length ? `<h3>Suspected waste (review before deleting)</h3>
      ${table(waste.map((w) => ({ item: w.address, what: w.title, why: w.reason, confidence: w.confidence,
        migration: w.exclude_from_migration ? "left out" : "clean up only",
        where: (w.evidence || []).slice(0, 3).map((e) => e.file + (e.line ? ":" + e.line : "")).join("; "), ai: w.ai })),
        [["item", "item"], ["what", "what"], ["why", "why"], ["confidence", "confidence"], ["migration", "migration"], ["where", "where"]])}` : ""}
    <h3>Support configuration and unclassified</h3>
    ${table(inv.other, [["resource", "resource"], ["category", "category"], ["level", "level"], ["note", "note"], ["file", "file"]], { render: lvlR })}`;
}

// ------------------------------------------------------------------ Common JSON tab
function renderJson() {
  $("#tab-json").innerHTML = `
    <p class="muted small">The common representation every input format is converted into (input for phase 2).</p>
    <pre class="code" style="max-height:520px">${esc(JSON.stringify(state.data.ir, null, 2))}</pre>
    <div class="row-actions"><button class="ghost" type="button" data-download="infrastructure.json">Download JSON</button></div>
    <p class="muted small">analysis.json is the complete file: facts, the AI's analysis of each dependency, the report and (if you generated it below) the migration plan.</p>
    <button class="ghost" type="button" data-download="analysis.json">Download analysis.json (complete)</button>`;
  bindDownloads($("#tab-json"));
}

// ------------------------------------------------------------------ Phase 2 (below the tabs, as in the Streamlit app)
function renderPhase2() {
  const box = $("#phase2"), m = state.migration;
  const hasWaste = (state.data.suspected_waste || []).length > 0;
  let html = `
    <h2>Phase 2: migration plan to Leaseweb</h2>
    <p class="muted small">Chooses a European target for every dependency, orders the work in waves, generates the Terraform, cloud-init, docker-compose and environment files, and projects the result. Nothing is applied: it is a proposal to review.</p>
    <label class="check" title="Unused disks, IPs, SDKs... are listed for clean-up instead of being moved."><input type="checkbox" id="mg-waste" checked ${hasWaste ? "" : "disabled"}> Leave suspected waste out of the migration</label>
    <button class="primary" type="button" id="mg-go">Generate migration plan</button>`;
  if (m) {
    const b = m.before, a = m.after;
    const steps = m.steps.filter((s) => s.kind !== "keep" && s.kind !== "drop").length;
    const lv = (v) => (v === null || v === undefined ? "?" : v);
    const rows = (list) => table(list.map((s) => ({ today: s.today, target: s.target, level: `${lv(s.level_before)} -> ${lv(s.level_after)}`, generated: (s.generated || []).join(", ") || "-", actions: s.actions || [] })),
      [["today", "today"], ["target", "target"], ["level", "level"], ["generated", "generated"], ["actions", "manual work"]],
      { render: { actions: (acts) => (acts.length ? `<ul class="acts">${acts.map((x) => x.startsWith("[AI]") ? `<li class="ai-line">${esc(x.slice(4).trim())}</li>` : `<li>${esc(x)}</li>`).join("")}</ul>` : "-") } });
    const decisions = m.steps.filter((s) => s.kind === "decision");
    const dropped = m.steps.filter((s) => s.kind === "drop");
    const kept = m.steps.filter((s) => s.kind === "keep").map((s) => s.today);
    html += `
      <div class="metrics three" style="margin-top:18px">
        ${metric("Sovereignty score after the plan", a.score.value === null ? "n/a" : `${Math.round(a.score.value)}<span class="of">/100</span>`, "", delta(a.score.value === null || b.score.value === null ? null : Math.round(a.score.value) - Math.round(b.score.value)))}
        ${metric("Controlled outside the EU after the plan", `${a.exposure.controlled_outside_eu}<span class="of">/${a.exposure.dependencies}</span>`, "", delta(a.exposure.controlled_outside_eu - b.exposure.controlled_outside_eu, true))}
        ${metric("Steps", steps, "Steps that need work (already European dependencies are kept).")}
      </div>
      <p class="muted small">Projected values: they assume every step is done. Self-hosting lowers lock-in but adds operations work (updates, backups, monitoring). Lines marked with the AI style were written by the AI.</p>
      ${decisions.length ? `<h3>Decisions needed first</h3>${rows(decisions)}` : ""}
      ${[[1, "Wave 1: quick wins"], [2, "Wave 2: managed services"], [3, "Wave 3: deep changes"]].map(([n, t]) => {
        const list = m.steps.filter((s) => s.wave === n && s.kind !== "decision" && s.kind !== "keep" && s.kind !== "drop");
        return list.length ? `<h3>${t}</h3>${rows(list)}` : "";
      }).join("")}
      ${dropped.length ? `<h3>Not migrated: suspected waste</h3>${table(dropped.map((s) => ({ item: s.today, why: (s.actions || []).join(" ") })), [["item", "item"], ["why", "why"]])}` : ""}
      ${kept.length ? `<p class="muted small">Already European (kept): ${esc(kept.join("; "))}</p>` : ""}
      <h3>Checks</h3>
      <div class="checks">${m.checks.map((c) => `<div>${c.status === "passed" ? "✅" : c.status === "failed" ? "❌" : "⏸️"} <b>${esc(c.name)}</b> (${esc(c.status)}): ${esc(c.detail)}</div>`).join("")}</div>
      <h3>Generated files</h3>
      ${Object.keys(m.files_content || {}).sort().map((f) => `<details class="expander"><summary>${esc(f)}</summary><pre class="code">${esc(m.files_content[f])}</pre></details>`).join("")}
      <button class="ghost" type="button" data-download="migration.zip">Download the migration (.zip)</button>`;
  }
  box.innerHTML = html;
  if (m) $("#mg-waste").checked = m.waste_excluded || !hasWaste ? true : false;
  $("#mg-go").addEventListener("click", generateMigration);
  bindDownloads(box);
}

async function generateMigration() {
  const cb = $("#mg-waste");
  loading.show("Writing the migration plan", ["Choosing European targets", "Ordering the work in waves", "Generating Terraform, cloud-init and docker-compose", "Checking the files"]);
  try {
    state.migration = await postJson("/api/migrate", { id: state.data.id, exclude_waste: cb ? cb.checked : true });
    renderPhase2();
    $("#phase2").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    toast(e.message, true);
  } finally {
    loading.hide();
  }
}

function bindDownloads(root) {
  $$("[data-download]", root).forEach((b) => {
    if (b.dataset.bound) return;
    b.dataset.bound = "1";
    b.addEventListener("click", () => {
      if (!state.data) return;
      window.location.href = `/api/download?id=${encodeURIComponent(state.data.id)}&what=${encodeURIComponent(b.dataset.download)}`;
    });
  });
}

// ------------------------------------------------------------------ tabs and wiring
function selectTab(name) {
  $$("#tabs button").forEach((b) => { const on = b.dataset.tab === name; b.classList.toggle("active", on); b.setAttribute("aria-selected", on); });
  $$(".tab-panel").forEach((p) => p.classList.toggle("hidden", p.id !== "tab-" + name));
  if (name === "market") fitMap();
}

function init() {
  $$("#tabs button").forEach((b) => b.addEventListener("click", () => selectTab(b.dataset.tab)));
  $("#analyse").addEventListener("click", analyse);
  $("#brand").addEventListener("click", () => showView("home"));
  $("#new-analysis").addEventListener("click", () => showView("home"));
  bindDownloads(document);

  const dz = $("#dropzone"), input = $("#file");
  input.addEventListener("change", () => selectFiles(input.files));
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => selectFiles(e.dataTransfer.files));
  loadStatus();
}

document.addEventListener("DOMContentLoaded", init);
