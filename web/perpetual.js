const $ = (selector) => document.querySelector(selector);
const formatter = new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 8});
const timeFormat = new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false});
const price = (value) => formatter.format(value);
const clock = (value) => timeFormat.format(value);
const periodNames = {"1m": "1m", "15m": "15m", "30m": "30m", "1h": "1H", "4h": "4H"};
const sides = {long: "做多", short: "做空", mixed: "方向未一致"};
const compressionStrategy = "eth_compression_short";
const btcStrategy = "btc_trend_reclaim";
const dualStrategy = "dual_trend_engine";
const strategyPeriods = {[dualStrategy]: "1h", [compressionStrategy]: "30m", [btcStrategy]: "1h"};
const setups = {[dualStrategy]: "S10 双引擎顺势", [btcStrategy]: "BTC 多周期均线确认", [compressionStrategy]: "30 分钟波动收缩做空", pullback: "旧版趋势回踩", breakout: "旧版收缩放量突破", trend_pullback: "十五分钟趋势回踩", channel_breakout: "十五分钟区间突破"};
const engines = {s4_pullback: "S4 顺势回踩", s3_keltner: "S3 通道突破"};
const statusLabels = {stopped: "已停止", online: "行情在线", connecting: "连接中", reconnecting: "重连中", error: "监控异常"};
const outcomeLabels = {
  take_profit: "成功 · 止盈", stop_loss: "失败 · 止损", time_limit: "到期退出",
  entry_expired: "未入场 · 窗口到期", pending: "追踪中", unverified: "待核实 · 缺成交证据",
  entry_unfilled: "未成交", entry_rejected: "开仓受阻", entry_skipped: "未送单", entry_ordered: "核对入场单",
  closing: "平仓核对中", protected: "止盈止损已挂", binance_closed: "已结案", public_signal: "行情信号",
};
let snapshot = null;
let period = "15m";
let chartKey = "";
let command = false;

function node(tag, text = "") {
  const element = document.createElement(tag);
  element.textContent = text;
  return element;
}

function metric(label, value) {
  const group = node("div");
  group.append(node("dt", label), node("dd", value));
  return group;
}

