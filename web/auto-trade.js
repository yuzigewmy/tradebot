const byId = (id) => document.getElementById(id);
const clock = new Intl.DateTimeFormat("zh-CN", {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: "Asia/Shanghai"});
const price = new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 8});
const environments = {live: "币安实盘", demo: "币安 Demo", quotes: "币安行情", off: "OKX 模拟"};
const strategies = {
  auto_regime_router: "自动切换 A（强单边 S10·30× / 震荡 A1·15×）",
  dual_trend_engine: "S10 双引擎顺势（回踩 + 突破 · 30×）",
  macro_a1_aggressive: "A1 宏观双引擎 · 激进版（4H超趋势 + 1D唐奇安 · 15×）",
  macro_a1_steady: "A1 宏观双引擎 · 稳健版（4H超趋势 + 1D唐奇安 · 10×）",
  btc_trend_reclaim: "BTC 多周期均线确认", eth_compression_short: "ETH 30 分钟波动收缩做空",
  all: "回踩与突破", channel_breakout: "十五分钟区间突破", trend_pullback: "十五分钟趋势回踩",
};
const monitorLabels = {
  stopped: ["已停止开新仓"], connecting: ["正在连接行情", "waiting"], online: ["行情在线", "online"],
  reconnecting: ["行情重连中", "waiting"], error: ["监控异常", "error"],
};
const wechatLabels = {
  unbound: ["未绑定"], expired: ["授权已到期", "error"], offline: ["离线", "error"],
  connecting: ["连接中", "waiting"], reconnecting: ["重连中", "waiting"], online: ["已连接", "online"],
};
const stages = {
  pending: [["准备提交", "waiting"], ["等待成交"], ["等待开仓"]],
  entry_ordered: [["核对开仓单", "waiting"], ["等待成交确认"], ["等待开仓确认"]],
  protected: [["开仓已成交", "online"], ["止盈止损已挂", "online"], ["持仓跟踪中", "waiting"]],
  closing: [["开仓已成交", "online"], ["持仓退出中"], ["核对平仓单", "waiting"]],
  binance_closed: [["开仓已成交", "online"], ["追踪已结束"], ["已平仓结案", "online"]],
  entry_expired: [["入场窗口已过期", "waiting"], ["未开仓"], ["未开仓"]],
  entry_unfilled: [["未成交", "waiting"], ["未开仓"], ["未开仓"]],
  entry_rejected: [["开仓受阻", "error"], ["未开仓"], ["未开仓"]],
  entry_skipped: [["未送单", "waiting"], ["未开仓"], ["未开仓"]],
};
const emptyStages = [["等待信号"], ["等待成交"], ["等待开仓"]];
const stateLabels = {
  pending: "准备提交", entry_ordered: "核对开仓单", protected: "保护单已挂", closing: "核对平仓单",
  binance_closed: "已结案", entry_expired: "入场过期", entry_unfilled: "未成交", entry_rejected: "开仓受阻", entry_skipped: "未送单",
};
const switches = [...document.querySelectorAll(".contract-switch button")];
const tableLabels = [...byId("activity-table").querySelectorAll("th")].map((header) => header.textContent);
let asset = "BTC";
let snapshot;

function label(id, [text, tone]) {
  const element = byId(id);
  if (element.textContent !== text) element.textContent = text;
  if (tone) element.dataset.tone = tone;
  else delete element.dataset.tone;
}

function step(id, [text, tone]) {
  const element = byId(`${id}-step`);
  element.textContent = text;
  if (tone) element.parentElement.dataset.tone = tone;
  else delete element.parentElement.dataset.tone;
}

function time(id, stamp, prefix) {
  const element = byId(id);
  element.hidden = stamp == null;
  if (stamp == null) return;
  element.dateTime = new Date(stamp).toISOString();
  element.textContent = `${prefix}${clock.format(stamp)} 北京时间`;
}

function tradingState({execution_mode: mode, trading_enabled: enabled}, count, markets) {
  if (!enabled) return ["自动交易未启用"];
  if (count === markets.length) return [`${environments[mode]}运行中`, "online"];
  if (count) return ["部分合约运行中", "waiting"];
  if (markets.some((market) => market.status === "error")) return ["行情监控异常", "error"];
  if (markets.some((market) => market.running && market.mode === "signals")) return ["等待行情连接", "waiting"];
  return ["已暂停开新仓"];
}

function renderSummary() {
  const {health, markets, wechat} = snapshot;
  const count = markets.filter((market) => market.running && market.mode === "signals" && market.status === "online").length;
  const {trading_enabled: enabled, execution_mode: mode} = health;
  label("runtime-state", tradingState(health, count, markets));
  byId("execution-mode").textContent = environments[mode];
  byId("running-count").textContent = enabled ? `${count} / ${markets.length} 个合约运行中` : "当前环境用于研究";
  const activeMarket = markets.find(({instrument}) => instrument === `${asset}-USDT-SWAP`) ?? markets[0];
  const usesRisk = activeMarket.risk_percent != null;
  const regime = activeMarket.evaluation?.setup?.regime;
  byId("margin-budget").textContent = !enabled ? "—"
    : usesRisk ? `单笔止损风险 ${activeMarket.risk_percent}%`
    : `USDT 钱包余额的 ${activeMarket.margin_percent}%`;
  byId("trade-leverage").textContent = enabled ? `${activeMarket.leverage} 倍 · 逐仓` : "—";
  byId("margin-budget").nextElementSibling.textContent = usesRisk
    ? (regime ? `${regime.label} · 上限 ${regime.active_max_notional}×` : "按止损距离动态计算仓位")
    : "开仓前按当时余额计算";
  byId("margin-budget").nextElementSibling.hidden = !enabled;
  byId("trade-leverage").nextElementSibling.hidden = !enabled;
  const waitingForChat = wechat.connection === "online" && !wechat.has_context;
  label("wechat-state", waitingForChat ? ["等待首次私聊", "waiting"] : wechatLabels[wechat.connection]);
  const delivery = wechat.last_delivery;
  byId("wechat-delivery").textContent = delivery
    ? `${clock.format(delivery.at)} · ${delivery.status === "sent" ? "接口已接收" : "投递失败"}`
    : "暂无信号投递记录";
  time("snapshot-time", Math.max(...markets.map((market) => market.server_time)), "更新于 ");
}

