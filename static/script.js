"use strict";

/* =====================================================================
 * Utilities
 * ===================================================================== */
const $ = (id) => document.getElementById(id);
const DAY_MS = 86400000;

async function api(url, opts = {}) {
  const init = { credentials: "same-origin", ...opts };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.json);
  }
  const res = await fetch(url, init);
  let data = null;
  try { data = await res.json(); } catch (_) { /* non-JSON */ }
  if (!res.ok) {
    const err = new Error((data && data.error) || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function titleCase(s) { return String(s || "").replace(/\b[a-z]/g, (c) => c.toUpperCase()); }

function fmtDateTime(iso) {
  if (!iso) return "No date";
  const [d, t] = iso.split("T");
  const [y, m, day] = d.split("-");
  return `${+m}/${+day}/${y}${t ? " " + t.slice(0, 5) : ""}`;
}

function showError(el, msg) { el.textContent = msg; el.classList.toggle("hidden", !msg); }

const SPECIES_PALETTE = ["#d9822b", "#3f8f4f", "#8e44ad", "#c0392b", "#16a085", "#7f6a3a", "#e84393",
  "#2c3e50", "#a0522d", "#6c5ce7", "#00838f", "#b8860b"];
function speciesColor(label) {
  const s = String(label || "blank").toLowerCase();
  if (s === "blank") return "#b9b4a7";
  if (s.includes("scrub-jay") || s.includes("scrub jay")) return "#2f6db5";
  if (s.includes("blue jay")) return "#5dade2";
  let h = 0;
  for (const c of s) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return SPECIES_PALETTE[h % SPECIES_PALETTE.length];
}

const KIND_COLORS = {
  nonfunctional: "#b3412f", download: "#2f6db5", refill: "#3f8f4f", cleaned: "#17a2b8",
  camswitch: "#8e44ad", baffle: "#c78a1c", pipe_feeder: "#a0522d", pipe_campost: "#7f6a3a",
  website_flag: "#e67e22", rfid_issue: "#d35454", feeder_down: "#6d2e2e", camera_issue: "#e0a03b",
  important_date: "#7a5aa6",
};
const SPAN_KINDS = new Set(["nonfunctional", "rfid_issue", "feeder_down", "camera_issue"]);

/* =====================================================================
 * Tabs
 * ===================================================================== */
const tabHandlers = {};
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});
function switchTab(name) {
  document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  history.replaceState(null, "", `#${name}`);
  if (tabHandlers[name]) tabHandlers[name]();
}

document.querySelectorAll(".modal").forEach((m) => {
  m.addEventListener("click", (e) => {
    if (e.target === m || e.target.closest("[data-close]")) m.classList.add("hidden");
  });
});

/* =====================================================================
 * Positions (shared by Library filter, viewer)
 * ===================================================================== */
let positions = [];
async function refreshPositions() {
  positions = await api("/api/positions");
  const opts = positions.map((p) => `<option value="${p}">Position ${p}</option>`).join("");
  const g = $("group-position"); const gcur = g.value;
  g.innerHTML = `<option value="">All</option>${opts}`; g.value = gcur;
  $("v-position").innerHTML = `<option value="">—</option>${opts}`;
}

/* =====================================================================
 * Workspace tab
 * ===================================================================== */
let lastProcessed = null;

function renderWorkspace(ws, { fillPaths = false } = {}) {
  if (fillPaths && ws.folder) {
    $("ws-folder").value = ws.folder;
    $("ws-csv").value = ws.positions_csv;
  }
  $("ws-save").disabled = ws.active;
  const process = $("ws-process");
  if (!ws.folder) return;

  const n = (v) => v.toLocaleString();
  const parts = [`${n(ws.photo_count)} photos in “${ws.folder}”, ${n(ws.with_labels ?? 0)} with Camelot labels.`];
  if (ws.not_in_csv) parts.push(`${n(ws.not_in_csv)} aren't in the position CSV, so they'll have no position and won't appear on the Timeline until you set one in the Library.`);
  $("ws-summary").textContent = parts.join(" ");

  process.classList.toggle("hidden", !ws.pending && !ws.active);
  process.disabled = ws.active;
  const count = `${n(ws.pending)} photo${ws.pending === 1 ? "" : "s"}`;
  process.textContent = ws.active ? "Processing…" : ws.processed ? `Process remaining ${count}` : `Process ${count}`;

  const bar = $("ws-progress");
  const finished = ws.photo_count - ws.pending;
  bar.classList.toggle("hidden", !finished && !ws.active);
  bar.querySelector(".progress-bar").style.width = `${ws.photo_count ? (100 * finished) / ws.photo_count : 0}%`;
  bar.querySelector(".progress-label").textContent = `${n(finished)} of ${n(ws.photo_count)} photos processed`;

  // New results arrive batch by batch during a run that can last days, so the
  // other tabs refresh as they come in rather than only at the end.
  if (lastProcessed !== null && ws.processed !== lastProcessed) {
    libraryDirty = true; timeline.refreshPhotos(); refreshPositions(); loadTaxonomy();
  }
  lastProcessed = ws.processed;
}

async function refreshWorkspace(opts) {
  try { renderWorkspace(await api("/api/workspace"), opts); } catch (_) { /* next poll retries */ }
}

