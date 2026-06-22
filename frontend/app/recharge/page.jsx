"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import QRCode from "qrcode";
import Nav from "../../components/Nav";
import { api } from "../../lib/api";

const PROVIDERS = [
  ["alipay", "支付宝"],
  ["wechat", "微信"],
];

export default function RechargePage() {
  const router = useRouter();
  const [me, setMe] = useState(null);
  const [packages, setPackages] = useState([]);
  const [providers, setProviders] = useState([]);
  const [paymentEnabled, setPaymentEnabled] = useState(false);
  const [orders, setOrders] = useState([]);
  const [provider, setProvider] = useState("alipay");
  const [packageId, setPackageId] = useState("");
  const [activeOrder, setActiveOrder] = useState(null);
  const [qrImage, setQrImage] = useState("");
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState("");
  const [now, setNow] = useState(() => Date.now());
  const pollRef = useRef(null);
  const pollFailuresRef = useRef(0);
  const createOrderSeqRef = useRef(0);
  const providerRef = useRef(provider);
  const packageIdRef = useRef(packageId);

  useEffect(() => {
    Promise.all([api.me(), api.paymentConfig(), api.paymentOrders(12)])
      .then(([u, paymentCfg, rows]) => {
        const enabled = paymentCfg.enabled !== false;
        const pkgs = paymentCfg.packages || [];
        const readyProviders = (paymentCfg.providers || [])
          .filter((p) => p.enabled && p.ready)
          .map((p) => p.provider);
        setMe(u);
        setPaymentEnabled(enabled);
        setPackages(pkgs);
        setProviders(readyProviders);
        setOrders(rows);
        const pending = rows.find((o) => o.status === "pending" && o.code_url);
        if (pending) {
          api.paymentOrder(pending.order_no)
            .then((next) => {
              setActiveOrder(next.status === "pending" ? next : null);
              patchOrder(next);
            })
            .catch(() => {});
        }
        setPackageId(pkgs[1]?.id || pkgs[0]?.id || "");
        setProvider(readyProviders.includes("alipay") ? "alipay" : readyProviders[0] || "alipay");
      })
      .catch((e) => setMsg(e.message));
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    if (!activeOrder || activeOrder.status !== "pending") return;
    let stopped = false;
    const poll = async () => {
      if (stopped) return;
      try {
        const next = await api.paymentOrder(activeOrder.order_no);
        pollFailuresRef.current = 0;
        setMsg("");
        setActiveOrder(next);
        patchOrder(next);
        if (next.status === "paid") {
          api.me().then(setMe).catch(() => {});
          api.paymentOrders(12).then(setOrders).catch(() => {});
        } else if (next.status !== "pending") {
          api.paymentOrders(12).then(setOrders).catch(() => {});
        }
        if (next.status !== "pending" && pollRef.current) {
          clearInterval(pollRef.current);
          pollRef.current = null;
        }
      } catch (e) {
        pollFailuresRef.current += 1;
        setMsg(`${e.message}。可稍后手动刷新订单状态。`);
        if (pollFailuresRef.current >= 5 && pollRef.current) {
          clearInterval(pollRef.current);
          pollRef.current = null;
        }
      }
    };
    pollFailuresRef.current = 0;
    pollRef.current = setInterval(poll, 2500);
    return () => {
      stopped = true;
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = null;
    };
  }, [activeOrder?.order_no, activeOrder?.status]);

  const orderExpired = Boolean(
    activeOrder?.status === "pending"
    && activeOrder.expires_at
    && Date.parse(activeOrder.expires_at) <= now,
  );
  const qrActive = Boolean(activeOrder?.status === "pending" && !orderExpired && activeOrder.code_url);
  const canMockPay = qrActive && isLocalMockOrder(activeOrder);
  const orderClosed = Boolean(activeOrder && !qrActive);

  useEffect(() => {
    let alive = true;
    setQrImage("");
    if (!qrActive) return undefined;
    QRCode.toDataURL(activeOrder.code_url, {
      errorCorrectionLevel: "M",
      margin: 1,
      scale: 8,
      color: { dark: "#111827", light: "#ffffff" },
    })
      .then((url) => {
        if (alive) setQrImage(url);
      })
      .catch((e) => {
        if (alive) setMsg(e.message || "二维码生成失败");
      });
    return () => {
      alive = false;
    };
  }, [activeOrder?.code_url, qrActive]);

  const selectedPackage = useMemo(
    () => packages.find((p) => p.id === packageId) || packages[0],
    [packages, packageId],
  );
  const providerOptions = useMemo(
    () => PROVIDERS.filter(([key]) => providers.includes(key)),
    [providers],
  );
  const selectedProviderLabel = PROVIDERS.find((p) => p[0] === provider)?.[1] || provider;

  useEffect(() => {
    providerRef.current = provider;
  }, [provider]);

  useEffect(() => {
    packageIdRef.current = packageId;
  }, [packageId]);

  function patchOrder(next) {
    setOrders((rows) => [next, ...rows.filter((r) => r.order_no !== next.order_no)].slice(0, 12));
  }

  async function refreshOrders() {
    const rows = await api.paymentOrders(12);
    setOrders(rows);
    if (activeOrder) {
      const current = rows.find((o) => o.order_no === activeOrder.order_no);
      if (current) setActiveOrder(current);
    }
    return rows;
  }

  function clearPendingOrderForNewSelection() {
    createOrderSeqRef.current += 1;
    setActiveOrder(null);
    setQrImage("");
  }

  async function createOrder() {
    if (!selectedPackage || loading) return;
    const orderProvider = provider;
    const orderPackageId = selectedPackage.id;
    const seq = ++createOrderSeqRef.current;
    setLoading(true);
    setMsg("");
    try {
      if (!paymentEnabled) throw new Error("支付充值功能未开启");
      if (!providers.includes(orderProvider)) throw new Error("当前没有可用支付渠道");
      const order = await api.createPaymentOrder({ provider: orderProvider, package_id: orderPackageId });
      patchOrder(order);
      if (
        seq === createOrderSeqRef.current
        && providerRef.current === orderProvider
        && packageIdRef.current === orderPackageId
      ) {
        setActiveOrder(order);
      }
    } catch (e) {
      setMsg(e.message);
      refreshOrders().catch(() => {});
    } finally {
      setLoading(false);
    }
  }

  async function mockPay() {
    if (!activeOrder) return;
    setLoading(true);
    setMsg("");
    try {
      const paid = await api.mockPayOrder(activeOrder.order_no);
      setActiveOrder(paid);
      patchOrder(paid);
      api.me().then(setMe).catch(() => {});
    } catch (e) {
      setMsg(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function refreshActiveOrder() {
    if (!activeOrder) return;
    setLoading(true);
    setMsg("");
    try {
      const next = await api.paymentOrder(activeOrder.order_no);
      setActiveOrder(next);
      patchOrder(next);
      refreshOrders().catch(() => {});
      if (next.status === "paid") api.me().then(setMe).catch(() => {});
    } catch (e) {
      setMsg(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function viewOrder(orderNo) {
    setLoading(true);
    setMsg("");
    try {
      const next = await api.paymentOrder(orderNo);
      setActiveOrder(next);
      patchOrder(next);
      if (next.status === "paid") api.me().then(setMe).catch(() => {});
    } catch (e) {
      setMsg(e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen">
      <Nav me={me} active="recharge" />
      <main className="mx-auto max-w-6xl px-4 pb-24 pt-10 sm:px-6">
        <section className="mb-6 grid gap-4 lg:grid-cols-[1fr_360px]">
          <div className="panel p-5 sm:p-6">
            <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
              <div>
                <h1 className="text-2xl font-bold">积分充值</h1>
                <p className="mt-1 text-sm text-fog">扫码支付后自动入账，可用于反推、图片和视频生成。</p>
              </div>
              <div className="rounded-xl2 border border-iris/30 bg-iris/10 px-4 py-2 text-right">
                <div className="font-display text-2xl font-bold text-grad">{me?.balance_credits ?? "--"}</div>
                <div className="text-xs text-fog">当前可用积分</div>
              </div>
            </div>

            {!paymentEnabled ? (
              <div className="mb-5 rounded-xl border border-warn/30 bg-warn/10 px-3 py-2 text-sm text-warn">
                支付充值功能未开启，请联系管理员在后台完成商户配置后启用。
              </div>
            ) : providerOptions.length > 0 ? (
              <div className="mb-5 inline-flex gap-1 rounded-full border border-line bg-white/5 p-1">
                {providerOptions.map(([key, label]) => (
                  <button
                    key={key}
                    onClick={() => {
                      if (loading) return;
                      setProvider(key);
                      clearPendingOrderForNewSelection();
                    }}
                    disabled={loading}
                    className={provider === key ? "chip chip-active disabled:opacity-50" : "chip disabled:opacity-50"}
                  >
                    {label}
                  </button>
                ))}
              </div>
            ) : (
              <div className="mb-5 rounded-xl border border-warn/30 bg-warn/10 px-3 py-2 text-sm text-warn">
                暂无可用支付渠道，请联系管理员配置。
              </div>
            )}

            {paymentEnabled && packages.length > 0 ? <div className="grid gap-3 sm:grid-cols-3">
              {packages.map((p) => (
                <button
                  key={p.id}
                  onClick={() => {
                    if (loading) return;
                    setPackageId(p.id);
                    clearPendingOrderForNewSelection();
                  }}
                  disabled={loading}
                  className={`relative rounded-xl2 border p-4 text-left transition ${
                    packageId === p.id ? "border-iris bg-iris/15" : "border-line bg-base2/50 hover:border-line2"
                  } disabled:cursor-not-allowed disabled:opacity-60`}
                >
                  {p.badge && <span className="badge absolute right-3 top-3 bg-brand text-white">{p.badge}</span>}
                  <div className="text-sm text-fog">{p.title}</div>
                  <div className="mt-2 font-display text-3xl font-bold text-snow">{p.credits}</div>
                  <div className="mt-1 text-xs text-fog">积分</div>
                  <div className="mt-4 text-sm text-mist">￥{(p.amount_cents / 100).toFixed(2)}</div>
                </button>
              ))}
            </div> : paymentEnabled ? (
              <div className="rounded-xl2 border border-line bg-base2/50 p-6 text-center text-sm text-fog">
                暂无可购买套餐，请联系管理员配置。
              </div>
            ) : (
              <div className="rounded-xl2 border border-line bg-base2/50 p-6 text-center text-sm text-fog">
                充值入口已关闭。
              </div>
            )}

            <div className="mt-5 flex flex-wrap items-center justify-between gap-3 rounded-xl2 border border-line bg-base2/40 p-4">
              <div className="text-sm text-mist">
                当前选择 <b className="text-snow">{selectedPackage?.credits || 0}</b> 积分 ·
                <span className="text-fog"> {selectedProviderLabel}扫码</span>
              </div>
              <button onClick={createOrder} disabled={loading || !paymentEnabled || !selectedPackage || !providers.includes(provider)} className="btn-primary">
                {loading ? "处理中…" : "生成支付二维码"}
              </button>
            </div>
          </div>

          <div className="panel p-5">
            <h2 className="mb-3 text-lg font-bold">扫码支付</h2>
            {!activeOrder ? (
              <div className="flex aspect-square items-center justify-center rounded-xl2 border border-line bg-base2/50 p-6 text-center text-sm text-fog">
                选择套餐后生成二维码
              </div>
            ) : (
              <>
                {qrActive ? (
                  <div className="relative rounded-xl2 border border-line bg-white p-3">
                    {qrImage ? (
                      <img
                        src={qrImage}
                        alt="支付二维码"
                        className="aspect-square w-full rounded-lg object-contain"
                      />
                    ) : (
                      <div className="flex aspect-square w-full items-center justify-center rounded-lg bg-white text-sm text-slate-500">
                        二维码生成中
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="flex aspect-square items-center justify-center rounded-xl2 border border-line bg-base2/50 p-6 text-center text-sm text-fog">
                    {activeOrder.status === "paid"
                      ? "支付已完成，积分已入账。"
                      : orderExpired
                        ? "二维码已过期，请重新生成。"
                        : "该订单二维码已失效。"}
                  </div>
                )}
                <div className="mt-3 flex items-center justify-between text-sm">
                  <span className={`badge ${statusStyle(orderExpired ? "closed" : activeOrder.status)}`}>
                    {orderExpired ? "已过期" : statusZh(activeOrder.status)}
                  </span>
                  <span className="text-fog">￥{(activeOrder.amount_cents / 100).toFixed(2)}</span>
                </div>
                <div className="mt-2 text-xs text-mist">
                  {PROVIDERS.find((p) => p[0] === activeOrder.provider)?.[1] || activeOrder.provider}
                  {" · "}
                  {activeOrder.credits} 积分
                </div>
                <p className="mt-2 break-all text-xs text-fog">订单号 {activeOrder.order_no}</p>
                {canMockPay && (
                  <button onClick={mockPay} disabled={loading} className="btn-secondary mt-3 w-full">
                    本地模拟支付成功
                  </button>
                )}
                {activeOrder.status === "pending" && (
                  <button onClick={refreshActiveOrder} disabled={loading} className="btn-secondary mt-2 w-full">
                    刷新订单状态
                  </button>
                )}
                {activeOrder.status === "paid" && (
                  <div className="mt-3 rounded-xl border border-ok/30 bg-ok/10 px-3 py-2 text-sm text-ok">
                    已入账 {activeOrder.credits} 积分
                  </div>
                )}
                {orderExpired && (
                  <p className="mt-2 rounded-xl border border-warn/30 bg-warn/10 px-3 py-2 text-xs text-warn">
                    本地已到订单有效期，可刷新确认状态或重新生成二维码。
                  </p>
                )}
                {orderClosed && activeOrder.status !== "paid" && (
                  <button onClick={createOrder} disabled={loading || !paymentEnabled || !selectedPackage || !providers.includes(provider)} className="btn-primary mt-3 w-full">
                    {loading ? "处理中…" : "重新生成二维码"}
                  </button>
                )}
              </>
            )}
          </div>
        </section>

        {msg && <div className="mb-5 rounded-xl border border-bad/30 bg-bad/10 px-4 py-2.5 text-sm text-bad">{msg}</div>}

        <section className="card p-5">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-lg font-bold">最近充值订单</h2>
            <button onClick={() => refreshOrders().catch((e) => setMsg(e.message))}
              className="btn-secondary btn-sm">刷新</button>
          </div>
          {orders.length === 0 ? (
            <p className="py-8 text-center text-sm text-fog">还没有充值订单。</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs text-fog">
                  <tr>
                    <th className="py-2 pr-3">订单</th>
                    <th className="pr-3">渠道</th>
                    <th className="pr-3">金额</th>
                    <th className="pr-3">积分</th>
                    <th className="pr-3">状态</th>
                    <th className="pr-3">时间</th>
                    <th className="pr-3">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {orders.map((o) => (
                    <tr key={o.order_no} className="border-t border-line">
                      <td className="py-2 pr-3 font-mono text-xs text-mist">{o.order_no}</td>
                      <td className="pr-3">{o.provider === "wechat" ? "微信" : "支付宝"}</td>
                      <td className="pr-3">￥{(o.amount_cents / 100).toFixed(2)}</td>
                      <td className="pr-3 font-display text-snow">{o.credits}</td>
                      <td className="pr-3"><span className={`badge ${statusStyle(o.status)}`}>{statusZh(o.status)}</span></td>
                      <td className="pr-3 text-xs text-fog">{o.created_at ? new Date(o.created_at).toLocaleString() : "-"}</td>
                      <td className="pr-3">
                        {o.status === "pending" ? (
                          <button onClick={() => viewOrder(o.order_no)} disabled={loading} className="btn-secondary btn-sm">
                            继续支付
                          </button>
                        ) : o.status === "closed" || o.status === "failed" ? (
                          <button
                            onClick={() => {
                              setPackageId(o.package_id);
                              setProvider(o.provider);
                              setActiveOrder(o);
                            }}
                            className="btn-ghost btn-sm"
                          >
                            查看
                          </button>
                        ) : (
                          <button onClick={() => viewOrder(o.order_no)} disabled={loading} className="btn-ghost btn-sm">
                            查看
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </main>
    </div>
  );
}

function isLocalMockOrder(order) {
  if (!order?.code_url) return false;
  try {
    const u = new URL(order.code_url);
    return (u.hostname === "localhost" || u.hostname === "127.0.0.1") && u.searchParams.has("mock_order");
  } catch (e) {
    return false;
  }
}

function statusZh(status) {
  return { pending: "待支付", paid: "已支付", closed: "已关闭", failed: "失败" }[status] || status;
}

function statusStyle(status) {
  return {
    pending: "bg-aqua/15 text-aqua",
    paid: "bg-ok/15 text-ok",
    closed: "bg-white/10 text-fog",
    failed: "bg-bad/15 text-bad",
  }[status] || "bg-white/10 text-fog";
}
