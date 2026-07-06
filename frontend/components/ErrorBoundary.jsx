"use client";

import { Component } from "react";

export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }

  componentDidCatch(error, info) {
    console.error("ErrorBoundary caught:", error, info);
  }

  render() {
    if (this.state.hasError) {
      return (
        <main className="min-h-screen px-4 py-10 text-snow sm:px-6">
          <section className="mx-auto flex min-h-[70vh] max-w-2xl flex-col items-center justify-center text-center">
            <div className="panel w-full px-6 py-8 sm:px-8">
              <p className="text-xs font-display uppercase tracking-[0.22em] text-fog">渲染异常</p>
              <h1 className="mt-3 text-2xl font-display font-semibold sm:text-3xl">
                页面组件渲染失败
              </h1>
              <p className="mt-3 text-sm leading-6 text-mist">
                当前页面发生了意外错误。点击重试恢复页面，或返回首页重新进入工作台。
              </p>
              <div className="mt-6 flex flex-col justify-center gap-3 sm:flex-row">
                <button
                  type="button"
                  className="btn-primary"
                  onClick={() => this.setState({ hasError: false, error: null })}
                >
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
    return this.props.children;
  }
}