document.querySelectorAll("[data-browse]").forEach((btn) => btn.addEventListener("click", async () => {
  showError($("ws-error"), "");
  btn.disabled = true;
  try {
    const { path } = await api("/api/workspace/browse", { json: { kind: btn.dataset.browse } });
    if (path) $(btn.dataset.target).value = path;
  } catch (err) { showError($("ws-error"), err.message); }
  finally { btn.disabled = false; }
}));
$("ws-save").addEventListener("click", async () => {
  showError($("ws-error"), "");
  $("ws-save").disabled = true;
  try {
    const ws = await api("/api/workspace", { json: { folder: $("ws-folder").value, positions_csv: $("ws-csv").value } });
    renderWorkspace(ws, { fillPaths: true });
  } catch (err) { showError($("ws-error"), err.message); $("ws-save").disabled = false; }
});
$("ws-process").addEventListener("click", async () => {
  showError($("ws-error"), "");
  $("ws-process").disabled = true;
  try {
    await api("/api/workspace/process", { json: {} });
    await Promise.all([refreshWorkspace(), refreshQueue()]);
  } catch (err) { showError($("ws-error"), err.message); $("ws-process").disabled = false; }
});

/* ----- queue ----- */
async function refreshQueue() {
  let jobs;
  try { jobs = await api("/api/queue"); } catch (_) { return; }
  const el = $("queue-list");
  if (!jobs.length) { el.innerHTML = `<p class="muted">Nothing processed yet.</p>`; return; }
  el.innerHTML = jobs.map((j) => `
    <div class="job">
      <div class="job-head">
        <span class="job-name">${esc(j.display_name)}</span>
        <span class="muted small">${(j.done_count ?? 0).toLocaleString()} of ${(j.file_count ?? 0).toLocaleString()} photos · ${esc(fmtDateTime(j.created_at))}</span>
        <span class="status-pill ${esc(j.status)}">${esc(j.status)}</span>
        ${["queued", "running"].includes(j.status) ? `<button class="link-btn" data-cancel="${esc(j.id)}">Cancel</button>` : ""}
        ${["error", "done", "cancelled"].includes(j.status) ? `<button class="link-btn" data-log="${esc(j.id)}">Show log</button>` : ""}
      </div>
      ${j.log_tail ? `<pre>${esc(j.log_tail)}</pre>` : ""}
      <pre class="hidden" id="log-${esc(j.id)}"></pre>
    </div>`).join("");
  el.querySelectorAll("[data-cancel]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Stop processing? Photos already finished are kept, and you can pick up the rest later. Your photo files aren't affected.")) return;
    try { await api(`/api/jobs/${b.dataset.cancel}/cancel`, { method: "POST" }); } catch (e) { alert(e.message); }
    refreshQueue();
  }));
  el.querySelectorAll("[data-log]").forEach((b) => b.addEventListener("click", async () => {
    const pre = $(`log-${b.dataset.log}`);
    if (!pre.classList.contains("hidden")) { pre.classList.add("hidden"); return; }
    const { log } = await api(`/api/jobs/${b.dataset.log}/log`);
    pre.textContent = log || "(empty)"; pre.classList.remove("hidden");
  }));
  hadActiveJobs = jobs.some((j) => ["queued", "running", "cancelling"].includes(j.status));
}
let hadActiveJobs = false;
setInterval(() => {
  if ($("tab-workspace").classList.contains("active") || hadActiveJobs) { refreshQueue(); refreshWorkspace(); }
}, 3000);

/* ----- field log ----- */
async function refreshLogSummary() {
  const { info, kinds } = await api("/api/log");
  timeline.kindLabels = kinds;
  if (!info) { $("log-summary").textContent = "No Log uploaded yet."; return; }
  const counts = Object.entries(info.event_counts || {})
    .map(([k, n]) => `${esc(kinds[k] || k)}: ${n}`).join(" · ");
  $("log-summary").innerHTML = `<dl>
    <dt>File</dt><dd>${esc(info.filename)}</dd>
    <dt>Uploaded</dt><dd>${esc(fmtDateTime(info.uploaded_at))}</dd>
    <dt>Covers</dt><dd>${esc(fmtDateTime(info.first_date))} – ${esc(fmtDateTime(info.last_date))} (${info.rows.toLocaleString()} rows)</dd>
    <dt>Events</dt><dd>${counts || "none"}</dd></dl>`;
}
$("log-input").addEventListener("change", async (e) => {
  const file = e.target.files[0]; e.target.value = "";
  if (!file) return;
  showError($("log-error"), "");
  $("log-uploading").classList.remove("hidden");
  const fd = new FormData(); fd.append("file", file);
  try {
    await api("/api/log", { method: "POST", body: fd });
    await refreshLogSummary();
    await refreshPositions();
    timeline.invalidateEvents();
  } catch (err) { showError($("log-error"), err.message); }
  finally { $("log-uploading").classList.add("hidden"); }
});

/* =====================================================================
 * Library tab
 * ===================================================================== */
let libraryDirty = true;
const group = { species: null, camelotSpecies: null, items: [], total: 0 };

tabHandlers.library = () => { if (libraryDirty) loadSpeciesGroups(); };

async function loadCompare() {
  const { species, totals } = await api("/api/camelot/compare");
  $("compare-card").classList.toggle("hidden", !species.length);
  if (!species.length) return;
  const n = (v) => v.toLocaleString();
  const compared = (totals.agree || 0) + (totals.group || 0) + (totals.differ || 0);
  $("compare-summary").textContent =
    `AI agrees on ${n(totals.agree || 0)} of ${n(compared)} photos compared (${Math.round((100 * (totals.agree || 0)) / (compared || 1))}%)`;
  const cell = (r, kind) => `<td class="num">${r[kind]
    ? `<button class="link-btn" data-camelot="${esc(r.species)}" data-agreement="${kind}">${n(r[kind])}</button>` : "0"}</td>`;
  $("compare-rows").innerHTML = species.map((r) => `<tr>
    <td>${esc(r.species)}</td>
    <td class="num"><button class="link-btn" data-camelot="${esc(r.species)}" data-agreement="">${n(r.total)}</button></td>
    ${cell(r, "agree")}${cell(r, "group")}${cell(r, "differ")}
    <td class="muted">${esc(r.called.map(titleCase).join(", "))}</td>
  </tr>`).join("");
}
$("compare-rows").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-camelot]");
  if (btn) openCamelotGroup(btn.dataset.camelot, btn.dataset.agreement);
});

