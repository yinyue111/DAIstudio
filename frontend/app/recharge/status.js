export function paymentStatusText(status) {
  return { pending: "待支付", paid: "已支付", closed: "已关闭", failed: "失败" }[status] || status;
}

export function paymentStatusStyle(status) {
  return {
    pending: "bg-aqua/15 text-aqua",
    paid: "bg-ok/15 text-ok",
    closed: "bg-white/10 text-fog",
    failed: "bg-bad/15 text-bad",
  }[status] || "bg-white/10 text-fog";
}
