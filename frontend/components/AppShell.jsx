"use client";

import { useEffect, useMemo, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { api, APP_SESSION_EVENT } from "../lib/api";
import { reportBackgroundError } from "../lib/errorHandling";
import Nav from "./Nav";
import {
  APP_NAV_ITEMS,
  activeNavKeyForPath,
  isPublicAppPath,
  normalizeAppNavItems,
} from "./AppShellContract";
import { AppShellProvider } from "./AppShellContext";

export default function AppShell({ children }) {
  const pathname = usePathname() || "/";
  const router = useRouter();
  const publicRoute = isPublicAppPath(pathname);
  const [me, setMe] = useState(null);
  const [sessionReady, setSessionReady] = useState(false);
  const [navItems, setNavItems] = useState(APP_NAV_ITEMS);

  useEffect(() => {
    const syncSession = (event) => {
      setMe(event.detail || null);
      if (!event.detail) setNavItems(APP_NAV_ITEMS);
      setSessionReady(true);
    };
    window.addEventListener(APP_SESSION_EVENT, syncSession);
    return () => window.removeEventListener(APP_SESSION_EVENT, syncSession);
  }, []);

  useEffect(() => {
    if (publicRoute) return undefined;
    let active = true;
    setSessionReady(false);
    (async () => {
      try {
        const user = await api.me();
        if (!active) return;
        setMe(user);
        try {
          const navigation = await api.navigation();
          if (active) {
            const resolvedItems = normalizeAppNavItems(navigation);
            setNavItems(resolvedItems);
          }
        } catch (error) {
          if (active && error?.status !== 401) {
            reportBackgroundError(error, "app shell navigation catalog");
          }
        }
      } catch (error) {
        if (active && error?.status !== 401) {
          reportBackgroundError(error, "app shell session probe");
        }
      } finally {
        if (active) setSessionReady(true);
      }
    })();
    return () => { active = false; };
  }, [publicRoute]);

  useEffect(() => {
    if (publicRoute) return;
    const requestedKey = activeNavKeyForPath(pathname, APP_NAV_ITEMS);
    if (
      requestedKey
      && requestedKey !== "studio"
      && !navItems.some((item) => item.key === requestedKey)
    ) {
      router.replace("/");
    }
  }, [navItems, pathname, publicRoute, router]);

  const context = useMemo(() => ({
    managed: !publicRoute,
    me,
    sessionReady,
    clearSession: () => setMe(null),
  }), [me, publicRoute, sessionReady]);

  if (publicRoute) return children;

  return (
    <AppShellProvider value={context}>
      <Nav me={me} active={activeNavKeyForPath(pathname, navItems)} items={navItems} shellOwner />
      {children}
    </AppShellProvider>
  );
}
