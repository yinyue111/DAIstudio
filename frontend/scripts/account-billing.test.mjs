import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const read = (path) => readFileSync(join(root, path), "utf8");

const page = read("app/recharge/page.jsx");
const tabs = read("app/recharge/AccountTabs.jsx");
const ledger = read("app/recharge/CreditLedger.jsx");
const rows = read("app/recharge/CreditLedgerRows.jsx");
const orders = read("app/recharge/PaymentOrdersPanel.jsx");
const api = read("lib/api.js");
const types = read("lib/api.d.ts");

assert.match(page, />积分账户</, "the recharge route should be presented as the credit account");
assert.match(page, />可用积分</, "the account summary should show available credits");
assert.match(page, />冻结积分</, "the account summary should show frozen credits");
assert.match(tabs, /"积分套餐"/, "the account should expose a packages tab");
assert.match(tabs, /"充值订单"/, "the account should expose an orders tab");
assert.match(tabs, /"积分账单"/, "the account should expose a billing tab");
assert.match(tabs, /role="tablist"/, "account navigation should use accessible tab semantics");
assert.match(tabs, /ArrowRight/, "account tabs should support arrow-key navigation");
assert.match(tabs, /grid-cols-3/, "account tabs should fit all three labels at mobile widths");
assert.match(tabs, /min-w-0/, "account tab buttons should be allowed to shrink without clipping the last tab");

assert.match(api, /\/api\/me\/credit-transactions/, "the API client should expose raw credit transactions");
assert.match(api, /\/api\/me\/billing\/entries/, "the API client should expose aggregated bills");
assert.match(ledger, /"业务账单"/, "billing should default to grouped business entries");
assert.match(ledger, /"原始流水"/, "billing should allow exact ledger inspection");
assert.match(ledger, /\["workflow", "工具工作流"\]/, "billing should expose workflow charges");
assert.match(types, /"workflow" \| "prompt"/, "billing kinds should type workflow entries");
assert.match(ledger, /nextCursor/, "billing should support cursor-based loading");
assert.match(ledger, /function resetView\(\)[\s\S]*requestSeq\.current \+= 1;[\s\S]*setItems\(\[\]\)/, "switching ledger views should invalidate stale requests and rows");
assert.match(ledger, /resetView\(\);\s*setMode\(nextMode\)/, "mode changes should clear rows before rendering the next row shape");
assert.match(rows, /label="冻结"/, "grouped bills should show the frozen stage");
assert.match(rows, /label="结算"/, "grouped bills should show the settled stage");
assert.match(rows, /label="退回"/, "grouped bills should show the refund stage");
assert.match(rows, /label="净消费"/, "grouped bills should show net consumption");
assert.match(rows, /账务与业务明细/, "grouped bills should expose expandable accounting details");
assert.match(rows, /label="业务实体"/, "billing details should trace the business entity");
assert.match(rows, /label="计费快照"/, "billing details should expose the server billing snapshot without quote UI wording");
assert.match(rows, /label="价格版本"/, "billing details should expose the price version");
assert.match(rows, /label="结算差额退回"/, "billing details should separate settlement returns");
assert.match(rows, /label="失败冻结退回"/, "billing details should separate reservation refunds");
assert.match(rows, /label="同步扣费退回"/, "billing details should separate consumed-charge refunds");
assert.match(rows, /balance_conserved && item\.reservation_conserved/, "billing details should expose conservation checks");
assert.match(rows, /等待人工核对/, "billing details should expose pending reconciliation state");
assert.match(rows, /人工核对后退款/, "billing details should expose audited refund resolution");
assert.match(rows, /\/history\?task=/, "task-backed bills should link to task details");
assert.match(types, /quote_id\?: number \| null/, "billing types should include the quote reference");
assert.match(types, /reservation_refunded_credits: number/, "billing types should include refund breakdowns");
assert.match(types, /balance_conserved: boolean/, "billing types should include conservation status");

assert.match(orders, /Ledger|OrderSkeleton|订单加载中/, "orders should expose a loading state");
assert.match(orders, /还没有充值订单/, "orders should expose an empty state");
assert.match(orders, /role="alert"/, "orders should expose an error state");
assert.match(orders, /加载更多/, "orders should support progressive loading");

console.log("account billing UI test passed");
