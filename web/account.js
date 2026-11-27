const el = (id) => document.getElementById(id);
const number = new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 8});
const cnyNumber = new Intl.NumberFormat("zh-CN", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const clock = new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", dateStyle: "short", timeStyle: "medium"});
const format = (amount) => number.format(Number(amount));
const formatCny = (amount, rate, showSign = false) => {
  if (rate == null) return "CNY 汇率暂不可用";
  const value = Number(amount) * Number(rate);
  const abs = cnyNumber.format(Math.abs(value));
  const prefix = value < 0 ? "-" : showSign && value > 0 ? "+" : "";
  return `≈ ${prefix}¥${abs}`;
};
const money = (amount) => `${format(amount)} USDT`;
const signed = (amount) => `${Number(amount) > 0 ? "+" : ""}${format(amount)}`;
const pct = (amount) => amount == null ? "—" : `${Number(amount).toFixed(2)}%`;
const time = (stamp) => `${clock.format(stamp)} 北京时间`;
let currentRisk;
let currentRate = null;
let dailyHistory;
let balanceCurve = null;
let curveMode = "balance";

function tone(element, value) {
  element.dataset.sign = Number(value) > 0 ? "positive" : Number(value) < 0 ? "negative" : "zero";
}

function fillRows(id, data) {
  const body = el(id);
  const headings = [...body.closest("table").querySelectorAll("th")].map((heading) => heading.textContent);
  body.replaceChildren(...data.map((cells) => {
    const row = document.createElement("tr");
    cells.forEach((value, index) => {
      const cell = row.insertCell();
      cell.dataset.label = headings[index];
      cell.textContent = value;
    });
    return row;
  }));
}

function drawShock() {
  const percent = Number(el("shock").value);
  const result = Number(currentRisk.shock_up) * percent || 0;
  el("shock-pct").textContent = `${signed(percent)}%`;
  el("shock-result").textContent = `${signed(result)} USDT`;
  tone(el("shock-result"), result);
}

function renderSnapshot({mode, as_of: stamp, wallet, assets, positions, risk, order_plan: plan, fx}) {
  const rate = fx ? fx.usd_cny : null;
  currentRate = rate;
  if (balanceCurve) drawBalanceCurve();
  const fxNote = rate == null ? "" : ` · 1 USDT ≈ ${format(rate)} CNY`;
  el("account-source").textContent = mode === "live" ? "币安实盘 · U 本位合约" : "币安 Demo · U 本位合约";
  el("snapshot-status").textContent = `更新于 ${time(stamp)} · 每 5 秒刷新${fxNote}`;
  el("wallet-balance").textContent = format(wallet.wallet);
  el("wallet-balance-cny").textContent = formatCny(wallet.wallet, rate);
  el("equity").textContent = format(wallet.equity);
  el("equity-cny").textContent = formatCny(wallet.equity, rate);
  el("available").textContent = format(wallet.available);
  el("available-cny").textContent = formatCny(wallet.available, rate);
  el("unrealized").textContent = signed(wallet.unrealized);
  el("unrealized-cny").textContent = formatCny(wallet.unrealized, rate, true);
  tone(el("unrealized"), wallet.unrealized);
  tone(el("unrealized-cny"), rate == null ? 0 : wallet.unrealized);
  el("used-margin").textContent = `${format(wallet.position_margin)} / ${money(wallet.order_margin)}`;
  el("margin-usage").textContent = pct(risk.margin_usage_pct);
  el("plan-label").textContent = `单笔逐仓预算 · 钱包余额 ${plan.margin_pct}%`;
  el("plan-margin").textContent = money(plan.margin);
  el("notional-label").textContent = `计划名义额 · ${plan.leverage} 倍`;
  el("plan-notional").textContent = money(plan.notional);
  el("plan-status").textContent = plan.available_covers_margin ? "可用余额覆盖保证金预算" : "可用余额未覆盖非零保证金预算";
  el("exposure").textContent = money(risk.gross_notional);
  el("net-exposure").textContent = `${signed(risk.net_notional)} USDT`;
  el("effective-leverage").textContent = risk.gross_leverage == null ? "—" : `${Number(risk.gross_leverage).toFixed(2)} 倍`;
  currentRisk = risk;
  drawShock();
  fillRows("asset-rows", assets.map((asset) => [asset.asset, format(asset.wallet), format(asset.equity), format(asset.available), signed(asset.unrealized)]));
  fillRows("position-rows", positions.map((position) => [position.symbol,
    `${position.side === "long" ? "做多" : "做空"} / ${format(Math.abs(Number(position.quantity)))}`,
    format(position.entry), format(position.mark), `${format(Math.abs(Number(position.notional)))} ${position.asset}`,
    `${signed(position.unrealized)} ${position.asset}`,
    Number(position.liquidation) > 0 ? `${format(position.liquidation)} / ${pct(position.liquidation_distance_pct)}` : "—",
  ]));
  el("positions-count").textContent = `${positions.length} 个持仓`;
  el("positions-table").hidden = !positions.length;
  el("no-positions").hidden = !!positions.length;
  el("account-content").hidden = false;
}

const incomeLabels = {trade: "交易净额", realized: "已实现盈亏", commission: "手续费", funding: "资金费", transfer: "净划转", other: "其他流水"};
const tradeNet = (item) => Number(item.realized) + Number(item.commission) + Number(item.funding);

function drawIncome() {
  const key = el("income-metric").value;
  const values = dailyHistory.map((item) => key === "trade" ? tradeNet(item) : Number(item[key]));
  const scale = Math.max(...values.map(Math.abs)) || 1;
  el("income-chart").replaceChildren(...dailyHistory.map((item, index) => {
    const row = document.createElement("li");
    const date = document.createElement("time");
    date.dateTime = item.date;
    date.textContent = item.date.slice(5);
    const track = document.createElement("div");
    track.className = "income-track";
    track.setAttribute("aria-hidden", "true");
    const bar = document.createElement("div");
    bar.className = "income-fill";
    bar.style.width = `${Math.abs(values[index]) / scale * 50}%`;
    tone(bar, values[index]);
    track.append(bar);
    const amount = document.createElement("span");
    amount.className = "income-amount mono";
    amount.textContent = `${signed(values[index])} USDT`;
    tone(amount, values[index]);
    row.append(date, track, amount);
    return row;
  }));
}

function drawBalanceCurve(activeIndex = null) {
  const block = el("balance-chart-block");
  if (!balanceCurve || !balanceCurve.length) {
    block.hidden = true;
    return;
  }
  block.hidden = false;
  const values = balanceCurve.map((p) => Number(p[curveMode]));
  const latestIdx = activeIndex ?? values.length - 1;
  const activePoint = balanceCurve[latestIdx];
  const activeVal = values[latestIdx];
  const signedMode = curveMode === "cum_trade";
  const usdtText = `${signedMode ? signed(activeVal) : format(activeVal)} USDT`;
  const cnyText = currentRate == null ? "" : ` (${formatCny(activeVal, currentRate, signedMode)})`;
  el("balance-readout").textContent = `${activePoint.label} · ${usdtText}${cnyText}`;
  tone(el("balance-readout"), signedMode ? activeVal : 0);

  const W = 820, H = 188, padL = 62, padR = 22, padT = 16, padB = 26;
  const pw = W - padL - padR, ph = H - padT - padB;
  let minY = Math.min(...values), maxY = Math.max(...values);
  if (signedMode) { minY = Math.min(minY, 0); maxY = Math.max(maxY, 0); }
  const span = maxY - minY || Math.max(Math.abs(maxY) * 0.2, 1);
  minY -= span * 0.14;
  maxY += span * 0.16;

  const xOf = (i) => padL + (i / Math.max(1, values.length - 1)) * pw;
  const yOf = (v) => padT + ph - ((v - minY) / (maxY - minY)) * ph;
  const delta = values[values.length - 1] - values[0];
  const stroke = delta > 0 ? "var(--positive)" : delta < 0 ? "var(--negative)" : "var(--primary)";

  const pts = values.map((v, i) => [xOf(i), yOf(v)]);
  const linePath = pts.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const areaPath = `${linePath} L${pts[pts.length - 1][0].toFixed(1)},${(padT + ph).toFixed(1)} L${pts[0][0].toFixed(1)},${(padT + ph).toFixed(1)} Z`;

  const gridLines = [0, 0.5, 1].map((f) => {
    const val = minY + (maxY - minY) * f;
    const y = yOf(val).toFixed(1);
    return `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" stroke="var(--line)" stroke-dasharray="3 3" />`
      + `<text x="${padL - 8}" y="${Number(y) + 4}" text-anchor="end" font-size="10.5" font-family="var(--mono)" fill="var(--muted)">${Number(val.toFixed(2))}</text>`;
  }).join("");

  const zeroLine = signedMode
    ? `<line x1="${padL}" y1="${yOf(0).toFixed(1)}" x2="${W - padR}" y2="${yOf(0).toFixed(1)}" stroke="var(--line-strong)" stroke-width="1.2" />`
    : "";

  const xLabels = balanceCurve.map((p, i) =>
    `<text x="${xOf(i).toFixed(1)}" y="${H - 6}" text-anchor="middle" font-size="10.5" font-family="var(--mono)" fill="var(--muted)">${p.label}</text>`
  ).join("");

  const circles = pts.map(([x, y], i) => {
    const isSelected = i === latestIdx;
    return `<circle class="curve-point" data-idx="${i}" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${isSelected ? 5 : 3.5}" fill="${isSelected ? stroke : "var(--panel)"}" stroke="${stroke}" stroke-width="2" />`;
  }).join("");

  const container = el("balance-chart");
  container.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="资金变化折线图">`
    + `<defs><linearGradient id="bal-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${stroke}" stop-opacity="0.16"/><stop offset="100%" stop-color="${stroke}" stop-opacity="0.01"/></linearGradient></defs>`
    + gridLines + zeroLine
    + `<path d="${areaPath}" fill="url(#bal-fill)" />`
    + `<path d="${linePath}" fill="none" stroke="${stroke}" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round" />`
    + xLabels + circles + `</svg>`;

  container.querySelectorAll(".curve-point").forEach((dot) => {
    dot.addEventListener("mouseenter", () => drawBalanceCurve(Number(dot.dataset.idx)));
    dot.addEventListener("click", () => drawBalanceCurve(Number(dot.dataset.idx)));
  });
}

function renderIncome({as_of: stamp, totals, daily, incomplete, balance_curve: curve}) {
  const values = {trade: tradeNet(totals), ...totals};
  el("income-summary").replaceChildren(...Object.entries(incomeLabels).map(([key, label]) => {
    const cell = document.createElement("div");
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = label;
    dd.textContent = `${signed(values[key])} USDT`;
    tone(dd, values[key]);
    cell.append(dt, dd);
    return cell;
  }));
  el("income-status").textContent = `更新于 ${time(stamp)}`;
  el("income-note").textContent = incomplete
    ? "币安返回的流水达到读取上限，合计只覆盖已读取记录，历史余额折线已隐藏。"
    : "交易净额 = 已实现盈亏 + 手续费 + 资金费。净划转和其他流水单列。";
  dailyHistory = daily;
  balanceCurve = curve || null;
  drawBalanceCurve();
  drawIncome();
  el("income-content").hidden = false;
}

async function poll(path, render, status, errorNode, interval) {
  try {
    const response = await fetch(path, {cache: "no-store", signal: AbortSignal.timeout(12000)});
    const body = await response.json();
    if (!response.ok) throw new Error(body.error.message);
    render(body);
    el(errorNode).hidden = true;
  } catch (error) {
    el(errorNode).textContent = error.message;
    el(errorNode).hidden = false;
    if (!el(status).textContent.startsWith("数据更新暂停")) {
      el(status).textContent = `数据更新暂停 · ${el(status).textContent}`;
    }
  } finally {
    setTimeout(() => poll(path, render, status, errorNode, interval), interval);
  }
}

el("shock").addEventListener("input", drawShock);
el("income-metric").addEventListener("change", drawIncome);
document.querySelectorAll("[data-curve]").forEach((btn) => btn.addEventListener("click", () => {
  curveMode = btn.dataset.curve;
  document.querySelectorAll("[data-curve]").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
  drawBalanceCurve();
}));
poll("/api/account", renderSnapshot, "snapshot-status", "account-error", 5000);
poll("/api/account/income", renderIncome, "income-status", "income-error", 60000);