async function request(path = "", body) {
  const response = await fetch(`/api/perpetual/${$("#instrument").value}${path}`, body === undefined ? {cache: "no-store"} : {method: "POST", cache: "no-store", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message ?? `本地监控接口返回 HTTP ${response.status}`);
  return data;
}

function showError(error) {
  $("#monitor-error").textContent = error.message;
  $("#monitor-error").hidden = false;
}

function render() {
  const status = $("#monitor-status");
  const label = statusLabels[snapshot.status];
  if (status.textContent !== label) status.textContent = label;
  status.dataset.status = snapshot.status;
  const fixedSignal = snapshot.selected_strategy in strategyPeriods;
  const binance = snapshot.exchange === "Binance";
  const mode = snapshot.execution_mode;
  const marketLabel = {quotes: "币安行情", demo: "币安 Demo", live: "币安实盘"}[mode] ?? "OKX";
  const canTrade = mode === "demo" || mode === "live";
  const sizing = snapshot.risk_percent != null
    ? `单笔风险 ${snapshot.risk_percent}% 逐仓 · ${snapshot.leverage} 倍`
    : `钱包余额 ${snapshot.margin_percent}% 逐仓 · ${snapshot.leverage} 倍`;
  const signalMode = binance ? `${marketLabel} · ${canTrade ? `自动下单 · ${sizing}` : "信号"}`
    : fixedSignal ? "模拟信号 · 250 USDT 逐仓 · 30 倍"
    : snapshot.model_configured ? "信号模式 · 仅复核合格候选" : "模型未配置";
  $("#exchange-label").textContent = `${marketLabel} · USDT 线性永续`;
  $("#execution-label").textContent = canTrade ? `${marketLabel} · 每个合约同时最多一仓`
    : `${marketLabel} · 研究信号 · 无下单权限`;
  $("#signal-result-label").textContent = canTrade ? "下单结果" : binance ? "信号结果" : "模拟结果";
  $('#review-mode [value="signals"]').textContent = canTrade ? "自动下单并推送" : "生成并推送信号";
  $("#stop-monitor").textContent = canTrade ? "停止开新仓" : "停止监控";
  $("#strategy-select").disabled = command;
  syncInstrumentControls();
  $("#model-config").textContent = snapshot.mode === "observe" ? "观察模式 · 收盘评估" : signalMode;
  $("#start-monitor").textContent = snapshot.running ? "应用方式" : "启动监控";
  $("#start-monitor").disabled = command;
  $("#stop-monitor").disabled = command || !snapshot.running;
  $("#market-note").textContent = snapshot.market_note;
  $("#policy-label").textContent = `${snapshot.policy_version} · ${snapshot.policy_id}`;
  renderChart();
  renderEvidence(fixedSignal);
  renderSignals();
}

function renderChart() {
  const series = snapshot.charts[period];
  $("#swap-chart").hidden = !series;
  $("#chart-empty").hidden = Boolean(series);
  if (!series) {
    chartKey = "";
    $("#chart-time").textContent = "";
    $("#chart-empty span").textContent = snapshot.market_note;
    return;
  }
  const last = series.closed.at(-1);
  const key = JSON.stringify([snapshot.instrument, snapshot.status, period, last, series.current]);
  if (key === chartKey) return;
  chartKey = key;
  const priceStatus = snapshot.status !== "online" ? "上次收盘" : series.current ? "形成中" : "收盘";
  const candlePrice = snapshot.status === "online" && series.current ? series.current[4] : last[4];
  $("#chart-time").textContent = `${periodNames[period]} · ${priceStatus} ${price(candlePrice)} USDT`;
  const candle = (rows, colorUp, colorDown, name) => ({
    type: "candlestick", name, x: rows.map((r) => new Date(r[0]).toISOString()),
    open: rows.map((r) => r[1]), high: rows.map((r) => r[2]), low: rows.map((r) => r[3]), close: rows.map((r) => r[4]),
    increasing: {line: {color: colorUp}}, decreasing: {line: {color: colorDown}},
  });
  const traces = [candle(series.closed, "#24765e", "#ab3833", "已收盘")];
  if (series.current && snapshot.status === "online") traces.push(candle([series.current], "#64748b", "#64748b", "形成中"));
  Plotly.react("swap-chart", traces, {
    paper_bgcolor: "white", plot_bgcolor: "white", showlegend: false, dragmode: "pan",
    margin: {l: 2, r: 72, t: 14, b: 25}, font: {size: 11},
    xaxis: {type: "date", rangeslider: {visible: false}, tickformat: "%m-%d %H:%M", showgrid: false},
    yaxis: {side: "right", gridcolor: "#ececef", tickformat: ",.1f"},
    uirevision: `${snapshot.instrument}:${period}`,
  }, {responsive: true, displayModeBar: false, scrollZoom: true});
}

function renderEvidence(fixedSignal) {
  const evaluation = snapshot.evaluation;
  const setup = evaluation?.setup;
  const dual = snapshot.selected_strategy === dualStrategy;
  const btc = snapshot.selected_strategy === btcStrategy;
  const compression = snapshot.selected_strategy === compressionStrategy;
  const engineTag = setup?.engine && engines[setup.engine] ? `（${engines[setup.engine]}）` : "";
  const title = setup?.strategy ? `${sides[setup.side]} · ${setups[setup.strategy]}${engineTag}`
    : dual ? (setup ? `${sides[setup.side]} · 等待双引擎触发` : "等待双引擎评估")
    : btc ? "等待多空条件" : setup ? `${sides[setup.side]} · 等待条件` : "暂无收盘评估";
  $("#setup-title").textContent = title;
  const timeframes = dual ? ["4h", "1h", "15m"] : btc ? ["4h", "1h", "30m"] : compression ? ["1h", "30m", "1m"] : ["1h", "15m", "1m"];
  $("#swap-context").replaceChildren(...(evaluation ? timeframes.filter((tf) => evaluation.context[tf]).map((tf) => {
    const values = evaluation.context[tf];
    const row = node("div");
    row.className = "cycle-row";
    row.append(node("strong", periodNames[tf]), node("span", `${sides[values.bias]} · RSI ${values.rsi.toFixed(1)}`), node("small", `ADX ${values.adx.toFixed(1)}`));
    return row;
  }) : []));
  const setupInfo = $("#setup-info");
  setupInfo.hidden = !setup;
  if (setup && dual && setup.engines) {
    setupInfo.replaceChildren(...setup.engines.map((item) => {
      const trend = item.trend_pass ? "通过" : "等待";
      if (item.engine === "s4_pullback") {
        const pb = item.pullback_pass ? "通过" : "等待";
        const trig = item.trigger_pass ? "通过" : "等待";
        return metric(`${item.label} · 1H ${clock(item.closed_at)} 北京`,
          `4H趋势 ${trend} · RSI回调 ${pb} · 复位触发 ${trig}（RSI ${item.rsi ?? "—"}）`);
      }
      const brk = item.breakout_pass ? "通过" : "等待";
      const vol = item.volume_pass ? "通过" : "等待";
      return metric(`${item.label} · 1H ${clock(item.closed_at)} 北京`,
        `4H趋势 ${trend} · 通道突破 ${brk} · 量能 ${vol}（${item.volume_ratio}×）`);
    }));
  } else if (setup && btc) {
    setupInfo.replaceChildren(...setup.legs.map((leg) => {
      const trend = leg.trend_pass ? "通过" : "等待";
      const cross = leg.range_cross ? "通过" : "等待";
      const volume = leg.volume_pass ? "通过" : "等待";
      return metric(`${periodNames[leg.timeframe]} ${sides[leg.side]} · ${clock(leg.closed_at)} 北京`,
        `趋势 ${trend} · 穿越 ${cross} · 量能 ${volume}（${leg.volume_ratio.toFixed(2)}×）`);
    }));
  } else if (setup) {
    const trigger = compression ? "30m" : "15m";
    setupInfo.replaceChildren(
      metric(`${trigger} 收盘 · 北京`, clock(evaluation.context[trigger].closed_at)),
      metric(`${trigger} 量比 · 前二十根`, `${setup.volume_ratio.toFixed(2)}×`),
      metric("此前区间 · USDT", `${price(setup.prior_low)} ~ ${price(setup.prior_high)}`),
      compression ? metric("收缩与跌破", `${setup.compressed ? "已收缩" : "未收缩"} · ${setup.range_cross ? "已跌破" : "未跌破"}`)
        : metric("趋势过滤", setup.trend_pass ? "已通过" : "等待方向或强度"),
    );
  }
  const quote = snapshot.quote;
  const info = $("#quote-info");
  info.hidden = !quote;
  if (quote) info.replaceChildren(
    metric("最新成交 · USDT", price(quote.last)), metric("买一 / 卖一", `${price(quote.bid)} / ${price(quote.ask)}`),
    metric("标记价 · USDT", price(quote.mark)), metric("资金费率参考", `${(100 * quote.funding_rate).toFixed(4)}%`),
    metric("盘口时点 · 北京", clock(quote.ticker_at)), metric("资金费结算 · 北京", clock(quote.funding_at)),
  );
  $("#review-heading").textContent = fixedSignal ? (snapshot.exchange === "Binance" ? "规则与入场" : "规则与模拟入场") : "模型复核";
  const review = snapshot.review;
  $("#review-meta").textContent = snapshot.mode === "observe" ? "行情观察模式" : review ? `${clock(review.closed_at)} 收盘 · ${review.note}` : "等待策略候选";
  const candidate = snapshot.mode === "signals" ? review?.candidate : null;
  $("#review-cost").textContent = candidate?.cost_to_stop === undefined ? "" : `预估成本占止损距离 ${(100 * candidate.cost_to_stop).toFixed(1)}%`;
  const result = snapshot.mode === "signals" ? review?.result : null;
  $("#review-summary").textContent = result?.analysis ?? "";
  $("#review-counter").textContent = result ? `反证：${result.counter_evidence}` : "";
  $("#review-evidence").textContent = result ? [result.model, result.service_tier, result.evidence_ids.join(" / ")].filter(Boolean).join(" · ") : "";
}

function renderSignals() {
  const latest = snapshot.signals[0];
  const strip = $("#signal-strip");
  strip.hidden = !latest;
  if (latest) {
    const active = snapshot.active_signal_id === latest.id;
    strip.dataset.active = String(active);
    $("#signal-status").textContent = active ? "已确认 · 参考价尚未到期" : "历史信号 · 开仓窗口不可用";
    const latestEngine = latest.engine && engines[latest.engine] ? `（${engines[latest.engine]}）` : "";
    $("#signal-title").textContent = `${sides[latest.side]} · ${setups[latest.strategy]}${latestEngine}`;
    const hold = latest.exit_at == null ? "" : ` · 持仓截止 ${clock(latest.exit_at)}`;
    let size = "";
    if (latest.risk_percent !== undefined) {
      size = ` · 单笔风险 ${latest.risk_percent}% × ${latest.leverage} 倍`;
      if (latest.outcome.margin_budget !== undefined) size += ` · 保证金预算 ${price(latest.outcome.margin_budget)} USDT`;
    } else if (latest.margin_percent !== undefined) {
      size = ` · 逐仓钱包余额的 ${latest.margin_percent}% × ${latest.leverage} 倍`;
      if (latest.outcome.margin_budget !== undefined) size += ` · 保证金预算 ${price(latest.outcome.margin_budget)} USDT`;
    } else if (latest.margin_budget !== undefined) {
      size = ` · 逐仓 ${latest.margin_budget} USDT × ${latest.leverage} 倍`;
    }
    $("#signal-clock").textContent = `发布 ${clock(latest.published_at)} · 入场截止 ${clock(latest.expires_at)}${hold} 北京时间${size}`;
    $("#signal-levels").replaceChildren(metric(latest.side === "long" ? "开仓上限 · USDT" : "开仓下限 · USDT", price(latest.entry)), metric("止盈 · USDT", price(latest.take_profit)), metric("止损 · USDT", price(latest.stop_loss)), metric("净收益风险比", `${latest.net_reward_risk.toFixed(2)} : 1`));
  }
  $("#history-table").hidden = !latest;
  $("#signals-empty").hidden = Boolean(latest);
  const labels = ["收盘", "策略", "开仓", "止盈", "止损", "净收益风险比"];
  $("#signal-rows").replaceChildren(...snapshot.signals.map((signal) => {
    const row = node("tr");
    const cells = [clock(signal.closed_at), `${sides[signal.side]} · ${setups[signal.strategy]}`, price(signal.entry), price(signal.take_profit), price(signal.stop_loss), `${signal.net_reward_risk.toFixed(2)} : 1`];
    row.append(...cells.map((value, i) => { const cell = node("td", value); cell.dataset.label = labels[i]; return cell; }));
    const {status, at, price: exitPrice, verdict, pnl_usdt: pnl} = signal.outcome;
    const outcome = node("td");
    outcome.dataset.label = $("#signal-result-label").textContent;
    outcome.dataset.outcome = status;
    outcome.append(node("strong", [verdict, outcomeLabels[status]].filter(Boolean).join(" · ")));
    if (at !== undefined) outcome.append(node("small", `${clock(at)} · ${price(exitPrice)}`));
    if (pnl !== undefined) outcome.append(node("small", `净盈亏 ${price(pnl)} USDT`));
    row.append(outcome);
    return row;
  }));
}

async function refresh(resetMode = false) {
  if (command) return;
  const data = await request();
  if (!command && data.instrument === $("#instrument").value) {
    if (resetMode || !snapshot) {
      syncInstrumentControls();
      $("#review-mode").value = data.mode;
      $("#strategy-select").value = data.selected_strategy;
      if (data.selected_strategy in strategyPeriods) selectPeriod(strategyPeriods[data.selected_strategy]);
    }
    snapshot = data;
    $("#monitor-error").hidden = true;
    render();
  }
}

async function changeMonitor(action, body) {
  command = true;
  $("#instrument").disabled = true;
  $("#strategy-select").disabled = true;
  $("#start-monitor").disabled = true;
  $("#stop-monitor").disabled = true;
  $("#monitor-error").hidden = true;
  try {
    snapshot = await request(action, body);
    if (action === "/start" && snapshot.selected_strategy in strategyPeriods) selectPeriod(strategyPeriods[snapshot.selected_strategy]);
  }
  catch (error) { showError(error); }
  finally { command = false; $("#instrument").disabled = false; render(); }
}

function selectPeriod(value) {
  period = value;
  document.querySelectorAll("[data-period]").forEach((item) => item.setAttribute("aria-selected", String(item.dataset.period === period)));
}

function syncInstrumentControls() {
  const eth = $("#instrument").value === "ETH-USDT-SWAP";
  const binance = snapshot?.exchange === "Binance";
  $(`#strategy-select [value="${dualStrategy}"]`).disabled = false;
  $(`#strategy-select [value="${compressionStrategy}"]`).disabled = !eth;
  $(`#strategy-select [value="${btcStrategy}"]`).disabled = eth;
  for (const okxOnly of ["all", "channel_breakout", "trend_pullback"]) {
    $(`#strategy-select [value="${okxOnly}"]`).disabled = binance;
  }
  $('[data-period="4h"]').hidden = false;
}

$("#instrument").addEventListener("change", () => {
  syncInstrumentControls();
  $("#start-monitor").disabled = true;
  $("#stop-monitor").disabled = true;
  refresh(true).catch(showError);
});
$("#monitor-form").addEventListener("submit", (event) => {
  event.preventDefault();
  changeMonitor("/start", {mode: $("#review-mode").value, strategy: $("#strategy-select").value});
});
$("#stop-monitor").addEventListener("click", () => changeMonitor("/stop", {}));
document.querySelectorAll("[data-period]").forEach((button) => button.addEventListener("click", () => {
  selectPeriod(button.dataset.period);
  if (snapshot) renderChart();
}));
async function poll() {
  try { await refresh(); }
  catch (error) { showError(error); }
  finally { setTimeout(poll, 1600); }
}
poll();
