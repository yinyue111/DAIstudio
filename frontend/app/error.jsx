"use client";

import { useEffect } from "react";

export default function Error({ error, reset }) {
  useEffect(() => {
    console.error("app route error", error);
  }, [error]);

  return (
    <main className="min-h-screen px-4 py-10 text-snow sm:px-6">
      <section className="mx-auto flex min-h-[70vh] max-w-2xl flex-col items-center justify-center text-center">
        <div className="panel w-full px-6 py-8 sm:px-8">
          <p className="text-xs font-display uppercase tracking-[0.22em] text-fog">页面异常</p>
          <h1 className="mt-3 text-2xl font-display font-semibold sm:text-3xl">
            当前页面加载失败
          </h1>
          <p className="mt-3 text-sm leading-6 text-mist">
            可以先重试当前页面；如果仍然失败，返回首页重新进入工作台。
          </p>
          <div className="mt-6 flex flex-col justify-center gap-3 sm:flex-row">
            <button type="button" className="btn-primary" onClick={() => reset()}>
              重试
            </button>
            <a className="btn-secondary" href="/">
              返回首页
            </a>
          </div>
        </div>
      </section>
    </main>
  );
}
