import { useEffect, useRef } from "react";

import { api } from "../lib/api";
import {
  redirectOnAuthError,
  reportBackgroundError,
  showError,
} from "../lib/errorHandling";

export default function useStudioRuntimeBootstrap({
  router,
  setMe,
  setCfg,
  setMsg,
  studioInitSeqRef,
  studioOwnerSessionCoordinatorRef,
  stopAllTracking,
  revokeUploadedObjectUrls,
  revokeProductObjectUrls,
}) {
  const configRefreshSeqRef = useRef(0);

  useEffect(() => {
    let canceled = false;

    async function initializeStudioSession() {
      const [meResult, configResult] = await Promise.allSettled([api.me(), api.config()]);
      if (canceled) return;

      if (configResult.status === "fulfilled") {
        setCfg(configResult.value);
      } else {
        showError(setMsg, configResult.reason, "加载创作配置失败");
      }

      if (meResult.status === "rejected") {
        redirectOnAuthError(meResult.reason, router, setMsg, "studio session probe");
        return;
      }
      setMe(meResult.value);
    }

    initializeStudioSession().catch((error) => (
      showError(setMsg, error, "初始化创作工作台失败")
    ));
    return () => {
      canceled = true;
      const seq = ++studioInitSeqRef.current;
      studioOwnerSessionCoordinatorRef.current.cancelScheduled();
      studioOwnerSessionCoordinatorRef.current.bindOwner("", seq);
      stopAllTracking();
      revokeUploadedObjectUrls();
      revokeProductObjectUrls();
    };
  }, []);

  useEffect(() => {
    const refreshStudioConfig = async () => {
      const seq = ++configRefreshSeqRef.current;
      const next = await api.config();
      if (seq === configRefreshSeqRef.current) setCfg(next);
      return next;
    };
    const onFocus = () => {
      refreshStudioConfig().catch((error) => (
        reportBackgroundError(error, "refresh studio config on focus")
      ));
    };
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") onFocus();
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, []);
}
