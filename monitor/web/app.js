// monitor/web/app.js - the Splash monitor page, fed by monitor.py's GET /metrics. No framework.
"use strict";

const $ = (id) => document.getElementById(id);
const SPRITE = "sprite.svg";
const icon = (name, cls = "st-icon") => `<svg class="${cls}" aria-hidden="true"><use href="${SPRITE}#i-${name}"/></svg>`;
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const fmt = (n, d = 0) => (n == null || Number.isNaN(n) ? "–" : Number(n).toLocaleString(undefined, {maximumFractionDigits: d, minimumFractionDigits: d}));
const kfmt = (n) => (n == null ? "–" : n >= 1000 ? `${fmt(n / 1000, n >= 10000 ? 0 : 1)}k` : fmt(n));
const ctxfmt = (n) => (n && n % 1024 === 0 ? `${fmt(n / 1024)}K` : kfmt(n));
const gb = (b, d = 1) => (b == null ? "–" : fmt(b / 1073741824, d));
const pct = (a, b) => (b ? Math.min(100, (100 * a) / b) : 0);
const MEMORY_WARN_GIB = 2, MEMORY_DANGER_GIB = 0.75;     // Metal headroom below this turns the number amber / red
const STREAK_STALE_S = 600;                              // the zero-reuse warning goes away after 10 quiet minutes
const mbfmt = (b) => (b == null ? "–" : b >= 1073741824 ? `${fmt(b / 1073741824, 2)} GB` : `${fmt(b / 1048576, b < 10485760 ? 1 : 0)} MB`);
const pretty = (k) => String(k).replace(/_/g, " ");
const span = (s) => (s == null ? "–" : s >= 3600 ? `${fmt(s / 3600, 1)} h` : s >= 60 ? `${fmt(s / 60, 1)} min` : `${fmt(s, s < 10 ? 1 : 0)} s`);
const kvRows = (rows) => rows.filter(Boolean).map(([k, v, tip]) => `<div${tip ? ` title="${esc(tip)}"` : ""}><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join("");

// ---- theme
function setTheme(t, save) {
  document.documentElement.dataset.theme = t;
  $("theme-icon").setAttribute("href", `${SPRITE}#i-${t === "dark" ? "sun" : "moon"}`);
  if (save) { try { localStorage.setItem("splash.theme", t); } catch (e) { /* private mode */ } }
}
$("theme-btn").onclick = () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true);
setTheme(document.documentElement.dataset.theme || "light", false);

// ---- the twelve cards (4 + 4 + 4 per row)
const METRICS = [
  {key: "speed", label: "Speed", icon: "gauge"},
  {key: "batch", label: "Batch", icon: "gauge"},
  {key: "reuse", label: "Prefix reuse", icon: "layers", tone: "info"},
  {key: "pressure", label: "Cache pressure", icon: "bolt", tone: "warn"},
  {key: "gpu", label: "GPU load", icon: "gpu"},
  {key: "vram", label: "Metal memory", icon: "layers"},
  {key: "power", label: "GPU power", icon: "bolt"},
  {key: "temp", label: "SoC temp", icon: "thermometer", tone: "warn"},
  {key: "cpu", label: "CPU", icon: "cpu"},
  {key: "ane", label: "Neural Engine", icon: "experts", tone: "info"},
  {key: "disk", label: "Disk read", icon: "disk", tone: "info"},
  {key: "draft", label: "Draft hit", icon: "activity"},
];
const SPARK_PATHS = `<path class="area" fill="currentColor" opacity=".12"/><path class="line" fill="none" stroke="currentColor"
  stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>`;
$("metrics").innerHTML = METRICS.map((m) => `
  <div class="st-card metric-card"><div class="st-metric">
    <span class="st-metric__label">${icon(m.icon, "st-icon st-icon--sm")}${esc(m.label)}${m.key === "pressure" ? ` <span class="st-badge st-badge--queued" id="pressure-badge" hidden>evicting</span>` : ""}</span>
    ${m.key === "speed" ? `<div class="speed-values">
      <div><span class="st-metric__value" id="mv-speed">–</span><span class="st-metric__sub" id="ms-speed">Decode</span></div>
      <div class="speed-prefill"><span class="st-metric__value" id="mv-prefill">–</span><span class="st-metric__sub" id="ms-prefill">Prefill</span></div>
    </div>` : `<span class="st-metric__value" id="mv-${m.key}">–</span><span class="st-metric__sub" id="ms-${m.key}"></span>`}
    <svg class="st-metric__spark" id="sp-${m.key}" viewBox="0 0 100 32" preserveAspectRatio="none"${m.tone ? ` data-tone="${m.tone}"` : ""}>
      ${SPARK_PATHS}${m.key === "speed" ? `<g id="sp-prefill" class="speed-prefill">${SPARK_PATHS}</g>` : ""}</svg>
    ${m.key === "pressure" ? `<details class="pressure-detail"><summary>Totals since the monitor started</summary><div id="pressure-detail"></div></details>` : ""}
  </div></div>`).join("");

