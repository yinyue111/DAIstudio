"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ListChecks, Menu, X } from "lucide-react";
import { api, clearToken, loginPath } from "../lib/api";
import { reportBackgroundError } from "../lib/errorHandling";
import BrandLogo from "./BrandLogo";
import GlobalTaskCenter from "./GlobalTaskCenter";
import useUnifiedTaskCenter from "../hooks/useUnifiedTaskCenter";
import { APP_NAV_ITEMS, canAccessNavItem } from "./AppShellContract";
import { useAppShellContext } from "./AppShellContext";

export default function Nav({ me, active, items = APP_NAV_ITEMS, shellOwner = false }) {
  const shell = useAppShellContext();
  if (shell?.managed && !shellOwner) return null;
  return <NavContent me={me} active={active} items={items} shell={shellOwner ? shell : null} />;
}

function NavContent({ me, active, items, shell }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [taskCenterOpen, setTaskCenterOpen] = useState(false);
  const [loginHref, setLoginHref] = useState("/login");
  const taskCenter = useUnifiedTaskCenter({ enabled: Boolean(me), limit: 8 });

  useEffect(() => {
    setLoginHref(loginPath());
  }, []);

  async function logout() {
    try { await api.logout(); } catch (e) { reportBackgroundError(e, "logout request"); }
    clearToken();
    shell?.clearSession?.();
    router.push("/login");
  }

  const navigationItems = Array.isArray(items) && items.length ? items : APP_NAV_ITEMS;
  const mobileLinks = navigationItems.filter((item) => canAccessNavItem(item, me));

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
            {open ? <X size={18} aria-hidden="true" /> : <Menu size={18} aria-hidden="true" />}
          </button>
          <div className="mr-1 hidden h-11 items-center gap-1 rounded-full border border-line bg-white/5 p-1 xl:flex">
            {navigationItems.map((item) => {
              const visible = canAccessNavItem(item, me);
              const disabled = item.enabled === false;
              const itemClassName = `${item.width || "w-20"} rounded-full px-3 py-1.5 text-center text-sm font-display font-medium transition-colors ${
                disabled
                  ? "cursor-not-allowed text-fog opacity-45"
                  : active === item.key
                    ? "bg-brand text-white shadow-glow-sm"
                    : "text-mist hover:bg-white/5 hover:text-snow"
              } ${visible ? "" : item.reserveDesktop ? "invisible pointer-events-none" : "hidden"}`;
              if (disabled) {
                return (
                  <span
                    key={item.key}
                    className={itemClassName}
                    aria-disabled="true"
                    title={item.disabledReason || "当前入口暂不可用"}
                  >
                    {item.label}
                  </span>
                );
              }
              return (
                <Link
                  key={item.key}
                  href={item.href}
                  className={itemClassName}
                  aria-current={active === item.key ? "page" : undefined}
                  aria-hidden={visible ? undefined : true}
                  tabIndex={visible ? undefined : -1}
                >
                  {item.label}
                </Link>
              );
            })}
          </div>

          <div className="hidden min-w-[274px] items-center justify-end gap-1 xl:flex">
            {me ? (
              <>
                <button
                  type="button"
                  onClick={() => setTaskCenterOpen(true)}
                  className="relative flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-mist transition hover:bg-white/5 hover:text-snow"
                  aria-label={`打开任务中心${taskCenter.activeCount ? `，${taskCenter.activeCount} 个进行中任务` : ""}`}
                  title="任务中心"
                >
                  <ListChecks size={17} aria-hidden="true" />
                  {taskCenter.activeCount > 0 && (
                    <span className="absolute -right-1 -top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-brand px-1 text-[10px] font-semibold leading-none text-white">
                      {taskCenter.activeCount > 99 ? "99+" : taskCenter.activeCount}
                    </span>
                  )}
                </button>
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
            {me && <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={() => setTaskCenterOpen(true)}
                className="relative flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-mist transition hover:bg-white/5 hover:text-snow"
                aria-label={`打开任务中心${taskCenter.activeCount ? `，${taskCenter.activeCount} 个进行中任务` : ""}`}
                title="任务中心"
              >
                <ListChecks size={17} aria-hidden="true" />
                {taskCenter.activeCount > 0 && <span className="absolute -right-1 -top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-brand px-1 text-[10px] font-semibold leading-none text-white">{taskCenter.activeCount > 99 ? "99+" : taskCenter.activeCount}</span>}
              </button>
              <div className="flex h-9 w-9 items-center justify-center rounded-full bg-brand text-sm font-display font-semibold text-white shadow-glow-sm">
                {(me.nickname || me.phone || "U").slice(-2)}
              </div>
            </div>}
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
            {mobileLinks.map((item) => (
              item.enabled === false ? (
                <span key={item.key} aria-disabled="true" title={item.disabledReason || "当前入口暂不可用"} className="rounded-xl border border-line bg-white/[0.03] px-3 py-2 text-center text-sm font-display font-medium text-fog opacity-45">
                  {item.label}
                </span>
              ) : (
                <Link
                  key={item.key}
                  href={item.href}
                  className={`rounded-xl border px-3 py-2 text-center text-sm font-display font-medium ${
                    active === item.key
                      ? "border-brand bg-brand text-white"
                      : "border-line bg-white/5 text-mist"
                  }`}
                >
                  {item.label}
                </Link>
              )
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
      <GlobalTaskCenter center={taskCenter} open={taskCenterOpen} onClose={() => setTaskCenterOpen(false)} />
    </header>
  );
}
