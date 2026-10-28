// The page: pick a scenario, switch defences on and off, run the fleet simulation, then
// replay it second by second on the architecture diagram, the p123 entry and the charts.

const $ = (s) => document.querySelector(s);
const NS = "http://www.w3.org/2000/svg";
// t = 0 is 08:00:20Z, so p123's warm entry (read at t = -20 s) shows as the lesson's 08:00:00Z.
const BASE = Date.UTC(2026, 9, 28, 8, 0, 20) / 1000;
const ui = { scenario: "expiry", result: null, i: 0, timer: null, charts: [], runs: 0 };

const fmt = (n) => (n >= 10000 ? `${Math.round(n / 1000)}k` : Math.round(n).toLocaleString("en"));
const pct = (x) => `${(x * 100).toFixed(x > 0 && x < 0.01 ? 2 : 1)}%`;
const iso = (t) => new Date((BASE + t) * 1000).toISOString().replace(".000", "");
const sum = (...xs) => xs.reduce((a, b) => a + b, 0);

async function call(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  return { status: res.status, body: await res.json() };
}

function el(name, attrs = {}, text) {
  const e = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (text !== undefined) e.textContent = text;
  return e;
}

// ------------------------------------------------------------------ controls

function settings() {
  const defenses = {};
  document.querySelectorAll("[data-d]").forEach((x) => { defenses[x.dataset.d] = x.checked; });
  return {
    scenario: ui.scenario, defenses,
    rate: +$("#rate").value, instances: +$("#instances").value, deadline: +$("#deadline").value,
  };
}

function showSliders() {
  $("#rate-out").textContent = `${(+$("#rate").value).toLocaleString("en")}`;
  $("#instances-out").textContent = $("#instances").value;
  $("#deadline-out").textContent = `${Math.round(+$("#deadline").value * 1000)} ms`;
}

async function run() {
  stop();
  const mine = ++ui.runs; // a newer click wins, even if an older run answers later
  $("#run").disabled = true;
  $("#pill").classList.add("busy");
  $("#pill-text").textContent = "simulating";
  $("#status").textContent = "simulating every request…";
  try {
    const { status, body } = await call("/api/run", settings());
    if (mine !== ui.runs) return;
    if (status !== 200) throw new Error(body.error || status);
    ui.result = body;
    $("#cursor").max = body.series.t.length - 1;
    $("#status").textContent = `${fmt(body.config.rate * body.config.seconds)} requests simulated in ${body.seconds_to_simulate} s`;
    drawCharts();
    verdict();
    seek(0);
    play();
  } catch (e) {
    $("#status").textContent = `failed: ${e.message}`;
  } finally {
    if (mine === ui.runs) {
      $("#run").disabled = false;
      $("#pill").classList.remove("busy");
      $("#pill-text").textContent = "ready";
    }
  }
}

// ------------------------------------------------------------------ playback

function seek(i) {
  ui.i = Math.max(0, Math.min(i, ui.result.series.t.length - 1));
  $("#cursor").value = ui.i;
  $("#now").textContent = Math.round(ui.result.series.t[ui.i]);
  drawArch(ui.i);
  drawEntry(ui.i);
  for (const c of ui.charts) c.cursor(ui.i);
}

function play() {
  stop();
  const n = ui.result.series.t.length;
  if (ui.i >= n - 1) seek(0);
  $("#play").textContent = "❚❚";
  ui.timer = setInterval(() => {
    if (ui.i >= n - 1) return stop();
    seek(ui.i + 1);
  }, Math.max(35, Math.min(120, 7000 / n)));
}

function stop() {
  clearInterval(ui.timer);
  ui.timer = null;
  $("#play").textContent = "▶";
}

// ------------------------------------------------------------------ one bucket

function at(i) {
  const s = ui.result.series;
  const served = s.fresh[i] + s.stale[i] + s.waited[i];
  const failed = s.rejected[i] + s.timed_out[i];
  const per = 1 / ui.result.config.bucket;
  return {
    t: s.t[i], served, failed, total: served + failed, share: served / Math.max(1, served + failed),
    stale: s.stale[i], fresh: s.fresh[i], waited: s.waited[i],
    sent: s.db_started[i] * per, ok: s.db_ok[i] * per, cpu: s.cpu[i], active: s.active_max[i],
    oldest: s.oldest[i], hot: s.hot[i], instances: s.instances[i] || ui.result.config.instances,
    local: s.from_local[i], p99: s.write_p99[i],
  };
}

function valkeyDown(t) {
  return (ui.result.scenario.bands || []).some(([a, b, label]) => /Valkey/.test(label) && t >= a && t < b);
}

