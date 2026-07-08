"use client";

import { createContext, useCallback, useContext, useMemo, useRef, useState } from "react";

const ToastContext = createContext(null);

const STYLE = {
  success: "border-ok/40 bg-ok/15 text-ok",
  error: "border-bad/45 bg-bad/15 text-bad",
  warning: "border-warn/45 bg-warn/15 text-warn",
  info: "border-line bg-base2/95 text-mist",
};

function normalizeToast(input, fallbackType = "info") {
  if (typeof input === "string") {
    return { type: fallbackType, message: input };
  }
  return {
    type: input?.type || fallbackType,
    message: input?.message || "",
    title: input?.title || "",
    duration: Number(input?.duration || 4200),
  };
}

export function ToastProvider({ children }) {
  const [items, setItems] = useState([]);
  const nextIdRef = useRef(1);

  const dismiss = useCallback((id) => {
    setItems((current) => current.filter((item) => item.id !== id));
  }, []);

  const push = useCallback((input, fallbackType = "info") => {
    const toast = normalizeToast(input, fallbackType);
    const message = String(toast.message || "").trim();
    if (!message) return null;
    const id = nextIdRef.current++;
    const item = { ...toast, id };
    setItems((current) => [item, ...current].slice(0, 5));
    const duration = Math.max(1200, Number(item.duration || 4200));
    window.setTimeout(() => dismiss(id), duration);
    return id;
  }, [dismiss]);

  const value = useMemo(() => ({
    push,
    dismiss,
    success: (message, options = {}) => push({ ...options, message, type: "success" }),
    error: (message, options = {}) => push({ ...options, message, type: "error" }),
    warn: (message, options = {}) => push({ ...options, message, type: "warning" }),
    info: (message, options = {}) => push({ ...options, message, type: "info" }),
  }), [dismiss, push]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        className="fixed inset-x-3 bottom-[calc(5.75rem+env(safe-area-inset-bottom))] z-[80] flex flex-col gap-2 sm:inset-x-auto sm:right-5 sm:top-5 sm:bottom-auto sm:w-[360px]"
        aria-live="polite"
        aria-atomic="false"
      >
        {items.map((item) => (
          <div
            key={item.id}
            className={`rounded-xl2 border px-4 py-3 shadow-pop backdrop-blur-xl ${STYLE[item.type] || STYLE.info}`}
          >
            <div className="flex items-start gap-3">
              <div className="min-w-0 flex-1">
                {item.title && <div className="mb-0.5 text-sm font-semibold text-snow">{item.title}</div>}
                <div className="break-words text-sm leading-relaxed">{item.message}</div>
              </div>
              <button
                type="button"
                onClick={() => dismiss(item.id)}
                className="shrink-0 rounded-full px-1.5 text-lg leading-none text-fog hover:bg-white/10 hover:text-snow"
                aria-label="关闭通知"
              >
                ×
              </button>
            </div>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const context = useContext(ToastContext);
  if (!context) {
    return {
      push: () => null,
      dismiss: () => {},
      success: () => null,
      error: () => null,
      warn: () => null,
      info: () => null,
    };
  }
  return context;
}
