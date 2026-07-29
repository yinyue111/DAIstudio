"use client";

import { useRef, useState } from "react";
import { canDownloadAsset, isAssetTakenDown } from "../components/AssetMedia";
import { api, downloadBlob } from "../lib/api";
import { reportBackgroundError } from "../lib/errorHandling";
import { createStudioOwnerRequestContext } from "../lib/studioSession";
import {
  clearPendingStudioActionRequest,
  pendingStudioActionRequestId,
} from "../app/studio/generationQuote";

export default function useAssetActions({
  getOwnerSession,
  ownerId,
  requestQuoteConfirmation,
  studioActionPendingRequestRef,
  task,
  setTask,
  lightbox,
  setLightbox,
  refreshMe,
  loadWorks,
  setMsg,
}) {
  const [busyAssetIds, setBusyAssetIds] = useState(() => new Set());
  const busyAssetIdsRef = useRef(new Set());
  const ownerRequestContextRef = useRef(null);

  if (!ownerRequestContextRef.current) {
    ownerRequestContextRef.current = createStudioOwnerRequestContext(getOwnerSession);
  }

  function setAssetBusy(assetId, busy) {
    if (busy) busyAssetIdsRef.current.add(assetId);
    else busyAssetIdsRef.current.delete(assetId);
    setBusyAssetIds(new Set(busyAssetIdsRef.current));
  }

  function resetOwnerAssetActions() {
    ownerRequestContextRef.current.invalidate();
    busyAssetIdsRef.current.clear();
    setBusyAssetIds(new Set());
  }

  async function unlock(asset) {
    const ownerRequest = ownerRequestContextRef.current.capture();
    if (!ownerRequest.isCurrent() || busyAssetIdsRef.current.has(asset.id)) return;
    const quoteRequest = { asset_id: Number(asset.id) };
    const actionRequestId = pendingStudioActionRequestId(
      studioActionPendingRequestRef,
      `asset-unlock:${ownerId || "unknown"}`,
      quoteRequest,
    );
    setAssetBusy(asset.id, true);
    try {
      const confirmation = await requestQuoteConfirmation({
        kind: "asset_unlock",
        request: quoteRequest,
        clientRequestId: actionRequestId,
        execute: ({ request: confirmedRequest }) => {
          if (!ownerRequest.isCurrent()) throw new Error("账号已切换，本次解锁已取消。");
          return api.unlock(asset.id, { quote_id: Number(confirmedRequest.quote_id) });
        },
      });
      if (confirmation.status !== "executed") {
        if (["quote_failed", "execution_failed"].includes(confirmation.status)) {
          throw confirmation.error || new Error("高清解锁失败。");
        }
        if (confirmation.status === "invalidated") {
          ownerRequest.commit(() => setMsg(confirmation.reason || "页面状态已变化，请重新解锁高清素材。"));
        }
        return;
      }
      clearPendingStudioActionRequest(studioActionPendingRequestRef, actionRequestId);
      const updated = confirmation.result;
      if (!ownerRequest.isCurrent()) return;
      const capturedTaskId = task?.id;
      if (capturedTaskId) {
        api.task(capturedTaskId)
          .then((nextTask) => ownerRequest.commit(() => setTask(
            (previous) => previous?.id === capturedTaskId ? nextTask : previous,
          )))
          .catch((error) => {
            if (ownerRequest.isCurrent()) reportBackgroundError(error, "refresh active task after unlock");
          });
      }
      ownerRequest.commit(() => {
        if (lightbox?.id === asset.id) setLightbox(updated);
        refreshMe();
        loadWorks();
      });
    } catch (error) {
      ownerRequest.commit(() => setMsg(error.message));
    } finally {
      ownerRequest.commit(() => setAssetBusy(asset.id, false));
    }
  }

  async function download(asset) {
    const ownerRequest = ownerRequestContextRef.current.capture();
    if (!ownerRequest.isCurrent() || busyAssetIdsRef.current.has(asset.id)) return;
    if (!canDownloadAsset(asset)) {
      setMsg(isAssetTakenDown(asset) ? "素材已下架，不能继续下载。" : "请先解锁后再下载。");
      return;
    }
    setAssetBusy(asset.id, true);
    try {
      const filename = await downloadBlob(
        `/api/assets/${asset.id}/download`,
        asset.type === "video" ? `asset-${asset.id}.mp4` : undefined,
      );
      ownerRequest.commit(() => setMsg(`已开始下载 ${filename}`));
    } catch (error) {
      ownerRequest.commit(() => setMsg(error.message));
    } finally {
      ownerRequest.commit(() => setAssetBusy(asset.id, false));
    }
  }

  return {
    busyAssetIds,
    unlock,
    download,
    resetOwnerAssetActions,
  };
}
