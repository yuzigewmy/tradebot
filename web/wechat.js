const byId = (id) => document.getElementById(id);
const connectionNames = {
  unbound: "微信未绑定", connecting: "正在连接微信", online: "微信已连接",
  reconnecting: "微信正在重连", expired: "微信授权已到期", offline: "微信离线",
};
const clock = new Intl.DateTimeFormat("zh-CN", {timeStyle: "medium", timeZone: "Asia/Shanghai"});
let busy = false;

async function request(path, options) {
  const response = await fetch(`/api/wechat${path}`, {cache: "no-store", ...options});
  if (response.status === 204) return;
  const value = await response.json();
  if (!response.ok) throw new Error(value.error?.message ?? "微信连接操作失败");
  return value;
}

function render(state) {
  const bound = !["unbound", "expired"].includes(state.connection);
  const qr = byId("qr");
  byId("connection").textContent = connectionNames[state.connection];
  byId("connection").dataset.online = String(state.connection === "online");
  if (state.qr_available && qr.hidden) qr.src = `/api/wechat/binding/qr?v=${Date.now()}`;
  if (!state.qr_available) qr.removeAttribute("src");
  qr.hidden = !state.qr_available;
  byId("bind").hidden = bound || state.qr_available;
  byId("bind").disabled = false;
  byId("unlink").hidden = state.connection === "unbound" && !state.qr_available;
  byId("unlink").textContent = state.qr_available ? "取消扫码" : "解除本机绑定";
  byId("verify-form").hidden = state.pairing !== "verification";

  let note = state.pair_note || "生成二维码后，用手机微信扫一扫授权。";
  if (bound) note = state.has_context ? "可以在微信里对话并接收信号。" : "已绑定。请在微信里给机器人发一条消息。";
  if (state.connection === "expired" && !state.qr_available) note = "请生成新二维码重新绑定。";
  byId("binding-note").textContent = note;
  const delivery = state.last_delivery;
  byId("delivery-note").textContent = delivery
    ? `${clock.format(delivery.at)}（北京时间） ${delivery.instrument} ${delivery.status === "sent" ? "微信接口已接收" : "投递失败"}`
    : "暂无推送记录";
}

async function update(action) {
  if (busy) return;
  busy = true;
  try {
    if (action) await action();
    render(await request(""));
    if (action) byId("wechat-error").hidden = true;
  } catch (error) {
    byId("wechat-error").textContent = error.message;
    byId("wechat-error").hidden = false;
  } finally {
    busy = false;
  }
}

byId("bind").addEventListener("click", () => update(() => request("/binding", {method: "POST"})));
byId("unlink").addEventListener("click", () => update(() => request("/binding", {method: "DELETE"})));
byId("verify-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = byId("verification");
  update(async () => {
    await request("/binding/verification", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({code: input.value})});
    input.value = "";
  });
});

update();
setInterval(update, 1800);
