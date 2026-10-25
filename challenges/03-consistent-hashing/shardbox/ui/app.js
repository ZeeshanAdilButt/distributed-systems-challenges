"use strict";

const $ = (s) => document.querySelector(s);
const COLORS = { A: "#2a78d6", B: "#1baf7a", C: "#eda100", D: "#e87ba4", E: "#6250d6", F: "#eb6834", G: "#0e9fb5", H: "#8a6d3b", I: "#c2410c", J: "#64748b" };
const LABELS = { modulo: "hash % n", buckets: "12 fixed buckets", ring: "ring, 1 token each", vnodes: "ring, 64 vnodes each", ranges: "key ranges" };
let state = null;

async function api(path, body) {
  const options = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(path, options);
  const data = await res.json();
  if (!res.ok) throw Object.assign(new Error(data.error || res.statusText), { data, status: res.status });
  return data;
}

function esc(t) { return String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]); }
function pct(x) { return `${(x * 100).toFixed(x < 0.1 ? 1 : 0)}%`; }
function color(n) { return COLORS[n] || "#888"; }
function showError(e) { $("#error").textContent = e ? e.message : ""; }

async function act(path, body = {}) {
  showError(null);
  try { render(await api(path, body)); } catch (e) { showError(e); }
}

/* ------------------------------------------------------------ drawing */

const W = 640, H = 400;

function drawRing(s) {
  const { space, tokens, ghosts = [] } = s.ring;
  const cx = 225, cy = 200, r = 140;
  const ang = (p) => (p / space) * Math.PI * 2 - Math.PI / 2;
  const pt = (p, rr = r) => [cx + rr * Math.cos(ang(p)), cy + rr * Math.sin(ang(p))];
  const sorted = tokens.slice().sort((a, b) => a[0] - b[0]);
  const parts = [`<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="var(--line)" stroke-width="3"/>`];
  // each arc, thin, in its owner's colour: from just after the previous token to this one
  sorted.forEach(([pos, node], i) => {
    const prev = sorted[(i - 1 + sorted.length) % sorted.length][0];
    let from = prev + 1, to = pos;
    if (sorted.length === 1) { from = pos + 1; to = pos + space - 1; }
    else if (to < from) to += space;
    const [x1, y1] = pt(from), [x2, y2] = pt(to);
    const large = to - from > space / 2 ? 1 : 0;
    parts.push(`<path d="M${x1} ${y1} A${r} ${r} 0 ${large} 1 ${x2} ${y2}" fill="none" stroke="${color(node)}" stroke-width="5" opacity="0.55"><title>DB ${node} owns ${from % space}..${pos}</title></path>`);
  });
  const named = space <= 1000;
  const label = sorted.length + ghosts.length <= 24;
  const dot = (pos, node, faint) => {
    const [x, y] = pt(pos), [lx, ly] = pt(pos, r + 28);
    const o = faint ? ' opacity="0.28"' : "";
    parts.push(`<circle cx="${x}" cy="${y}" r="${faint ? 13 : 12}" fill="${color(node)}" stroke="var(--card)" stroke-width="3"${o}><title>${faint ? "removed: " : ""}DB ${node} token at ${pos}</title></circle>`);
    if (label) parts.push(`<text x="${lx}" y="${ly + 5}" text-anchor="middle" font-weight="600" font-size="14"${faint ? ' class="muted-text" opacity="0.6"' : ""}>${node}${named ? pos : ""}</text>`);
  };
  for (const [pos, node] of ghosts) dot(pos, node, true);
  for (const [pos, node] of sorted) dot(pos, node, false);
  for (const smp of s.samples) {
    const [x, y] = pt(smp.position, r - 24);
    parts.push(`<rect x="${x - 4}" y="${y - 4}" width="8" height="8" transform="rotate(45 ${x} ${y})" fill="${color(smp.owner)}"><title>post ${smp.post_id} at ${smp.position} → DB ${smp.owner}</title></rect>`);
  }
  const up = s.nodes.filter((n) => n.up).length;
  parts.push(`<text x="${cx}" y="${cy - 4}" text-anchor="middle" font-weight="600" font-size="17">${up} physical database${up === 1 ? "" : "s"}</text>`);
  parts.push(`<text x="${cx}" y="${cy + 20}" text-anchor="middle" class="muted-text" font-size="14">Clockwise ownership</text>`);
  parts.push(`<text x="${cx}" y="${cy + 40}" text-anchor="middle" class="muted-text" font-size="12">${sorted.length} tokens · ring 0..${space === 4294967296 ? "2³²" : space - 1}</text>`);
  // legend: dot per database, with its share of the ring
  const owned = {};
  sorted.forEach(([pos, node], i) => {
    const prev = sorted[(i - 1 + sorted.length) % sorted.length][0];
    owned[node] = (owned[node] || 0) + (sorted.length === 1 ? space : ((pos - prev + space) % space));
  });
  const all = [...new Set([...Object.keys(owned), ...ghosts.map((g) => g[1])])].sort();
  all.forEach((n, i) => {
    const y = 80 + i * 30, gone = !(n in owned);
    parts.push(`<circle cx="440" cy="${y - 5}" r="8" fill="${color(n)}"${gone ? ' opacity="0.28"' : ""}/>`);
    parts.push(`<text x="456" y="${y}" font-size="14"${gone ? ' class="muted-text"' : ""}>DB ${n}${gone ? " · removed" : ` · ${pct(owned[n] / space)} of the ring`}</text>`);
  });
  parts.push(`<text x="430" y="${96 + all.length * 30}" class="muted-text">◆ sample posts at their positions</text>`);
  return parts.join("");
}