async function loadSpeciesGroups() {
  libraryDirty = false;
  loadCompare();
  const groups = await api("/api/species");
  const total = groups.reduce((a, g) => a + g.count, 0);
  $("library-total").textContent = `${total.toLocaleString()} photos`;
  const grid = $("species-grid");
  if (!groups.length) { grid.innerHTML = `<div class="empty-state">No photos yet — process some on the Workspace tab.</div>`; return; }
  grid.innerHTML = groups.map((g) => `
    <button class="species-card" data-species="${esc(g.label)}">
      <div class="cover" style="background-image:url('/thumb/${esc(g.cover_id)}')"></div>
      <div class="meta">
        <span class="name">${esc(g.label)}</span>
        <span class="muted small">${g.count.toLocaleString()} photo${g.count === 1 ? "" : "s"}${g.review_count ? ` · <span class="review-text">${g.review_count.toLocaleString()} to review</span>` : ""}</span>
      </div>
    </button>`).join("");
  grid.querySelectorAll(".species-card").forEach((c) => c.addEventListener("click", () => openGroup(c.dataset.species)));
  if (group.species || group.camelotSpecies) loadGroupPage(true);
}

function showGroup(title, agreement) {
  $("group-title").textContent = title;
  $("group-agreement").value = agreement;
  $("library-groups-view").classList.add("hidden");
  $("library-group-view").classList.remove("hidden");
  loadGroupPage(true);
}
function openGroup(species) {
  group.species = species; group.camelotSpecies = null;
  showGroup(titleCase(species), "");
}
function openCamelotGroup(species, agreement) {
  group.species = null; group.camelotSpecies = species;
  showGroup(`Camelot: ${species}`, agreement);
}
$("library-back").addEventListener("click", () => {
  group.species = null; group.camelotSpecies = null;
  $("library-group-view").classList.add("hidden");
  $("library-groups-view").classList.remove("hidden");
  if (libraryDirty) loadSpeciesGroups();
});
["group-position", "group-agreement", "group-sort", "group-review", "group-start", "group-end"].forEach((id) =>
  $(id).addEventListener("change", () => loadGroupPage(true)));
$("load-more").addEventListener("click", () => loadGroupPage(false));

async function loadGroupPage(reset) {
  if (reset) { group.items = []; $("image-grid").innerHTML = ""; }
  const p = new URLSearchParams({ offset: group.items.length, limit: 120, sort: $("group-sort").value });
  if (group.species) p.set("species", group.species);
  if (group.camelotSpecies) p.set("camelot_species", group.camelotSpecies);
  if ($("group-agreement").value) p.set("agreement", $("group-agreement").value);
  if ($("group-position").value) p.set("position", $("group-position").value);
  if ($("group-review").checked) p.set("review", "1");
  if ($("group-start").value) p.set("start", $("group-start").value);
  if ($("group-end").value) p.set("end", $("group-end").value);
  const data = await api(`/api/images?${p}`);
  group.total = data.total;
  const startIdx = group.items.length;
  group.items.push(...data.items);
  $("group-count").textContent = `${data.total.toLocaleString()} photos`;
  $("image-grid").insertAdjacentHTML("beforeend", data.items.map((it, k) => imageCardHtml(it, startIdx + k)).join(""));
  $("load-more").classList.toggle("hidden", group.items.length >= group.total);
}

function imageCardHtml(it, idx) {
  return `<button class="image-card" data-idx="${idx}" data-id="${esc(it.id)}">
    <img loading="lazy" src="/thumb/${esc(it.id)}" alt="">
    ${it.marked_for_review ? `<span class="review-dot" title="Marked for review"></span>` : ""}
    <div class="cap"><span>${esc(fmtDateTime(it.captured_at))}</span><span class="muted">P${esc(it.position ?? "?")}${it.count > 1 ? ` · ×${it.count}` : ""}</span></div>
  </button>`;
}
$("image-grid").addEventListener("click", (e) => {
  const card = e.target.closest(".image-card");
  if (card) viewer.open(group.items.map((i) => i.id), +card.dataset.idx);
});

/* =====================================================================
 * Image viewer (Library + Timeline)
 * ===================================================================== */
