"use client";

import { useRef } from "react";
import { Coins, ReceiptText, ScrollText } from "lucide-react";

export const ACCOUNT_TABS = [
  { id: "packages", label: "积分套餐", icon: Coins },
  { id: "orders", label: "充值订单", icon: ReceiptText },
  { id: "billing", label: "积分账单", icon: ScrollText },
];

export default function AccountTabs({ activeTab, onChange, tabs = ACCOUNT_TABS }) {
  const refs = useRef([]);

  function moveFocus(event, index) {
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
    else if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    else return;
    event.preventDefault();
    onChange(tabs[next].id);
    refs.current[next]?.focus();
  }

  return (
    <div
      role="tablist"
      aria-label="积分账户"
      className={`grid min-h-12 gap-1 rounded-xl2 border border-line bg-white/5 p-1 ${tabs.length === 1 ? "grid-cols-1" : "grid-cols-3"}`}
    >
      {tabs.map((tab, index) => {
        const Icon = tab.icon;
        const selected = activeTab === tab.id;
        return (
          <button
            key={tab.id}
            ref={(element) => { refs.current[index] = element; }}
            id={`account-tab-${tab.id}`}
            type="button"
            role="tab"
            aria-selected={selected}
            aria-controls={`account-panel-${tab.id}`}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(tab.id)}
            onKeyDown={(event) => moveFocus(event, index)}
            className={`flex h-10 min-w-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-xl px-2 text-sm font-display font-medium transition-colors ${
              selected ? "bg-brand text-white shadow-glow-sm" : "text-mist hover:bg-white/5 hover:text-snow"
            }`}
          >
            <Icon size={16} aria-hidden="true" />
            {tab.label}
          </button>
        );
      })}
    </div>
  );
}
