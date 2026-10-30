/* RAID Architect — front-end logic.
   All RAID/pricing math is computed server-side by raid_architect.py via the API.
   This script only gathers form inputs, calls the API, and renders results. */
"use strict";

const $ = (id) => document.getElementById(id);
const TIER_CLASS = { Refurb: "dot-refurb", NAS: "dot-nas", Ent: "dot-ent" };

let META = null; // /api/levels payload
let LAST_COMPARISON = null; // comparison block from the most recent plan
let CMP_METRIC = "total"; // which metric the Baseline|Live|Δ table shows

// ---- helpers --------------------------------------------------------------
function money(v) {
  return "$" + Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function signedMoney(v) {
  const s = money(Math.abs(v));
  if (v > 0.004) return "+" + s;
  if (v < -0.004) return "−" + s;
  return s;
}
function signedPct(v) {
  const sign = v > 0.04 ? "+" : v < -0.04 ? "−" : "";
  return sign + Math.abs(v).toFixed(1) + "%";
}
function tb(v) {
  const n = Number(v);
  return (Math.abs(n - Math.round(n)) < 1e-9 ? Math.round(n) : n.toFixed(2)) + " TB";
}
function setStatus(kind, msg) {
  const box = $("statusBox");
  if (!kind) { box.classList.add("hidden"); return; }
  box.className = "status " + kind;
  box.textContent = msg;
  box.classList.remove("hidden");
}

// ---- form state -----------------------------------------------------------
function currentMode() {
  return document.querySelector('input[name="mode"]:checked').value;
}
function syncMode() {
  const m = currentMode();
  $("capacityField").classList.toggle("hidden", m !== "capacity");
  $("drivesField").classList.toggle("hidden", m !== "drives");
}
function syncLevel() {
  // When an explicit level is chosen, the fault-tolerance selector is irrelevant.
  const hasLevel = $("level").value !== "";
  $("faultField").style.opacity = hasLevel ? ".45" : "1";
  $("fault").disabled = hasLevel;
  // Group fields only matter for 50/60 (or auto, which might pick neither).
  const lvl = $("level").value;
  const grouped = lvl === "50" || lvl === "60";
  $("groupRow").style.display = (lvl === "" || grouped) ? "" : "none";
}
function syncSource() {
  const live = document.querySelector('input[name="pricing_source"]:checked').value === "live";
  $("sourceHint").textContent = live
    ? "Live: fetches recertified pricing from ServerPartDeals (falls back to baseline on failure)."
    : "Baseline: built-in rough early-2026 estimates. No network needed.";
}

function gatherPayload() {
  const p = {
    size: $("size").value.trim(),
    level: $("level").value,
    hot_spare: $("hot_spare").value,
    groups: $("groups").value,
    group_size: $("group_size").value,
    pricing_source: document.querySelector('input[name="pricing_source"]:checked').value,
    rate_refurb: $("rate_refurb").value,
    rate_nas: $("rate_nas").value,
    rate_ent: $("rate_ent").value,
    price_per_drive: $("price_per_drive").value,
  };
  if (!p.level) p.fault = $("fault").value;
  if (currentMode() === "capacity") p.capacity = $("capacity").value.trim();
  else p.drives = $("drives").value;
  return p;
}

async function postJSON(url, payload) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({ error: "Invalid server response." }));
  if (!res.ok) throw new Error(data.error || ("Request failed (" + res.status + ")"));
  return data;
}

// ---- rendering: single plan ----------------------------------------------
function renderPlan(data) {
  const L = data.layout;
  $("compareResult").classList.add("hidden");
  $("planResult").classList.remove("hidden");

  $("planLevelName").textContent = L.level_name;
  $("planSummary").textContent = L.summary;

  $("statUsable").textContent = tb(L.usable_capacity_tb);
  $("statRaw").textContent = tb(L.raw_capacity_tb);
  const driveTxt = L.hot_spares ? `${L.total_drives} (${L.data_drives}+${L.hot_spares})` : String(L.total_drives);
  $("statDrives").textContent = driveTxt;
  $("statFault").textContent = L.fault_tolerance + " drive" + (L.fault_tolerance === 1 ? "" : "s");

  const eff = L.efficiency_pct;
  $("effPct").textContent = eff.toFixed(1) + "%";
  $("effBar").style.width = Math.max(0, Math.min(100, eff)) + "%";

  $("rowMin").textContent = L.min_drives + (L.groups ? ` · ${L.groups} parity groups` : "");
  $("rowFaultNote").textContent = L.fault_tolerance_note;
  $("rowRead").textContent = L.read;
  $("rowWrite").textContent = L.write;

  const order = data.tier_order || ["Refurb", "NAS", "Ent"];

  // Baseline-vs-live comparison is the primary pricing presentation.
  LAST_COMPARISON = data.comparison || null;
  if (LAST_COMPARISON) {
    renderComparisonBadge(data);
    renderLiveNote(LAST_COMPARISON);
    renderCmpPrice();
  }

  renderMarket(data.market_links, order);
}

function renderComparisonBadge(data) {
  const c = data.comparison;
  $("planSource").textContent = c.live_succeeded ? "baseline vs live" : "live unavailable";
  $("planSource").classList.toggle("warn", !c.live_succeeded);
  const liveNote = (c.live && c.live.note) ? " — " + c.live.note : "";
  $("priceNote").textContent = liveNote;
  // Clarify the Live column sub-label when overrides are in play.
  $("liveColSub").textContent = c.live_has_manual_override ? "your override" : "ServerPartDeals";
}

