import "./globals.css";
import AppShell from "../components/AppShell";
import ErrorBoundary from "../components/ErrorBoundary";
import { ToastProvider } from "../components/ToastProvider";

export const metadata = {
  title: "造梦 · AI 创作工作台",
  description: "输入一句话，生成你的画面 —— 内部 AI 文生图 / 视频创作平台",
  icons: {
    icon: "/icon.svg",
    shortcut: "/icon.svg",
    apple: "/logo.svg",
  },
};

export const viewport = {
  themeColor: "#07070e",
};

export default function RootLayout({ children }) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body className="min-h-screen" suppressHydrationWarning>
        <ErrorBoundary>
          <ToastProvider>
            <AppShell>{children}</AppShell>
          </ToastProvider>
        </ErrorBoundary>
      </body>
    </html>
  );
}
