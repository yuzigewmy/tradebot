const $ = (selector) => document.querySelector(selector);
const number = (value, digits = 2) => value === null ? "—" : new Intl.NumberFormat("en-US", {maximumFractionDigits: digits, minimumFractionDigits: digits}).format(value);
const date = (timestamp) => new Date(timestamp).toISOString().slice(0, 16).replace("T", " ") + " UTC";
const sourceNames = {live: "真实行情", synthetic: "合成演示", import: "CSV 回放"};
let report = null;
let activeTimeframe = "1h";
let busy = false;

function useReport(next) {
  if (report && ["source", "symbol", "exchange"].some((key) => report[key] !== next[key])) {
    $("#portfolio-form").reset();
  }
  report = next;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function request(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error.message);
  return data;
}

function showError(error) {
  $("#error").textContent = error.message;
  $("#error").hidden = false;
}

function setBusy(value, text = "正在读取现货 K 线并计算技术证据…") {
  busy = value;
  $("#loading").hidden = !value;
  $("#loading-text").textContent = text;
  $("#analyze-button").disabled = value;
  $("#portfolio-button").disabled = value;
  $("#export-button").disabled = value || report === null;
  if (value) $("#error").hidden = true;
}

async function generate(event) {
  event.preventDefault();
  if (busy) return;
  setBusy(true);
  $("#results").hidden = true;
  try {
    useReport(await request("/api/analyze", {source: $("#source").value, exchange: $("#exchange").value, symbol: $("#symbol").value}));
    activeTimeframe = "1h";
    render();
  } catch (error) { showError(error); }
  finally { setBusy(false); }
}

function render() {
  $("#results").hidden = false;
  $("#market-symbol").textContent = report.symbol;
  $("#portfolio-symbol").textContent = report.symbol;
  $("#source-badge").textContent = sourceNames[report.source];
  $("#source-badge").className = "badge" + (report.source === "live" ? "" : " demo");
  $("#market-time").textContent = "统一截止 " + date(report.as_of);
  $("#market-source").textContent = report.exchange.toUpperCase() + " · " + sourceNames[report.source] + " · UTC 日线";
  $("#snapshot-label").textContent = "快照 " + report.snapshot_id;
  $("#market-state").textContent = report.consensus.state;
  $("#market-action").textContent = report.consensus.action;
  $("#counter-evidence").replaceChildren(...report.consensus.counter_evidence.map((text) => element("li", "", text)));
  const cycleRows = Object.entries(report.timeframes).reverse().map(([tf, analysis]) => {
    const row = element("div", "cycle-row");
    row.append(element("strong", "mono", tf.toUpperCase()), element("span", "", analysis.trend_label), element("small", "mono", "ADX " + number(analysis.indicators.adx14, 1)));
    return row;
  });
  $("#cycle-summary").replaceChildren(...cycleRows);
  const cautions = [...new Set(Object.values(report.timeframes).flatMap((a) => a.cautions))];
  $("#quality-notes").textContent = cautions.join("　");
  $("#quality-notes").hidden = cautions.length === 0;
  const tabs = Object.keys(report.timeframes).map((tf) => {
    const button = element("button", "", tf.toUpperCase());
    button.type = "button";
    button.setAttribute("role", "tab");
    button.dataset.timeframe = tf;
    button.addEventListener("click", () => { activeTimeframe = tf; renderTimeframe(); });
    return button;
  });
  $("#timeframe-tabs").replaceChildren(...tabs);
  $("#scenarios").replaceChildren(...report.consensus.scenarios.map((scenario) => {
    const row = element("article", "scenario-row");
    const title = element("div");
    title.append(element("h3", "", scenario.name), element("span", "quiet", scenario.status));
    if (scenario.reference_zone !== null) title.append(element("p", "mono quiet", number(scenario.reference_zone.low) + " – " + number(scenario.reference_zone.high) + " USDT"));
    const trigger = element("p");
    trigger.append(element("span", "scenario-label", "确认条件"), document.createTextNode(scenario.trigger));
    const invalidation = element("p");
    invalidation.append(element("span", "scenario-label", "失效条件"), document.createTextNode(scenario.invalidation));
    row.append(title, trigger, invalidation);
    return row;
  }));
  renderPortfolio();
  $("#insight-result").replaceChildren();
  $("#insight-status").textContent = "深度研判将只引用当前快照证据；模型未配置时，上面的规则分析仍可完整使用。";
  renderTimeframe();
}

