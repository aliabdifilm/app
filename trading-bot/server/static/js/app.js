/* ApexAlgo control panel.
 *
 * Vanilla JS on purpose: the page runs under a strict CSP with script-src
 * 'self', it has to open fast on a phone over mobile data, and a control
 * surface for a live trading account is not the place for a dependency tree.
 */
(function () {
  "use strict";

  var POLL_MS = 4000;
  var state = { botId: null, maxRisk: 2, chartHours: 12, riskDirty: false, timer: null };

  function $(id) { return document.getElementById(id); }

  function fmt(value, digits) {
    if (value === null || value === undefined || isNaN(value)) return "--";
    return Number(value).toLocaleString(undefined, {
      minimumFractionDigits: digits === undefined ? 2 : digits,
      maximumFractionDigits: digits === undefined ? 2 : digits
    });
  }

  function signClass(value) { return value > 0 ? "pos" : (value < 0 ? "neg" : ""); }

  function relTime(seconds) {
    if (seconds < 60) return seconds + "s ago";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m ago";
    if (seconds < 86400) return Math.floor(seconds / 3600) + "h ago";
    return Math.floor(seconds / 86400) + "d ago";
  }

  /* ---------------------------------------------------------------- toast */
  var toastTimer = null;
  function toast(message, kind) {
    var el = $("toast");
    el.textContent = message;
    el.className = "toast " + (kind ? "toast-" + kind : "");
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.hidden = true; }, 3200);
  }

  /* ------------------------------------------------------------ confirm */
  var pendingAction = null;

  function askConfirm(message, onYes) {
    $("sheetText").textContent = message;
    pendingAction = onYes;
    $("sheetBackdrop").hidden = false;
  }

  function closeSheet() {
    $("sheetBackdrop").hidden = true;
    pendingAction = null;
  }

  /* --------------------------------------------------------------- fetch */
  function api(path, options) {
    return fetch(path, Object.assign({ credentials: "same-origin" }, options || {}))
      .then(function (response) {
        if (response.status === 401) {
          window.location.href = "/login";
          throw new Error("unauthorized");
        }
        return response.json().then(function (body) {
          if (!response.ok) throw new Error(body.error || ("HTTP " + response.status));
          return body;
        });
      });
  }

  function sendCommand(type, symbol, value, confirmed) {
    return api("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        bot: state.botId, type: type, symbol: symbol || "",
        value: value || 0, confirm: !!confirmed
      })
    }).then(function () {
      toast("Command sent to the bot", "ok");
      refresh();
    }).catch(function (err) {
      toast("Rejected: " + err.message, "error");
    });
  }

  /* --------------------------------------------------------------- chart */
  function drawChart(points) {
    var svg = $("equityChart");
    var empty = $("chartEmpty");
    while (svg.firstChild) svg.removeChild(svg.firstChild);

    if (!points || points.length < 2) {
      empty.hidden = false;
      return;
    }
    empty.hidden = true;

    var W = 600, H = 180, pad = 8;
    var values = points.map(function (p) { return p.equity; });
    var balances = points.map(function (p) { return p.balance; });
    var lo = Math.min.apply(null, values.concat(balances));
    var hi = Math.max.apply(null, values.concat(balances));
    if (hi - lo < 1e-9) { hi = lo + 1; }
    var span = hi - lo;
    // a little headroom so the line never touches the frame
    lo -= span * 0.08;
    hi += span * 0.08;

    function x(i) { return pad + (i / (points.length - 1)) * (W - pad * 2); }
    function y(v) { return H - pad - ((v - lo) / (hi - lo)) * (H - pad * 2); }

    function pathFor(series) {
      return series.map(function (v, i) {
        return (i === 0 ? "M" : "L") + x(i).toFixed(1) + " " + y(v).toFixed(1);
      }).join(" ");
    }

    var ns = "http://www.w3.org/2000/svg";
    var rising = values[values.length - 1] >= values[0];
    var color = rising ? "#3fb950" : "#f85149";

    // shaded area under the equity line
    var area = document.createElementNS(ns, "path");
    area.setAttribute("d", pathFor(values) + " L" + x(points.length - 1).toFixed(1) +
                      " " + (H - pad) + " L" + x(0).toFixed(1) + " " + (H - pad) + " Z");
    area.setAttribute("fill", color);
    area.setAttribute("fill-opacity", "0.10");
    svg.appendChild(area);

    // balance line: the realised curve, for contrast with floating equity
    var balanceLine = document.createElementNS(ns, "path");
    balanceLine.setAttribute("d", pathFor(balances));
    balanceLine.setAttribute("fill", "none");
    balanceLine.setAttribute("stroke", "#8b949e");
    balanceLine.setAttribute("stroke-width", "1");
    balanceLine.setAttribute("stroke-dasharray", "3 3");
    balanceLine.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(balanceLine);

    var line = document.createElementNS(ns, "path");
    line.setAttribute("d", pathFor(values));
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", color);
    line.setAttribute("stroke-width", "2");
    line.setAttribute("stroke-linejoin", "round");
    line.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(line);
  }

  function loadChart() {
    api("/api/history?hours=" + state.chartHours + "&bot=" + encodeURIComponent(state.botId || ""))
      .then(function (body) { drawChart(body.points); })
      .catch(function () { /* the status poll already reports connection trouble */ });
  }

  /* -------------------------------------------------------------- render */
  function renderGuard(bot, online) {
    var banner = $("guardBanner");
    var title = $("guardTitle");
    var reason = $("guardReason");
    var st = bot.state || {};

    var cls = "banner banner-ok";
    var text = "Trading active";
    var detail = st.halt_reason || "All risk guards clear.";

    if (!online) {
      cls = "banner banner-danger";
      text = "Bot offline";
      detail = "No heartbeat from MetaTrader. Check the terminal, the VPS and the EA.";
    } else if (st.halt === "KILL_SWITCH") {
      cls = "banner banner-danger";
      text = "Kill switch engaged";
    } else if (bot.paused) {
      cls = "banner banner-warn";
      text = "Paused";
      detail = "Open trades are still managed. No new entries.";
    } else if (st.halt && st.halt !== "NONE") {
      cls = "banner banner-warn";
      text = st.halt.replace(/_/g, " ");
    }

    banner.className = cls;
    title.textContent = text;
    reason.textContent = detail;
  }

  function renderKpis(st) {
    $("kpiEquity").textContent = fmt(st.equity);
    $("kpiCurrency").textContent = (st.currency || "") + " · bal " + fmt(st.balance);

    var pnl = Number(st.day_pnl || 0);
    var pnlEl = $("kpiDayPnl");
    pnlEl.textContent = (pnl >= 0 ? "+" : "") + fmt(pnl);
    pnlEl.className = "kpi-value " + signClass(pnl);
    $("kpiDayPct").textContent = (Number(st.day_pnl_pct || 0)).toFixed(2) + "%";

    var dd = Number(st.drawdown_pct || 0);
    var ddEl = $("kpiDrawdown");
    ddEl.textContent = dd.toFixed(2) + "%";
    ddEl.className = "kpi-value " + (dd >= 7 ? "neg" : (dd >= 3 ? "" : "pos"));
    $("kpiPeak").textContent = "peak " + fmt(st.peak_equity);

    $("kpiPositions").textContent = st.open_positions || 0;
    $("kpiTrades").textContent = (st.trades_today || 0) + " trades today";
  }

  function renderPositions(positions) {
    var tbody = $("positionsTable").querySelector("tbody");
    tbody.innerHTML = "";

    if (!positions || !positions.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="muted centered">No open positions</td></tr>';
      $("positionsTotal").textContent = "";
      return;
    }

    var total = 0;
    positions.forEach(function (p) {
      total += Number(p.profit || 0);
      var tr = document.createElement("tr");

      function cell(text, className) {
        var td = document.createElement("td");
        if (className) td.className = className;
        td.textContent = text;
        return td;
      }

      tr.appendChild(cell(p.symbol));

      var sideTd = document.createElement("td");
      var tag = document.createElement("span");
      tag.className = "tag " + (p.side === "BUY" ? "tag-buy" : "tag-sell");
      tag.textContent = p.side;
      sideTd.appendChild(tag);
      tr.appendChild(sideTd);

      tr.appendChild(cell(fmt(p.volume, 2), "num"));
      tr.appendChild(cell(p.open, "num"));

      var profit = Number(p.profit || 0);
      var profitTd = cell((profit >= 0 ? "+" : "") + fmt(profit), "num " + signClass(profit));
      tr.appendChild(profitTd);

      var actionTd = document.createElement("td");
      var button = document.createElement("button");
      button.className = "btn-mini";
      button.textContent = "Close";
      button.addEventListener("click", function () {
        askConfirm("Close all " + p.symbol + " positions?", function () {
          sendCommand("close_symbol", p.symbol, 0, true);
        });
      });
      actionTd.appendChild(button);
      tr.appendChild(actionTd);

      tbody.appendChild(tr);
    });

    var totalEl = $("positionsTotal");
    totalEl.textContent = (total >= 0 ? "+" : "") + fmt(total);
    totalEl.className = "small " + signClass(total);
  }

  function renderSymbols(symbols) {
    var tbody = $("symbolsTable").querySelector("tbody");
    tbody.innerHTML = "";
    if (!symbols || !symbols.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="muted centered">--</td></tr>';
      return;
    }
    symbols.forEach(function (s) {
      var tr = document.createElement("tr");
      [[s.symbol, ""], [fmt(s.spread, 1), "num"], [s.positions, "num"],
       [s.note || "--", "wrap-cell"]].forEach(function (pair) {
        var td = document.createElement("td");
        if (pair[1]) td.className = pair[1];
        td.textContent = pair[0];
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
  }

  function renderEvents(events) {
    var list = $("eventsList");
    list.innerHTML = "";
    if (!events || !events.length) {
      list.innerHTML = '<li class="muted">No activity yet.</li>';
      return;
    }
    events.forEach(function (e) {
      var li = document.createElement("li");
      li.className = "ev-" + (e.level || "info");

      var time = document.createElement("span");
      time.className = "ev-time";
      time.textContent = new Date(e.ts * 1000).toLocaleTimeString([], {
        hour: "2-digit", minute: "2-digit"
      });

      var body = document.createElement("span");
      body.className = "ev-body";
      var title = document.createElement("span");
      title.className = "ev-title";
      title.textContent = e.title + " ";
      var message = document.createElement("span");
      message.className = "muted";
      message.textContent = e.message;
      body.appendChild(title);
      body.appendChild(message);

      li.appendChild(time);
      li.appendChild(body);
      list.appendChild(li);
    });
  }

  function renderBotSelect(bots) {
    var select = $("botSelect");
    var wanted = state.botId;
    if (select.options.length === bots.length &&
        Array.prototype.every.call(select.options, function (opt, i) {
          return opt.value === bots[i].bot_id;
        })) {
      select.value = wanted;
      return;
    }
    select.innerHTML = "";
    bots.forEach(function (b) {
      var option = document.createElement("option");
      option.value = b.bot_id;
      option.textContent = (b.online ? "🟢 " : "🔴 ") + b.bot_id;
      select.appendChild(option);
    });
    select.value = wanted;
  }

  /* -------------------------------------------------------------- polling */
  function refresh() {
    var query = state.botId ? ("?bot=" + encodeURIComponent(state.botId)) : "";
    return api("/api/state" + query).then(function (body) {
      state.maxRisk = body.max_risk_percent || 2;
      $("riskCeiling").textContent = state.maxRisk;
      $("riskRange").max = state.maxRisk;

      if (!body.current) {
        $("guardBanner").className = "banner banner-warn";
        $("guardTitle").textContent = "Waiting for the bot";
        $("guardReason").textContent =
          "No Expert Advisor has connected yet. Check InpRemoteUrl and InpRemoteToken in MT5.";
        $("statusDot").className = "dot dot-unknown";
        return;
      }

      state.botId = body.bot_id;
      renderBotSelect(body.bots || []);

      var bot = body.current;
      var online = !!bot.online;
      var st = bot.state || {};

      $("statusDot").className = "dot " + (online ? "dot-online" : "dot-offline");
      $("lastSeen").textContent = relTime(bot.seconds_since_seen || 0);

      renderGuard(bot, online);
      renderKpis(st);
      renderPositions(st.positions);
      renderSymbols(st.symbols);
      renderEvents(body.events);

      // Do not fight the user while they are dragging the slider.
      if (!state.riskDirty) {
        var risk = Number(st.risk_percent || 0.5);
        $("riskRange").value = risk;
        $("riskValue").textContent = risk.toFixed(2);
      }

      $("btnPause").classList.toggle("is-active", !!bot.paused);
      $("btnResume").classList.toggle("is-active", !bot.paused);
    }).catch(function (err) {
      if (err.message !== "unauthorized") {
        $("statusDot").className = "dot dot-offline";
        $("lastSeen").textContent = "panel offline";
      }
    });
  }

  /* ----------------------------------------------------------------- wire */
  function init() {
    state.maxRisk = parseFloat(document.body.dataset.maxRisk || "2");

    document.querySelectorAll("[data-cmd]").forEach(function (button) {
      button.addEventListener("click", function () {
        var type = button.dataset.cmd;
        var confirmText = button.dataset.confirm;
        if (confirmText) {
          askConfirm(confirmText, function () { sendCommand(type, "", 0, true); });
        } else {
          sendCommand(type, "", 0, false);
        }
      });
    });

    $("sheetCancel").addEventListener("click", closeSheet);
    $("sheetConfirm").addEventListener("click", function () {
      var action = pendingAction;
      closeSheet();
      if (action) action();
    });
    $("sheetBackdrop").addEventListener("click", function (event) {
      if (event.target === $("sheetBackdrop")) closeSheet();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") closeSheet();
    });

    var riskRange = $("riskRange");
    riskRange.addEventListener("input", function () {
      state.riskDirty = true;
      $("riskValue").textContent = Number(riskRange.value).toFixed(2);
    });
    $("btnApplyRisk").addEventListener("click", function () {
      var value = Number(riskRange.value);
      sendCommand("set_risk", "", value, false).then(function () {
        state.riskDirty = false;
      });
    });

    $("botSelect").addEventListener("change", function () {
      state.botId = $("botSelect").value;
      refresh();
      loadChart();
    });

    $("rangeTabs").addEventListener("click", function (event) {
      var tab = event.target.closest(".range-tab");
      if (!tab) return;
      document.querySelectorAll(".range-tab").forEach(function (t) {
        t.classList.remove("is-active");
      });
      tab.classList.add("is-active");
      state.chartHours = parseInt(tab.dataset.hours, 10);
      loadChart();
    });

    // Stop polling while the tab is hidden: on a phone this is the difference
    // between a panel you keep open and a panel that eats the battery.
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) {
        clearInterval(state.timer);
        state.timer = null;
      } else if (!state.timer) {
        refresh();
        loadChart();
        state.timer = setInterval(refresh, POLL_MS);
      }
    });

    refresh().then(loadChart);
    state.timer = setInterval(refresh, POLL_MS);
    setInterval(function () { if (!document.hidden) loadChart(); }, 60000);

    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js").catch(function () { /* optional */ });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