function renderWorkflow() {
  const {health, markets} = snapshot;
  const market = markets.find(({instrument}) => instrument === `${asset}-USDT-SWAP`);
  const enabled = health.trading_enabled;
  const current = market.status === "online" && market.mode === "signals";
  const latest = enabled ? market.signals[0] : undefined;
  const review = market.review;
  const reviewing = current && review && ["queued", "reviewing"].includes(review.status);
  byId("contract-strategy").textContent = strategies[market.selected_strategy];
  label("contract-state", market.status === "online" && market.mode === "observe" ? ["行情观察", "online"] : monitorLabels[market.status]);
  time("contract-close", market.evaluation?.closed_at, "最近收盘 ");
  const detail = byId("contract-detail");
  detail.textContent = current && review ? `最近候选 ${clock.format(review.closed_at)} · ${review.note}` : market.market_note;
  detail.hidden = !detail.textContent;
  step("market", market.evaluation && market.status === "online" ? ["收盘已研判", "online"] : monitorLabels[market.status]);
  let signalStage = ["等待策略触发"];
  if (latest) signalStage = ["信号已发布", "online"];
  else if (reviewing) signalStage = ["入场条件核对中", "waiting"];
  else if (!enabled) signalStage = ["自动交易未启用"];
  else if (!current) signalStage = ["等待监控运行"];
  step("signal", signalStage);
  const progress = latest ? stages[latest.outcome.status] : emptyStages;
  ["entry", "protection", "close"].forEach((id, index) => step(id, progress[index]));
  const tracked = byId("tracked-signal");
  tracked.hidden = !latest;
  if (latest) {
    const id = latest.trade_agent_id ? ` · 子 agent ${latest.trade_agent_id}` : "";
    tracked.textContent = `订单进度对应 ${clock.format(latest.published_at)} 发布的最近信号（北京时间）${id}`;
  }
}

function renderActivity() {
  const {health, markets} = snapshot;
  const signals = health.trading_enabled
    ? markets.flatMap((market) => market.signals).sort((left, right) => right.published_at - left.published_at).slice(0, 8)
    : [];
  const empty = byId("activity-empty");
  empty.hidden = signals.length > 0;
  byId("activity-table").hidden = !signals.length;
  empty.textContent = health.trading_enabled ? "暂无自动开单记录。策略触发后会在这里显示信号与执行结果。" : "币安自动交易启用后会在这里显示信号与执行结果。";
  byId("activity-rows").replaceChildren(...signals.map((signal) => {
    const row = document.createElement("tr");
    const {status, verdict, pnl_usdt: pnl} = signal.outcome;
    const direction = signal.side === "long" ? "做多" : "做空";
    const outcome = verdict ? `${stateLabels[status]} · ${verdict}` : stateLabels[status];
    const values = [clock.format(signal.published_at), `${signal.instrument.split("-")[0]}USDT · ${direction}`,
      price.format(signal.entry), price.format(signal.take_profit), price.format(signal.stop_loss), outcome, signal.trade_agent_id ?? "—"];
    values.forEach((value, index) => {
      const cell = row.insertCell();
      cell.dataset.label = tableLabels[index];
      cell.textContent = value;
    });
    const result = row.cells[5];
    if (verdict) result.dataset.tone = verdict === "成功" ? "online" : "error";
    if (pnl != null) {
      const detail = document.createElement("small");
      detail.textContent = `净盈亏 ${price.format(pnl)} USDT`;
      result.append(detail);
    }
    return row;
  }));
}

async function get(path, signal) {
  const response = await fetch(path, {cache: "no-store", signal});
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

async function refresh() {
  let readings;
  try {
    const signal = AbortSignal.timeout(5000);
    readings = await Promise.all([
      get("/api/health", signal), get("/api/wechat", signal),
      get("/api/perpetual/BTC-USDT-SWAP", signal), get("/api/perpetual/ETH-USDT-SWAP", signal), get("/api/perpetual/SOL-USDT-SWAP", signal),
    ]);
  } catch {
    label("runtime-state", ["后台状态暂不可用", "error"]);
    byId("runtime-error").hidden = false;
    if (!snapshot) byId("activity-empty").textContent = "等待后台连接后读取信号记录。";
    return;
  } finally {
    setTimeout(refresh, 3000);
  }
  const [health, wechat, btc, eth, sol] = readings;
  snapshot = {health, wechat, markets: [btc, eth, sol]};
  renderSummary();
  renderWorkflow();
  renderActivity();
  byId("runtime-error").hidden = true;
}

for (const button of switches) {
  button.addEventListener("click", () => {
    asset = button.dataset.asset;
    switches.forEach((control) => control.setAttribute("aria-pressed", String(control === button)));
    if (snapshot) { renderSummary(); renderWorkflow(); }
  });
}

refresh();