// ------------------------------------------------------------------ the architecture

function arrow(svg, x1, y1, x2, y2, width, color, dashed) {
  svg.append(el("line", {
    x1, y1, x2, y2, stroke: color, "stroke-width": width, class: "flow",
    "stroke-dasharray": dashed ? "5 5" : "none", "marker-end": `url(#head-${color.replace("#", "")})`,
  }));
}

function drawArch(i) {
  const svg = $("#arch");
  const b = at(i);
  const d = ui.result.defenses;
  const down = valkeyDown(b.t);
  const css = getComputedStyle(document.documentElement);
  const C = (v) => css.getPropertyValue(v).trim();
  svg.replaceChildren();
  const defs = el("defs");
  for (const color of [C("--muted"), C("--down"), C("--fresh"), C("--waited")]) {
    const m = el("marker", { id: `head-${color.replace("#", "")}`, viewBox: "0 0 10 10", refX: 8, refY: 5, markerWidth: 10, markerHeight: 10, markerUnits: "userSpaceOnUse", orient: "auto-start-reverse" });
    m.append(el("path", { d: "M0 0 L10 5 L0 10 z", fill: color }));
    defs.append(m);
  }
  svg.append(defs);

  // clients
  svg.append(el("rect", { x: 8, y: 115, width: 112, height: 96, rx: 14, class: "box" }));
  svg.append(el("text", { x: 64, y: 140, "text-anchor": "middle", class: "muted" }, "Clients"));
  svg.append(el("text", { x: 64, y: 168, "text-anchor": "middle", class: "big" }, fmt(b.total)));
  svg.append(el("text", { x: 64, y: 188, "text-anchor": "middle", class: "muted" }, "reads / s"));
  arrow(svg, 122, 163, 152, 163, 3, C("--muted"));

  // the API fleet across two AZs
  svg.append(el("rect", { x: 156, y: 8, width: 300, height: 314, rx: 14, class: "box" }));
  svg.append(el("text", { x: 170, y: 30 }, "Go API on ECS"));
  svg.append(el("text", { x: 442, y: 30, "text-anchor": "end", class: "muted" }, `${b.instances} processes`));
  const color = b.total === 0 ? C("--muted") : b.share < 0.9 ? C("--down") : b.stale > b.fresh ? C("--stale") : C("--fresh");
  const n = b.instances;
  const half = [Math.ceil(n / 2), Math.floor(n / 2)];
  half.forEach((count, az) => {
    const y0 = 44 + az * 120;
    svg.append(el("rect", { x: 168, y: y0, width: 276, height: 108, rx: 10, fill: "none", stroke: C("--line"), "stroke-dasharray": "4 4" }));
    svg.append(el("text", { x: 178, y: y0 + 16, class: "muted" }, `Availability Zone ${az ? "b" : "a"}`));
    const size = count > 20 ? 14 : 18;
    const gap = count > 20 ? 4 : 6;
    const perRow = Math.floor(256 / (size + gap));
    for (let k = 0; k < count; k++) {
      const x = 178 + (k % perRow) * (size + gap);
      const y = y0 + 26 + Math.floor(k / perRow) * (size + gap);
      svg.append(el("rect", { x, y, width: size, height: size, rx: 4, fill: color, opacity: 0.85 }));
      if (d.local) svg.append(el("circle", { cx: x + size / 2, cy: y + size / 2, r: 3, fill: "#fff", opacity: 0.85 }));
    }
  });
  svg.append(el("text", { x: 306, y: 296, "text-anchor": "middle" }, `served ${pct(b.share)} · 503s ${fmt(b.failed)}/s`));
  svg.append(el("text", { x: 306, y: 313, "text-anchor": "middle", class: "muted" }, b.served ? `oldest list served: ${b.oldest.toFixed(0)} s` : "no list served"));

  // ElastiCache
  svg.append(el("rect", { x: 520, y: 8, width: 232, height: 120, rx: 14, class: "box", stroke: down ? C("--down") : C("--line") }));
  svg.append(el("text", { x: 534, y: 30 }, "ElastiCache for Valkey"));
  svg.append(el("text", { x: 534, y: 48, class: "muted" }, "primary + replica, Multi-AZ"));
  svg.append(el("text", { x: 534, y: 80, class: "big", fill: down ? C("--down") : C("--fresh") }, down ? "unreachable" : "up"));
  const e = entryState(i);
  svg.append(el("text", { x: 534, y: 108, class: "muted" }, `p123: ${e.label}`));
  arrow(svg, 458, 70, 516, 70, 2.5, down ? C("--down") : C("--muted"), down);

  // PostgreSQL
  const hot = b.sent > ui.result.safe_rate;
  svg.append(el("rect", { x: 520, y: 150, width: 232, height: 172, rx: 14, class: "box", stroke: hot ? C("--down") : C("--line") }));
  svg.append(el("text", { x: 534, y: 172 }, "RDS PostgreSQL"));
  const cx = 578, cy = 250, r = 36;
  const angle = Math.min(0.9999, b.cpu) * 2 * Math.PI;
  svg.append(el("circle", { cx, cy, r, fill: "none", stroke: C("--track"), "stroke-width": 10 }));
  const busy = b.cpu > 0.9 ? C("--down") : C("--waited");
  if (b.cpu >= 0.995) {
    svg.append(el("circle", { cx, cy, r, fill: "none", stroke: busy, "stroke-width": 10 }));
  } else if (b.cpu > 0.001) {
    const x = cx + r * Math.sin(angle), y = cy - r * Math.cos(angle);
    svg.append(el("path", { d: `M${cx} ${cy - r} A${r} ${r} 0 ${angle > Math.PI ? 1 : 0} 1 ${x.toFixed(1)} ${y.toFixed(1)}`, fill: "none", stroke: busy, "stroke-width": 10, "stroke-linecap": "round" }));
  }
  svg.append(el("text", { x: cx, y: cy + 5, "text-anchor": "middle", class: "big" }, `${Math.round(b.cpu * 100)}%`));
  svg.append(el("text", { x: cx, y: cy + 54, "text-anchor": "middle", class: "muted" }, "CPU"));
  svg.append(el("text", { x: 628, y: 214 }, `${fmt(b.sent)} queries/s`));
  svg.append(el("text", { x: 628, y: 232, class: "muted" }, `safe: ${ui.result.safe_rate}/s`));
  svg.append(el("text", { x: 628, y: 258 }, `${fmt(b.ok)} finished/s`));
  svg.append(el("text", { x: 628, y: 284 }, `${fmt(b.active)} running`));
  const w = b.sent ? Math.min(10, 1.5 + Math.log10(b.sent + 1) * 2) : 1;
  arrow(svg, 458, 222, 516, 222, w, hot ? C("--down") : C("--muted"));
  svg.append(el("text", { x: 487, y: 212, "text-anchor": "middle", class: "muted", "font-size": 10 }, d.budget ? "admitted" : "misses"));
  arrow(svg, 516, 262, 458, 262, 2, b.ok ? C("--fresh") : C("--muted"), true);
  svg.append(el("text", { x: 487, y: 280, "text-anchor": "middle", class: "muted", "font-size": 10 }, "results"));
}