function drawBuckets(s) {
  const entries = Object.entries(s.buckets);
  const cols = 4, size = 92, gap = 14, x0 = 40, y0 = 40;
  const prevOwner = {};
  for (const smp of s.samples) if (smp.before) prevOwner[smp.bucket] = smp.before;
  const parts = [];
  entries.forEach(([b, node], i) => {
    const x = x0 + (i % cols) * (size + gap), y = y0 + Math.floor(i / cols) * (size + gap);
    parts.push(`<rect x="${x}" y="${y}" width="${size}" height="${size}" rx="12" fill="${color(node)}" opacity="0.9"><title>bucket ${b} lives on DB ${node}</title></rect>`);
    parts.push(`<text x="${x + 12}" y="${y + 26}" font-size="18" font-weight="700" fill="#fff" style="fill:#fff">${b}</text>`);
    parts.push(`<text x="${x + size - 12}" y="${y + size - 12}" text-anchor="end" style="fill:#fff">DB ${node}</text>`);
    const here = s.samples.filter((smp) => String(smp.bucket) === b).map((smp) => smp.post_id);
    if (here.length) parts.push(`<text x="${x + 12}" y="${y + 50}" font-size="11" style="fill:#fff">${here.join(" ")}</text>`);
  });
  parts.push(`<text x="480" y="60" font-weight="600">Partition map</text>`);
  const byNode = {};
  entries.forEach(([b, n]) => (byNode[n] = byNode[n] || []).push(b));
  Object.keys(byNode).sort().forEach((n, i) => {
    parts.push(`<rect x="480" y="${80 + i * 28}" width="14" height="14" rx="3" fill="${color(n)}"/><text x="502" y="${92 + i * 28}">DB ${n}: ${byNode[n].join(", ")}</text>`);
  });
  parts.push(`<text x="480" y="${110 + Object.keys(byNode).length * 28}" class="muted-text">bucket = crc32 % 12</text>`);
  parts.push(`<text x="480" y="${130 + Object.keys(byNode).length * 28}" class="muted-text">never changes; the map does</text>`);
  return parts.join("");
}