function chartColor(token) {
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("2d");
  context.fillStyle = getComputedStyle(document.documentElement).getPropertyValue(token).trim();
  context.fillRect(0, 0, 1, 1);
  const [r, g, b] = context.getImageData(0, 0, 1, 1).data;
  return `rgb(${r},${g},${b})`;
}

function renderTimeframe() {
  const analysis = report.timeframes[activeTimeframe];
  const rows = analysis.chart.rows;
  const series = analysis.chart.series;
  $("#price").textContent = number(analysis.price, 2);
  $("#price-change").textContent = analysis.change_percent === null ? "—" : (analysis.change_percent >= 0 ? "+" : "") + number(analysis.change_percent, 2) + "% / 本周期";
  $("#price-change").className = analysis.change_percent >= 0 ? "positive" : "negative";
  $("#chart-time").textContent = "最新收盘 " + date(analysis.last_closed_at);
  $("#evidence-timeframe").textContent = activeTimeframe.toUpperCase() + " · 参数固定并随快照归档";
  document.querySelectorAll("[data-timeframe]").forEach((button) => button.setAttribute("aria-selected", String(button.dataset.timeframe === activeTimeframe)));
  $("#evidence-table").replaceChildren(...analysis.evidence.map((evidence) => {
    const row = element("tr");
    const description = element("td", "", evidence.text);
    const values = Object.entries(evidence.values).filter(([, value]) => typeof value === "number" || value === null).map(([key, value]) => key + " " + number(value, 4));
    if (evidence.category === "structure") {
      for (const name of ["support", "resistance"]) {
        const zone = evidence.values[name];
        values.push((name === "support" ? "支撑 " : "阻力 ") + (zone === null ? "未识别" : number(zone.low) + "–" + number(zone.high)));
      }
    }
    if (values.length) description.append(element("p", "evidence-values mono", values.join(" · ")));
    row.append(element("td", "", evidence.title), description, element("td", "", "[" + evidence.id + "]"));
    return row;
  }));
  const colors = {ink: chartColor("--ink"), muted: chartColor("--muted"), line: chartColor("--line"), primary: chartColor("--primary"), positive: chartColor("--positive"), negative: chartColor("--negative")};
  const x = rows.map((r) => new Date(r[0]).toISOString());
  const traces = [
    {type: "candlestick", name: "OHLC", x, open: rows.map((r) => r[1]), high: rows.map((r) => r[2]), low: rows.map((r) => r[3]), close: rows.map((r) => r[4]), increasing: {line: {color: colors.positive}, fillcolor: colors.positive}, decreasing: {line: {color: colors.negative}, fillcolor: colors.negative}},
    {type: "bar", name: "成交量", x, y: rows.map((r) => r[5]), yaxis: "y2", marker: {color: rows.map((r) => r[4] >= r[1] ? colors.positive : colors.negative), opacity: 0.3}, hoverinfo: "skip"},
  ];
  for (const [name, color] of [["ema20", colors.primary], ["ema50", "rgb(77,112,159)"], ["ema200", colors.muted]]) {
    traces.push({type: "scatter", mode: "lines", name: name.toUpperCase(), x, y: series[name], line: {color, width: 1.2}});
  }
  const shapes = [];
  for (const [kind, zone] of Object.entries(analysis.zones)) {
    if (zone === null) continue;
    shapes.push({type: "rect", xref: "paper", x0: 0, x1: 1, y0: zone.low, y1: zone.high, fillcolor: kind === "support" ? colors.positive : colors.negative, opacity: 0.07, line: {width: 0}, layer: "below"});
  }
  const base = {paper_bgcolor: "white", plot_bgcolor: "white", font: {family: '"Segoe UI", "Microsoft YaHei", sans-serif', size: 10, color: colors.muted}, margin: {l: 6, r: 65, t: 16, b: 28}, showlegend: true, legend: {orientation: "h", x: 0, y: 1.08, font: {size: 9}}, dragmode: "pan", xaxis: {type: "date", rangeslider: {visible: false}, showgrid: false, zeroline: false}, yaxis: {side: "right", gridcolor: colors.line, zeroline: false, tickformat: ",.0f", domain: [0.24, 1]}, yaxis2: {domain: [0, 0.16], side: "right", showgrid: false, zeroline: false, tickformat: "~s"}, shapes, uirevision: report.snapshot_id + activeTimeframe};
  const config = {responsive: true, displayModeBar: false, scrollZoom: true};
  Plotly.react("price-chart", traces, base, config);
  const momentumLayout = {...base, margin: {l: 6, r: 65, t: 2, b: 20}, showlegend: false, yaxis: {side: "right", range: [0, 100], tickvals: [30, 70], gridcolor: colors.line, zeroline: false}, shapes: [30, 70].map((y) => ({type: "line", xref: "paper", x0: 0, x1: 1, y0: y, y1: y, line: {color: colors.line, dash: "dot", width: 1}}))};
  delete momentumLayout.yaxis2;
  Plotly.react("momentum-chart", [{type: "scatter", mode: "lines", name: "RSI14", x, y: series.rsi, line: {color: colors.primary, width: 1.4}}], momentumLayout, config);
}

