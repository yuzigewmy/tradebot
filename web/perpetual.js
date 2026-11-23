const $ = (selector) => document.querySelector(selector);
const formatter = new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 8});
const timeFormat = new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false});
const price = (value) => formatter.format(value);
const clock = (value) => timeFormat.format(value);
const periodNames = {"1m": "1m", "15m": "15m", "30m": "30m", "1h": "1H"};
const sides = {long: "做多", short: "做空", mixed: "方向未一致"};
const compressionStrategy = "eth_compression_short";
const setups = {[compressionStrategy]: "30 分钟波动收缩做空", pullback: "旧版趋势回踩", breakout: "旧版收缩放量突破", trend_pullback: "十五分钟趋势回踩", channel_breakout: "十五分钟区间突破"};
const statusLabels = {stopped: "已停止", online: "行情在线", connecting: "连接中", reconnecting: "重连中", error: "监控异常"};
const outcomeLabels = {
  take_profit: "成功 · 止盈", stop_loss: "失败 · 止损", time_limit: "到期退出",
  entry_expired: "未入场 · 窗口到期", pending: "追踪中", unverified: "待核实 · 缺成交证据",
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
  const response = await fetch(`/api/perpetual/${$("#instrument").value}${path}`, body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
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
  const compression = snapshot.selected_strategy === compressionStrategy;
  const signalMode = compression ? "模拟信号 · 250 USDT 逐仓 · 30 倍"
    : snapshot.model_configured ? "信号模式 · 仅复核合格候选" : "模型未配置";
  $("#model-config").textContent = snapshot.mode === "observe" ? "观察模式 · 收盘评估" : signalMode;
  $("#start-monitor").textContent = snapshot.running ? "应用方式" : "启动监控";
  $("#start-monitor").disabled = command;
  $("#stop-monitor").disabled = command || !snapshot.running;
  $("#market-note").textContent = snapshot.market_note;
  $("#policy-label").textContent = `${snapshot.policy_version} · ${snapshot.policy_id}`;
  renderChart();
  renderEvidence(compression);
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

function renderEvidence(compression) {
  const evaluation = snapshot.evaluation;
  const setup = evaluation?.setup;
  $("#setup-title").textContent = setup ? `${sides[setup.side]}${setup.strategy ? ` · ${setups[setup.strategy]}` : " · 等待条件"}` : "暂无收盘评估";
  const timeframes = compression ? ["1h", "30m", "1m"] : ["1h", "15m", "1m"];
  $("#swap-context").replaceChildren(...(evaluation ? timeframes.map((tf) => {
    const values = evaluation.context[tf];
    const row = node("div");
    row.className = "cycle-row";
    row.append(node("strong", periodNames[tf]), node("span", `${sides[values.bias]} · RSI ${values.rsi.toFixed(1)}`), node("small", `ADX ${values.adx.toFixed(1)}`));
    return row;
  }) : []));
  const setupInfo = $("#setup-info");
  setupInfo.hidden = !setup;
  if (setup) {
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
  if (quote) info.replaceChildren(metric("标记价 · USDT", price(quote.mark)), metric("预计资金费率", `${(100 * quote.funding_rate).toFixed(4)}%`), metric("报价时点 · 北京", clock(quote.ticker_at)), metric("资金费结算 · 北京", clock(quote.funding_at)));
  $("#review-heading").textContent = compression ? "规则与模拟入场" : "模型复核";
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
    $("#signal-title").textContent = `${sides[latest.side]} · ${setups[latest.strategy]}`;
    const hold = latest.exit_at == null ? "" : ` · 持仓截止 ${clock(latest.exit_at)}`;
    const size = latest.margin_budget === undefined ? "" : ` · 逐仓 ${latest.margin_budget} USDT × ${latest.leverage} 倍`;
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
    const {status, at, price: exitPrice} = signal.outcome;
    const outcome = node("td");
    outcome.dataset.label = "模拟结果";
    outcome.dataset.outcome = status;
    outcome.append(node("strong", outcomeLabels[status]));
    if (at !== undefined) outcome.append(node("small", `${clock(at)} · ${price(exitPrice)}`));
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
      if (data.selected_strategy === compressionStrategy) selectPeriod("30m");
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
    if (action === "/start" && snapshot.selected_strategy === compressionStrategy) selectPeriod("30m");
  }
  catch (error) { showError(error); }
  finally { command = false; $("#instrument").disabled = false; $("#strategy-select").disabled = false; render(); }
}

function selectPeriod(value) {
  period = value;
  document.querySelectorAll("[data-period]").forEach((item) => item.setAttribute("aria-selected", String(item.dataset.period === period)));
}

function syncInstrumentControls() {
  const eth = $("#instrument").value === "ETH-USDT-SWAP";
  const option = $("#strategy-select").querySelector(`[value="${compressionStrategy}"]`);
  option.disabled = option.hidden = !eth;
  $('[data-period="30m"]').hidden = !eth;
  if (!eth && period === "30m") selectPeriod("15m");
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