function spark(id, values, max) {
  const svg = $(id);
  const v = (values || []).map((x) => (x == null ? 0 : x));
  if (v.length < 2) { svg.querySelector(".line").setAttribute("d", ""); svg.querySelector(".area").setAttribute("d", ""); return; }
  const top = Math.max(max || 0, ...v, 1e-9);
  const pts = v.map((x, i) => [(i / (v.length - 1)) * 100, 30 - (x / top) * 26]);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(2)},${p[1].toFixed(2)}`).join("");
  svg.querySelector(".line").setAttribute("d", line);
  svg.querySelector(".area").setAttribute("d", `${line}L100,32L0,32Z`);
}
function setMetric(key, value, unit, sub) {
  $(`mv-${key}`).innerHTML = value == null ? "–" : `${esc(value)}${unit ? `<small>${esc(unit)}</small>` : ""}`;
  $(`ms-${key}`).textContent = sub || "";
}
function setPill(state, text) {
  $("pill").dataset.state = state === "error" ? "queued" : state;
  $("pill-text").textContent = text;
}
const STATES = {idle: "Idle", reading: "Reading prompt", generating: "Generating", queued: "Queued", released: "Weights released",
  restoring: "Restoring weights", recovering: "Engine restarting", error: "Engine stopped", offline: "Server offline"};

// ---- render
let reqShowAll = false, lastMetrics = null, failures = 0, hideAux = false;
const PATH_TONES = ["", "info", "warn", "danger"];
function render(m) {
  const live = m.live || {}, hw = m.hardware || {}, st = m.hardware_static || {}, eng = m.engine || {}, h = m.history || {};
  const cache = m.cache || {}, lg = m.log || {}, mem = m.memory || {}, srv = m.server || {};
  const reqs = m.requests || [];
  const last = reqs.find((r) => r.finish !== "error") || null;
  const running = (live.active || [])[0] || null;
  $("model-name").textContent = eng.model || "";
  $("header-uptime").textContent = eng.started_at && srv.online ? `up ${span(m.now - eng.started_at)}` : "";

  // notice: what to do when there is no data yet
  const notice = !srv.online ? `Server ${srv.url} is not answering${srv.error ? ` (${srv.error})` : ""}. Start it with launcher/start.command.`
               : !lg.found ? `No log at ${lg.path}, so requests show only what /status saw. launcher/start.command writes this log.` : "";
  $("notice").hidden = !notice;
  $("notice-text").textContent = notice;

  const on = live.state || "offline";
  setPill(["reading", "generating", "queued"].includes(on) ? on : ["error", "offline", "recovering"].includes(on) ? "error" : "idle", STATES[on] || "Idle");
  const badge = on === "offline" || on === "recovering" ? "error" : on === "restoring" ? "released" : on;
  for (const b of document.querySelectorAll("#state-badges .st-badge")) b.classList.toggle("on", b.dataset.s === badge);

  // model state: the engine reports each request's progress
  const prog = $("state-progress");
  let label = STATES[on] || "Waiting for a request", detail = "", p = 0;
  delete prog.dataset.tone;
  if (running && running.phase === "prefill") {
    prog.dataset.tone = "info";
    p = pct(running.prompt_processed, running.prompt_tokens);
    const left = running.prompt_tokens - running.prompt_processed;
    const eta = live.prefill_tok_s > 0 ? ` · about ${span(left / live.prefill_tok_s)} left` : "";
    detail = `${fmt(running.prompt_processed)} / ${fmt(running.prompt_tokens)} tokens · ${fmt(live.prefill_tok_s)} tok/s${eta}`;
  } else if (running && running.phase === "decode") {
    p = pct(running.generated_tokens, running.max_new_tokens);
    detail = `${fmt(running.generated_tokens)} / ${fmt(running.max_new_tokens)} tokens · ${fmt(live.tok_s, 1)} tok/s${live.queued ? ` · ${fmt(live.queued)} waiting` : ""}`;
  } else if (on === "queued" && running) {
    prog.dataset.tone = "warn"; p = 100;
    detail = `${pretty(running.phase)} for ${span((running.age_ms || 0) / 1000)}`;
  } else if (on === "released") {
    detail = "Memory given back while idle; the next request restores it";
  } else if (on === "restoring") {
    prog.dataset.tone = "warn"; p = 100;
  } else if (last) {
    const idle = (live.weights || {}).idle_release_seconds;
    detail = `last: ${fmt(last.output_tokens)} tokens${last.decode_tok_s ? ` at ${fmt(last.decode_tok_s, 1)} tok/s` : ""}${idle ? ` · idle release after ${span(idle)}` : ""}`;
  }
  $("state-label").textContent = label;
  $("state-detail").textContent = detail;
  $("state-bar").style.width = `${p}%`;
  const list = $("active-list");
  list.hidden = (live.active || []).length < 2;
  list.innerHTML = (live.active || []).map((a) => `<div><span class="st-badge">#${esc(a.id)} ${esc(pretty(a.phase))}</span>
    <span class="muted">${fmt(a.prompt_processed)} / ${fmt(a.prompt_tokens)} read · ${fmt(a.generated_tokens)} / ${fmt(a.max_new_tokens)} written · ${span((a.age_ms || 0) / 1000)}</span></div>`).join("");

  // speed: measured from the engine's counters while busy; 0 when idle (the last request's below it)
  const busy = (live.prefilling || 0) + (live.decoding || 0) > 0;
  const lastDec = last && last.decode_tok_s ? `last request ${fmt(last.decode_tok_s, 1)}` : "Decode";
  const lastPre = last && last.prefill_tok_s ? `last request ${fmt(last.prefill_tok_s)}` : "Prefill";
  setMetric("speed", fmt(busy ? live.tok_s || 0 : 0, 1), "t/s", busy ? "Decode now" : lastDec);
  setMetric("prefill", fmt(busy ? live.prefill_tok_s || 0 : 0), "t/s", busy ? "Prefill now" : lastPre);
  spark("sp-speed", h.tok_s);
  spark("sp-prefill", h.prefill_tok_s);

  setMetric("gpu", hw.gpu_util == null ? null : fmt(hw.gpu_util), "%",
            [st.gpu_cores ? `${st.gpu_name || "GPU"} · ${st.gpu_cores} cores` : st.gpu_name, hw.gpu_mhz ? `${fmt(hw.gpu_mhz)} MHz` : ""].filter(Boolean).join(" · "));
  spark("sp-gpu", h.gpu_util, 100);
  setMetric("vram", mem.current == null ? null : gb(mem.current), mem.limit ? `/ ${gb(mem.limit, 0)} GB` : "GB", "");
  spark("sp-vram", h.gpu_mem_used, mem.limit);
  const free = mem.headroom == null ? null : mem.headroom / 1073741824;
  $("ms-vram").textContent = free == null ? "" : `${fmt(free, 1)} GiB headroom · peak ${gb(mem.peak)} GB`;
  $("mv-vram").style.color = free == null ? "" : free < MEMORY_DANGER_GIB ? "var(--st-danger)" : free < MEMORY_WARN_GIB ? "var(--st-warn)" : "";
  setMetric("temp", hw.temp_soc == null ? null : fmt(hw.temp_soc), "°C", [`hottest ${fmt(hw.temp_max, 0)} °C`, eng.thermal ? `thermal ${eng.thermal}` : ""].filter(Boolean).join(" · "));
  spark("sp-temp", h.temp_soc, 100);
  $("mv-temp").style.color = eng.thermal === "critical" ? "var(--st-danger)" : ["serious", "fair"].includes(eng.thermal) ? "var(--st-warn)" : "";
  setMetric("power", hw.gpu_power == null ? null : fmt(hw.gpu_power, hw.gpu_power < 10 ? 1 : 0), "W", "GPU only; this Mac reports no other rail");
  spark("sp-power", h.gpu_power);
  setMetric("cpu", hw.cpu == null ? null : fmt(hw.cpu), "%", (st.clusters || []).map((c) => `${c.cores} ${c.name}`).join(" · "));
  spark("sp-cpu", h.cpu, 100);
  const ane = eng.ane || {};
  setMetric("ane", hw.ane_active == null ? null : fmt(hw.ane_active), "%", hw.ane_awake > 5 && !hw.ane_active ? "awake, not evaluating" : ane.state === "split" ? `FFN split at ${fmt((ane.share || 0) * 100)}% · ${fmt(ane.evaluations)} evaluations` : ane.state || "");
  spark("sp-ane", h.ane_active, 100);
  if (hw.disk_read_mb == null) {
    setMetric("disk", null, "", "");
  } else {
    const big = hw.disk_read_mb >= 1000;
    setMetric("disk", big ? fmt(hw.disk_read_mb / 1024, 2) : fmt(hw.disk_read_mb, hw.disk_read_mb < 10 ? 1 : 0), big ? "GB/s" : "MB/s", `write ${fmt(hw.disk_write_mb, 1)} MB/s`);
  }
  spark("sp-disk", h.disk_read_mb);

  // reuse trend, batch size, draft hit, cache pressure
  const reuseHist = h.reuse || [];
  setMetric("reuse", reuseHist.length ? fmt(reuseHist[reuseHist.length - 1] * 100, 0) : null, "%", reuseHist.length ? `last request · ${reuseHist.length} in trend` : "");
  spark("sp-reuse", reuseHist, 1);
  const bt = m.batch || {};
  setMetric("batch", fmt(bt.width || 0), bt.lanes ? `/ ${bt.lanes}` : "", `${fmt(bt.running)} running · ${fmt(bt.waiting)} waiting`);
  spark("sp-batch", h.batch_size, bt.lanes);
  const sp = m.speculative || {};
  setMetric("draft", sp.rate == null ? null : fmt(sp.rate * 100, 1), "%", sp.cycles ? `${fmt(sp.output / Math.max(1, sp.cycles), 1)} tok/cycle · verify ${sp.verify_rows || "–"}` : "");
  spark("sp-draft", h.draft_hit, 1);
  const pr = m.pressure || {};
  setMetric("pressure", pr.last60s == null ? null : fmt(pr.last60s), "", "evicted / lost / suspended, last 60 s");
  spark("sp-pressure", pr.series);
  $("pressure-badge").hidden = !(pr.last60s > 0);
  $("pressure-detail").innerHTML = Object.entries(pr.total || {}).map(([k, v]) => `<div class="p-row"><span>${esc(pretty(k))}</span><span>${fmt(v)}</span></div>`).join("") || "None";

  // context fill: the running request while there is one, else the last finished one
  const ctx = eng.max_context || 0;
  const used = running ? (running.prompt_tokens || 0) + (running.generated_tokens || 0) : last ? (last.prompt_tokens || 0) + (last.output_tokens || 0) : 0;
  const frac = ctx ? Math.min(1, used / ctx) : 0;
  $("ctx-fill").setAttribute("stroke-dasharray", `${(235.6 * frac).toFixed(1)} 314.2`);
  $("ctx-fill").style.opacity = 235.6 * frac >= 3 ? "1" : "0";
  $("ctx-pct").textContent = `${Math.round(frac * 100)}%`;
  $("ctx-sub").textContent = ctx ? `${kfmt(used)} / ${ctxfmt(ctx)}` : "–";
  $("ctx-note").textContent = `${running ? "This request" : "Last request"}, limited by the ${esc(mem.context_bound || "model")}. KV pool for all cached chats: ${fmt(eng.kv_capacity_tokens)} tokens.`;

  const rr = (m.reuse || {}).recent;
  $("reuse-text").textContent = rr == null ? "–" : `${fmt(rr * 100, 1)}%` + (m.reuse.zero_of[0] ? ` · ${m.reuse.zero_of[0]} of ${m.reuse.zero_of[1]} read in full` : "");
  $("reuse-bar").style.width = `${rr == null ? 0 : rr * 100}%`;
  $("ram-text").textContent = hw.ram_total ? `${gb(hw.ram_used)} / ${gb(hw.ram_total, 0)} GB · macOS ${fmt(hw.memory_level)}% free` : "–";
  const ramPct = pct(hw.ram_used, hw.ram_total);
  $("ram-bar").style.width = `${ramPct}%`;
  if (ramPct > 92 || mem.pressure === "critical") $("ram-progress").dataset.tone = "danger"; else if (mem.pressure === "warning") $("ram-progress").dataset.tone = "warn"; else delete $("ram-progress").dataset.tone;
  $("swap-text").textContent = hw.swap_total == null ? "–" : `${gb(hw.swap_used, 2)} / ${gb(hw.swap_total, 1)} GB`;
  $("swap-bar").style.width = `${pct(hw.swap_used, hw.swap_total)}%`;
  $("temp-text").textContent = hw.temp_soc == null ? "–" : `${fmt(hw.temp_soc)} °C`;
  $("temp-bar").style.width = hw.temp_soc == null ? "0%" : `${Math.min(100, hw.temp_soc)}%`;
  const memWarn = mem.pressure && mem.pressure !== "normal";
  $("mem-strip").hidden = !memWarn;
  $("mem-text").textContent = `macOS reports ${mem.pressure} memory pressure. Other applications may slow down; the engine suspends requests that need more memory at critical.`;

  // recent requests
  const body = $("req-body");
  const shownReqs = hideAux ? reqs.filter((r) => r.kind !== "aux") : reqs;
  if (!shownReqs.length) {
    body.innerHTML = `<tr><td colspan="13" class="muted">${reqs.length ? "Only aux requests so far" : "No requests yet"}</td></tr>`;
  } else {
    const badges = {stop: ["", "Done"], length: ["", "Max tokens"], cancel: ["st-badge--queued", "Stopped"], error: ["st-badge--error", "Error"]};
    body.innerHTML = shownReqs.map((r) => {
      const [cls, text] = badges[r.finish] || ["", r.finish || "–"];
      const t = new Date(r.time * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
      const pathTip = r.reuse_path ? ` title="${esc(r.reuse_path)}${r.deduplicated ? ` · ${fmt(r.deduplicated)} deduplicated` : ""}"` : r.message ? ` title="${esc(r.message)}"` : "";
      const draft = r.drafted ? `${fmt((100 * r.accepted) / r.drafted, 0)}%` : "–";
      const sec = (v) => (v == null ? "–" : `${fmt(v, v < 10 ? 2 : 1)} s`);
      const think = r.thinking == null ? "–" : `<span title="effort ${esc(r.effort || "absent")}">${r.thinking ? "on" : "off"}</span>`;
      const tools = r.tools ? `<span title="${esc(r.tool_signature || "")}">${fmt(r.tools)}</span>` : "–";
      return `<tr${r.zero_reuse ? ' class="zero" title="Longer prompt than the previous request but nothing reused (possibly the same conversation)"' : ""}>
        <td>${esc(t)}</td><td><span class="st-badge ${cls}"${pathTip}>${esc(text)}</span>${r.kind === "aux" ? '<span class="tag" title="aux: very short request without tools">aux</span>' : ""}</td>
        <td class="num">${fmt(r.prompt_tokens)}</td><td class="num"${pathTip}>${fmt(r.reused + (r.deduplicated || 0))}</td><td class="num">${fmt(r.output_tokens)}</td>
        <td class="num">${fmt(r.decode_tok_s, 1)}</td><td class="num">${draft}</td><td class="num">${fmt(r.tok_cycle, 1)}</td><td class="num">${sec(r.ttft_s)}</td><td class="num">${sec(r.queue_s)}</td>
        <td>${think}</td><td class="num">${tools}</td><td class="num">${durCell(r)}</td></tr>`;
    }).join("");
  }
  const all = $("req-all");
  all.hidden = (m.kept || 0) <= 12;
  all.textContent = reqShowAll ? "Show fewer" : `Show all (${m.kept})`;
  $("req-wrap").classList.toggle("all", reqShowAll);
  $("req-totals").textContent = renderTotals(m.totals || {}, m.finish_counts || {});

  // conversation cache: the engine's own gauges
  const bar = (key, used, total, text) => {
    $(`cc-${key}-text`).textContent = total ? text : "–";
    $(`cc-${key}-bar`).style.width = `${pct(used, total)}%`;
  };
  bar("pg", cache.kv_pages_used, cache.kv_pages_total, `${fmt(cache.kv_pages_used)} / ${fmt(cache.kv_pages_total)} pages (×${cache.kv_page_tokens} tok)`);
  bar("kv", cache.kv_allocated, cache.kv_capacity, `${gb(cache.kv_allocated, 2)} / ${gb(cache.kv_capacity, 1)} GB`);
  const ds = cache.disk || {}, ss = cache.state || {};
  bar("disk", ds.used_bytes, ds.limit_bytes, `${gb(ds.used_bytes, 2)} / ${gb(ds.limit_bytes, 1)} GB${ds.persistent ? " · persistent" : ""}`);
  $("cc-state-text").textContent = `${fmt(ss.entries)} entries · ${mbfmt(ss.bytes)} · ${fmt(ss.checkpoint_entries)} checkpoints`;
  $("cc-state-bar").style.width = `${pct(ss.bytes, mem.dynamic)}%`;
  $("cc-sum").textContent = cache.hit_rate == null ? "" : `hit rate ${fmt(cache.hit_rate * 100, 0)}% · ${fmt(cache.hits)} hits · ${fmt(cache.cold_misses)} cold · ${fmt(cache.lost_state_misses)} lost`;
  $("xfer").innerHTML = `<div><strong>SSD</strong>: read ${mbfmt(ds.read_bytes)} · wrote ${mbfmt(ds.written_bytes)} · ${gb(ds.volume_available_bytes, 0)} GB free on the volume · writes gated: ${esc(ds.writes_gated || "–")}</div>`;
  const ic = cache.image_cache || {}, tc = cache.tokenizer || {}, gc = cache.grammar || {}, rs = cache.responses || {};
  $("cc-grid").innerHTML = kvRows([
    ["Reused tokens", fmt(cache.reused_tokens)], ["KV hit tokens", fmt(cache.kv_hit_tokens)], ["KV disk hit tokens", fmt(cache.kv_disk_hit_tokens)],
    ["Deduplicated KV tokens", fmt(cache.deduplicated_kv_tokens)], ["State publications", fmt(ss.publications)], ["State evictions", fmt(ss.evictions)],
    ["Offloads to SSD", fmt(ss.offloads)], ["Disk promotions", fmt(ss.disk_promotions)], ["KV demotions", fmt(ds.kv_demotions)], ["KV restores", fmt(ds.kv_restores)],
    ["Requests suspended", fmt(cache.resource_suspensions)], ["Replayed tokens", fmt(cache.resource_replay_tokens)],
    ["Tokenizer cache", `${fmt(tc.entries)} / ${fmt(tc.capacity)} · ${fmt(tc.reused_tokens)} tok reused`], ["Grammar cache", `${fmt(gc.entries)} / ${fmt(gc.capacity)} · ${fmt(gc.hits)} hits`],
    ["Response store", `${fmt(rs.entries)} · ${mbfmt(rs.bytes)}`], ["Image cache", `${fmt(ic.entries)} · ${mbfmt(ic.bytes)}`],
  ]);

  // reuse paths, last 100 requests
  const pe = Object.entries((m.reuse || {}).paths || {}).sort((a, b) => b[1].n - a[1].n);
  const pn = pe.reduce((a, [, v]) => a + v.n, 0);
  $("paths-sum").textContent = pn ? `${pn} requests` : "–";
  $("paths").innerHTML = pn ? pe.map(([k, v], i) => `<div><div class="p-row"><span>${esc(pretty(k))}</span><span class="muted">${v.n} · ${fmt((100 * v.n) / pn, 0)}% · ${kfmt(v.tokens)} tok</span></div>
    <div class="st-progress"${PATH_TONES[i % 4] ? ` data-tone="${PATH_TONES[i % 4]}"` : ""}><div class="st-progress__bar" style="width:${(100 * v.n) / pn}%"></div></div></div>`).join("")
    : '<span class="muted small">–</span>';

  // warning when the same conversation keeps being read in full
  const zs = (m.reuse || {}).zero_streak || 0;
  const zAge = (m.reuse || {}).streak_age_s;
  $("warn-strip").hidden = zs < ((m.reuse || {}).alert_at || 3) || zAge == null || zAge > STREAK_STALE_S;
  $("warn-text").textContent = `${zs} requests in a row read a longer prompt in full with no reuse (possibly one conversation whose cache was lost).`;

  // speculative decoding: tokens per cycle of the recent requests, out of the block of eight
  const rec = sp.recent || [];
  const nb = rec.length, block = sp.block || 8;
  $("spec-bars").innerHTML = nb ? rec.map((r, i) => {
    const w = 140 / Math.max(nb, 8), x = i * w + w * 0.15, bw = w * 0.7, hh = 54 * Math.min(1, r.tok_cycle / block);
    return `<rect class="track" x="${x.toFixed(1)}" y="2" width="${bw.toFixed(1)}" height="54" rx="2"/><rect class="fill" x="${x.toFixed(1)}" y="${(56 - hh).toFixed(1)}" width="${bw.toFixed(1)}" height="${hh.toFixed(1)}" rx="2"><title>${fmt(r.tok_cycle, 1)} tokens a cycle${r.hit == null ? "" : ` · draft hit ${fmt(r.hit * 100, 0)}%`}</title></rect>` +
           (nb <= 10 ? `<text x="${(x + bw / 2).toFixed(1)}" y="66">${fmt(r.tok_cycle, 1)}</text>` : "");
  }).join("") : "";
  $("spec-note").textContent = sp.drafted ? `Tokens per cycle, last ${nb} requests · accepted ${fmt(sp.accepted)} of ${fmt(sp.drafted)} drafted · ${fmt(sp.cycles)} cycles · ${fmt(sp.resets)} draft resets · the engine reports no per-position counts` : "No speculative requests yet";

  // GPU vs host
  const pf = m.perf || {};
  const share = (value, tid, bid) => { $(tid).textContent = value == null ? "–" : `${fmt(value * 100, 1)}%`; $(bid).style.width = `${value == null ? 0 : value * 100}%`; };
  share(pf.decode_gpu_share, "wait-text", "wait-bar");
  share(pf.decode_host_share, "host-text", "host-bar");
  share(pf.prefill_ane_share, "ane-share-text", "ane-share-bar");
  $("host-note").textContent = pf.decode_cycle_ms ? `Since the engine started. Last prefill command ${fmt(pf.last_prefill_ms)} ms, last decode command ${fmt(pf.last_decode_ms, 1)} ms.` : "Since the engine started";

  // prefill speed by prompt size
  const bks = m.prefill_buckets || [];
  const bmax = Math.max(1, ...bks.map((b) => b.avg_tok_s || 0));
  $("buckets").innerHTML = bks.map((b) => `<div class="bar-row"><span>${esc(b.name)} <span class="muted">(${b.n})</span></span><span class="muted">${b.avg_tok_s == null ? "–" : `${fmt(b.avg_tok_s)} tok/s`}</span></div>
    <div class="st-progress" data-tone="info"><div class="st-progress__bar" style="width:${b.avg_tok_s == null ? 0 : (100 * b.avg_tok_s) / bmax}%"></div></div>`).join("");

  // errors
  const er = m.errors || {};
  $("err-badge").hidden = !er.total;
  $("err-badge").textContent = er.total ? `${fmt(er.total)} ${er.total === 1 ? "error" : "errors"}` : "";
  $("err-note").textContent = er.transport_error || er.metal_reason || (er.total ? `${fmt((er.by_reason || []).length)} distinct ${(er.by_reason || []).length === 1 ? "reason" : "reasons"}` : "No errors");
  $("err-details").hidden = !er.total;
  $("err-list").innerHTML = (er.by_reason || []).map(([k, v]) => {
    const last = (er.recent || []).find((e) => e.code === k);
    return `<div title="${esc(last ? last.message : "")}"><span>${esc(k)}</span><span class="muted">${fmt(v)}</span></div>`;
  }).join("");
  $("err-counters").innerHTML = kvRows(Object.entries(er.counters || {}).map(([k, v]) => [k, fmt(v)]));

  renderPlan(mem, hw);
  renderEngine(m, eng, hw, st);
  renderLatency(m);
  renderSensors(hw, st);
  renderLog(lg);
  if ($("raw-card").open) pollRaw();
}
const PLAN_COLORS = ["var(--st-accent)", "var(--st-info)", "var(--st-warn)", "#8b5cf6", "#ec4899", "#14b8a6", "#a3a3a3", "#737373", "#525252", "#404040"];
function renderPlan(mem, hw) {
  const parts = mem.parts || [];
  if (!mem.limit) { $("plan-stack").innerHTML = ""; $("plan-legend").innerHTML = ""; $("plan-list").innerHTML = ""; $("plan-sum").textContent = ""; return; }
  const kvIn = mem.kv_allocated || 0, kvRest = Math.max(0, (mem.kv_capacity || 0) - kvIn);
  const segs = parts.map(([name, b], i) => [name, b, PLAN_COLORS[i % PLAN_COLORS.length], ""]).concat([
    ["Lane state", mem.lane_state || 0, "#d97706", ""], ["KV in use", kvIn, "var(--st-danger)", ""], ["KV room", kvRest, "var(--st-danger)", " stack__seg--room"]]);
  const total = mem.limit;
  $("plan-stack").innerHTML = segs.filter((s) => s[1] > 0).map(([name, b, color, cls]) =>
    `<i class="stack__seg${cls}" style="width:${(100 * b) / total}%;background:${color}" title="${esc(name)}: ${gb(b, 2)} GB"></i>`).join("");
  $("plan-legend").innerHTML = segs.filter((s) => s[1] > 0).map(([name, b, color]) =>
    `<span><i style="background:${color}"></i>${esc(name)} ${gb(b, 2)}</span>`).join("");
  $("plan-sum").textContent = `fixed ${gb(mem.fixed)} + dynamic ${gb(mem.dynamic)} = ${gb(mem.limit)} GB`;
  $("plan-list").innerHTML = kvRows([
    ["Metal allocated now", `${gb(mem.current, 2)} GB · peak ${gb(mem.peak, 2)} GB`], ["Headroom under the limit", `${gb(mem.headroom, 2)} GB`],
    ["Host memory available", `${gb(mem.host_available, 2)} GB · reserve ${gb(mem.host_reserve, 1)} GB`], ["Memory pressure", mem.pressure || "–"],
    ["Growth allowed", mem.growth_allowed == null ? "–" : mem.growth_allowed ? "yes" : "no"], ["Recommended working set", `${gb(mem.recommended)} GB`],
    ["--max-memory", mem.configured ? `${gb(mem.configured)} GB` : "auto"],
    hw.ram_wired == null ? null : ["Mac: wired / app / compressed / file", `${gb(hw.ram_wired)} / ${gb(hw.ram_app)} / ${gb(hw.ram_compressed)} / ${gb(hw.ram_file)} GB`],
  ]);
}
function renderEngine(m, eng, hw, st) {
  const ane = eng.ane || {}, proc = m.process || {}, srv = proc.server, en = proc.engine, tr = m.transport || {}, http = m.http || {}, fe = m.frontend || {}, rq = m.requests_counters || {}, sc = m.scheduler || {}, wa = (m.live || {}).weights || {};
  const b = $("ane-badge");
  b.textContent = ane.state || "–";
  b.className = `st-badge${ane.state === "split" ? " st-badge--generating" : ane.state === "stopped" ? " st-badge--error" : ""}`;
  $("ane-list").innerHTML = kvRows([
    ["Share of the prefill FFN", ane.share == null ? "–" : `${fmt(ane.share * 100, 1)}% · chunks of ${fmt(ane.minimum_rows)}+ rows`],
    ["Awake now (SoC counters)", hw.ane_awake == null ? "–" : `${fmt(hw.ane_awake, 1)}%`, "The SoC counts the Neural Engine as active while it is awake, which outlasts its work"],
    ["Busy in the last block (engine)", eng.ane_engine_share == null ? "–" : `${fmt(eng.ane_engine_share * 100, 0)}%`],
    ["Evaluations · commands", `${fmt(ane.evaluations)} · ${fmt(ane.split_commands)}`], ["Time on the ANE", `${fmt((ane.ane_ms || 0) / 1000, 1)} s`], ["Chunks rerun on the GPU", fmt(ane.reruns)],
    ["Weights", wa.released ? "released" : wa.restoring ? "restoring" : `loaded · ${fmt(wa.restores)} restores`],
    ["Requests", `${fmt(rq.submitted)} in · ${fmt(rq.completed)} done · ${fmt(rq.cancelled)} cancelled · ${fmt(rq.failed)} failed`],
    ["Scheduler", `${fmt(sc.prefill_batches)} prefill batches · ${fmt(sc.decode_batches)} decode batches`],
    ["Decode batches by width", Object.entries(sc.decode_batches_by_width || {}).map(([k, v]) => `${k} ${fmt(v)}`).join(" · ") || "–"],
  ]);
  const row = (name, p) => (p ? [name, `${gb(p.footprint, 2)} GB footprint · CPU ${fmt(p.cpu_percent)}% · read ${fmt(p.disk_read_mb, 1)} MB/s`, `resident ${gb(p.resident, 2)} GB · ${fmt(p.pageins)} page-ins · ${gb(p.disk_read)} GB read · ${gb(p.disk_written)} GB written`] : null);
  $("proc-list").innerHTML = kvRows([
    row("Engine process", en), row("Server process", srv),
    ["HTTP", `${fmt((http.requests || {}).active)} / ${fmt((http.requests || {}).capacity)} requests · ${fmt((http.connections || {}).active)} / ${fmt((http.connections || {}).capacity)} connections`],
    ["Preparation", `${fmt(fe.active)} / ${fmt(fe.preparation_capacity)} active · ${fmt(fe.waiting)} waiting`],
    ["Engine transport", `${fmt(tr.pending)} / ${fmt(tr.pending_limit)} pending · ${fmt(tr.restarts)} restarts · status ${fmt(tr.status_age_ms)} ms old`],
    ["Longest engine tick", `${fmt((m.loop || {}).max_tick_ms, 1)} ms`], ["PID · instance", `${fmt(eng.pid).replace(/,/g, "")} · ${eng.instance || "–"}`],
  ]);
}
// The histogram's cumulative buckets: the upper bound of the one holding quantile q.
function bucketQuantile(entry, q) {
  const target = entry.count * q;
  // JSON keeps the buckets in order, but an object lists integer-like keys first.
  const rows = Object.entries(entry.buckets || {}).sort((a, b) => Number(a[0]) - Number(b[0]));
  for (const [bound, count] of rows) if (count >= target) return bound;
  return null;
}
function renderLatency(m) {
  const lat = m.latency || {};
  const time = (s) => (s == null ? "–" : s < 1 ? `${fmt(s * 1000, s < 0.01 ? 2 : 0)} ms` : `${fmt(s, 2)} s`);
  const bound = (b) => (b == null ? "–" : b === "+Inf" ? "over 1800 s" : time(Number(b)));
  const rows = Object.entries(lat).filter(([, e]) => e.count).map(([k, e]) =>
    `<tr><td>${esc(pretty(k))}</td><td class="num">${fmt(e.count)}</td><td class="num">${time(e.mean_s)}</td><td class="num">${bound(bucketQuantile(e, 0.5))}</td><td class="num">${bound(bucketQuantile(e, 0.95))}</td></tr>`);
  $("lat-body").innerHTML = rows.join("") || '<tr><td colspan="5" class="muted">No requests yet</td></tr>';
  const t = m.ttft || {}, i = m.itl || {};
  $("lat-sum").textContent = t.samples ? `native TTFT p50 ${fmt(t.p50)} ms · p95 ${fmt(t.p95)} ms · interval p50 ${fmt(i.p50)} ms` : "";
}
function renderSensors(hw, st) {
  const sensors = Object.entries(hw.sensors || {}).sort((a, b) => b[1] - a[1]);
  const top = Math.max(100, ...sensors.map((s) => s[1]));
  $("sensors-sum").textContent = sensors.length ? `${sensors.length} sensors · hottest ${fmt(hw.temp_max, 1)} °C` : "";
  $("sensors").innerHTML = sensors.map(([k, v]) => `<div title="${esc(k)}"><span>${esc(k.replace(/^PMU /, ""))}</span><span class="st-progress" data-tone="${v >= 85 ? "danger" : "warn"}"><span class="st-progress__bar" style="display:block;width:${(100 * v) / top}%"></span></span><span class="muted">${fmt(v, 1)}</span></div>`).join("");
  const clusters = Object.entries(hw.cpu_clusters || {}).map(([k, v]) => `${k} ${fmt(v)}%`).join(" · ");
  $("hw-list").innerHTML = kvRows([
    ["Chip", `${st.chip || "–"}${st.threads ? ` · ${st.threads} threads` : ""}`], ["CPU clusters busy", clusters || "–"],
    ["GPU active / tiler / renderer", `${fmt(hw.gpu_active)}% / ${fmt(hw.gpu_tiler)}% / ${fmt(hw.gpu_renderer)}%`, "Active: P-state residency, which counts a GPU that is merely awake"],
    ["GPU frequency", hw.gpu_mhz ? `${fmt(hw.gpu_mhz)} MHz of ${fmt(st.gpu_mhz_max)}` : "–"], ["GPU recoveries", fmt(hw.gpu_recoveries)],
    ["Disk write", `${fmt(hw.disk_write_mb, 1)} MB/s`], ["Swapped out / in", `${gb(hw.swapouts)} / ${gb(hw.swapins)} GB since boot`],
    Object.keys((lastMetrics || {}).hardware_errors || {}).length ? ["Unavailable", Object.keys(lastMetrics.hardware_errors).join(", ")] : null,
  ]);
}
function renderLog(lg) {
  const lines = lg.lines || [];
  $("log-sum").textContent = lg.found ? `${lg.path}${lg.skipped ? ` · ${lg.skipped} lines skipped` : ""}` : "no log";
  const box = $("log-box");
  const follow = box.scrollTop < 8;
  box.innerHTML = lines.map((l) => {
    const t = l.time ? new Date(l.time * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"}) : "";
    const bad = /^(Error|Warning)/i.test(l.text);
    return `<div${bad ? ' class="bad"' : ""}><span class="muted">${esc(t)}</span> ${esc(l.text)}</div>`;
  }).join("") || '<span class="muted">No startup lines yet</span>';
  if (follow) box.scrollTop = 0;
}
function durCell(r) {
  if (r.duration_s == null) return "–";
  const parts = [["d-queue", r.queue_s, "queue"], ["d-pre", r.prefill_s, "prefill"], ["d-dec", r.decode_s, "decode"]];
  const sum = parts.reduce((a, p) => a + (p[1] || 0), 0);
  const scale = Math.max(r.duration_s, sum) || 1;
  const tip = parts.map((p) => `${p[2]} ${p[1] == null ? "–" : fmt(p[1], 2) + " s"}`).join(" · ") + ` · total ${fmt(r.duration_s, 2)} s`;
  return `<div class="dur" title="${esc(tip)}">${parts.map((p) => `<i class="${p[0]}" style="width:${(100 * (p[1] || 0)) / scale}%"></i>`).join("")}</div>${fmt(r.duration_s, 1)} s`;
}
function renderTotals(t, fc) {
  if (!t.requests) return "";
  const since = t.since ? new Date(t.since * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}) : "the log start";
  const read = t.computed || 0;
  const pSpeed = t.prefill_s > 0 && read > 0 ? ` at ${fmt(read / t.prefill_s)} tok/s` : "";
  const oSpeed = t.decode_s > 0 && t.output_tokens > 0 ? ` at ${fmt(t.output_tokens / t.decode_s, 1)} tok/s` : "";
  return `Since ${since}: ${fmt(t.requests)} requests · ${fmt(read)} prompt tokens read${pSpeed} (${fmt(t.reused)} reused of ${fmt(t.prompt_tokens)}) · ` +
         `${fmt(t.output_tokens)} written${oSpeed}${t.errors ? ` · ${fmt(t.errors)} errors` : ""}` +
         (fc.length ? ` · ${fmt(fc.length)} hit max tokens` : "") + (fc.cancel ? ` · ${fmt(fc.cancel)} stopped` : "");
}
$("hide-aux").addEventListener("change", (e) => { hideAux = e.target.checked; if (lastMetrics) render(lastMetrics); });
$("req-all").addEventListener("click", () => { reqShowAll = !reqShowAll; if (lastMetrics) render(lastMetrics); poll(true); });

// ---- every field of /status, as flat rows
let rawStatus = null, rawBusy = false;
function flatten(value, path, out) {
  if (value && typeof value === "object") {
    const entries = Array.isArray(value) ? value.map((v, i) => [i, v]) : Object.entries(value);
    if (!entries.length) out.push([path, Array.isArray(value) ? "[]" : "{}"]);
    for (const [k, v] of entries) flatten(v, path ? `${path}.${k}` : String(k), out);
  } else out.push([path, value === null ? "null" : String(value)]);
  return out;
}
function renderRaw() {
  if (!rawStatus) return;
  const rows = flatten(rawStatus, "", []);
  const q = $("raw-search").value.trim().toLowerCase();
  const shown = q ? rows.filter(([k, v]) => k.toLowerCase().includes(q) || v.toLowerCase().includes(q)) : rows;
  $("raw-sum").textContent = `${fmt(shown.length)} of ${fmt(rows.length)} fields, live`;
  $("raw-tree").innerHTML = shown.map(([k, v]) => `<div><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join("");
}
async function pollRaw() {
  if (rawBusy) return;
  rawBusy = true;
  try {
    const r = await fetch("api/status", apiKey ? {headers: {Authorization: `Bearer ${apiKey}`}} : {});
    if (r.ok) { rawStatus = await r.json(); renderRaw(); }
  } catch (e) { /* the next poll tries again */ }
  rawBusy = false;
}
$("raw-card").addEventListener("toggle", () => { if ($("raw-card").open) pollRaw(); });
$("raw-search").addEventListener("input", renderRaw);

// ---- poll once a second; the next poll is queued after the previous one finishes
let timer = null, apiKey = "", keyDeclined = false;
try { apiKey = sessionStorage.getItem("splash.key") || ""; } catch (e) { /* private mode */ }
async function poll(once) {
  try {
    const r = await fetch(reqShowAll ? "metrics?requests=all" : "metrics", apiKey ? {headers: {Authorization: `Bearer ${apiKey}`}} : {});
    if (r.status === 401) {
      setPill("error", "API key needed");
      const k = keyDeclined ? null : prompt(apiKey ? "That key was rejected. API key for the monitor (--api-key):" : "API key for the monitor (--api-key):");
      if (!k) keyDeclined = true;            // asked once; reload the page to be asked again
      else { apiKey = k.trim(); try { sessionStorage.setItem("splash.key", apiKey); } catch (e) { /* private mode */ } }
    }
    else if (r.ok) { lastMetrics = await r.json(); failures = 0; render(lastMetrics); }
  } catch (e) {
    if (++failures >= 3) setPill("error", "Monitor not reachable");
  }
  if (!once) { clearTimeout(timer); timer = setTimeout(poll, 1000); }
}
poll();
