export function modelUseLabel(use) {
  return use === "vision" ? "反推 / 视觉理解" : use === "image" ? "图片生成" : "视频生成";
}

export function reportReasonLabel(reason) {
  return {
    copyright: "版权",
    sensitive: "敏感",
    illegal: "违法",
    privacy: "隐私",
    other: "其他",
  }[reason] || reason;
}

export function reportStatusLabel(status) {
  return {
    open: "待处理",
    dismissed: "已驳回",
    takedown: "已下架",
  }[status] || status;
}

export function providerLabel(provider) {
  return provider === "wechat" ? "微信支付" : "支付宝";
}

export function paymentPackageDiff(before, after) {
  const fields = [
    ["title", "名称", (v) => String(v || "")],
    ["amount_cents", "金额(分)", (v) => String(Number(v || 0))],
    ["credits", "积分", (v) => String(Number(v || 0))],
    ["badge", "角标", (v) => String(v || "")],
    ["enabled", "状态", (v) => (v ? "启用" : "停用")],
    ["sort_order", "排序", (v) => String(Number(v || 0))],
  ];
  return fields.flatMap(([key, label, normalise]) => {
    const oldValue = normalise(before[key]);
    const newValue = normalise(after[key]);
    return oldValue === newValue ? [] : [`${label}: ${oldValue || "-"} -> ${newValue || "-"}`];
  });
}

export function formatMinutes(value) {
  const minutes = Number(value || 0);
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} 小时 ${rest} 分钟` : `${hours} 小时`;
}

export function confirmReviewTaskAction(task, action, note = "", resultUrl = "") {
  const lines = [
    `确认${action}待对账任务 #${task.id}？`,
    `用户：${task.user_id || "-"}`,
    `冻结积分：${task.cost_frozen || 0}`,
    `等待：${formatMinutes(task.age_minutes)}`,
    `request：${task.video_request_id || "-"}`,
    `external：${task.external_task_id || "-"}`,
  ];
  if (resultUrl) lines.push(`结果 URL：${resultUrl}`);
  if (note) lines.push(`备注：${note}`);
  lines.push("该操作会影响用户积分和任务状态，请确认已核对上游平台。");
  return window.confirm(lines.join("\n"));
}

export function promptAdminPassword(action) {
  return promptPassword(`${action}需要管理员密码确认`, "管理员密码");
}

export function promptPassword(title, placeholder = "密码") {
  if (typeof document === "undefined") return Promise.resolve("");
  return new Promise((resolve) => {
    const wrap = document.createElement("div");
    wrap.className = "fixed inset-0 z-[9999] flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm";
    const panel = document.createElement("form");
    panel.className = "panel w-full max-w-sm p-5";
    const h = document.createElement("div");
    h.className = "mb-3 text-sm font-display font-semibold text-snow";
    h.textContent = title;
    const input = document.createElement("input");
    input.type = "password";
    input.autocomplete = "current-password";
    input.placeholder = placeholder;
    input.className = "input w-full";
    const actions = document.createElement("div");
    actions.className = "mt-4 flex justify-end gap-2";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn-secondary btn-sm";
    cancel.textContent = "取消";
    const ok = document.createElement("button");
    ok.type = "submit";
    ok.className = "btn-primary btn-sm";
    ok.textContent = "确认";
    actions.append(cancel, ok);
    panel.append(h, input, actions);
    wrap.append(panel);
    const cleanup = (value) => {
      input.value = "";
      wrap.remove();
      resolve(value);
    };
    cancel.addEventListener("click", () => cleanup(""));
    wrap.addEventListener("click", (e) => { if (e.target === wrap) cleanup(""); });
    panel.addEventListener("submit", (e) => {
      e.preventDefault();
      cleanup(input.value);
    });
    document.body.append(wrap);
    input.focus();
  });
}
