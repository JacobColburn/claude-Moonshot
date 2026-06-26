/* Moonshot Engine dashboard — live WebSocket client. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const SIG_KEYS = ["momentum", "volume", "pressure", "liquidity", "turnover", "freshness"];
  const SIG_VARS = ["--mom", "--vol", "--pre", "--liq", "--tur", "--fre"];

  const fmt = (n, d = 2) => (n === null || n === undefined || isNaN(n)) ? "—" : Number(n).toFixed(d);
  const sol = (n, d = 3) => `${fmt(n, d)} ◎`;
  const signed = (n, d = 2) => (n >= 0 ? "+" : "") + fmt(n, d);
  const cls = (n) => (n > 0 ? "pos" : n < 0 ? "neg" : "");
  const ago = (s) => {
    s = Math.max(0, Math.floor(s));
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m";
    if (s < 86400) return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
    return Math.floor(s / 86400) + "d";
  };
  const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour12: false });

  // ── equity chart ──────────────────────────────────────────────────────────
  let chart = null;
  function initChart() {
    const ctx = $("equityChart");
    if (!ctx || !window.Chart) return;
    const g = ctx.getContext("2d").createLinearGradient(0, 0, 0, 260);
    g.addColorStop(0, "rgba(139,108,255,0.45)");
    g.addColorStop(1, "rgba(139,108,255,0.0)");
    chart = new Chart(ctx, {
      type: "line",
      data: { labels: [], datasets: [{
        data: [], borderColor: "#8b6cff", borderWidth: 2.5,
        backgroundColor: g, fill: true, tension: 0.35,
        pointRadius: 0, pointHoverRadius: 4, pointHoverBackgroundColor: "#fff",
      }]},
      options: {
        responsive: true, maintainAspectRatio: false, animation: { duration: 350 },
        plugins: { legend: { display: false }, tooltip: {
          backgroundColor: "#15122b", borderColor: "#8b6cff", borderWidth: 1,
          callbacks: { label: (c) => " " + c.parsed.y.toFixed(4) + " SOL" } } },
        scales: {
          x: { display: false },
          y: { grid: { color: "rgba(255,255,255,0.05)" },
               ticks: { color: "#8a86a8", font: { family: "JetBrains Mono", size: 10 },
                        callback: (v) => v.toFixed(2) } },
        },
      },
    });
  }
  function updateChart(curve) {
    if (!chart || !curve) return;
    chart.data.labels = curve.map((p) => hhmm(p.t));
    chart.data.datasets[0].data = curve.map((p) => p.equity);
    const first = curve.length ? curve[0].equity : 0;
    const last = curve.length ? curve[curve.length - 1].equity : 0;
    chart.data.datasets[0].borderColor = last >= first ? "#3ddc84" : "#ff4d6d";
    chart.update("none");
  }

  // ── renderers ───────────────────────────────────────────────────────────
  function renderState(s) {
    const cfg = s.config || {};
    const p = s.portfolio || {};
    const st = p.stats || {};

    // header
    const mode = s.mode || cfg.mode || "PAPER";
    const modeChip = $("modeChip");
    modeChip.textContent = mode === "LIVE" ? "● LIVE" : "🧪 PAPER";
    modeChip.className = "chip " + (mode === "LIVE" ? "live" : "paper");
    $("brainChip").textContent = cfg.brain_enabled ? "🧠 " + (cfg.brain_model || "claude") : "brain off";
    $("uptimeChip").textContent = "up " + ago(s.uptime_seconds || 0);
    $("scanChip").textContent = "scans " + (s.scan_count || 0);
    if (s.brain_read) $("brainRead").textContent = s.brain_read;

    // stat cards
    $("equity").textContent = sol(st.equity_sol);
    const ret = st.total_return_pct || 0;
    const rp = $("equityReturn"); rp.textContent = signed(ret) + "%"; rp.className = "pill " + cls(ret);
    $("realized").innerHTML = `<span class="${cls(st.realized_pnl_sol)}">${signed(st.realized_pnl_sol, 3)} ◎</span>`;
    $("realizedSub").textContent = `${st.closed_trades || 0} closed trades`;
    $("winrate").textContent = fmt(st.win_rate, 1) + "%";
    $("winrateSub").textContent = `${st.wins || 0} W / ${st.losses || 0} L`;
    $("cash").textContent = sol(st.cash_sol);
    $("deployed").textContent = `${sol(st.positions_value_sol)} in positions`;
    $("openCount").textContent = `${st.open_positions || 0}/${cfg.max_positions || "—"}`;
    $("pfSub").textContent = "PF " + (st.profit_factor === null || st.profit_factor === undefined ? "—" : fmt(st.profit_factor, 2));

    updateChart(p.equity_curve);
    $("equityNote").textContent = st.starting_sol ? `start ${fmt(st.starting_sol, 2)} ◎` : "";

    renderPositions(p.positions || []);
    renderTrades(p.closed || []);
    renderBoard(s.leaderboard || []);
  }

  function renderPositions(positions) {
    const el = $("positions");
    $("posNote").textContent = positions.length ? `${positions.length} open` : "flat";
    if (!positions.length) { el.innerHTML = `<div class="empty">No open positions yet.</div>`; return; }
    el.innerHTML = positions.map((p) => {
      const pnl = p.pnl_pct || 0;
      const peakGain = p.entry_price_usd ? ((p.peak_price_usd - p.entry_price_usd) / p.entry_price_usd * 100) : 0;
      const barW = Math.max(2, Math.min(100, 50 + pnl));
      const col = pnl >= 0 ? "var(--green)" : "var(--red)";
      return `<div class="pcard">
        <div class="row"><span class="sym">${p.symbol}</span><span class="tag">${fmt(p.score,0)}</span></div>
        <div class="row"><span class="big ${cls(pnl)}">${signed(pnl,1)}%</span>
          <span class="${cls(p.value_sol - p.sol_in)}">${sol(p.value_sol)}</span></div>
        <div class="pbar"><i style="width:${barW}%;background:${col}"></i></div>
        <div class="meta"><span>in ${fmt(p.sol_in,3)} ◎</span><span>peak +${fmt(peakGain,0)}%</span><span>${ago(p.age_minutes*60)}</span></div>
      </div>`;
    }).join("");
  }

  function renderTrades(closed) {
    const el = $("trades");
    if (!closed.length) { el.innerHTML = `<div class="empty">No closed trades yet.</div>`; return; }
    el.innerHTML = closed.map((t) => `
      <div class="trow">
        <div class="l"><b>${t.symbol}</b><span class="reason">${t.reason || ""}</span></div>
        <div class="pnl ${cls(t.pnl_sol)}">${signed(t.pnl_pct,1)}%<br><small>${signed(t.pnl_sol,3)} ◎</small></div>
      </div>`).join("");
  }

  function renderBoard(rows) {
    const el = $("board");
    if (!rows.length) { el.innerHTML = `<div class="empty">Scanning the market…</div>`; return; }
    el.innerHTML = rows.map((r, i) => {
      const sig = r.signals || {};
      const bars = SIG_KEYS.map((k, j) => {
        const v = sig[k] || 0;
        return `<span class="sb" style="height:${Math.max(6, v)}%;background:var(${SIG_VARS[j]})" title="${k}: ${fmt(v,0)}"></span>`;
      }).join("");
      const h1 = r.change_h1 || 0, m5 = r.change_m5 || 0;
      const liq = r.liquidity_usd ? "$" + (r.liquidity_usd >= 1000 ? (r.liquidity_usd/1000).toFixed(0)+"k" : r.liquidity_usd.toFixed(0)) : "—";
      const hot = r.score >= 62 ? "color:var(--green)" : r.score >= 45 ? "color:var(--amber)" : "color:var(--muted)";
      return `<div class="brow">
        <span class="rank">#${i+1}</span>
        <span class="tk"><b>${r.symbol}</b><small>${(r.name||"").slice(0,18)}</small></span>
        <span class="scorebox"><div class="sv" style="${hot}">${fmt(r.score,0)}</div><div class="sl">score</div></span>
        <span class="sigbars">${bars}</span>
        <span class="market">
          <span class="ch ${cls(m5)}">5m ${signed(m5,1)}%</span>
          <span class="ch ${cls(h1)}">1h ${signed(h1,1)}%</span>
          <span>liq ${liq}</span>
        </span>
      </div>`;
    }).join("");
  }

  // ── live feed ─────────────────────────────────────────────────────────────
  function pushFeed(level, text, ts) {
    const feed = $("feed");
    const line = document.createElement("div");
    const lvl = ["trade","warn","error","info"].includes(level) ? level : "info";
    line.className = "feed-line " + lvl;
    line.innerHTML = `<span class="t">${hhmm(ts || Date.now()/1000)}</span><span>${text}</span>`;
    feed.prepend(line);
    while (feed.children.length > 60) feed.removeChild(feed.lastChild);
  }

  // ── websocket ─────────────────────────────────────────────────────────────
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    const conn = $("conn");
    ws.onopen = () => { conn.classList.add("on"); $("connText").textContent = "live"; };
    ws.onclose = () => {
      conn.classList.remove("on"); $("connText").textContent = "reconnecting…";
      setTimeout(connect, 1500);
    };
    ws.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "state") renderState(msg.data);
      else if (msg.type === "log") pushFeed(msg.data.level, msg.data.text, msg.data.ts);
      else if (msg.type === "event") {
        const d = msg.data;
        if (d.kind === "buy") pushFeed("trade", `🚀 BUY ${d.symbol} · ${fmt(d.sol,3)} ◎ · conv ${fmt(d.conviction,2)}`, d.ts);
      }
    };
  }

  initChart();
  connect();
})();