function drawRanges(s) {
  const { top, starts } = s.ranges;
  const x0 = 30, x1 = 610, y = 160, h = 70;
  const X = (k) => x0 + (k / top) * (x1 - x0);
  const ownerAt = (k) => { let o = starts[0][1]; for (const [st, n] of starts) if (k >= st) o = n; return o; };
  const parts = [];
  starts.forEach(([start, node], i) => {
    const end = i + 1 < starts.length ? starts[i + 1][0] : top;
    parts.push(`<rect x="${X(start)}" y="${y}" width="${Math.max(2, X(end) - X(start) - 3)}" height="${h}" rx="10" fill="${color(node)}"><title>DB ${node} owns ${start}..${end - 1}</title></rect>`);
    parts.push(`<text x="${(X(start) + X(end)) / 2}" y="${y + 42}" text-anchor="middle" style="fill:#fff" font-weight="700" font-size="15">DB ${node}</text>`);
    parts.push(`<text x="${X(start)}" y="${y + h + 20}" font-size="12" class="muted-text">${start.toLocaleString()}</text>`);
  });
  // the lesson posts sit side by side: one marker for all of them
  const ids = s.samples.map((p) => p.post_id).sort((a, b) => a - b);
  const xs = X(ids[0]);
  parts.push(`<line x1="${xs}" x2="${xs}" y1="${y - 4}" y2="${y - 34}" stroke="var(--ink)"/>`);
  parts.push(`<text x="${xs + 6}" y="${y - 38}" font-size="13">posts ${ids[0]} to ${ids[ids.length - 1]}: all on DB ${ownerAt(ids[0])}</text>`);
  // a range scan of 1,000 neighbouring posts touches one database
  const a = 15000, b = 15999;
  parts.push(`<path d="M${X(a)} ${y + h + 34} V${y + h + 42} H${X(b) + 26} V${y + h + 34}" fill="none" stroke="var(--accent)" stroke-width="2"/>`);
  parts.push(`<text x="${X(a)}" y="${y + h + 62}" font-size="13">scan posts ${a.toLocaleString()} to ${b.toLocaleString()}: asks 1 database (DB ${ownerAt(a)})</text>`);
  // new posts get the next id, so they all go to the last range
  const xr = X(top - 1);
  parts.push(`<path d="M${xr - 70} ${y - 22} H${xr} M${xr - 8} ${y - 28} L${xr} ${y - 22} L${xr - 8} ${y - 16}" fill="none" stroke="var(--down)" stroke-width="2"/>`);
  parts.push(`<text x="${xr}" y="${y - 32}" text-anchor="end" font-size="13" style="fill:var(--down)">every new post lands on DB ${ownerAt(top - 1)}</text>`);
  parts.push(`<text x="${x0}" y="${y + h + 100}" class="muted-text">post ids in order, no hashing. Neighbours stay together; the newest keys pile up at the end.</text>`);
  return parts.join("");
}

function drawModulo(s) {
  const { n, nodes } = s.modulo;
  const parts = [`<text x="320" y="70" text-anchor="middle" font-size="18" font-weight="600">database = nodes[ crc32(post_id) % ${n} ]</text>`];
  const w = Math.min(130, 560 / n - 14);
  nodes.forEach((node, i) => {
    const x = 320 - (n * (w + 14)) / 2 + i * (w + 14);
    parts.push(`<rect x="${x}" y="130" width="${w}" height="110" rx="14" fill="${color(node)}"/><text x="${x + w / 2}" y="180" text-anchor="middle" style="fill:#fff" font-size="16" font-weight="700">DB ${node}</text><text x="${x + w / 2}" y="204" text-anchor="middle" style="fill:#fff">remainder ${i}</text>`);
  });
  parts.push(`<text x="320" y="300" text-anchor="middle" class="muted-text">Change n and almost every remainder changes. Add a database and watch the table below.</text>`);
  return parts.join("");
}

/* ------------------------------------------------------------ rendering */

