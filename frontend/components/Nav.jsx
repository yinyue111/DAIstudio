"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api, clearToken, loginPath } from "../lib/api";
import { reportBackgroundError } from "../lib/errorHandling";
import BrandLogo from "./BrandLogo";

const LINKS = [
  ["studio", "创作", "/"],
  ["prompts", "提示词库", "/prompts"],
  ["profile", "我的作品", "/profile"],
  ["recharge", "充值", "/recharge"],
  ["history", "历史", "/history"],
];
const DESKTOP_LINKS = [...LINKS, ["admin", "管理后台", "/admin"]];
const LINK_WIDTH = {
  studio: "w-16",
  prompts: "w-24",
  profile: "w-24",
  recharge: "w-16",
  history: "w-16",
  admin: "w-24",
};

export default function Nav({ me, active }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [loginHref, setLoginHref] = useState("/login");

  useEffect(() => {
    setLoginHref(loginPath());
  }, []);

  async function logout() {
    try { await api.logout(); } catch (e) { reportBackgroundError(e, "logout request"); }
    clearToken();
    router.push("/login");
  }

  const mobileLinks = me?.is_admin
    ? [...LINKS, ["admin", "管理后台", "/admin"]]
    : LINKS;

  return (
    <header className="sticky top-0 z-40 border-b border-line bg-base/70 backdrop-blur-xl">
      <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-3 sm:px-6">
        <Link href="/" className="flex shrink-0 items-center gap-2.5">
          <BrandLogo className="h-8 w-8" />
          <span className="font-display text-[16px] font-bold tracking-tight">
            造梦<span className="text-fog font-medium"> Studio</span>
          </span>
        </Link>

        <nav className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="btn-ghost btn-sm xl:hidden"
            aria-label={open ? "关闭导航" : "打开导航"}
            aria-expanded={open}
          >
            <span className="block h-4 w-4">
              <span className={`mb-1 block h-0.5 rounded bg-current transition ${open ? "translate-y-1.5 rotate-45" : ""}`} />
              <span className={`mb-1 block h-0.5 rounded bg-current transition ${open ? "opacity-0" : ""}`} />
              <span className={`block h-0.5 rounded bg-current transition ${open ? "-translate-y-1.5 -rotate-45" : ""}`} />
            </span>
          </button>
          <div className="mr-1 hidden h-11 items-center gap-1 rounded-full border border-line bg-white/5 p-1 xl:flex">
            {DESKTOP_LINKS.map(([key, label, href]) => {
              const visible = key !== "admin" || me?.is_admin;
              return (
                <Link
                  key={key}
                  href={href}
                  className={`${LINK_WIDTH[key] || "w-20"} rounded-full px-3 py-1.5 text-center text-sm font-display font-medium transition-colors ${
                    active === key
                      ? "bg-brand text-white shadow-glow-sm"
                      : "text-mist hover:bg-white/5 hover:text-snow"
                  } ${visible ? "" : "invisible pointer-events-none"}`}
                  aria-current={active === key ? "page" : undefined}
                  aria-hidden={visible ? undefined : true}
                  tabIndex={visible ? undefined : -1}
                >
                  {label}
                </Link>
              );
            })}
          </div>

          <div className="hidden min-w-[274px] items-center justify-end gap-1 xl:flex">
            {me ? (
              <>
                <span
                  className="inline-flex h-9 w-[176px] items-center justify-center gap-1.5 rounded-full border border-iris/30 bg-iris/10 px-3 text-xs"
                  aria-label={`可用积分 ${me.balance_credits}${me.frozen_credits ? `，冻结积分 ${me.frozen_credits}` : ""}`}
                  title={`可用积分 ${me.balance_credits}${me.frozen_credits ? ` · 冻结积分 ${me.frozen_credits}` : ""}`}
                >
                  <svg viewBox="0 0 24 24" className="h-3.5 w-3.5 shrink-0 text-iris-400" fill="currentColor" aria-hidden>
                    <path d="M12 2l2.4 6.3L21 9l-5 4.3L17.5 21 12 17.2 6.5 21 8 13.3 3 9l6.6-.7L12 2z" />
                  </svg>
                  <span className="text-fog">可用</span>
                  <b className="max-w-[52px] truncate text-snow">{me.balance_credits}</b>
                  {me.frozen_credits ? <span className="max-w-[58px] truncate text-fog">冻结 {me.frozen_credits}</span> : null}
                </span>
                <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-brand text-sm font-display font-semibold text-white shadow-glow-sm">
                  {(me.nickname || me.phone || "U").slice(-2)}
                </div>
                <button onClick={logout} className="btn-ghost btn-sm ml-0.5 w-14">退出</button>
              </>
            ) : (
              <>
                <span className="h-9 w-[176px] rounded-full border border-line bg-white/5 opacity-60" aria-hidden />
                <span className="h-9 w-9 rounded-full bg-white/5 opacity-60" aria-hidden />
                <Link href={loginHref} className="btn-ghost btn-sm ml-0.5 w-14">登录</Link>
              </>
            )}
          </div>

          <div className="xl:hidden">
            {me && (
              <div className="flex h-9 w-9 items-center justify-center rounded-full bg-brand text-sm font-display font-semibold text-white shadow-glow-sm">
                {(me.nickname || me.phone || "U").slice(-2)}
              </div>
            )}
            {!me && (
              <Link href={loginHref} className="btn-ghost btn-sm ml-0.5">登录</Link>
            )}
          </div>
          {me && (
            <div className="xl:hidden">
              <button onClick={logout} className="btn-ghost btn-sm ml-0.5">退出</button>
            </div>
          )}
        </nav>
      </div>
      {open && (
        <div className="border-t border-line bg-base/95 px-4 py-3 shadow-2xl backdrop-blur-xl xl:hidden">
          <div className="grid grid-cols-2 gap-2">
            {mobileLinks.map(([key, label, href]) => (
              <Link
                key={key}
                href={href}
                className={`rounded-xl border px-3 py-2 text-center text-sm font-display font-medium ${
                  active === key
                    ? "border-brand bg-brand text-white"
                    : "border-line bg-white/5 text-mist"
                }`}
              >
                {label}
              </Link>
            ))}
          </div>
          {me && (
            <div className="mt-2 rounded-xl border border-iris/30 bg-iris/10 px-3 py-2 text-xs text-fog">
              积分 <b className="text-snow">{me.balance_credits}</b>
              {me.frozen_credits ? <span> · 冻结 {me.frozen_credits}</span> : null}
            </div>
          )}
          <div className="mt-2">
            {me ? (
              <button onClick={logout} className="btn-ghost btn-sm w-full">退出</button>
            ) : (
              <Link href={loginHref} className="btn-ghost btn-sm block w-full text-center">登录</Link>
            )}
          </div>
        </div>
      )}
    </header>
  );
}
