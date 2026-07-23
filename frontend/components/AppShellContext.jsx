"use client";

import { createContext, useContext } from "react";

const AppShellContext = createContext(null);

export function AppShellProvider({ children, value }) {
  return <AppShellContext.Provider value={value}>{children}</AppShellContext.Provider>;
}

export function useAppShellContext() {
  return useContext(AppShellContext);
}