const viewer = {
  ids: [], idx: 0, rec: null,

  open(ids, idx) {
    this.ids = ids; this.idx = idx;
    $("viewer").classList.remove("hidden");
    this.load();
  },

  async load() {
    showError($("v-error"), "");
    const id = this.ids[this.idx];
    $("viewer-prev").disabled = this.idx <= 0;
    $("viewer-next").disabled = this.idx >= this.ids.length - 1;
    $("viewer-img").src = `/media/${id}`;
    const r = this.rec = await api(`/api/images/${id}`);
    $("v-species").textContent = r.display_species;
    $("v-species").style.color = speciesColor(r.display_species);
    if (r.ai_species === "florida scrub-jay") {
      const speciesNet = r.speciesnet_species === "not classified" ? "SpeciesNet hasn't run on this photo." : `SpeciesNet said ${r.speciesnet_species || "blank"}.`;
      $("v-ai").textContent = `AI: florida scrub-jay${r.fsj_possible ? " (possible, please check)" : ""}, scrub-jay model ${(r.fsj_score * 100).toFixed(1)}%. ${speciesNet}`;
    } else if (r.ai_species === "not classified") {
      $("v-ai").textContent = `AI: not classified. Under the cutoff for the scrub-jay model (${(r.fsj_score * 100).toFixed(1)}%), and SpeciesNet hasn't run on this photo.`;
    } else {
      $("v-ai").textContent = r.ai_species
        ? `AI: ${r.ai_species} (${Math.round((r.ai_conf || 0) * 100)}% classifier, ${Math.round((r.ai_det_conf || 0) * 100)}% detector)`
        : "AI: blank (no animal detected)";
    }
    const verdict = { agree: "AI agrees", group: "AI only gave a group", differ: "AI disagrees" }[r.camelot_agreement];
    $("v-camelot").textContent = r.camelot_sightings.length
      ? `Camelot: ${r.camelot_sightings.map((s) => s.species + (s.quantity > 1 ? ` ×${s.quantity}` : "") + (s.jay_id ? ` (${s.jay_id})` : "")).join("; ")}${verdict ? ` · ${verdict}` : ""}`
      : "Camelot: no label";
    $("v-verified").textContent = r.corrected_species ? `Corrected ${fmtDateTime(r.corrected_at)}` : "";
    $("v-revert").classList.toggle("hidden", !r.corrected_species);
    $("v-correct").value = "";
    $("v-datetime").value = r.captured_at ? r.captured_at.slice(0, 16) : "";
    $("v-position").value = r.position ?? "";
    $("v-count").value = r.count ?? 0;
    $("v-band").value = r.band_id || "";
    $("v-notes").value = r.notes || "";
    $("v-review").checked = r.marked_for_review;
    const meta = [
      ["File", r.filename],
      ["Camera", r.camera_id ? `${r.camera_id}${r.image_seq ? " · image " + r.image_seq : ""}` : "not in file"],
      ["Hardware ID", r.hardware_id],
      ["Model", r.firmware || r.camera_model],
      ["Battery", r.battery ? `${r.battery}${r.battery_source ? " (" + r.battery_source + ")" : ""}` : null],
      ["Trigger", r.trigger],
      ["Time from", { exif: "EXIF", manual: "set by hand" }[r.time_source] || "no date in file"],
      ["Diel period", r.diel_period],
    ].filter(([, v]) => v);
    $("v-meta").innerHTML = meta.map(([k, v]) => `<span>${esc(k)}</span><span>${esc(v)}</span>`).join("");
  },

  step(d) {
    const n = this.idx + d;
    if (n >= 0 && n < this.ids.length) { this.idx = n; this.load(); }
  },

  changed() { libraryDirty = true; timeline.invalidatePhotos(); },
};
$("viewer-prev").addEventListener("click", () => viewer.step(-1));
$("viewer-next").addEventListener("click", () => viewer.step(1));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { hidePopover(); document.querySelectorAll(".modal").forEach((m) => m.classList.add("hidden")); }
  if ($("viewer").classList.contains("hidden") || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
  if (e.key === "ArrowLeft") viewer.step(-1);
  if (e.key === "ArrowRight") viewer.step(1);
});

async function loadTaxonomy() {
  const all = await api("/api/species/all");
  $("species-datalist").innerHTML = all.map((s) => `<option value="${esc(s)}">`).join("");
  const sel = $("tl-species"); const cur = sel.value;
  sel.innerHTML = `<option value="__nonblank">All except blank</option><option value="__all">All, incl. blank</option>` +
    all.filter((s) => s !== "blank").map((s) => `<option value="${esc(s)}">${esc(titleCase(s))}</option>`).join("");
  sel.value = cur || "__nonblank";
}

async function viewerAction(fn) {
  showError($("v-error"), "");
  try { await fn(); viewer.changed(); await viewer.load(); refreshGroupCard(); }
  catch (err) { showError($("v-error"), err.message); }
}
function refreshGroupCard() {
  const r = viewer.rec;
  const i = group.items.findIndex((x) => x.id === r.id);
  if (i < 0) return;
  group.items[i] = r;
  const card = $("image-grid").querySelector(`[data-id="${r.id}"]`);
  if (card) card.outerHTML = imageCardHtml(r, i);
}
$("v-correct-save").addEventListener("click", () => viewerAction(async () => {
  const species = $("v-correct").value.trim();
  if (!species) throw new Error("Type a species name first");
  await api(`/api/images/${viewer.rec.id}/correct`, { json: { species } });
  loadTaxonomy();
}));
$("v-correct").addEventListener("keydown", (e) => { if (e.key === "Enter") $("v-correct-save").click(); });
$("v-revert").addEventListener("click", () => viewerAction(() =>
  api(`/api/images/${viewer.rec.id}/correct`, { json: { species: "" } })));
$("v-save").addEventListener("click", () => viewerAction(() => {
  const r = viewer.rec;
  const body = {
    count: $("v-count").value, notes: $("v-notes").value, band_id: $("v-band").value,
    marked_for_review: $("v-review").checked,
  };
  if ($("v-position").value) body.position = $("v-position").value;
  const dt = $("v-datetime").value;
  if ((r.captured_at || "").slice(0, 16) !== dt) body.captured_at = dt;
  return api(`/api/images/${r.id}/update`, { json: body });
}));
$("v-delete").addEventListener("click", async () => {
  if (!confirm("Delete this entry from the library? The photo file stays in your folder, and won't be processed again.")) return;
  try {
    await api(`/api/images/${viewer.rec.id}/delete`, { method: "POST" });
    viewer.changed();
    const i = group.items.findIndex((x) => x.id === viewer.rec.id);
    if (i >= 0) { group.items.splice(i, 1); $("image-grid").querySelector(`[data-id="${viewer.rec.id}"]`)?.remove(); }
    viewer.ids.splice(viewer.idx, 1);
    if (!viewer.ids.length) { $("viewer").classList.add("hidden"); return; }
    viewer.idx = Math.min(viewer.idx, viewer.ids.length - 1);
    viewer.load();
  } catch (err) { showError($("v-error"), err.message); }
});

