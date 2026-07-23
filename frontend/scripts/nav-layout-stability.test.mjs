import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  APP_NAV_ITEMS,
  activeNavKeyForPath,
  canAccessNavItem,
  isPublicAppPath,
  normalizeAppNavItems,
} from "../components/AppShellContract.js";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const nav = readFileSync(join(root, "components/Nav.jsx"), "utf8");
const shell = readFileSync(join(root, "components/AppShell.jsx"), "utf8");
const layout = readFileSync(join(root, "app/layout.jsx"), "utf8");
const api = readFileSync(join(root, "lib/api.js"), "utf8");
const adminSettings = readFileSync(join(root, "app/admin/components/payments-settings.jsx"), "utf8");

assert.equal(isPublicAppPath("/login"), true);
assert.equal(isPublicAppPath("/recipes/shared/public-slug"), true);
assert.equal(isPublicAppPath("/"), false);
assert.equal(isPublicAppPath("/catalog"), false);
assert.equal(activeNavKeyForPath("/"), "studio");
assert.equal(activeNavKeyForPath("/projects/42"), "projects");
assert.equal(canAccessNavItem(APP_NAV_ITEMS.find((item) => item.key === "admin"), null), false);
assert.equal(canAccessNavItem(APP_NAV_ITEMS.find((item) => item.key === "admin"), { is_admin: true }), true);
const remoteNavigation = normalizeAppNavItems({
  items: [
    { key: "studio", label: "创作台", href: "/", width: "w-20", enabled: true },
    { key: "catalog", label: "能力市场", href: "/catalog", enabled: false, disabled_reason: "维护中" },
    { key: "admin", label: "管理", href: "/admin", permission: "admin", reserve_desktop: true },
    { key: "unsafe", label: "外部", href: "https://example.com" },
  ],
});
assert.deepEqual(remoteNavigation.map((item) => item.key), ["studio", "catalog", "admin"]);
assert.equal(remoteNavigation[1].enabled, false);
assert.equal(remoteNavigation[1].disabledReason, "维护中");
assert.equal(remoteNavigation[2].reserveDesktop, true);
assert.equal(activeNavKeyForPath("/catalog/models", remoteNavigation), "catalog");
assert.equal(normalizeAppNavItems({ items: [{ key: "bad", label: "Bad", href: "/bad" }] }), APP_NAV_ITEMS);

assert.match(layout, /<AppShell>\{children\}<\/AppShell>/, "root layout should own the persistent shell");
assert.match(shell, /api\.navigation\(\)/, "app shell should load the server navigation catalog");
assert.match(shell, /items=\{navItems\}/, "app shell should pass normalized server navigation to Nav");
assert.match(shell, /\}, \[publicRoute\]\);/, "session probe must not restart between protected pages");
assert.match(shell, /window\.addEventListener\(APP_SESSION_EVENT, syncSession\)/, "shell should receive balance and profile refreshes without remounting");
assert.match(api, /publishAppSession\(user\)/, "successful session probes should publish the latest user snapshot");
assert.match(api, /publishAppSession\(null\)/, "logout should clear the persistent session snapshot");
assert.match(shell, /if \(publicRoute\) return children;/, "public pages must not receive the protected shell");
assert.match(nav, /shell\?\.managed && !shellOwner/, "legacy page-level navs should be suppressed inside AppShell");
assert.match(nav, /navigationItems\.map/, "desktop navigation should use the resolved navigation contract");
assert.match(nav, /navigationItems\.filter/, "mobile navigation should use the resolved navigation contract");
assert.match(nav, /item\.enabled === false/, "disabled navigation entries should remain non-interactive");
assert.match(nav, /item\.reserveDesktop \? "invisible pointer-events-none"/, "admin slot should remain stable while permissions load");
assert.match(nav, /useUnifiedTaskCenter/, "the persistent navigation should own unified task state");
assert.match(nav, /GlobalTaskCenter/, "the persistent navigation should render the task drawer");
assert.match(nav, /taskCenter\.activeCount/, "the navigation should expose active task count");
assert.match(nav, /import Link from "next\/link"/);
assert.doesNotMatch(nav, /<a\s+href=/, "internal shell navigation should not force page reloads");
assert.match(adminSettings, /navigation_states: s\.navigation_states \|\| \{\}/, "admin settings should persist navigation states");
assert.match(adminSettings, /<option value="enabled">启用<\/option>/, "navigation entries should support enabled state");
assert.match(adminSettings, /<option value="disabled">暂停<\/option>/, "navigation entries should support disabled state");
assert.match(adminSettings, /<option value="hidden">隐藏<\/option>/, "navigation entries should support hidden state");

console.log("persistent app shell and navigation contract tests passed");