function render(s) {
  state = s;
  const tab = s.preset.startsWith("ring-vnodes") ? "ring-vnodes-lesson" : s.preset.startsWith("ring") ? "ring-lesson" : s.preset;
  document.querySelectorAll(".strategies button").forEach((b) => b.classList.toggle("on", b.dataset.preset === tab));
  $("#real").checked = s.preset === "ring-hashed" || s.preset === "ring-vnodes";
  $("#real").parentElement.hidden = s.strategy !== "ring";
  const teaching = s.ring && s.ring.space <= 1000;
  $("#at-label").hidden = !teaching;
  $("#viz-title").textContent = s.presets[s.preset].replace(/^./, (c) => c.toUpperCase());
  $("#viz").innerHTML = s.ring ? drawRing(s) : s.buckets ? drawBuckets(s) : s.ranges ? drawRanges(s) : drawModulo(s);
  $("#rule").textContent = s.samples.length ? s.samples[0].rule : "";
  $("#last-action").textContent = s.last_action;
  $("#badge").hidden = !s.badge;
  $("#badge").textContent = s.badge;
  $("#restore").innerHTML = (s.ring ? s.removed : []).map((n) => `<button data-restore="${n}">&#x21BA; Restore DB ${n}</button>`).join("");
  $("#where-head").textContent = s.ring ? "Position" : s.buckets ? "Bucket" : s.modulo ? "crc32 % n" : "Range";

  $("#nodes").innerHTML = s.nodes.map((n) => `
    <div class="node ${n.up ? "" : "down"}">
      <span class="chip" style="background:${color(n.name)}"></span>
      <strong>DB ${n.name}</strong>
      <div><div class="bar"><i style="width:${(n.share * 100).toFixed(1)}%;background:${color(n.name)}"></i></div>
        <div class="meta">${n.rows.toLocaleString()} rows · ${pct(n.share)}${n.up ? "" : " · crashed"}</div></div>
      <div class="actions">
        <button data-do="remove" data-node="${n.name}" ${n.up && s.nodes.length > 1 ? "" : "disabled"} title="Move its data, then retire it">Remove</button>
        <button data-do="crash" data-node="${n.name}" ${n.up ? "" : "disabled"} title="The process dies">Crash</button>
        <button data-do="forget" data-node="${n.name}" ${n.up ? "disabled" : ""} title="Drop it from the placement without restoring its data">Forget</button>
      </div>
    </div>`).join("");

  $("#samples").innerHTML = s.samples.map((r) => {
    const where = r.position ?? r.bucket ?? (r.hash !== undefined ? r.hash % s.nodes.length : "");
    const moved = r.before && r.before !== r.owner;
    const owner = moved ? `<span class="was">${r.before} →</span> <span class="moved">DB ${r.owner}</span>` : `DB ${r.owner}`;
    const comments = r.comments === null ? `<span class="bad">unavailable</span>` : r.comments === 0 ? `<span class="bad">0 (missing)</span>` : r.comments;
    return `<tr><td>${r.post_id}</td><td>${where}</td><td>${owner}</td><td>${comments}</td></tr>`;
  }).join("");

  $("#steps").innerHTML = s.steps.length
    ? s.steps.map((t) => `<li>${esc(t)}</li>`).join("")
    : `<li class="muted">Add, remove or crash a database to see a live migration.</li>`;
  $("#moved").innerHTML = s.migration
    ? `<p><strong>${s.migration.copied.toLocaleString()}</strong> rows copied · fence ${s.migration.fenced_ms} ms · ${s.migration.captured} writes captured and replayed</p>` +
      Object.entries(s.migration.moved).map(([k, v]) => `<span class="hop">${k}: ${v}</span>`).join(" ")
    : "";

  const total = s.nodes.length;
  $("#compare").innerHTML = Object.entries(s.next_join).map(([k, v]) => `
    <div class="cmp"><span>${LABELS[k]}</span>
      <span class="track"><i style="width:${(v * 100).toFixed(1)}%"></i><b style="left:${(100 / (total + 1)).toFixed(1)}%"></b></span>
      <strong>${pct(v)}</strong></div>`).join("");
}

function traceHtml(r, verb) {
  const hops = [`<span class="hop">${verb} /posts/${r.post_id}/comments</span>`];
  if (r.position !== undefined) hops.push(`<span class="hop">crc32 % ${state.ring.space === 4294967296 ? "2³²" : state.ring.space} = ${r.position}</span>`, `<span class="hop">first token clockwise</span>`);
  if (r.bucket !== undefined) hops.push(`<span class="hop">crc32 % 12 = bucket ${r.bucket}</span>`, `<span class="hop">partition map</span>`);
  if (r.hash !== undefined) hops.push(`<span class="hop">crc32 % ${state.nodes.length} = ${r.hash % state.nodes.length}</span>`);
  if (state.ranges) hops.push(`<span class="hop">range lookup on the id itself</span>`);
  hops.push(`<span class="hop" style="border-color:${color(r.owner)}">DB ${r.owner}</span>`);
  return hops.join('<span class="arrow">→</span>');
}

/* ------------------------------------------------------------ wiring */

