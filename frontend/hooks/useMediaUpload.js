"use client";

import { useEffect, useRef } from "react";
import { api } from "../lib/api";
import {
  normalizeReverseOperation,
  reverseOperationFailure,
  reverseOperationResult,
} from "../lib/reverseOperations";
import { reportBackgroundError } from "../lib/errorHandling";
import { createAbortableRequestRegistry } from "../lib/abortableRequestRegistry";
import { createStudioOwnerRequestContext } from "../lib/studioSession";
import { assetSignature, isRequestTimeoutError } from "../app/studio/helpers";
import { buildReverseOperationRequestSnapshotV2 } from "../app/studio/reverseSnapshot";
import {
  buildSubjectProfileRequestIdentity,
  cacheSubjectProfileResult,
  normalizeSubjectProfileResult,
} from "../lib/studioSubjectProfile";
import {
  clearPendingReverseRequest,
  generateReverseClientRequestId,
  shouldKeepPendingReverseRequest,
} from "../app/studio/generationRequestId";
import { MAX_PRODUCT_DETAIL_IMAGES } from "../app/studio/constants";

const DEFAULT_MAX_UPLOAD_IMAGE_BYTES = 20 * 1024 * 1024;
const DEFAULT_MAX_UPLOAD_VIDEO_BYTES = 512 * 1024 * 1024;

function formatBytes(bytes) {
  const n = Number(bytes || 0);
  if (!Number.isFinite(n) || n <= 0) return "";
  if (n >= 1024 * 1024 * 1024) return `${(n / 1024 / 1024 / 1024).toFixed(1)}GB`;
  if (n >= 1024 * 1024) return `${Math.ceil(n / 1024 / 1024)}MB`;
  if (n >= 1024) return `${Math.ceil(n / 1024)}KB`;
  return `${n}B`;
}

function bumpRequest(ref, mode) {
  const next = Number(ref.current[mode] || 0) + 1;
  ref.current[mode] = next;
  return next;
}

function isRequestCurrent(ref, mode, id) {
  return Number(ref.current[mode] || 0) === id;
}

function trackingLostError(operation) {
  return Object.assign(
    new Error(operation?.error || "主体档案仍在后台处理中，稍后会自动恢复进度"),
    { operation_id: operation?.id },
  );
}

function waitForTrackedProfileOperation(operation, {
  mode,
  signal,
  trackProfileReverseOperation,
  context,
}) {
  if (typeof trackProfileReverseOperation !== "function") {
    return Promise.reject(new Error("主体档案任务跟踪器不可用"));
  }
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      signal?.removeEventListener("abort", onAbort);
      callback(value);
    };
    const onAbort = () => finish(
      reject,
      Object.assign(new Error("主体档案任务跟踪已取消"), { name: "AbortError" }),
    );
    if (signal?.aborted) {
      onAbort();
      return;
    }
    signal?.addEventListener("abort", onAbort, { once: true });
    try {
      Promise.resolve(trackProfileReverseOperation(operation, mode, {
        ...context,
        onUpdate: (nextOperation) => {
          context.onUpdate?.(nextOperation);
          if (nextOperation?.error_code === "tracking_lost") {
            finish(reject, trackingLostError(nextOperation));
          }
        },
        onSettled: (settledOperation) => {
          context.onSettled?.(settledOperation);
          finish(resolve, settledOperation);
        },
      })).catch((error) => finish(reject, error));
    } catch (error) {
      finish(reject, error);
    }
  });
}