function renderPortfolio() {
  if (report.portfolio === null) {
    $("#portfolio-result").replaceChildren(element("p", "quiet", "尚未填写持仓。成本未知时保持未知，资金计算不包含其他资产。"));
    return;
  }
  const p = report.portfolio;
  const metrics = element("div", "portfolio-metrics");
  for (const [label, value, digits] of [["本次资金 / USDT", p.equity, 2], ["已有估计风险 / USDT", p.existing_risk, 2], ["剩余风险预算 / USDT", p.remaining_risk_budget, 2], ["新增数量情景上限", p.additional_quantity, 8]]) {
    const item = element("div");
    item.append(element("span", "", label), element("strong", "", number(value, digits)));
    metrics.append(item);
  }
  const nodes = [metrics, element("p", "", "未实现盈亏：" + (p.unrealized_pnl === null ? "成本未知，不计算" : number(p.unrealized_pnl) + " USDT") + "；当前权重 " + number(p.current_weight_percent) + "% ；最多可卖 " + number(p.max_sell_quantity, 8)), ...p.reasons.map((text) => element("p", "", text)), element("p", "quiet", "持仓估值参考最新已收盘执行周期价格：" + number(p.price) + " USDT。"), element("p", "quiet", p.note), element("p", "quiet", p.scope)];
  $("#portfolio-result").replaceChildren(...nodes);
}

$("#analysis-form").addEventListener("submit", generate);
$("#export-button").addEventListener("click", () => { window.location.href = `/api/reports/${report.snapshot_id}/markdown`; });
$("#portfolio-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  const position = Object.fromEntries([...new FormData(event.currentTarget)].map(([key, value]) => [key, value === "" ? null : Number(value)]));
  setBusy(true, "正在用当前快照计算持仓风险，不重新请求行情…");
  try { report = await request(`/api/reports/${report.snapshot_id}/portfolio`, position); render(); }
  catch (error) { showError(error); }
  finally { setBusy(false); }
});
$("#insight-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const snapshotId = report.snapshot_id;
  $("#insight-button").disabled = true;
  $("#insight-status").textContent = "Agent 正在调用快照证据工具并整理研判…";
  try {
    const result = await request(`/api/reports/${snapshotId}/insight`, {question: $("#question").value});
    if (report.snapshot_id !== result.snapshot_id) return;
    $("#insight-result").textContent = result.answer + "\n\n反证：\n" + result.counter_evidence.join("\n") + "\n\n引用：" + result.evidence_ids.join(" · ");
    $("#insight-status").textContent = `模型解释 · ${result.model} · ${result.service_tier ?? "服务未报告档位"} · ${result.tool_calls} 次取证`;
  } catch (error) { if (report.snapshot_id === snapshotId) $("#insight-status").textContent = error.message; }
  finally { $("#insight-button").disabled = false; }
});
$("#import-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  const file = $("#csv-file").files[0];
  if (!file) { showError(new Error("请先选择 CSV 文件")); return; }
  if (file.size > 2000000) { showError(new Error("CSV文件最多2MB")); return; }
  setBusy(true, "正在解析CSV并按其历史截止时间分析…");
  try {
    const timeframe = $("#import-timeframe").value;
    useReport(await request("/api/import", {csv: await file.text(), symbol: $("#symbol").value, timeframe}));
    activeTimeframe = timeframe;
    render();
  } catch (error) { showError(error); }
  finally { setBusy(false); }
});

async function initialize() {
  const health = await request("/api/health");
  $("#provider-status").textContent = health.model_configured ? "深度研判已配置" : "基础分析可用 · 模型待配置";
  $("#insight-button").disabled = !health.model_configured;
  $("#analysis-form").requestSubmit();
}
initialize().catch(showError);
