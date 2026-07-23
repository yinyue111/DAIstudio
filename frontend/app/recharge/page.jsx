"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import QRCode from "qrcode";
import Nav from "../../components/Nav";
import { useToast } from "../../components/ToastProvider";
import { api, loginPath, wsUrl } from "../../lib/api";
import { reportBackgroundError } from "../../lib/errorHandling";
import AccountTabs, { ACCOUNT_TABS } from "./AccountTabs";
import CreditLedger from "./CreditLedger";
import PaymentOrdersPanel from "./PaymentOrdersPanel";
import {
  createPaymentOrderPoller,
  selectMonotonicPaymentOrder,
  selectMonotonicPaymentOrders,
  shouldStopPaymentOrderPoll,
} from "./polling";
import { paymentStatusStyle, paymentStatusText } from "./status";

const PROVIDERS = [
  ["alipay", "支付宝"],
  ["wechat", "微信"],
];

export default function RechargePage() {
  const router = useRouter();
  const notify = useToast();
  const [me, setMe] = useState(null);
  const [packages, setPackages] = useState([]);
  const [providers, setProviders] = useState([]);
  const [paymentEnabled, setPaymentEnabled] = useState(false);
  const [paymentCapabilityKnown, setPaymentCapabilityKnown] = useState(false);
  const [orders, setOrders] = useState([]);
  const [provider, setProvider] = useState("alipay");
  const [packageId, setPackageId] = useState("");
  const [activeOrder, setActiveOrder] = useState(null);
  const [qrImage, setQrImage] = useState("");
  const [loading, setLoading] = useState(false);
  const [initialLoading, setInitialLoading] = useState(true);
  const [ordersLoading, setOrdersLoading] = useState(true);
  const [ordersError, setOrdersError] = useState("");
  const [orderLimit, setOrderLimit] = useState(20);
  const [activeTab, setActiveTab] = useState("packages");
  const [billingRefreshKey, setBillingRefreshKey] = useState(0);
  const [msg, setMsg] = useState("");
  const [now, setNow] = useState(() => Date.now());
  const pollRef = useRef(null);
  const pollFailuresRef = useRef(0);
  const eventWsRef = useRef(null);
  const createOrderSeqRef = useRef(0);
  const providerRef = useRef(provider);
  const packageIdRef = useRef(packageId);
  const activeOrderRef = useRef(activeOrder);

  useEffect(() => {
    api.me()
      .then(async (u) => {
        setMe(u);
        const paymentCfg = await api.paymentConfig();
        const enabled = paymentCfg.enabled !== false;
        const rows = enabled ? await api.paymentOrders(20) : [];
        const pkgs = paymentCfg.packages || [];
        const readyProviders = (paymentCfg.providers || [])
          .filter((p) => p.enabled && p.ready)
          .map((p) => p.provider);
        setPaymentEnabled(enabled);
        setPaymentCapabilityKnown(true);
        if (!enabled) setActiveTab("billing");
        setPackages(pkgs);
        setProviders(readyProviders);
        replaceOrdersMonotonically(rows);
        const pending = rows.find((o) => o.status === "pending" && o.code_url);
        if (pending) {
          const seq = createOrderSeqRef.current;
          api.paymentOrder(pending.order_no)
            .then((next) => {
              patchOrder(next);
              if (seq === createOrderSeqRef.current && !activeOrderRef.current) {
                setActiveOrder(next.status === "pending" ? next : null);
              }
            })
            .catch((e) => reportBackgroundError(e, "refresh pending payment order"));
        }
        setPackageId(pkgs[1]?.id || pkgs[0]?.id || "");
        setProvider(readyProviders.includes("alipay") ? "alipay" : readyProviders[0] || "alipay");
      })
      .catch((e) => {
        if (e.status === 401) {
          router.push(loginPath());
          return;
        }
        setMsg(e.message);
        setOrdersError(e.message || "充值订单加载失败");
        notify.error(e.message || "充值页加载失败");
      })
      .finally(() => {
        setPaymentCapabilityKnown(true);
        setInitialLoading(false);
        setOrdersLoading(false);
      });
    return () => {
      if (pollRef.current) pollRef.current.stop();
      if (eventWsRef.current) eventWsRef.current.close();
    };
  }, []);

  useEffect(() => {
    const availableTabs = paymentCapabilityKnown && !paymentEnabled
      ? ACCOUNT_TABS.filter((tab) => tab.id === "billing")
      : ACCOUNT_TABS;
    const knownTabs = new Set(availableTabs.map((tab) => tab.id));
    const syncTabFromUrl = () => {
      const tab = new URLSearchParams(window.location.search).get("tab");
      if (knownTabs.has(tab)) setActiveTab(tab);
      else if (paymentCapabilityKnown) setActiveTab(paymentEnabled ? "packages" : "billing");
    };
    syncTabFromUrl();
    window.addEventListener("popstate", syncTabFromUrl);
    return () => window.removeEventListener("popstate", syncTabFromUrl);
  }, [paymentCapabilityKnown, paymentEnabled]);

  const accountTabs = useMemo(
    () => paymentCapabilityKnown && !paymentEnabled
      ? ACCOUNT_TABS.filter((tab) => tab.id === "billing")
      : ACCOUNT_TABS,
    [paymentCapabilityKnown, paymentEnabled],
  );

  useEffect(() => {
    let stopped = false;
    let retryTimer = null;
    async function connect() {
      if (stopped) return;
      try {
        const ticket = await api.eventWsTicket();
        if (stopped) return;
        const ws = new WebSocket(wsUrl(`/ws/events?ticket=${encodeURIComponent(ticket.ticket)}&last_id=$`));
        eventWsRef.current = ws;
        ws.onmessage = (event) => {
          let data = null;
          try { data = JSON.parse(event.data); } catch (e) { return; }
          for (const item of data.events || []) {
            if (item.type !== "payment_paid") continue;
            const orderNo = item.payload?.order_no;
            if (!orderNo) continue;
            api.paymentOrder(orderNo)
              .then((next) => {
                patchOrder(next);
                if (activeOrderRef.current?.order_no === orderNo) {
                  if (shouldStopPaymentOrderPoll(activeOrderRef.current, next) && pollRef.current) {
                    pollRef.current.stop();
                  }
                  updateActiveOrder(next);
                }
                api.me().then(setMe).catch((e) => reportBackgroundError(e, "refresh current user after payment event"));
                api.paymentOrders(20).then(replaceOrdersMonotonically).catch((e) => reportBackgroundError(e, "refresh payment orders after event"));
                setBillingRefreshKey((value) => value + 1);
                notify.success("支付成功，积分已到账。");
              })
              .catch((e) => reportBackgroundError(e, "load paid payment order from event"));
          }
        };
        ws.onclose = () => {
          if (stopped) return;
          retryTimer = window.setTimeout(connect, 3000);
        };
      } catch (e) {
        if (!stopped) retryTimer = window.setTimeout(connect, 5000);
      }
    }
    connect();
    return () => {
      stopped = true;
      if (retryTimer) window.clearTimeout(retryTimer);
      if (eventWsRef.current) eventWsRef.current.close();
    };
  }, [activeOrder?.order_no]);

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (pollRef.current) {
      pollRef.current.stop();
      pollRef.current = null;
    }
    if (!activeOrder || activeOrder.status !== "pending") return;
    const orderNo = activeOrder.order_no;
    const seq = createOrderSeqRef.current;
    pollFailuresRef.current = 0;
    const poller = createPaymentOrderPoller({
      request: () => api.paymentOrder(orderNo),
      onResult: (next) => {
        if (
          seq !== createOrderSeqRef.current
          || activeOrderRef.current?.order_no !== orderNo
        ) {
          return false;
        }
        pollFailuresRef.current = 0;
        setMsg("");
        patchOrder(next);
        const applied = updateActiveOrder(next);
        if (!applied) return activeOrderRef.current?.status === "pending";
        if (next.status === "paid") {
          notify.success("支付成功，积分已到账。");
          api.me().then(setMe).catch((e) => reportBackgroundError(e, "refresh current user after paid order poll"));
          api.paymentOrders(20).then(replaceOrdersMonotonically).catch((e) => reportBackgroundError(e, "refresh payment orders after paid poll"));
          setBillingRefreshKey((value) => value + 1);
        } else if (next.status !== "pending") {
          api.paymentOrders(20).then(replaceOrdersMonotonically).catch((e) => reportBackgroundError(e, "refresh payment orders after terminal poll"));
        }
        return next.status === "pending";
      },
      onError: (e) => {
        if (
          seq !== createOrderSeqRef.current
          || activeOrderRef.current?.order_no !== orderNo
        ) {
          return false;
        }
        pollFailuresRef.current += 1;
        const retryDelay = pollFailuresRef.current >= 5 ? "自动检查会降低频率继续进行。" : "正在继续自动检查。";
        setMsg(`${e.message}。${retryDelay}`);
        notify.warn(`${e.message}。${retryDelay}`, { duration: 5000 });
        return pollFailuresRef.current >= 5 ? 15000 : true;
      },
    });
    pollRef.current = poller;
    return () => {
      poller.stop();
      if (pollRef.current === poller) pollRef.current = null;
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
        if (alive) {
          setMsg(e.message || "二维码生成失败");
          notify.error(e.message || "二维码生成失败");
        }
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

  useEffect(() => {
    activeOrderRef.current = activeOrder;
  }, [activeOrder]);

  function patchOrder(next) {
    setOrders((rows) => {
      const current = rows.find((row) => row.order_no === next.order_no);
      const selected = selectMonotonicPaymentOrder(current, next);
      if (selected === current) return rows;
      return [selected, ...rows.filter((row) => row.order_no !== next.order_no)].slice(0, 100);
    });
  }

  function replaceOrdersMonotonically(nextRows) {
    setOrders((currentRows) => selectMonotonicPaymentOrders(currentRows, nextRows));
  }

  function updateActiveOrder(next) {
    const current = activeOrderRef.current;
    const selected = selectMonotonicPaymentOrder(current, next);
    if (selected === current) return false;
    activeOrderRef.current = selected;
    setActiveOrder(selected);
    return true;
  }

  function clearActiveOrder() {
    activeOrderRef.current = null;
    setActiveOrder(null);
  }

  async function refreshOrders(limit = orderLimit, { visible = false } = {}) {
    const seq = createOrderSeqRef.current;
    const currentOrderNo = activeOrderRef.current?.order_no;
    if (visible) {
      setOrdersLoading(true);
      setOrdersError("");
    }
    try {
      const rows = await api.paymentOrders(limit);
      replaceOrdersMonotonically(rows);
      if (currentOrderNo && seq === createOrderSeqRef.current && activeOrderRef.current?.order_no === currentOrderNo) {
        const current = rows.find((o) => o.order_no === currentOrderNo);
        if (current) updateActiveOrder(current);
      }
      return rows;
    } catch (error) {
      if (visible) setOrdersError(error.message || "充值订单加载失败");
      throw error;
    } finally {
      if (visible) setOrdersLoading(false);
    }
  }

  async function loadMoreOrders() {
    const nextLimit = Math.min(100, orderLimit + 20);
    await refreshOrders(nextLimit, { visible: true });
    setOrderLimit(nextLimit);
  }

  function selectAccountTab(tab) {
    setActiveTab(tab);
    const url = new URL(window.location.href);
    if (tab === "packages") url.searchParams.delete("tab");
    else url.searchParams.set("tab", tab);
    window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
  }

  function clearPendingOrderForNewSelection() {
    createOrderSeqRef.current += 1;
    clearActiveOrder();
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
      notify.success("订单已创建，请扫码支付。");
      if (
        seq === createOrderSeqRef.current
        && providerRef.current === orderProvider
        && packageIdRef.current === orderPackageId
      ) {
        updateActiveOrder(order);
      }
    } catch (e) {
      setMsg(e.message);
      notify.error(e.message || "创建订单失败");
      refreshOrders().catch((err) => reportBackgroundError(err, "refresh orders after create failure"));
    } finally {
      setLoading(false);
    }
  }

  async function mockPay() {
    if (!activeOrder) return;
    const orderNo = activeOrder.order_no;
    const seq = createOrderSeqRef.current;
    setLoading(true);
    setMsg("");
    try {
      const paid = await api.mockPayOrder(orderNo);
      patchOrder(paid);
      if (seq === createOrderSeqRef.current && activeOrderRef.current?.order_no === orderNo) {
        if (shouldStopPaymentOrderPoll(activeOrderRef.current, paid) && pollRef.current) {
          pollRef.current.stop();
        }
        updateActiveOrder(paid);
      }
      api.me().then(setMe).catch((err) => reportBackgroundError(err, "refresh current user after mock pay"));
      setBillingRefreshKey((value) => value + 1);
      notify.success("模拟支付成功，积分已到账。");
    } catch (e) {
      setMsg(e.message);
      notify.error(e.message || "模拟支付失败");
    } finally {
      setLoading(false);
    }
  }

  async function refreshActiveOrder() {
    if (!activeOrder) return;
    const orderNo = activeOrder.order_no;
    const seq = createOrderSeqRef.current;
    setLoading(true);
    setMsg("");
    try {
      const next = await api.paymentOrder(orderNo);
      patchOrder(next);
      if (seq === createOrderSeqRef.current && activeOrderRef.current?.order_no === orderNo) {
        if (shouldStopPaymentOrderPoll(activeOrderRef.current, next) && pollRef.current) {
          pollRef.current.stop();
        }
        updateActiveOrder(next);
      }
      refreshOrders().catch((err) => reportBackgroundError(err, "refresh orders after active order refresh"));
      if (next.status === "paid") {
        api.me().then(setMe).catch((err) => reportBackgroundError(err, "refresh current user after active order paid"));
        setBillingRefreshKey((value) => value + 1);
        notify.success("支付成功，积分已到账。");
      } else {
        notify.info("订单状态已刷新。");
      }
    } catch (e) {
      setMsg(e.message);
      notify.error(e.message || "刷新订单失败");
    } finally {
      setLoading(false);
    }
  }

  async function viewOrder(orderNo) {
    const seq = ++createOrderSeqRef.current;
    const row = orders.find((order) => order.order_no === orderNo);
    if (row) {
      setPackageId(row.package_id);
      setProvider(row.provider);
    }
    setLoading(true);
    setMsg("");
    try {
      const next = await api.paymentOrder(orderNo);
      patchOrder(next);
      if (seq === createOrderSeqRef.current) updateActiveOrder(next);
      selectAccountTab("packages");
      if (next.status === "paid") api.me().then(setMe).catch((err) => reportBackgroundError(err, "refresh current user after viewing paid order"));
    } catch (e) {
      setMsg(e.message);
      notify.error(e.message || "打开订单失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen">
      <Nav me={me} active="recharge" />
      <main className="mx-auto max-w-6xl px-4 pb-24 pt-8 sm:px-6">
        <section className="mb-5 flex flex-col gap-4 border-b border-line pb-5 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <h1 className="text-2xl font-bold">积分账户</h1>
            <p className="mt-1 text-sm text-fog">
              {paymentEnabled ? "查看余额、充值订单与每笔任务的结算明细。" : "查看积分余额与每笔任务的结算明细。"}
            </p>
          </div>
          <div className="grid grid-cols-3 gap-2 sm:min-w-[420px]">
            {initialLoading ? (
              Array.from({ length: 3 }).map((_, index) => <div key={index} className="skeleton h-[68px]" />)
            ) : (
              <>
                <div className="rounded-xl border border-iris/30 bg-iris/10 px-3 py-2.5">
                  <div className="text-[11px] text-fog">可用积分</div>
                  <div className="mt-1 truncate font-display text-xl font-bold text-snow">{me?.balance_credits ?? 0}</div>
                </div>
                <div className="rounded-xl border border-warn/25 bg-warn/10 px-3 py-2.5">
                  <div className="text-[11px] text-fog">冻结积分</div>
                  <div className="mt-1 truncate font-display text-xl font-bold text-warn">{me?.frozen_credits ?? 0}</div>
                </div>
                <div className="rounded-xl border border-line bg-white/5 px-3 py-2.5">
                  <div className="text-[11px] text-fog">账户总额</div>
                  <div className="mt-1 truncate font-display text-xl font-bold text-mist">
                    {Number(me?.balance_credits || 0) + Number(me?.frozen_credits || 0)}
                  </div>
                </div>
              </>
            )}
          </div>
        </section>

        <AccountTabs activeTab={activeTab} onChange={selectAccountTab} tabs={accountTabs} />
        {msg && <div role="alert" className="mt-4 rounded-xl border border-bad/30 bg-bad/10 px-4 py-2.5 text-sm text-bad">{msg}</div>}

        <div className="mt-5">
          {activeTab === "packages" && (
            <section
              id="account-panel-packages"
              role="tabpanel"
              aria-labelledby="account-tab-packages"
              className="grid gap-4 lg:grid-cols-[1fr_360px]"
            >
              <div className="panel p-5 sm:p-6">
                <h2 className="mb-5 text-lg font-bold">选择积分套餐</h2>
                {initialLoading ? <div className="grid gap-3 sm:grid-cols-3">
                  {Array.from({ length: 3 }).map((_, index) => <div key={index} className="skeleton h-40" />)}
                </div> : (
                  <>
                    {!paymentEnabled ? (
                      <div className="mb-5 rounded-xl border border-warn/30 bg-warn/10 px-3 py-2 text-sm text-warn">
                        支付充值功能未开启，请联系管理员在后台完成商户配置后启用。
                      </div>
                    ) : providerOptions.length > 0 ? (
                      <div className="mb-5 inline-flex gap-1 rounded-full border border-line bg-white/5 p-1">
                        {providerOptions.map(([key, label]) => (
                          <button
                            key={key}
                            type="button"
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
                      {packages.map((item) => (
                        <button
                          key={item.id}
                          type="button"
                          onClick={() => {
                            if (loading) return;
                            setPackageId(item.id);
                            clearPendingOrderForNewSelection();
                          }}
                          disabled={loading}
                          className={`relative rounded-xl2 border p-4 text-left transition-colors ${
                            packageId === item.id ? "border-iris bg-iris/15" : "border-line bg-base2/50 hover:border-line2"
                          } disabled:cursor-not-allowed disabled:opacity-60`}
                        >
                          {item.badge && <span className="badge absolute right-3 top-3 bg-brand text-white">{item.badge}</span>}
                          <div className="text-sm text-fog">{item.title}</div>
                          <div className="mt-2 font-display text-3xl font-bold text-snow">{item.credits}</div>
                          <div className="mt-1 text-xs text-fog">积分</div>
                          <div className="mt-4 text-sm text-mist">￥{(item.amount_cents / 100).toFixed(2)}</div>
                        </button>
                      ))}
                    </div> : paymentEnabled ? (
                      <div className="rounded-xl2 border border-line bg-base2/50 p-8 text-center text-sm text-fog">
                        暂无可购买套餐，请联系管理员配置。
                      </div>
                    ) : (
                      <div className="rounded-xl2 border border-line bg-base2/50 p-8 text-center text-sm text-fog">充值入口已关闭。</div>
                    )}

                    <div className="mt-5 flex flex-wrap items-center justify-between gap-3 border-t border-line pt-4">
                      <div className="text-sm text-mist">
                        当前选择 <b className="text-snow">{selectedPackage?.credits || 0}</b> 积分
                        <span className="text-fog"> · {selectedProviderLabel}扫码</span>
                      </div>
                      <button
                        type="button"
                        onClick={createOrder}
                        disabled={loading || !paymentEnabled || !selectedPackage || !providers.includes(provider)}
                        className="btn-primary"
                      >
                        {loading ? "处理中" : "生成支付二维码"}
                      </button>
                    </div>
                  </>
                )}
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
                          <img src={qrImage} alt="支付二维码" className="aspect-square w-full rounded-lg object-contain" />
                        ) : (
                          <div className="flex aspect-square w-full items-center justify-center rounded-lg bg-white text-sm text-slate-500">二维码生成中</div>
                        )}
                      </div>
                    ) : (
                      <div className="flex aspect-square items-center justify-center rounded-xl2 border border-line bg-base2/50 p-6 text-center text-sm text-fog">
                        {activeOrder.status === "paid" ? "支付已完成，积分已入账。" : orderExpired ? "二维码已过期，请重新生成。" : "该订单二维码已失效。"}
                      </div>
                    )}
                    <div className="mt-3 flex items-center justify-between text-sm">
                      <span className={`badge ${paymentStatusStyle(orderExpired ? "closed" : activeOrder.status)}`}>
                        {orderExpired ? "已过期" : paymentStatusText(activeOrder.status)}
                      </span>
                      <span className="text-fog">￥{(activeOrder.amount_cents / 100).toFixed(2)}</span>
                    </div>
                    <div className="mt-2 text-xs text-mist">
                      {PROVIDERS.find((item) => item[0] === activeOrder.provider)?.[1] || activeOrder.provider}
                      {" · "}{activeOrder.credits} 积分
                    </div>
                    <p className="mt-2 break-all text-xs text-fog">订单号 {activeOrder.order_no}</p>
                    {canMockPay && <button type="button" onClick={mockPay} disabled={loading} className="btn-secondary mt-3 w-full">本地模拟支付成功</button>}
                    {activeOrder.status === "pending" && <button type="button" onClick={refreshActiveOrder} disabled={loading} className="btn-secondary mt-2 w-full">刷新订单状态</button>}
                    {activeOrder.status === "paid" && <div className="mt-3 rounded-xl border border-ok/30 bg-ok/10 px-3 py-2 text-sm text-ok">已入账 {activeOrder.credits} 积分</div>}
                    {orderExpired && <p className="mt-2 rounded-xl border border-warn/30 bg-warn/10 px-3 py-2 text-xs text-warn">本地已到订单有效期，可刷新确认状态或重新生成二维码。</p>}
                    {orderClosed && activeOrder.status !== "paid" && (
                      <button type="button" onClick={createOrder} disabled={loading || !paymentEnabled || !selectedPackage || !providers.includes(provider)} className="btn-primary mt-3 w-full">
                        {loading ? "处理中" : "重新生成二维码"}
                      </button>
                    )}
                  </>
                )}
              </div>
            </section>
          )}

          {activeTab === "orders" && (
            <PaymentOrdersPanel
              orders={orders}
              loading={ordersLoading}
              actionLoading={loading}
              error={ordersError}
              hasMore={orders.length >= orderLimit && orderLimit < 100}
              onRefresh={() => refreshOrders(orderLimit, { visible: true }).catch((error) => notify.error(error.message || "订单列表刷新失败"))}
              onLoadMore={() => loadMoreOrders().catch((error) => notify.error(error.message || "订单列表加载失败"))}
              onOpen={viewOrder}
            />
          )}

          {activeTab === "billing" && <CreditLedger refreshKey={billingRefreshKey} />}
        </div>
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