export default function useMediaUpload({
  cfg,
  creationMode,
  category,
  isEditMode,
  subjectMode = "",
  productAsset = null,
  productDetailAssets = [],
  productDetailLimit = MAX_PRODUCT_DETAIL_IMAGES,
  visionModelConfigId = null,
  uploading,
  setMsg,
  setWorkspacePatch,
  setCreationMode,
  setRefOpen,
  bumpRefVersion,
  isRefVersionCurrent,
  bumpReverseRequest,
  isModeVisible,
  selectAssetForMode,
  getOwnerSession,
  subjectProfilePendingRequestRef = null,
  subjectProfileResultCacheRef = null,
  subjectProfileOperationsRef = null,
  profileOperation = null,
  cancelRecoveredProfileOperation = null,
  trackProfileReverseOperation = null,
  requestQuoteConfirmation = null,
}) {
  const uploadRequestRef = useRef({});
  const productUploadRequestRef = useRef({});
  const imageUploadInputRef = useRef(null);
  const productUploadInputRef = useRef(null);
  const productDetailUploadInputRef = useRef(null);
  const lastFrameUploadInputRef = useRef(null);
  const videoUploadInputRef = useRef(null);
  const objectUrlsByModeRef = useRef({});
  const productObjectUrlsRef = useRef({});
  const fallbackSubjectProfilePendingRequestRef = useRef(null);
  const fallbackSubjectProfileResultCacheRef = useRef(null);
  const fallbackSubjectProfileOperationsRef = useRef({});
  const profileAbortControllersRef = useRef({});
  const getOwnerSessionRef = useRef(getOwnerSession);
  const ownerRequestContextRef = useRef(null);
  const activeUploadRequestsRef = useRef(null);
  const pendingProfileReverseRequestRef = subjectProfilePendingRequestRef || fallbackSubjectProfilePendingRequestRef;
  const profileResultCacheRef = subjectProfileResultCacheRef || fallbackSubjectProfileResultCacheRef;
  const profileOperationsRef = subjectProfileOperationsRef || fallbackSubjectProfileOperationsRef;

  getOwnerSessionRef.current = getOwnerSession;
  if (!ownerRequestContextRef.current) {
    ownerRequestContextRef.current = createStudioOwnerRequestContext(
      () => getOwnerSessionRef.current?.(),
    );
  }
  if (!activeUploadRequestsRef.current) {
    activeUploadRequestsRef.current = createAbortableRequestRegistry();
  }

  function uploadLimitExceeded(file, kind) {
    const fallback = kind === "video" ? DEFAULT_MAX_UPLOAD_VIDEO_BYTES : DEFAULT_MAX_UPLOAD_IMAGE_BYTES;
    const configured = Number(kind === "video" ? cfg?.max_upload_video_bytes : cfg?.max_upload_image_bytes);
    const limit = Number.isFinite(configured) && configured > 0 ? configured : fallback;
    if (!limit || !file?.size || file.size <= limit) return false;
    setMsg(`${kind === "video" ? "视频" : "图片"}文件过大，当前 ${formatBytes(file.size)}，上限 ${formatBytes(limit)}。请压缩后再上传。`);
    return true;
  }

  function resetInput(inputRef) {
    if (inputRef.current) inputRef.current.value = "";
  }

  function rememberUploadedObjectUrl(mode, url) {
    if (!objectUrlsByModeRef.current[mode]) objectUrlsByModeRef.current[mode] = new Set();
    objectUrlsByModeRef.current[mode].add(url);
  }

  function revokeUploadedObjectUrls(mode = null) {
    if (mode) {
      objectUrlsByModeRef.current[mode]?.forEach((url) => URL.revokeObjectURL(url));
      objectUrlsByModeRef.current[mode]?.clear();
      return;
    }
    Object.values(objectUrlsByModeRef.current).forEach((urls) => {
      urls?.forEach((url) => URL.revokeObjectURL(url));
      urls?.clear();
    });
    objectUrlsByModeRef.current = {};
  }

  function revokeProductObjectUrl(mode = creationMode) {
    const url = productObjectUrlsRef.current[mode];
    if (url) {
      URL.revokeObjectURL(url);
      delete productObjectUrlsRef.current[mode];
    }
  }

  function revokeProductObjectUrls() {
    Object.values(productObjectUrlsRef.current).forEach((url) => URL.revokeObjectURL(url));
    productObjectUrlsRef.current = {};
  }

  function buildDisplayAsset(uploaded, file, { thumb } = {}) {
    const previewUrl = URL.createObjectURL(file);
    return {
      previewUrl,
      asset: {
        ...uploaded,
        display_url: previewUrl,
        display_thumb: thumb || previewUrl,
      },
    };
  }

  async function assignUploadedReferenceAsset(mode, displayAsset) {
    await selectAssetForMode(displayAsset, mode);
    setWorkspacePatch((current) => ({
      assets: [displayAsset, ...current.assets],
      variationSource: null,
    }), mode);
    setRefOpen(true);
  }

  async function prefetchProductProfile(mode, asset, uploadReqId, ownerRequest) {
    const profileSubjectMode = creationMode === "video" ? "product" : subjectMode;
    if (
      !ownerRequest.isCurrent()
      || !isRequestCurrent(productUploadRequestRef, mode, uploadReqId)
      || (!isEditMode && creationMode !== "video")
      || !asset?.url
      || !["product", "portrait"].includes(profileSubjectMode)
    ) return;
    const signature = assetSignature(asset);
    setWorkspacePatch({
      productProfiling: true,
      productProfile: null,
      productProfileSource: "",
      portraitProfile: null,
      portraitProfileSource: "",
    }, mode);
    const requestIdentity = buildSubjectProfileRequestIdentity({
      mode,
      subjectMode: profileSubjectMode,
      assetUrl: asset.url,
      assetSignature: signature,
      modelConfigId: visionModelConfigId,
    });
    const profileRequestId = generateReverseClientRequestId(
      pendingProfileReverseRequestRef,
      requestIdentity.scope,
      requestIdentity.signature,
    );
    try {
      const profileAbortController = new AbortController();
      profileAbortControllersRef.current[mode]?.abort();
      profileAbortControllersRef.current[mode] = profileAbortController;
      const profileTarget = profileSubjectMode === "portrait" ? "portrait_profile" : "product_profile";
      const profileRequest = {
        asset_url: asset.url,
        target: profileTarget,
        source_type: "image",
        ...(visionModelConfigId ? { model_config_id: Number(visionModelConfigId) } : {}),
        client_request_id: profileRequestId,
        workspace_snapshot_v2: {
          ...buildReverseOperationRequestSnapshotV2({
            creationMode: mode,
            subjectMode: profileSubjectMode,
            target: profileTarget,
            productAsset: asset,
          }),
          source_signature: signature,
          ...(visionModelConfigId ? { model_config_id: Number(visionModelConfigId) } : {}),
        },
      };
      if (typeof requestQuoteConfirmation !== "function") {
        throw new Error("主体识别服务不可用，本次未识别主体档案。");
      }
      const profileConfirmation = await requestQuoteConfirmation({
        kind: "reverse",
        request: profileRequest,
        clientRequestId: profileRequestId,
        execute: ({ request }) => api.createReverseOperation(request),
      });
      if (profileConfirmation.status !== "executed") {
        clearPendingReverseRequest(pendingProfileReverseRequestRef, profileRequestId);
        setWorkspacePatch({
          productProfile: null,
          productProfileSource: "",
          portraitProfile: null,
          portraitProfileSource: "",
          productProfiling: false,
          profileOperation: null,
        }, mode);
        delete profileAbortControllersRef.current[mode];
        if (["quote_failed", "execution_failed"].includes(profileConfirmation.status)) {
          throw profileConfirmation.error || new Error("主体档案提交失败。");
        }
        if (profileConfirmation.status === "invalidated" && isModeVisible(mode)) {
          setMsg(profileConfirmation.reason || "主体素材或模型已变化，请重新执行识别。");
        }
        return;
      }
      const createdOperation = normalizeReverseOperation(profileConfirmation.result);
      profileOperationsRef.current[mode] = createdOperation;
      const settledOperation = await waitForTrackedProfileOperation(createdOperation, {
        mode,
        signal: profileAbortController.signal,
        trackProfileReverseOperation,
        context: {
          clientRequestId: profileRequestId,
          targetSignature: signature,
          sourceAsset: asset,
          onUpdate: (operation) => {
            profileOperationsRef.current[mode] = operation;
            if (
              ownerRequest.isCurrent()
              && isRequestCurrent(productUploadRequestRef, mode, uploadReqId)
            ) setWorkspacePatch({ profileOperation: operation }, mode);
          },
          onSettled: (operation) => {
            delete profileOperationsRef.current[mode];
            if (["succeeded", "failed", "canceled"].includes(operation.status)) {
              clearPendingReverseRequest(pendingProfileReverseRequestRef, profileRequestId);
            }
          },
        },
      });
      if (settledOperation.status !== "succeeded") {
        throw reverseOperationFailure(settledOperation);
      }
      const profile = reverseOperationResult(settledOperation);
      if (!profile) throw new Error("主体档案任务完成但未返回结果");
      const resolvedProfile = normalizeSubjectProfileResult(profile);
      if (
        !ownerRequest.isCurrent()
        || !isRequestCurrent(productUploadRequestRef, mode, uploadReqId)
      ) {
        return;
      }
      clearPendingReverseRequest(pendingProfileReverseRequestRef, profileRequestId);
      cacheSubjectProfileResult(
        profileResultCacheRef,
        requestIdentity,
        profileRequestId,
        resolvedProfile,
      );
      setWorkspacePatch(profileSubjectMode === "portrait" ? {
        portraitProfile: resolvedProfile,
        portraitProfileSource: signature,
        productProfile: null,
        productProfileSource: "",
        productProfiling: false,
        profileOperation: null,
      } : {
        productProfile: resolvedProfile,
        productProfileSource: signature,
        portraitProfile: null,
        portraitProfileSource: "",
        productProfiling: false,
        profileOperation: null,
      }, mode);
      delete profileOperationsRef.current[mode];
      delete profileAbortControllersRef.current[mode];
    } catch (e) {
      if (
        !ownerRequest.isCurrent()
        || !isRequestCurrent(productUploadRequestRef, mode, uploadReqId)
      ) return;
      const keepTracking = shouldKeepPendingReverseRequest(e);
      if (!isRequestTimeoutError(e) && !keepTracking) {
        clearPendingReverseRequest(pendingProfileReverseRequestRef, profileRequestId);
      }
      if (keepTracking) {
        delete profileAbortControllersRef.current[mode];
        const activeOperation = profileOperationsRef.current[mode];
        if (activeOperation) {
          setWorkspacePatch({
            productProfiling: true,
            profileOperation: activeOperation,
          }, mode);
          if (isModeVisible(mode)) {
            setMsg(`${e.message}，可刷新页面继续查看进度。`);
          }
        }
        return;
      }
      setWorkspacePatch({
        productProfile: null,
        productProfileSource: "",
        portraitProfile: null,
        portraitProfileSource: "",
        productProfiling: false,
        profileOperation: null,
      }, mode);
      delete profileOperationsRef.current[mode];
      delete profileAbortControllersRef.current[mode];
      if (isModeVisible(mode)) {
        setMsg(
          shouldKeepPendingReverseRequest(e)
            ? `${e.message}，请稍后重试。`
            : `主体档案识别失败，可重新上传更清晰图片后再试：${e.message}`,
        );
      }
    }
  }

  async function cancelProfileOperation(mode = creationMode) {
    const operation = profileOperationsRef.current[mode]
      || (mode === creationMode ? profileOperation : null);
    let canceledOperation = null;
    if (operation?.id && ["queued", "running", "needs_confirmation"].includes(operation.status)) {
      canceledOperation = await api.cancelReverseOperation(operation.id);
    }
    profileAbortControllersRef.current[mode]?.abort();
    delete profileAbortControllersRef.current[mode];
    delete profileOperationsRef.current[mode];
    setWorkspacePatch({ profileOperation: null, productProfiling: false }, mode);
    return canceledOperation;
  }

  async function cancelAllProfileOperations() {
    const entries = Object.entries(profileOperationsRef.current || {});
    const results = await Promise.allSettled(entries
      .filter(([, operation]) => operation?.id && ["queued", "running", "needs_confirmation"].includes(operation.status))
      .map(([, operation]) => api.cancelReverseOperation(operation.id)));
    const failure = results.find((result) => result.status === "rejected");
    if (failure) throw failure.reason;
    Object.values(profileAbortControllersRef.current).forEach((controller) => controller?.abort());
    profileAbortControllersRef.current = {};
    profileOperationsRef.current = {};
    return results;
  }

  async function doUploadImage(file) {
    if (!file || uploading) return;
    const ownerRequest = ownerRequestContextRef.current.capture();
    const mode = creationMode;
    if (!file.type?.startsWith("image/")) {
      setMsg("请选择图片文件");
      resetInput(imageUploadInputRef);
      return;
    }
    if (uploadLimitExceeded(file, "image")) {
      resetInput(imageUploadInputRef);
      return;
    }
    const reqId = bumpRequest(uploadRequestRef, mode);
    const refVersion = bumpRefVersion(mode);
    bumpReverseRequest(mode);
    setMsg("");
    setWorkspacePatch({ uploading: true, uploadingRole: "reference" }, mode);
    const uploadRequest = activeUploadRequestsRef.current.capture();
    try {
      const uploaded = await api.uploadImage(file, { signal: uploadRequest.signal });
      if (!ownerRequest.isCurrent() || !isRefVersionCurrent(mode, refVersion)) return;
      const { previewUrl, asset } = buildDisplayAsset(uploaded, file);
      rememberUploadedObjectUrl(mode, previewUrl);
      await assignUploadedReferenceAsset(mode, asset);
    } catch (e) {
      if (
        ownerRequest.isCurrent()
        && isRefVersionCurrent(mode, refVersion)
        && isModeVisible(mode)
      ) setMsg(e.message);
    } finally {
      uploadRequest.release();
      if (
        ownerRequest.isCurrent()
        && isRequestCurrent(uploadRequestRef, mode, reqId)
      ) {
        setWorkspacePatch({ uploading: false, uploadingRole: null }, mode);
        resetInput(imageUploadInputRef);
      }
    }
  }

  async function doUploadProductImage(file) {
    if (!file || uploading) return;
    const ownerRequest = ownerRequestContextRef.current.capture();
    const mode = creationMode;
    if (!file.type?.startsWith("image/")) {
      setMsg("请选择产品图片文件");
      resetInput(productUploadInputRef);
      return;
    }
    if (uploadLimitExceeded(file, "image")) {
      resetInput(productUploadInputRef);
      return;
    }
    try {
      await cancelRecoveredProfileOperation?.(mode);
      await cancelProfileOperation(mode);
    } catch (error) {
      setMsg(error.message || "旧主体档案任务取消失败，已保留当前素材");
      resetInput(productUploadInputRef);
      return;
    }
    const productReqId = bumpRequest(productUploadRequestRef, mode);
    const reqId = bumpRequest(uploadRequestRef, mode);
    setMsg("");
    setWorkspacePatch({ uploading: true, uploadingRole: "product" }, mode);
    const uploadRequest = activeUploadRequestsRef.current.capture();
    try {
      const uploaded = await api.uploadImage(file, { signal: uploadRequest.signal });
      if (
        !ownerRequest.isCurrent()
        || !isRequestCurrent(productUploadRequestRef, mode, productReqId)
      ) return;
      const { previewUrl, asset } = buildDisplayAsset(uploaded, file);
      if (!isRequestCurrent(productUploadRequestRef, mode, productReqId)) {
        URL.revokeObjectURL(previewUrl);
        return;
      }
      revokeProductObjectUrl(mode);
      productObjectUrlsRef.current[mode] = previewUrl;
      setWorkspacePatch({
        productAsset: asset,
        productProfile: null,
        productProfileSource: "",
        portraitProfile: null,
        portraitProfileSource: "",
        productProfiling: false,
        profileOperation: null,
        subjectProtection: null,
        subjectProtectionLoading: true,
        subjectProtectionSource: "",
        variationSource: null,
      }, mode);
      setRefOpen(true);
      window.setTimeout(() => {
        prefetchProductProfile(mode, asset, productReqId, ownerRequest);
      }, 0);
    } catch (e) {
      if (ownerRequest.isCurrent() && isModeVisible(mode)) setMsg(e.message);
    } finally {
      uploadRequest.release();
      if (
        ownerRequest.isCurrent()
        && isRequestCurrent(uploadRequestRef, mode, reqId)
      ) {
        setWorkspacePatch({ uploading: false, uploadingRole: null }, mode);
        resetInput(productUploadInputRef);
      }
    }
  }

  async function selectProductAsset(asset) {
    if (!asset?.url || uploading) return;
    const ownerRequest = ownerRequestContextRef.current.capture();
    const mode = creationMode;
    try {
      await cancelRecoveredProfileOperation?.(mode);
      await cancelProfileOperation(mode);
    } catch (error) {
      setMsg(error.message || "旧主体档案任务取消失败，已保留当前素材");
      return;
    }
    const productReqId = bumpRequest(productUploadRequestRef, mode);
    revokeProductObjectUrl(mode);
    setWorkspacePatch({
      productAsset: asset,
      productProfile: null,
      productProfileSource: "",
      portraitProfile: null,
      portraitProfileSource: "",
      productProfiling: false,
      profileOperation: null,
      subjectProtection: null,
      subjectProtectionLoading: true,
      subjectProtectionSource: "",
      variationSource: null,
    }, mode);
    setRefOpen(true);
    window.setTimeout(() => {
      prefetchProductProfile(mode, asset, productReqId, ownerRequest);
    }, 0);
  }

  async function doUploadProductDetailImages(fileList) {
    const files = Array.from(fileList || []);
    const effectiveProductDetailLimit = Number.isInteger(productDetailLimit)
      ? Math.min(MAX_PRODUCT_DETAIL_IMAGES, Math.max(0, productDetailLimit))
      : MAX_PRODUCT_DETAIL_IMAGES;
    if (!files.length || uploading) return;
    if (!productAsset?.url) {
      setMsg("请先选择产品主题图");
      resetInput(productDetailUploadInputRef);
      return;
    }
    const mode = creationMode;
    if (files.some((file) => !file.type?.startsWith("image/"))) {
      setMsg("产品细节图只支持图片文件");
      resetInput(productDetailUploadInputRef);
      return;
    }
    if (files.some((file) => uploadLimitExceeded(file, "image"))) {
      resetInput(productDetailUploadInputRef);
      return;
    }
    if (productDetailAssets.length + files.length > MAX_PRODUCT_DETAIL_IMAGES) {
      setMsg(`产品细节图最多 ${MAX_PRODUCT_DETAIL_IMAGES} 张，当前已有 ${productDetailAssets.length} 张。`);
      resetInput(productDetailUploadInputRef);
      return;
    }
    if (productDetailAssets.length + files.length > effectiveProductDetailLimit) {
      setMsg(`当前模型最多支持 ${effectiveProductDetailLimit} 张产品细节图，当前已有 ${productDetailAssets.length} 张。`);
      resetInput(productDetailUploadInputRef);
      return;
    }
    const ownerRequest = ownerRequestContextRef.current.capture();
    const reqId = bumpRequest(uploadRequestRef, mode);
    setMsg("");
    setWorkspacePatch({ uploading: true, uploadingRole: "product_detail" }, mode);
    const uploadRequest = activeUploadRequestsRef.current.capture();
    try {
      const uploadedRows = [];
      for (const file of files) {
        const uploaded = await api.uploadImage(file, { signal: uploadRequest.signal });
        if (!ownerRequest.isCurrent() || !isRequestCurrent(uploadRequestRef, mode, reqId)) return;
        const { previewUrl, asset } = buildDisplayAsset(uploaded, file);
        rememberUploadedObjectUrl(mode, previewUrl);
        uploadedRows.push(asset);
      }
      setWorkspacePatch((current) => {
        const existing = current.productDetailAssets || [];
        const urls = new Set([current.productAsset?.url, ...existing.map((item) => item?.url)].filter(Boolean));
        const additions = uploadedRows.filter((item) => item?.url && !urls.has(item.url));
        if (existing.length + additions.length > MAX_PRODUCT_DETAIL_IMAGES) return current;
        if (existing.length + additions.length > effectiveProductDetailLimit) return current;
        return { productDetailAssets: [...existing, ...additions] };
      }, mode);
      setRefOpen(true);
    } catch (error) {
      if (ownerRequest.isCurrent() && isModeVisible(mode)) setMsg(error.message || "产品细节图上传失败");
    } finally {
      uploadRequest.release();
      if (ownerRequest.isCurrent() && isRequestCurrent(uploadRequestRef, mode, reqId)) {
        setWorkspacePatch({ uploading: false, uploadingRole: null }, mode);
        resetInput(productDetailUploadInputRef);
      }
    }
  }

  async function doUploadLastFrameImage(file) {
    if (!file || uploading) return;
    const ownerRequest = ownerRequestContextRef.current.capture();
    const mode = creationMode;
    if (!file.type?.startsWith("image/")) {
      setMsg("视频尾帧只支持图片文件");
      resetInput(lastFrameUploadInputRef);
      return;
    }
    if (uploadLimitExceeded(file, "image")) {
      resetInput(lastFrameUploadInputRef);
      return;
    }
    const reqId = bumpRequest(uploadRequestRef, mode);
    setMsg("");
    setWorkspacePatch({ uploading: true, uploadingRole: "last_frame" }, mode);
    const uploadRequest = activeUploadRequestsRef.current.capture();
    try {
      const uploaded = await api.uploadImage(file, { signal: uploadRequest.signal });
      if (!ownerRequest.isCurrent() || !isRequestCurrent(uploadRequestRef, mode, reqId)) return;
      const { previewUrl, asset } = buildDisplayAsset(uploaded, file);
      rememberUploadedObjectUrl(mode, previewUrl);
      setWorkspacePatch({ lastFrameAsset: asset }, mode);
      setRefOpen(true);
    } catch (error) {
      if (ownerRequest.isCurrent() && isModeVisible(mode)) {
        setMsg(error.message || "视频尾帧上传失败");
      }
    } finally {
      uploadRequest.release();
      if (ownerRequest.isCurrent() && isRequestCurrent(uploadRequestRef, mode, reqId)) {
        setWorkspacePatch({ uploading: false, uploadingRole: null }, mode);
        resetInput(lastFrameUploadInputRef);
      }
    }
  }

  function selectLastFrameAsset(asset) {
    if (!asset?.url || asset.type !== "image" || uploading) return false;
    setWorkspacePatch({ lastFrameAsset: asset }, creationMode);
    setRefOpen(true);
    return true;
  }

  async function doUploadVideo(file) {
    if (!file || uploading) return;
    const ownerRequest = ownerRequestContextRef.current.capture();
    const mode = creationMode;
    const targetMode = !isEditMode && category !== "video" ? "video" : mode;
    if (!file.type?.startsWith("video/")) {
      setMsg("请选择视频文件");
      resetInput(videoUploadInputRef);
      return;
    }
    if (uploadLimitExceeded(file, "video")) {
      resetInput(videoUploadInputRef);
      return;
    }
    const reqId = bumpRequest(uploadRequestRef, targetMode);
    const refVersion = bumpRefVersion(targetMode);
    bumpReverseRequest(targetMode);
    setMsg("");
    if (targetMode !== mode) setCreationMode(targetMode);
    setWorkspacePatch({ uploading: true, uploadingRole: "video_reference" }, targetMode);
    const uploadRequest = activeUploadRequestsRef.current.capture();
    try {
      const uploaded = await api.uploadVideo(file, { signal: uploadRequest.signal });
      if (
        !ownerRequest.isCurrent()
        || !isRefVersionCurrent(targetMode, refVersion)
      ) return;
      const { previewUrl, asset } = buildDisplayAsset(uploaded, file, { thumb: uploaded.thumb });
      rememberUploadedObjectUrl(targetMode, previewUrl);
      await assignUploadedReferenceAsset(targetMode, asset);
    } catch (e) {
      if (
        ownerRequest.isCurrent()
        && isRefVersionCurrent(targetMode, refVersion)
        && isModeVisible(targetMode)
      ) setMsg(e.message);
    } finally {
      uploadRequest.release();
      if (
        ownerRequest.isCurrent()
        && isRequestCurrent(uploadRequestRef, targetMode, reqId)
      ) {
        setWorkspacePatch({ uploading: false, uploadingRole: null }, targetMode);
        resetInput(videoUploadInputRef);
      }
    }
  }

  function resetOwnerMediaUpload() {
    Object.values(profileAbortControllersRef.current).forEach((controller) => controller?.abort());
    profileAbortControllersRef.current = {};
    profileOperationsRef.current = {};
    activeUploadRequestsRef.current.abortAll();
    ownerRequestContextRef.current.invalidate();
    uploadRequestRef.current = {};
    productUploadRequestRef.current = {};
    pendingProfileReverseRequestRef.current = null;
    profileResultCacheRef.current = null;
    revokeUploadedObjectUrls();
    revokeProductObjectUrls();
    resetInput(imageUploadInputRef);
    resetInput(productUploadInputRef);
    resetInput(productDetailUploadInputRef);
    resetInput(lastFrameUploadInputRef);
    resetInput(videoUploadInputRef);
  }

  function cancelOwnerProfileOperationsAndReset() {
    cancelAllProfileOperations().catch((error) => {
      reportBackgroundError(error, "cancel owner subject profile operations");
    });
    resetOwnerMediaUpload();
  }

  useEffect(() => () => {
    resetOwnerMediaUpload();
  }, []);

  return {
    imageUploadInputRef,
    productUploadInputRef,
    productDetailUploadInputRef,
    lastFrameUploadInputRef,
    videoUploadInputRef,
    bumpUploadRequest: (mode = creationMode) => bumpRequest(uploadRequestRef, mode),
    bumpProductUploadRequest: (mode = creationMode) => bumpRequest(productUploadRequestRef, mode),
    revokeUploadedObjectUrls,
    revokeProductObjectUrl,
    revokeProductObjectUrls,
    cancelProfileOperation,
    cancelAllProfileOperations,
    cancelOwnerProfileOperationsAndReset,
    resetOwnerMediaUpload,
    doUploadImage,
    doUploadProductImage,
    doUploadProductDetailImages,
    doUploadLastFrameImage,
    selectProductAsset,
    selectLastFrameAsset,
    doUploadVideo,
  };
}
