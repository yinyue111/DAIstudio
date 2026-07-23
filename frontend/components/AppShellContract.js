export const APP_NAV_ITEMS = [
  { key: "studio", label: "创作", href: "/", width: "w-16" },
  { key: "catalog", label: "能力", href: "/catalog", width: "w-16" },
  { key: "prompts", label: "灵感配方", href: "/prompts", width: "w-24" },
  { key: "projects", label: "项目", href: "/projects", width: "w-16" },
  { key: "profile", label: "素材", href: "/profile", width: "w-16" },
  { key: "recharge", label: "充值", href: "/recharge", width: "w-16" },
  { key: "history", label: "历史", href: "/history", width: "w-16" },
  {
    key: "admin",
    label: "管理后台",
    href: "/admin",
    width: "w-24",
    permission: "admin",
    reserveDesktop: true,
  },
];

const NAV_KEY = /^[a-z][a-z0-9_-]{0,31}$/;
const NAV_WIDTHS = new Set(["w-16", "w-20", "w-24"]);
const NAV_PERMISSIONS = new Set(["admin"]);

export function normalizeAppNavItems(payload) {
  const rows = Array.isArray(payload) ? payload : payload?.items;
  if (!Array.isArray(rows)) return APP_NAV_ITEMS;
  const seen = new Set();
  const normalized = [];
  for (const row of rows) {
    const key = String(row?.key || "").trim();
    const label = String(row?.label || "").trim();
    const href = String(row?.href || "").trim();
    const permission = row?.permission ? String(row.permission) : undefined;
    if (
      !NAV_KEY.test(key)
      || seen.has(key)
      || !label
      || label.length > 12
      || !href.startsWith("/")
      || href.startsWith("//")
      || /[\s\\]/.test(href)
      || (permission && !NAV_PERMISSIONS.has(permission))
    ) continue;
    seen.add(key);
    normalized.push({
      key,
      label,
      href,
      width: NAV_WIDTHS.has(row.width) ? row.width : "w-20",
      ...(permission ? { permission } : {}),
      reserveDesktop: Boolean(row.reserve_desktop ?? row.reserveDesktop),
      enabled: row.enabled !== false,
      visible: row.visible !== false,
      disabledReason: String(row.disabled_reason || row.disabledReason || "").slice(0, 160),
    });
  }
  return normalized.some((item) => item.key === "studio" && item.href === "/")
    ? normalized
    : APP_NAV_ITEMS;
}

const PUBLIC_APP_PATHS = new Set(["/login", "/recipes/shared"]);

export function isPublicAppPath(pathname) {
  const path = String(pathname || "/").replace(/\/+$/, "") || "/";
  return PUBLIC_APP_PATHS.has(path) || path.startsWith("/recipes/shared/");
}

export function activeNavKeyForPath(pathname, items = APP_NAV_ITEMS) {
  const path = String(pathname || "/");
  if (path === "/") return "studio";
  return items.find((item) => item.href !== "/" && (
    path === item.href || path.startsWith(`${item.href}/`)
  ))?.key || "";
}

export function canAccessNavItem(item, user) {
  if (!item || item.visible === false) return false;
  if (!item?.permission) return true;
  if (item.permission === "admin") return Boolean(user?.is_admin);
  return false;
}