function renderLiveNote(c) {
  const box = $("liveNote");
  if (!c.live_succeeded) {
    box.className = "live-note warn";
    box.innerHTML = "⚠ <strong>Live pricing unavailable</strong> — the network fetch failed, " +
      "so the Live column falls back to the baseline and the two columns are identical.";
    box.classList.remove("hidden");
  } else if (c.live_has_manual_override) {
    box.className = "live-note info";
    box.innerHTML = "ℹ The <strong>Live</strong> column reflects your manual price override(s).";
    box.classList.remove("hidden");
  } else {
    box.classList.add("hidden");
  }
}

function renderCmpPrice() {
  const c = LAST_COMPARISON;
  if (!c) return;
  const metric = CMP_METRIC; // "total" | "per_drive" | "per_tb"
  const body = $("cmpPriceBody");
  body.innerHTML = "";
  c.tiers.forEach((t) => {
    const base = t.baseline[metric];
    const live = t.live[metric];
    const d = t.delta[metric];
    const dir = d > 0.004 ? "up" : d < -0.004 ? "down" : "flat";
    const deltaCell =
      `<span class="delta ${dir}">${signedMoney(d)}` +
      (metric === "total" ? ` <span class="dpct">(${signedPct(t.delta.pct)})</span>` : "") +
      `</span>`;
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td><span class="tier-dot ${TIER_CLASS[t.tier] || ""}"></span>${t.label}</td>` +
      `<td class="num">${money(base)}</td>` +
      `<td class="num live-col">${money(live)}</td>` +
      `<td class="num">${deltaCell}</td>`;
    body.appendChild(tr);
  });
}

function renderMarket(links, order) {
  const box = $("marketLinks");
  box.innerHTML = "";
  if (!links) return;
  const labels = (META && META.tier_labels) || { Refurb: "Recertified", NAS: "NAS Pro (New)", Ent: "Enterprise (New)" };
  (order || Object.keys(links)).forEach((tier) => {
    const refs = links[tier];
    if (!refs) return;
    const row = document.createElement("div");
    row.className = "market-row";
    const anchors = refs.map((r) => `<a href="${r.url}" target="_blank" rel="noopener">${r.label} ↗</a>`).join("");
    row.innerHTML = `<span class="mlabel">${labels[tier] || tier}</span>${anchors}`;
    box.appendChild(row);
  });
}

// ---- rendering: compare ---------------------------------------------------
function renderCompare(data) {
  $("planResult").classList.add("hidden");
  $("compareResult").classList.remove("hidden");
  $("cmpSource").textContent = "pricing: " + data.pricing_source;
  $("cmpTarget").textContent =
    `Target usable ≈ ${tb(data.target_tb)} with ${tb(data.drive_size_tb)} drives. ${data.pricing_note}`;

  const body = $("cmpBody");
  body.innerHTML = "";
  data.levels.forEach((item) => {
    const L = item.layout;
    const q = {};
    item.pricing.quotes.forEach((x) => (q[x.tier] = x.total));
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${L.level_name}</td>` +
      `<td class="num">${L.total_drives}</td>` +
      `<td class="num">${tb(L.usable_capacity_tb)}</td>` +
      `<td class="num">${L.efficiency_pct.toFixed(0)}%</td>` +
      `<td class="num">${L.fault_tolerance}</td>` +
      `<td class="num">${money(q.Refurb)}</td>` +
      `<td class="num">${money(q.NAS)}</td>` +
      `<td class="num">${money(q.Ent)}</td>`;
    body.appendChild(tr);
  });
}

// ---- actions --------------------------------------------------------------
async function doPlan(e) {
  if (e) e.preventDefault();
  const btn = $("planBtn");
  btn.disabled = true;
  setStatus("loading", "Computing array plan…");
  try {
    const data = await postJSON("/api/plan", gatherPayload());
    setStatus(null);
    renderPlan(data);
  } catch (err) {
    setStatus("error", err.message);
  } finally {
    btn.disabled = false;
  }
}

async function doCompare() {
  const btn = $("compareBtn");
  if (currentMode() !== "capacity") {
    // Comparison needs a target capacity; nudge the user there.
    document.getElementById("mode-cap").checked = true;
    syncMode();
  }
  btn.disabled = true;
  setStatus("loading", "Comparing RAID levels…");
  try {
    const data = await postJSON("/api/compare", gatherPayload());
    setStatus(null);
    renderCompare(data);
  } catch (err) {
    setStatus("error", err.message);
  } finally {
    btn.disabled = false;
  }
}

// ---- init -----------------------------------------------------------------
async function init() {
  document.querySelectorAll('input[name="mode"]').forEach((r) => r.addEventListener("change", syncMode));
  document.querySelectorAll('input[name="pricing_source"]').forEach((r) => r.addEventListener("change", syncSource));
  $("level").addEventListener("change", syncLevel);
  $("planForm").addEventListener("submit", doPlan);
  $("compareBtn").addEventListener("click", doCompare);

  // Metric toggle for the Baseline|Live|Δ table (Total / $per-drive / $per-TB).
  document.querySelectorAll(".cmp-metric-toggle .chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      document.querySelectorAll(".cmp-metric-toggle .chip").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      CMP_METRIC = chip.dataset.metric;
      renderCmpPrice();
    });
  });

  try {
    const res = await fetch("/api/levels");
    META = await res.json();
    const sel = $("level");
    META.levels.forEach((l) => {
      const opt = document.createElement("option");
      opt.value = l.key;
      opt.textContent = `${l.name}${l.grouped ? " (grouped)" : ""} — min ${l.min_drives}`;
      sel.appendChild(opt);
    });
    $("verTag").textContent = "v" + META.version;
  } catch (_) { /* level dropdown still has the Auto option */ }

  syncMode();
  syncLevel();
  syncSource();
  doPlan(); // show an initial result on load
}

document.addEventListener("DOMContentLoaded", init);
