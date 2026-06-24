"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Nav from "../../components/Nav";
import PromptLibraryBrowser, { STUDIO_DRAFT_PROMPT_KEY } from "../../components/PromptLibraryBrowser";
import { api, loginPath } from "../../lib/api";

export default function PromptsPage() {
  const router = useRouter();
  const [me, setMe] = useState(null);
  const [msg, setMsg] = useState("");

  useEffect(() => {
    api.me().then(setMe).catch(() => router.push(loginPath()));
  }, []);

  function usePrompt(item) {
    try {
      window.localStorage.setItem(STUDIO_DRAFT_PROMPT_KEY, item.prompt || "");
    } catch (e) {}
    router.push("/");
  }

  async function copyPrompt(item) {
    try {
      await navigator.clipboard.writeText(item.prompt || "");
      setMsg("提示词已复制");
      window.setTimeout(() => setMsg(""), 1400);
    } catch (e) {
      setMsg("复制失败，请直接进入创作页套用");
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
          <div className="mb-4 rounded-xl border border-ok/30 bg-ok/10 px-4 py-2.5 text-sm text-ok">
            {msg}
          </div>
        )}

        <PromptLibraryBrowser
          variant="page"
          title="全部提示词"
          description="按分类和关键词筛选参考，点击套用会写入创作页输入框。"
          primaryLabel="套用"
          secondaryLabel="复制"
          onPrimary={usePrompt}
          onSecondary={copyPrompt}
        />
      </main>
    </div>
  );
}
