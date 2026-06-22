"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "../../lib/api";
import Nav from "../../components/Nav";
import { AssetReports, Models, ReviewTasks } from "./components/model-review-moderation";
import { Payments, Settings } from "./components/payments-settings";
import { Audit, Report } from "./components/reports-audit";
import { Users, Whitelist } from "./components/whitelist-users";

const TABS = [
  ["whitelist", "白名单"],
  ["users", "用户 / 额度"],
  ["models", "模型配置"],
  ["review", "待对账"],
  ["moderation", "举报处理"],
  ["report", "用量报表"],
  ["payments", "支付设置"],
  ["settings", "平台设置"],
  ["audit", "审计日志"],
];

const TAB_COMPONENTS = {
  whitelist: Whitelist,
  users: Users,
  models: Models,
  review: ReviewTasks,
  moderation: AssetReports,
  report: Report,
  payments: Payments,
  settings: Settings,
  audit: Audit,
};

export default function AdminPage() {
  const router = useRouter();
  const [tab, setTab] = useState("whitelist");
  const [me, setMe] = useState(null);
  const ActiveTab = TAB_COMPONENTS[tab] || Whitelist;

  useEffect(() => {
    api.me().then((u) => {
      if (!u.is_admin) router.push("/");
      else setMe(u);
    }).catch(() => router.push("/login"));
  }, [router]);

  if (!me) return <div className="p-10 text-sm text-mist">加载中…</div>;

  return (
    <div className="min-h-screen">
      <Nav me={me} active="admin" />
      <main className="mx-auto max-w-5xl px-4 py-8 sm:px-6">
        <h1 className="mb-1 text-2xl font-bold sm:text-3xl">
          管理<span className="text-grad">后台</span>
        </h1>
        <p className="mb-6 text-sm text-fog">白名单、用户额度、模型与平台设置、用量与审计一站式管控。</p>

        <div className="mb-6 inline-flex flex-wrap gap-1 rounded-full border border-line bg-white/5 p-1 backdrop-blur-xl">
          {TABS.map(([k, label]) => (
            <button
              key={k}
              onClick={() => setTab(k)}
              className={`rounded-full px-4 py-1.5 text-sm font-display font-medium transition-all ${
                tab === k ? "bg-brand text-white shadow-glow-sm" : "text-mist hover:bg-white/5 hover:text-snow"
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        <ActiveTab />
      </main>
    </div>
  );
}