/* =====================================================================
 * Popover
 * ===================================================================== */
function showPopover(html, x, y) {
  const p = $("popover");
  p.innerHTML = html; p.classList.remove("hidden");
  const r = p.getBoundingClientRect();
  p.style.left = `${Math.max(8, Math.min(x + 12, innerWidth - r.width - 8))}px`;
  p.style.top = `${Math.max(8, Math.min(y + 12, innerHeight - r.height - 8))}px`;
  return p;
}
function hidePopover() { $("popover").classList.add("hidden"); }
document.addEventListener("mousedown", (e) => {
  if (!e.target.closest("#popover") && !e.target.closest(".tl-layer [data-pop]")) hidePopover();
});

/* =====================================================================
 * Timeline tab
 * ===================================================================== */
const timeline = {
  MIN_PX: 2, MAX_PX: 8000, DETAIL_PX: 260,
  pxPerDay: 12,
  startMs: 0, days: 1,
  positions: [],
  events: null, daily: null,
  kindLabels: {}, kindsOff: new Set(),
  // Detail mode photo cache: day string -> array of photos
  photosByDay: new Map(), loadingPhotos: false,
  laneOf: new Map(),      // event id -> span sub-lane
  loaded: false,

  labelW() { return parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--label-w")) || 92; },
  dayOf(dateStr) { return (Date.parse(dateStr.slice(0, 10) + "T00:00:00Z") - this.startMs) / DAY_MS; },
  dayOfTime(iso) { return (Date.parse(iso.length <= 10 ? iso + "T00:00:00Z" : iso + "Z") - this.startMs) / DAY_MS; },
  dateOfDay(d) { return new Date(this.startMs + Math.floor(d) * DAY_MS).toISOString().slice(0, 10); },
  x(day) { return day * this.pxPerDay; },

  invalidateEvents() { this.events = null; this.loaded = false; if (this.isActive()) this.load(); },
  invalidatePhotos() { this.daily = null; this.photosByDay.clear(); this.loaded = false; if (this.isActive()) this.load(); },
  // Like invalidatePhotos, but keeps the current zoom and scroll position.
  refreshPhotos() { this.daily = null; this.photosByDay.clear(); if (this.isActive() && this.loaded) this.load(); },
  isActive() { return $("tab-timeline").classList.contains("active"); },

  async load() {
    $("tl-status").textContent = "Loading…";
    const [range, events, daily] = await Promise.all([
      api("/api/timeline/range"),
      this.events ? Promise.resolve(this.events) : api("/api/timeline/events"),
      this.daily ? Promise.resolve(this.daily) : api("/api/timeline/daily"),
    ]);
    this.events = events; this.daily = daily;
    this.positions = range.positions;
    this.startMs = Date.parse(range.start + "T00:00:00Z") - DAY_MS;      // one day of padding each side
    this.days = (Date.parse(range.end + "T00:00:00Z") - this.startMs) / DAY_MS + 2;
    const empty = !events.length && !daily.length;
    $("tl-empty").classList.toggle("hidden", !empty);
    $("tl-scroller").classList.toggle("hidden", empty);
    this.assignLanes();
    this.renderLegend();
    this.buildRows();
    if (!this.loaded) { this.loaded = true; this.fit(); }
    else this.applyZoom(this.pxPerDay);
    $("tl-status").textContent = [
      range.undated_images ? `${range.undated_images} photo(s) have no date and aren't shown.` : "",
      range.unassigned_images ? `${range.unassigned_images} photo(s) have no position and aren't shown.` : "",
    ].filter(Boolean).join(" ");
  },

  assignLanes() {
    // Overlapping spans in one position row get stacked into sub-lanes.
    this.laneOf.clear();
    const byPos = new Map();
    for (const e of this.events) {
      if (!SPAN_KINDS.has(e.kind) && !(e.kind === "important_date" && e.end)) continue;
      const key = e.position ?? "site";
      if (!byPos.has(key)) byPos.set(key, []);
      byPos.get(key).push(e);
    }
    for (const list of byPos.values()) {
      list.sort((a, b) => a.start.localeCompare(b.start));
      const laneEnds = [];
      for (const e of list) {
        const end = e.end || e.start;
        let lane = laneEnds.findIndex((le) => le < e.start);
        if (lane < 0) { lane = laneEnds.length; laneEnds.push(end); } else laneEnds[lane] = end;
        this.laneOf.set(e.id, Math.min(lane, 2));
      }
    }
  },

  renderLegend() {
    const kinds = Object.keys(this.kindLabels).length ? this.kindLabels : Object.fromEntries(Object.keys(KIND_COLORS).map((k) => [k, k]));
    const present = new Set(this.events.map((e) => e.kind));
    $("tl-legend").innerHTML = Object.entries(kinds).filter(([k]) => present.has(k)).map(([k, label]) => `
      <span class="legend-chip ${this.kindsOff.has(k) ? "off" : ""}" data-kind="${esc(k)}" title="Click to show/hide">
        <span class="legend-swatch" style="background:${KIND_COLORS[k] || "#888"}"></span>${esc(label)}
      </span>`).join("");
    $("tl-legend").querySelectorAll(".legend-chip").forEach((c) => c.addEventListener("click", () => {
      const k = c.dataset.kind;
      this.kindsOff.has(k) ? this.kindsOff.delete(k) : this.kindsOff.add(k);
      c.classList.toggle("off");
      this.render();
    }));
  },

  buildRows() {
    const rows = [`<div class="tl-row site" data-pos="site"><div class="tl-row-label">Site<span class="sub">notes</span></div><div class="tl-layer"></div></div>`];
    for (const p of this.positions) {
      rows.push(`<div class="tl-row" data-pos="${p}"><div class="tl-row-label">Position ${p}<span class="sub">log · photos</span></div><div class="tl-layer"></div></div>`);
    }
    $("tl-rows").innerHTML = rows.join("");
    this.layers = new Map([...$("tl-rows").querySelectorAll(".tl-row")].map((r) => [r.dataset.pos, r.querySelector(".tl-layer")]));
  },

  sliderToPx(v) { return this.MIN_PX * Math.pow(this.MAX_PX / this.MIN_PX, v / 100); },
  pxToSlider(px) { return (100 * Math.log(px / this.MIN_PX)) / Math.log(this.MAX_PX / this.MIN_PX); },

  fit() {
    const w = $("tl-scroller").clientWidth - this.labelW() - 4;
    this.applyZoom(Math.max(this.MIN_PX, Math.min(this.MAX_PX, w / this.days)), 0);
  },

  // Zoom keeping the timeline point under anchorX (scroller-relative px) fixed.
  applyZoom(px, anchorX) {
    const sc = $("tl-scroller");
    const lw = this.labelW();
    if (anchorX === undefined) anchorX = (sc.clientWidth - lw) / 2;
    const dayAtAnchor = (sc.scrollLeft + anchorX) / this.pxPerDay;
    this.pxPerDay = Math.max(this.MIN_PX, Math.min(this.MAX_PX, px));
    $("tl-zoom").value = this.pxToSlider(this.pxPerDay);
    $("tl-canvas").style.width = `${lw + this.days * this.pxPerDay}px`;
    sc.scrollLeft = dayAtAnchor * this.pxPerDay - anchorX;
    this.render();
  },

  goTo(dateStr) {
    const sc = $("tl-scroller");
    if (this.pxPerDay < 40) this.pxPerDay = 40;
    $("tl-canvas").style.width = `${this.labelW() + this.days * this.pxPerDay}px`;
    sc.scrollLeft = this.x(this.dayOf(dateStr)) - (sc.clientWidth - this.labelW()) / 2 + this.pxPerDay / 2;
    $("tl-zoom").value = this.pxToSlider(this.pxPerDay);
    this.render();
  },

  visibleDays() {
    const sc = $("tl-scroller");
    const buffer = (sc.clientWidth / this.pxPerDay) * 0.5;
    const a = sc.scrollLeft / this.pxPerDay - buffer;
    const b = (sc.scrollLeft + sc.clientWidth) / this.pxPerDay + buffer;
    return [Math.max(0, Math.floor(a)), Math.min(this.days, Math.ceil(b))];
  },

  speciesVisible(sp) {
    const f = $("tl-species").value;
    if (f === "__all") return true;
    if (f === "__nonblank") return sp !== "blank";
    return sp === f;
  },

  _raf: 0,
  render() {
    cancelAnimationFrame(this._raf);
    this._raf = requestAnimationFrame(() => this._render());
  },

  _render() {
    if (!this.events || !this.layers) return;
    const [d0, d1] = this.visibleDays();
    this.renderAxis(d0, d1);
    const html = new Map([...this.layers.keys()].map((k) => [k, []]));
    const push = (pos, s) => { const a = html.get(pos == null ? "site" : String(pos)); if (a) a.push(s); };

    // Month gridlines (in every row)
    const grid = [];
    for (let d = d0; d <= d1; d++) {
      const ds = this.dateOfDay(d);
      if (ds.endsWith("-01") || (this.pxPerDay >= 30)) {
        grid.push(`<div class="tl-gridline" style="left:${this.x(d)}px;${ds.endsWith("-01") ? "background:#ddd8cc" : ""}"></div>`);
      }
    }
    for (const a of html.values()) a.push(grid.join(""));

    // ---- events ----
    const points = new Map(); // "pos|date" -> events[]
    for (const e of this.events) {
      if (this.kindsOff.has(e.kind)) continue;
      const s = this.dayOf(e.start), en = e.end ? this.dayOf(e.end) + 1 : s + 1;
      if (en < d0 || s > d1) continue;
      const color = KIND_COLORS[e.kind] || "#888";
      if (e.kind === "important_date") {
        const w = Math.max(3, this.x(en) - this.x(s));
        push(null, `<div class="tl-site-event" data-pop="ev" data-ev="${e.id}" style="left:${this.x(s)}px;--site-color:${color};${e.end ? `width:${w}px` : ""}" title="${esc(e.title)}">${this.pxPerDay > 6 ? esc(e.title) : ""}</div>`);
      } else if (SPAN_KINDS.has(e.kind)) {
        const lane = this.laneOf.get(e.id) || 0;
        const left = this.x(s), w = Math.max(3, this.x(en) - left);
        push(e.position, `<div class="tl-span" data-pop="ev" data-ev="${e.id}" style="left:${left}px;width:${w}px;top:${4 + lane * 7}px;background:${color}" title="${esc(e.title)}"></div>`);
      } else {
        const key = `${e.position}|${e.start}`;
        if (!points.has(key)) points.set(key, []);
        points.get(key).push(e);
      }
    }
    for (const [key, evs] of points) {
      const [pos, date] = key.split("|");
      const kinds = [...new Set(evs.map((e) => e.kind))];
      const cx = this.x(this.dayOf(date) + 0.5);
      const ids = evs.map((e) => e.id).join(",");
      const title = evs.map((e) => e.title).join("\n");
      push(pos === "null" ? null : pos, `<div class="tl-point" data-pop="ev" data-ev="${ids}" style="left:${cx}px" title="${esc(title)}">${kinds.map((k) => `<i style="background:${KIND_COLORS[k] || "#888"}"></i>`).join("")}</div>`);
    }

    // ---- photos ----
    if (this.pxPerDay < this.DETAIL_PX) this.renderDaily(d0, d1, push);
    else this.renderPhotos(d0, d1, push);

    for (const [k, layer] of this.layers) layer.innerHTML = html.get(k).join("");
  },

  renderDaily(d0, d1, push) {
    const cells = new Map(); // "pos|date" -> {total, species: {sp: n}}
    let maxN = 1;
    const lo = this.dateOfDay(d0), hi = this.dateOfDay(d1);
    for (const r of this.daily) {
      if (r.date < lo || r.date > hi || !this.speciesVisible(r.species)) continue;
      const key = `${r.position}|${r.date}`;
      let c = cells.get(key);
      if (!c) cells.set(key, (c = { total: 0, species: {} }));
      c.total += r.n; c.species[r.species] = (c.species[r.species] || 0) + r.n;
    }
    for (const c of cells.values()) maxN = Math.max(maxN, c.total);
    const w = Math.max(2, this.pxPerDay * 0.8);
    for (const [key, c] of cells) {
      const [pos, date] = key.split("|");
      const left = this.x(this.dayOf(date)) + (this.pxPerDay - w) / 2;
      const h = 4 + (26 * Math.log1p(c.total)) / Math.log1p(maxN);
      const segs = Object.entries(c.species).sort((a, b) => b[1] - a[1]);
      const title = `${fmtDateTime(date)} · Position ${pos}\n` + segs.map(([s, n]) => `${titleCase(s)}: ${n}`).join("\n");
      push(pos, `<div class="tl-day" data-pop="day" data-date="${date}" data-pos="${pos}" style="left:${left}px;width:${w}px;height:${h}px" title="${esc(title)}">${
        segs.map(([s, n]) => `<i style="height:${(100 * n) / c.total}%;background:${speciesColor(s)}"></i>`).join("")}</div>`);
      if (this.pxPerDay >= 28) push(pos, `<div class="tl-day-count" style="left:${left + w / 2}px">${c.total}</div>`);
    }
  },

  renderPhotos(d0, d1, push) {
    const needed = [];
    for (let d = d0; d < d1; d++) { const ds = this.dateOfDay(d); if (!this.photosByDay.has(ds)) needed.push(ds); }
    if (needed.length) this.fetchPhotos(needed[0], needed[needed.length - 1]);

    const byPos = new Map();
    for (let d = d0; d < d1; d++) {
      for (const p of this.photosByDay.get(this.dateOfDay(d)) || []) {
        if (!this.speciesVisible(p.display_species)) continue;
        if (!byPos.has(p.position)) byPos.set(p.position, []);
        byPos.get(p.position).push(p);
      }
    }
    const MIN_GAP = 46;
    for (const [pos, list] of byPos) {
      list.sort((a, b) => a.captured_at.localeCompare(b.captured_at));
      let cluster = [];
      const flush = () => {
        if (!cluster.length) return;
        const xs = cluster.map((p) => this.x(this.dayOfTime(p.captured_at)));
        const cx = (xs[0] + xs[xs.length - 1]) / 2;
        const ids = cluster.map((p) => p.id).join(",");
        if (cluster.length === 1) {
          const p = cluster[0];
          push(pos, `<div class="tl-photo" data-pop="photo" data-ids="${ids}" style="left:${cx}px;--sp:${speciesColor(p.display_species)};background-image:url('/thumb/${p.id}')" title="${esc(`${titleCase(p.display_species)} · ${fmtDateTime(p.captured_at)}`)}"></div>`);
        } else {
          const counts = {};
          cluster.forEach((p) => (counts[p.display_species] = (counts[p.display_species] || 0) + 1));
          const top = Object.entries(counts).sort((a, b) => b[1] - a[1])[0][0];
          const title = `${fmtDateTime(cluster[0].captured_at)} – ${fmtDateTime(cluster[cluster.length - 1].captured_at).split(" ")[1]}\n` +
            Object.entries(counts).map(([s, n]) => `${titleCase(s)}: ${n}`).join("\n");
          push(pos, `<div class="tl-cluster" data-pop="photo" data-ids="${ids}" style="left:${cx}px;--sp:${speciesColor(top)}" title="${esc(title)}">${cluster.length}</div>`);
        }
        cluster = [];
      };
      let clusterStartX = -Infinity;
      for (const p of list) {
        const px = this.x(this.dayOfTime(p.captured_at));
        if (px - clusterStartX > MIN_GAP) { flush(); clusterStartX = px; }
        cluster.push(p);
      }
      flush();
    }
  },

  async fetchPhotos(start, end) {
    if (this.loadingPhotos) return;
    this.loadingPhotos = true;
    $("tl-status").textContent = "Loading photos…";
    try {
      const rows = await api(`/api/timeline/images?start=${start}&end=${end}&limit=10000`);
      const dates = [];
      for (let t = Date.parse(start + "T00:00:00Z"); t <= Date.parse(end + "T00:00:00Z"); t += DAY_MS) dates.push(new Date(t).toISOString().slice(0, 10));
      const truncated = rows.length >= 10000;
      const lastDay = truncated ? rows[rows.length - 1].captured_at.slice(0, 10) : null;
      for (const ds of dates) if (!truncated || ds < lastDay) this.photosByDay.set(ds, []);
      for (const r of rows) {
        const ds = r.captured_at.slice(0, 10);
        if (truncated && ds >= lastDay) continue;
        this.photosByDay.get(ds)?.push(r);
      }
      $("tl-status").textContent = "";
    } catch (err) {
      $("tl-status").textContent = err.message;
    } finally {
      this.loadingPhotos = false;
      this.render();
    }
  },

  renderAxis(d0, d1) {
    const lw = this.labelW();
    const ticks = [`<div class="tl-axis-corner">${this.pxPerDay >= this.DETAIL_PX ? "photos" : "photos / day"}</div>`];
    const ppd = this.pxPerDay;
    for (let d = d0; d <= d1; d++) {
      const ds = this.dateOfDay(d);
      const dt = new Date(this.startMs + d * DAY_MS);
      const left = lw + this.x(d);
      const monthStart = ds.endsWith("-01");
      if (ppd >= 60) {
        ticks.push(`<div class="tick major" style="left:${left}px">${dt.toLocaleDateString(undefined, { timeZone: "UTC", weekday: "short", month: "short", day: "numeric" })}</div>`);
        const step = ppd >= 2400 ? 1 : ppd >= 800 ? 3 : ppd >= 300 ? 6 : 12;
        for (let h = step; h < 24; h += step) {
          ticks.push(`<div class="tick minor" style="left:${left + (h / 24) * ppd}px">${h}:00</div>`);
        }
      } else {
        if (monthStart || d === d0) {
          ticks.push(`<div class="tick major" style="left:${left}px">${dt.toLocaleDateString(undefined, { timeZone: "UTC", month: "short", year: "numeric" })}</div>`);
        }
        const every = ppd >= 18 ? 1 : ppd >= 5 ? 7 : 0;
        const dayNum = dt.getUTCDate();
        if (every && !monthStart && (every === 1 || dt.getUTCDay() === 1)) {
          ticks.push(`<div class="tick minor" style="left:${left}px">${dayNum}</div>`);
        }
      }
    }
    $("tl-axis").innerHTML = ticks.join("");
  },

  popoverFor(el, ev) {
    const kind = el.dataset.pop;
    if (kind === "ev") {
      const ids = new Set(el.dataset.ev.split(",").map(Number));
      const evs = this.events.filter((e) => ids.has(e.id));
      showPopover(evs.map((e) => `<div class="ev">
        <div class="ev-kind"><span class="legend-swatch" style="background:${KIND_COLORS[e.kind]}"></span>${esc(this.kindLabels[e.kind] || e.kind)}</div>
        <h4>${esc(e.title)}</h4>
        <div class="muted small">${esc(fmtDateTime(e.start))}${e.end ? " – " + esc(fmtDateTime(e.end)) : ""}</div>
        ${e.detail ? `<div>${esc(e.detail)}</div>` : ""}
      </div>`).join(""), ev.clientX, ev.clientY);
    } else if (kind === "day") {
      // Zoom in on that day.
      hidePopover();
      const sc = $("tl-scroller");
      this.pxPerDay = Math.max(this.DETAIL_PX * 1.5, Math.min(this.MAX_PX, sc.clientWidth - this.labelW() - 40));
      this.goTo(el.dataset.date);
    } else if (kind === "photo") {
      const ids = el.dataset.ids.split(",");
      if (ids.length === 1) { viewer.open(ids, 0); return; }
      const all = new Map();
      for (const list of this.photosByDay.values()) for (const p of list) all.set(p.id, p);
      const items = ids.map((id) => all.get(id)).filter(Boolean);
      const pop = showPopover(`<h4>${items.length} photos</h4><div class="thumbs">${items.map((p, i) => `
        <button data-i="${i}" style="--sp:${speciesColor(p.display_species)}"><img src="/thumb/${p.id}" loading="lazy" alt="">
          <div class="t">${esc(p.captured_at.slice(11, 16))}</div></button>`).join("")}</div>`, ev.clientX, ev.clientY);
      pop.querySelectorAll("[data-i]").forEach((b) => b.addEventListener("click", () => { hidePopover(); viewer.open(ids, +b.dataset.i); }));
    }
  },
};

tabHandlers.timeline = () => { if (!timeline.loaded) timeline.load(); else timeline.render(); };

(function wireTimeline() {
  const sc = $("tl-scroller");
  sc.addEventListener("scroll", () => timeline.render(), { passive: true });
  $("tl-zoom").addEventListener("input", (e) => timeline.applyZoom(timeline.sliderToPx(+e.target.value)));
  $("tl-fit").addEventListener("click", () => timeline.fit());
  $("tl-goto").addEventListener("change", (e) => e.target.value && timeline.goTo(e.target.value));
  $("tl-species").addEventListener("change", () => timeline.render());
  window.addEventListener("resize", () => timeline.isActive() && timeline.render());

  sc.addEventListener("wheel", (e) => {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    const rect = sc.getBoundingClientRect();
    const anchor = e.clientX - rect.left - timeline.labelW();
    timeline.applyZoom(timeline.pxPerDay * Math.exp(-e.deltaY * 0.002), Math.max(0, anchor));
  }, { passive: false });

  // Drag to pan
  let drag = null;
  sc.addEventListener("mousedown", (e) => {
    if (e.button !== 0 || e.target.closest("[data-pop]")) return;
    drag = { x: e.clientX, y: e.clientY, left: sc.scrollLeft, top: sc.scrollTop, moved: false };
  });
  window.addEventListener("mousemove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) { drag.moved = true; sc.classList.add("dragging"); }
    sc.scrollLeft = drag.left - dx; sc.scrollTop = drag.top - dy;
  });
  window.addEventListener("mouseup", () => { drag = null; sc.classList.remove("dragging"); });

  $("tl-rows").addEventListener("click", (e) => {
    const el = e.target.closest("[data-pop]");
    if (el) timeline.popoverFor(el, e);
  });
})();

/* =====================================================================
 * Boot
 * ===================================================================== */
(async function boot() {
  await Promise.all([refreshPositions(), refreshLogSummary(), loadTaxonomy(), refreshQueue(), refreshWorkspace({ fillPaths: true })]);
  const initial = location.hash.slice(1);
  if (["workspace", "library", "timeline"].includes(initial)) switchTab(initial);
})();