// ------------------------------------------------------------------ the entry

function entryState(i) {
  const hot = ui.result.series.hot[i];
  const t = ui.result.series.t[i] + ui.result.config.bucket;
  if (valkeyDown(ui.result.series.t[i])) return { label: "unreachable", cls: "gone", hot: null, t };
  if (!hot) return { label: "absent", cls: "gone", hot: null, t };
  const [read, refresh, discard] = hot;
  const age = t - read;
  if (t < refresh) return { label: `fresh, ${age.toFixed(0)} s old`, cls: "fresh", hot, t, age };
  if (t < discard) return { label: `stale, ${age.toFixed(0)} s old`, cls: "stale", hot, t, age };
  return { label: "expired", cls: "gone", hot, t, age };
}

function drawEntry(i) {
  const e = entryState(i);
  const badge = $("#entry-state");
  badge.className = `badge ${e.cls}`;
  badge.textContent = e.label.toUpperCase();
  if (!e.hot) {
    $("#entry").textContent = `// no entry for public-comments:p123:limit20\n// the next read has to refresh it`;
    $("#age-fill").style.left = "100%";
    return;
  }
  const [read, refresh, discard] = e.hot;
  $("#entry").textContent = JSON.stringify({
    key: "public-comments:p123:limit20",
    source_read_at: iso(read),
    refresh_after: iso(refresh),
    discard_after: iso(discard),
    comments: [{ id: "c901", text: "Helpful explanation" }],
  }, null, 2);
  $("#age-fill").style.left = `${Math.min(100, (e.age / 120) * 100)}%`;
}

// ------------------------------------------------------------------ charts

