"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Nav from "../../components/Nav";
import PromptLibraryBrowser, { STUDIO_DRAFT_PROMPT_KEY } from "../../components/PromptLibraryBrowser";
import { api } from "../../lib/api";
import { redirectOnAuthError, reportBackgroundError } from "../../lib/errorHandling";

export default function PromptsPage() {
  const router = useRouter();
  const [me, setMe] = useState(null);
  const [msg, setMsg] = useState("");
  const [msgKind, setMsgKind] = useState("ok");
  const [activeTab, setActiveTab] = useState("system");
  const [myPrompts, setMyPrompts] = useState([]);
  const [historyQuery, setHistoryQuery] = useState("");
  const [historyFilter, setHistoryFilter] = useState("all");
  const [manualPrompt, setManualPrompt] = useState("");
  const [manualTitle, setManualTitle] = useState("");
  const historyReqRef = useRef(0);

  useEffect(() => {
    api.me().then((u) => {
      setMe(u);
      loadHistory();
    }).catch((e) => redirectOnAuthError(e, router, setMsg, "prompts session probe"));
  }, []);

  async function loadHistory(overrides = {}) {
    const req = ++historyReqRef.current;
    try {
      const filter = overrides.filter ?? historyFilter;
      const q = overrides.q ?? historyQuery;
      const rows = await api.promptHistory({
        favorite: filter === "favorite" ? true : null,
        source: filter === "reverse" || filter === "generate" ? filter : "",
        q,
        limit: 30,
      });
      if (req !== historyReqRef.current) return;
      setMyPrompts(rows);
    } catch (e) {
      if (req !== historyReqRef.current) return;
      setMsgKind("bad");
      setMsg(e.message);
    }
  }

  function usePrompt(item) {
    try {
      window.localStorage.setItem(STUDIO_DRAFT_PROMPT_KEY, item.prompt || "");
    } catch (e) {
      reportBackgroundError(e, "save prompt draft");
    }
    if (item.id) {
      api.updatePromptHistory(item.id, { increment_usage: true })
        .catch((e) => reportBackgroundError(e, "increment prompt usage"));
    }
    router.push("/");
  }

  async function copyPrompt(item) {
    try {
      await navigator.clipboard.writeText(item.prompt || "");
      setMsgKind("ok");
      setMsg("提示词已复制");
      window.setTimeout(() => setMsg(""), 1400);
    } catch (e) {
      setMsgKind("bad");
      setMsg("复制失败，请直接进入创作页套用");
    }
  }

  async function saveManualPrompt() {
    const text = manualPrompt.trim();
    if (!text) {
      setMsgKind("bad");
      return setMsg("请先填写提示词");
    }
    setMsg("");
    try {
      await api.createPromptHistory({
        title: manualTitle.trim() || undefined,
        prompt: text,
        category: "general",
        source: "manual",
      });
      setManualPrompt("");
      setManualTitle("");
      loadHistory();
      setMsgKind("ok");
      setMsg("已保存到我的提示词");
    } catch (e) {
      setMsgKind("bad");
      setMsg(e.message);
    }
  }

  async function toggleFavorite(item) {
    try {
      const updated = await api.favoritePromptHistory(item.id);
      setMyPrompts((rows) => {
        if (historyFilter === "favorite" && updated.favorite === false) {
          return rows.filter((row) => row.id !== updated.id);
        }
        return rows.map((row) => (row.id === updated.id ? updated : row));
      });
    } catch (e) {
      setMsgKind("bad");
      setMsg(e.message);
    }
  }

  async function deletePrompt(item) {
    if (!window.confirm("确认删除这条提示词历史？")) return;
    try {
      await api.deletePromptHistory(item.id);
      setMyPrompts((rows) => rows.filter((row) => row.id !== item.id));
    } catch (e) {
      setMsgKind("bad");
      setMsg(e.message);
    }
  }

  return (
    <div className="min-h-screen">
      <Nav me={me} active="prompts" />
      <main className="mx-auto max-w-7xl px-4 pb-24 pt-10 sm:px-6">
        <section className="mb-6 flex flex-wrap items-end justify-between gap-3 animate-fadeup">
          <div>
            <h1 className="text-3xl font-extrabold leading-tight">
              提示词<span className="text-grad">库</span>
            </h1>
            <p className="mt-1.5 text-sm text-mist">浏览本地参考案例，选中后可带回创作页继续生成。</p>
          </div>
          <a href="/" className="btn-secondary btn-sm">返回创作</a>
        </section>

        {msg && (
          <div className={`mb-4 rounded-xl border px-4 py-2.5 text-sm ${
            msgKind === "bad" ? "border-bad/30 bg-bad/10 text-bad" : "border-ok/30 bg-ok/10 text-ok"
          }`}>
            {msg}
          </div>
        )}

        <div className="mb-4 flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => setActiveTab("system")}
            className={`chip justify-center px-4 py-2 ${activeTab === "system" ? "chip-active" : ""}`}
            aria-pressed={activeTab === "system"}
          >
            系统提示词
            <span className="text-[10px] opacity-70">内置案例</span>
          </button>
          <button
            type="button"
            onClick={() => setActiveTab("mine")}
            className={`chip justify-center px-4 py-2 ${activeTab === "mine" ? "chip-active" : ""}`}
            aria-pressed={activeTab === "mine"}
          >
            我的提示词
            <span className="text-[10px] opacity-70">{myPrompts.length ? `${myPrompts.length} 条` : "历史/收藏"}</span>
          </button>
        </div>

        {activeTab === "system" ? (
          <PromptLibraryBrowser
            variant="page"
            title="系统提示词"
            description="按分类和关键词筛选平台内置参考，点击套用会写入创作页输入框。"
            primaryLabel="套用"
            secondaryLabel="复制"
            onPrimary={usePrompt}
            onSecondary={copyPrompt}
          />
        ) : (
          <section className="panel mb-6 p-4 sm:p-5">
            <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
              <div>
                <h2 className="text-xl font-bold text-snow">我的提示词</h2>
                <p className="mt-1 text-xs text-fog">自动保存反推和生成历史，也可以手动收藏常用模板。</p>
              </div>
              <div className="flex flex-wrap gap-2">
                <input
                  className="input w-48 px-3 py-2 text-xs"
                  placeholder="搜索我的提示词"
                  value={historyQuery}
                  onChange={(e) => setHistoryQuery(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter") loadHistory({ q: historyQuery }); }}
                />
                <select
                  className="input px-3 py-2 text-xs"
                  value={historyFilter}
                  onChange={(e) => {
                    setHistoryFilter(e.target.value);
                    loadHistory({ filter: e.target.value });
                  }}
                >
                  <option value="all">全部</option>
                  <option value="favorite">收藏</option>
                  <option value="reverse">反推</option>
                  <option value="generate">生成</option>
                </select>
                <button onClick={() => loadHistory()} className="btn-secondary btn-sm">刷新</button>
              </div>
            </div>
            <div className="mb-4 grid gap-2 sm:grid-cols-[180px_1fr_auto]">
              <input className="input px-3 py-2 text-xs" placeholder="标题（可选）" value={manualTitle} onChange={(e) => setManualTitle(e.target.value)} />
              <input className="input px-3 py-2 text-xs" placeholder="保存一条常用提示词" value={manualPrompt} onChange={(e) => setManualPrompt(e.target.value)} />
              <button onClick={saveManualPrompt} className="btn-primary btn-sm">保存</button>
            </div>
            {myPrompts.length === 0 ? (
              <div className="rounded-xl border border-line bg-black/10 px-3 py-8 text-center text-xs text-fog">还没有提示词历史</div>
            ) : (
              <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                {myPrompts.map((item) => (
                  <article key={item.id} className="rounded-xl2 border border-line bg-black/15 p-3">
                    <div className="mb-2 flex items-center justify-between gap-2">
                      <h3 className="min-w-0 truncate text-sm font-semibold text-snow">{item.title}</h3>
                      <button
                        type="button"
                        onClick={() => toggleFavorite(item)}
                        aria-label={item.favorite ? "取消收藏提示词" : "收藏提示词"}
                        aria-pressed={Boolean(item.favorite)}
                        className={item.favorite ? "text-rose" : "text-fog hover:text-snow"}
                      >
                        {item.favorite ? "★" : "☆"}
                      </button>
                    </div>
                    <p className="line-clamp-3 text-xs leading-relaxed text-fog">{item.prompt}</p>
                    <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
                      <span className="text-[11px] text-fog">{sourceLabel(item.source)} · 用过 {item.usage_count || 0} 次</span>
                      <div className="flex gap-1.5">
                        <button onClick={() => copyPrompt(item)} className="btn-secondary btn-sm px-2 py-1 text-[11px]">复制</button>
                        <button onClick={() => usePrompt(item)} className="btn-primary btn-sm px-2 py-1 text-[11px]">套用</button>
                        <button onClick={() => deletePrompt(item)} className="btn-ghost btn-sm px-2 py-1 text-[11px] text-bad">删除</button>
                      </div>
                    </div>
                  </article>
                ))}
              </div>
            )}
          </section>
        )}
      </main>
    </div>
  );
}

function sourceLabel(source) {
  return { manual: "手动", reverse: "反推", generate: "生成", library: "库" }[source] || source;
}
