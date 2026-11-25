const el = (id) => document.getElementById(id);
const number = new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 8});
const clock = new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", dateStyle: "short", timeStyle: "medium"});
const format = (amount) => number.format(Number(amount));
const money = (amount) => `${format(amount)} USDT`;
const signed = (amount) => `${Number(amount) > 0 ? "+" : ""}${format(amount)}`;
const pct = (amount) => amount == null ? "—" : `${Number(amount).toFixed(2)}%`;
const time = (stamp) => `${clock.format(stamp)} 北京时间`;
let currentRisk;
let dailyHistory;

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
  const result = Number(currentRisk.shock_up) * percent;
  el("shock-pct").textContent = `${signed(percent)}%`;
  el("shock-result").textContent = `${signed(result)} USDT`;
  tone(el("shock-result"), result);
}

function renderSnapshot({mode, as_of: stamp, wallet, assets, positions, risk, order_plan: plan}) {
  el("account-source").textContent = mode === "live" ? "币安实盘 · U 本位合约" : "币安 Demo · U 本位合约";
  el("snapshot-status").textContent = `更新于 ${time(stamp)} · 每 5 秒刷新`;
  el("wallet-balance").textContent = format(wallet.wallet);
  el("equity").textContent = format(wallet.equity);
  el("available").textContent = format(wallet.available);
  el("unrealized").textContent = signed(wallet.unrealized);
  tone(el("unrealized"), wallet.unrealized);
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

function renderIncome({as_of: stamp, totals, daily, incomplete}) {
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
    ? "币安返回的流水达到读取上限，合计只覆盖已读取记录。"
    : "交易净额 = 已实现盈亏 + 手续费 + 资金费。净划转和其他流水单列。";
  dailyHistory = daily;
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
poll("/api/account", renderSnapshot, "snapshot-status", "account-error", 5000);
poll("/api/account/income", renderIncome, "income-status", "income-error", 60000);
