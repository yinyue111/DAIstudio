"use client";

import { useEffect } from "react";

export default function GlobalError({ error, reset }) {
  useEffect(() => {
    console.error("global app error", error);
  }, [error]);

  return (
    <html lang="zh-CN">
      <body className="min-h-screen bg-base text-snow">
        <main className="min-h-screen px-4 py-10 sm:px-6">
          <section className="mx-auto flex min-h-[70vh] max-w-2xl flex-col items-center justify-center text-center">
            <div className="panel w-full px-6 py-8 sm:px-8">
              <p className="text-xs font-display uppercase tracking-[0.22em] text-fog">应用异常</p>
              <h1 className="mt-3 text-2xl font-display font-semibold sm:text-3xl">
                平台加载失败
              </h1>
              <p className="mt-3 text-sm leading-6 text-mist">
                请重试当前页面；如果仍然失败，返回首页重新进入。
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
      </body>
    </html>
  );
}