document.querySelectorAll(".strategies button").forEach((b) => b.addEventListener("click", () => {
  let preset = b.dataset.preset;
  if ($("#real").checked && preset === "ring-lesson") preset = "ring-hashed";
  if ($("#real").checked && preset === "ring-vnodes-lesson") preset = "ring-vnodes";
  act("/api/reset", { preset });
}));
$("#real").addEventListener("change", () => {
  const vn = state.preset.includes("vnodes");
  act("/api/reset", { preset: $("#real").checked ? (vn ? "ring-vnodes" : "ring-hashed") : (vn ? "ring-vnodes-lesson" : "ring-lesson") });
});
$("#add").addEventListener("submit", (e) => {
  e.preventDefault();
  const teaching = state.ring && state.ring.space <= 1000;
  act("/api/nodes", teaching ? { position: Number($("#at").value) } : {});
});
$("#burst").addEventListener("click", async () => { showError(null); try { await api("/api/burst", { count: 200 }); refresh(); } catch (e) { showError(e); } });
$("#restore").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-restore]");
  if (btn) act(`/api/nodes/${btn.dataset.restore}/restore`);
});
$("#nodes").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-do]");
  if (btn) act(`/api/nodes/${btn.dataset.node}/${btn.dataset.do}`);
});
$("#req").addEventListener("submit", async (e) => {
  e.preventDefault();
  const post = Number($("#post").value);
  try {
    const r = await api(`/posts/${post}/comments`, { user_id: 42, text: "Hello from the page" });
    $("#trace").innerHTML = traceHtml(r, "POST") + `<span class="arrow">·</span><span>stored as comment ${r.comment_id}</span>`;
    refresh();
  } catch (err) { $("#trace").innerHTML = `<span class="bad">${esc(err.message)}</span>`; }
});
$("#read").addEventListener("click", async () => {
  const post = Number($("#post").value);
  const res = await fetch(`/posts/${post}/comments`);
  const r = await res.json();
  if (!res.ok) { $("#trace").innerHTML = traceHtml(r, "GET") + `<span class="arrow">→</span><span class="bad">${esc(r.error)}</span>`; return; }
  $("#trace").innerHTML = traceHtml(r, "GET") + `<span class="arrow">·</span><span>${r.comments.length} comment(s)</span>`;
});

function bars(rows, total, fmt) {
  return Object.entries(rows).map(([n, v]) => `
    <div class="cmp"><span><i class="chip" style="background:${color(n)};display:inline-block;width:10px;height:10px;border-radius:3px"></i> DB ${n}</span>
      <span class="track"><i style="width:${total ? (v / total * 100).toFixed(1) : 0}%"></i></span>
      <strong>${fmt(v)}</strong></div>`).join("");
}

$("#hot").addEventListener("submit", async (e) => {
  e.preventDefault();
  showError(null);
  try {
    const r = await api("/api/hot", { stripes: Number($("#stripes").value) });
    if (r.note) { $("#hot-out").innerHTML = `<span class="bad">${esc(r.note)}</span>`; return; }
    $("#hot-out").innerHTML = bars(r.rows, 3000, (v) => `${(v / 30).toFixed(0)}%`) +
      `<p class="small">Busiest database: <strong>${(r.busiest * 100).toFixed(0)}%</strong> of the writes. A read asks <strong>${r.asked.length}</strong> database${r.asked.length === 1 ? "" : "s"} and gets all ${r.read_back.toLocaleString()} comments back in order.</p>`;
  } catch (err) { showError(err); }
});

$("#stale").addEventListener("submit", async (e) => {
  e.preventDefault();
  showError(null);
  try {
    const r = await api("/api/stale", { fenced: $("#fenced").checked });
    $("#stale-out").innerHTML =
      `<ol class="steps">${r.steps.map((t) => `<li>${esc(t)}</li>`).join("")}</ol>` +
      `<p class="small">Epoch ${r.epoch[0]} to ${r.epoch[1]}. Refused: <strong>${r.refused}</strong>. Metadata reads: <strong>${r.refreshes}</strong>. ` +
      `Readable from the current owner: <strong style="color:var(--${r.readable === r.writes ? "ok" : "down"})">${r.readable} of ${r.writes}</strong>. ` +
      `Stranded on the old owner: <strong style="color:var(--${r.stranded ? "down" : "ok"})">${r.stranded}</strong>.</p>`;
  } catch (err) { showError(err); }
});

async function refresh() {
  try {
    render(await api("/api/state"));
    $("#pill").className = "pill live"; $("#pill-text").textContent = "live";
  } catch {
    $("#pill").className = "pill off"; $("#pill-text").textContent = "server stopped";
  }
}
refresh();
setInterval(refresh, 3000);
