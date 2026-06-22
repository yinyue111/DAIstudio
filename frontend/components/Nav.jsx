"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api, clearToken } from "../lib/api";

function Logo({ className = "" }) {
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden>
      <defs>
        <linearGradient id="navlogo" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#7b61ff" />
          <stop offset="50%" stopColor="#b65cff" />
          <stop offset="100%" stopColor="#ff5fa2" />
        </linearGradient>
      </defs>
      <rect x="2" y="2" width="28" height="28" rx="9" fill="url(#navlogo)" />
      <path
        d="M16 8l1.9 4.6 4.6 1.9-4.6 1.9L16 21l-1.9-4.6L9.5 14.5l4.6-1.9L16 8z"
        fill="#fff"
        fillOpacity="0.95"
      />
    </svg>
  );
}

const LINKS = [
  ["studio", "创作", "/"],
  ["prompts", "提示词库", "/prompts"],
  ["profile", "我的作品", "/profile"],
  ["recharge", "充值", "/recharge"],
  ["history", "历史", "/history"],
];

export default function Nav({ me, active }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);

  async function logout() {
    try { await api.logout(); } catch (e) {}
    clearToken();
    router.push("/login");
  }

  const allLinks = me?.is_admin
    ? [...LINKS, ["admin", "管理后台", "/admin"]]
    : LINKS;

  return (
    <header className="sticky top-0 z-40 border-b border-line bg-base/70 backdrop-blur-xl">
      <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-3 sm:px-6">
        <a href="/" className="flex items-center gap-2.5">
          <Logo className="h-8 w-8 drop-shadow-[0_4px_14px_rgba(123,97,255,0.5)]" />
          <span className="font-display text-[16px] font-bold tracking-tight">
            造梦<span className="text-fog font-medium"> Studio</span>
          </span>
        </a>

        <nav className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="btn-ghost btn-sm sm:hidden"
            aria-label={open ? "关闭导航" : "打开导航"}
            aria-expanded={open}
          >
            <span className="block h-4 w-4">
              <span className={`mb-1 block h-0.5 rounded bg-current transition ${open ? "translate-y-1.5 rotate-45" : ""}`} />
              <span className={`mb-1 block h-0.5 rounded bg-current transition ${open ? "opacity-0" : ""}`} />
              <span className={`block h-0.5 rounded bg-current transition ${open ? "-translate-y-1.5 -rotate-45" : ""}`} />
            </span>
          </button>
          <div className="mr-1 hidden items-center gap-1 rounded-full border border-line bg-white/5 p-1 sm:flex">
            {allLinks.map(([key, label, href]) => (
              <a
                key={key}
                href={href}
                className={`rounded-full px-4 py-1.5 text-sm font-display font-medium transition-all ${
                  active === key
                    ? "bg-brand text-white shadow-glow-sm"
                    : "text-mist hover:bg-white/5 hover:text-snow"
                }`}
              >
                {label}
              </a>
            ))}
          </div>

          {me && (
            <span
              className="hidden items-center gap-1.5 rounded-full border border-iris/30 bg-iris/10 px-3 py-1.5 text-xs sm:inline-flex"
              aria-label={`可用积分 ${me.balance_credits}${me.frozen_credits ? `，冻结积分 ${me.frozen_credits}` : ""}`}
              title={`可用积分 ${me.balance_credits}${me.frozen_credits ? ` · 冻结积分 ${me.frozen_credits}` : ""}`}
            >
              <svg viewBox="0 0 24 24" className="h-3.5 w-3.5 text-iris-400" fill="currentColor" aria-hidden>
                <path d="M12 2l2.4 6.3L21 9l-5 4.3L17.5 21 12 17.2 6.5 21 8 13.3 3 9l6.6-.7L12 2z" />
              </svg>
              <span className="text-fog">可用</span>
              <b className="text-snow">{me.balance_credits}</b>
              {me.frozen_credits ? <span className="text-fog">冻结 {me.frozen_credits}</span> : null}
            </span>
          )}

          {me && (
            <div className="flex h-9 w-9 items-center justify-center rounded-full bg-brand text-sm font-display font-semibold text-white shadow-glow-sm">
              {(me.nickname || me.phone || "U").slice(-2)}
            </div>
          )}
          <button onClick={logout} className="btn-ghost btn-sm ml-0.5">退出</button>
        </nav>
      </div>
      {open && (
        <div className="border-t border-line bg-base/95 px-4 py-3 shadow-2xl backdrop-blur-xl sm:hidden">
          <div className="grid grid-cols-2 gap-2">
            {allLinks.map(([key, label, href]) => (
              <a
                key={key}
                href={href}
                className={`rounded-xl border px-3 py-2 text-center text-sm font-display font-medium ${
                  active === key
                    ? "border-brand bg-brand text-white"
                    : "border-line bg-white/5 text-mist"
                }`}
              >
                {label}
              </a>
            ))}
          </div>
          {me && (
            <div className="mt-2 rounded-xl border border-iris/30 bg-iris/10 px-3 py-2 text-xs text-fog">
              积分 <b className="text-snow">{me.balance_credits}</b>
              {me.frozen_credits ? <span> · 冻结 {me.frozen_credits}</span> : null}
            </div>
          )}
        </div>
      )}
    </header>
  );
}