function chart(svg, { layers = [], lines = [], limits = [], top }) {
  const W = 320, H = 150, L = 34, R = 6, T = 8, B = 18;
  const s = ui.result.series;
  const n = s.t.length;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.replaceChildren();
  const x = (i) => L + ((W - L - R) * i) / Math.max(1, n - 1);
  const y = (v) => H - B - ((H - T - B) * Math.min(v, top)) / top;
  const css = getComputedStyle(document.documentElement);
  for (const [a, b] of (ui.result.scenario.bands || [])) {
    const i0 = Math.round(a / ui.result.config.bucket), i1 = Math.min(n - 1, Math.round(b / ui.result.config.bucket));
    svg.append(el("rect", { x: x(i0), y: T, width: x(i1) - x(i0), height: H - T - B, fill: "#eb683422" }));
  }
  for (let k = 0; k <= 5; k++) {
    const v = (top * k) / 5;
    svg.append(el("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: css.getPropertyValue("--line") }));
    svg.append(el("text", { x: L - 4, y: y(v) + 3, "text-anchor": "end" }, fmt(v)));
  }
  for (let k = 0; k <= 4; k++) {
    const i = Math.round(((n - 1) * k) / 4);
    svg.append(el("text", { x: x(i), y: H - 5, "text-anchor": "middle" }, `${Math.round(s.t[i])}s`));
  }
  for (const [at] of (ui.result.scenario.marks || [])) {
    const i = Math.round(at / ui.result.config.bucket);
    svg.append(el("line", { x1: x(i), x2: x(i), y1: T, y2: H - B, stroke: "#eb6834", "stroke-dasharray": "3 3" }));
  }
  let base = new Array(n).fill(0);
  for (const [color, values] of layers) {
    const topLine = values.map((v, i) => base[i] + v);
    const pts = topLine.map((v, i) => `${x(i)},${y(v)}`).join(" ");
    const back = base.map((v, i) => `${x(i)},${y(v)}`).reverse().join(" ");
    svg.append(el("polygon", { points: `${pts} ${back}`, fill: color, opacity: 0.85 }));
    base = topLine;
  }
  for (const [color, values, dashed] of lines) {
    svg.append(el("polyline", { points: values.map((v, i) => `${x(i)},${y(v)}`).join(" "), fill: "none", stroke: color, "stroke-width": 2, "stroke-dasharray": dashed ? "4 3" : "none", "stroke-linejoin": "round" }));
  }
  for (const v of limits) {
    svg.append(el("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: css.getPropertyValue("--down"), "stroke-dasharray": "5 4" }));
  }
  const cur = el("line", { y1: T, y2: H - B, stroke: css.getPropertyValue("--ink"), "stroke-width": 1, opacity: 0.6 });
  svg.append(cur);
  return { cursor: (i) => { cur.setAttribute("x1", x(i)); cur.setAttribute("x2", x(i)); } };
}

function nice(v) {
  if (v <= 0) return 1;
  const step = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (v <= step * m) return step * m;
  return v;
}

function drawCharts() {
  const s = ui.result.series;
  const css = getComputedStyle(document.documentElement);
  const C = (v) => css.getPropertyValue(v).trim();
  const per = 1 / ui.result.config.bucket;
  const failed = s.rejected.map((v, i) => v + s.timed_out[i]);
  const total = s.fresh.map((v, i) => v + s.stale[i] + s.waited[i] + failed[i]);
  const sent = s.db_started.map((v) => v * per);
  const ok = s.db_ok.map((v) => v * per);
  ui.charts = [
    chart($("#c-reads"), {
      layers: [[C("--fresh"), s.fresh], [C("--stale"), s.stale], [C("--waited"), s.waited], [C("--down"), failed]],
      top: nice(Math.max(...total) * 1.05),
    }),
    chart($("#c-db"), {
      lines: [[C("--sent"), sent], [C("--waited"), ok]],
      limits: [ui.result.safe_rate],
      top: nice(Math.max(600, ...sent) * 1.05),
    }),
    chart($("#c-age"), {
      lines: [[C("--sent"), s.oldest]],
      limits: [30, 120],
      top: nice(Math.max(130, ...s.oldest) * 1.05),
    }),
    chart($("#c-cpu"), {
      lines: [[C("--waited"), s.cpu.map((v) => v * 100)], [C("--sent"), s.write_p99.map((v) => v * 1000), true]],
      top: nice(Math.max(100, ...s.write_p99.map((v) => v * 1000)) * 1.05),
    }),
  ];
}

// ------------------------------------------------------------------ the verdict

function tile(label, value, mood) {
  return `<div class="tile ${mood || ""}"><b>${value}</b><span>${label}</span></div>`;
}

function verdict() {
  const r = ui.result;
  const a = r.after;
  const from = r.scenario.watch_from;
  const span = r.config.seconds - from;
  $("#verdict-title").textContent = from ? `What happened from t = ${from} s` : "What happened";
  $("#tiles").innerHTML = [
    tile("reads served", pct(a.served_share), a.served_share >= 0.99 ? "good" : a.served_share < 0.9 ? "bad" : ""),
    tile("queries sent to PostgreSQL per second", fmt(a.db_started / span), a.db_started / span > r.safe_rate ? "bad" : "good"),
    tile("queries that finished in time", fmt(a.db_ok)),
    tile("database CPU, average", pct(a.cpu_mean), a.cpu_mean > 0.9 ? "bad" : ""),
    tile("oldest data served", a.served ? `${a.oldest.toFixed(0)} s` : "none", a.oldest > 120 ? "bad" : ""),
    tile("comment writes, p99", `${Math.round(a.write_p99 * 1000)} ms`, a.write_p99 > 0.1 ? "bad" : "good"),
  ].join("");
  let text;
  if (a.served_share < 0.01) {
    text = `Nobody got comments. PostgreSQL started ${fmt(a.db_started)} queries at ${pct(a.cpu_mean)} CPU and finished ${fmt(a.db_ok)} of them in time.`;
  } else if (a.served_share >= 0.99) {
    text = `Every reader was served${a.stale ? `, ${pct(a.stale / Math.max(1, a.served))} of them a stale list` : ""}. PostgreSQL saw ${fmt(a.db_started)} queries in ${span} s.`;
  } else {
    text = `${pct(a.served_share)} of reads were served and ${fmt(a.failed)} got a 503${a.rejected ? `, ${fmt(a.rejected)} of them rejected at once by the budget` : ""}. PostgreSQL saw ${fmt(a.db_started / span)} queries a second.`;
  }
  text += r.recovered_at === null ? " It never got back to 99% served." : "";
  $("#verdict").textContent = text;
}

// ------------------------------------------------------------------ the real code

async function realBurst(coalesce) {
  $("#burst-out").textContent = "100 threads asking for one cold post…";
  const { body } = await call("/api/real/burst", { callers: 100, coalesce });
  $("#burst-out").innerHTML = `<b>${body.callers} callers → ${body.database_reads} database read${body.database_reads === 1 ? "" : "s"}</b>, ${body.served} served in ${body.seconds} s${coalesce ? "" : " (each caller ran its own query)"}.`;
}

async function realGet() {
  const { status, body } = await call("/posts/p123/comments?limit=20");
  const { comments, ...rest } = body;
  $("#real-out").textContent = `HTTP ${status}\n${JSON.stringify({ ...rest, comments: comments ? [...comments.slice(0, 2), `… ${comments.length} in all`] : undefined }, null, 2)}`;
}

async function realPost() {
  const { status, body } = await call("/posts/p123/comments", { user_id: "u7", text: "Thanks, this helped" });
  $("#real-out").textContent = `HTTP ${status}  (the reply carries the new comment, so the client shows it at once)\n${JSON.stringify(body, null, 2)}`;
}

// ------------------------------------------------------------------ start

async function init() {
  const { body } = await call("/api/scenarios");
  const nav = $("#scenarios");
  for (const [key, sc] of Object.entries(body.scenarios)) {
    const b = document.createElement("button");
    b.textContent = sc.title;
    b.dataset.s = key;
    b.onclick = () => {
      ui.scenario = key;
      for (const id of ["rate", "instances", "deadline"]) $(`#${id}`).value = sc[id];
      showSliders();
      nav.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
      $("#story").textContent = sc.story;
      run();
    };
    nav.append(b);
  }
  nav.querySelector("button").classList.add("on");
  $("#story").textContent = body.scenarios.expiry.story;
  for (const id of ["rate", "instances", "deadline"]) $(`#${id}`).oninput = showSliders;
  showSliders();
  $("#run").onclick = run;
  $("#play").onclick = () => (ui.timer ? stop() : play());
  $("#cursor").oninput = (e) => { stop(); seek(+e.target.value); };
  document.querySelectorAll("[data-layers]").forEach((b) => {
    b.onclick = () => {
      const on = b.dataset.layers.split(" ");
      for (const k of ["stale", "coalesce", "budget", "local"]) document.querySelector(`[data-d="${k}"]`).checked = on.includes(k);
      run();
    };
  });
  document.querySelectorAll("[data-burst]").forEach((b) => { b.onclick = () => realBurst(b.dataset.burst === "true"); });
  $("#get").onclick = realGet;
  $("#post").onclick = realPost;
  run();
}

init();
