"use client";

import { useRef } from "react";
import { api } from "../lib/api";
import { assetSignature, isRequestTimeoutError } from "../app/studio/helpers";
import {
  clearPendingReverseRequest,
  generateReverseClientRequestId,
} from "../app/studio/generationRequestId";

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

export default function useMediaUpload({
  cfg,
  creationMode,
  category,
  isEditMode,
  subjectMode = "",
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
}) {
  const uploadRequestRef = useRef({});
  const productUploadRequestRef = useRef({});
  const imageUploadInputRef = useRef(null);
  const productUploadInputRef = useRef(null);
  const videoUploadInputRef = useRef(null);
  const objectUrlsByModeRef = useRef({});
  const productObjectUrlsRef = useRef({});
  const pendingProfileReverseRequestRef = useRef(null);

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

  function assignUploadedReferenceAsset(mode, displayAsset) {
    setWorkspacePatch((current) => ({
      assets: [displayAsset, ...current.assets],
      variationSource: null,
    }), mode);
    selectAssetForMode(displayAsset, mode);
    setRefOpen(true);
  }

  async function prefetchProductProfile(mode, asset, uploadReqId) {
    if (!isEditMode || !asset?.url || !["product", "portrait"].includes(subjectMode)) return;
    const signature = assetSignature(asset);
    setWorkspacePatch({ productProfiling: true, productProfile: null, productProfileSource: "" }, mode);
    const profileRequestId = generateReverseClientRequestId(
      pendingProfileReverseRequestRef,
      `${mode}-profile-prefetch`,
      JSON.stringify({
        url: asset.url,
        sourceType: "image",
        target: "product_profile",
        subjectMode,
      }),
    );
    try {
      const profile = await api.reverse(
        asset.url,
        "product_profile",
        null,
        "image",
        null,
        profileRequestId,
      );
      clearPendingReverseRequest(pendingProfileReverseRequestRef, profileRequestId);
      if (!isRequestCurrent(productUploadRequestRef, mode, uploadReqId)) return;
      setWorkspacePatch({
        productProfile: {
          structured: profile?.structured || {},
          final_text: profile?.final_text || "",
        },
        productProfileSource: signature,
        productProfiling: false,
      }, mode);
    } catch (e) {
      if (!isRequestTimeoutError(e)) {
        clearPendingReverseRequest(pendingProfileReverseRequestRef, profileRequestId);
      }
      if (!isRequestCurrent(productUploadRequestRef, mode, uploadReqId)) return;
      setWorkspacePatch({
        productProfile: null,
        productProfileSource: "",
        productProfiling: false,
      }, mode);
      if (isModeVisible(mode)) setMsg(`主体档案识别失败，可重新上传更清晰图片后再试：${e.message}`);
    }
  }

  async function doUploadImage(file) {
    if (!file || uploading) return;
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
    setWorkspacePatch({ uploading: true }, mode);
    try {
      const uploaded = await api.uploadImage(file);
      if (!isRefVersionCurrent(mode, refVersion)) return;
      const { previewUrl, asset } = buildDisplayAsset(uploaded, file);
      rememberUploadedObjectUrl(mode, previewUrl);
      assignUploadedReferenceAsset(mode, asset);
    } catch (e) {
      if (isRefVersionCurrent(mode, refVersion) && isModeVisible(mode)) setMsg(e.message);
    } finally {
      if (isRequestCurrent(uploadRequestRef, mode, reqId)) {
        setWorkspacePatch({ uploading: false }, mode);
        resetInput(imageUploadInputRef);
      }
    }
  }

  async function doUploadProductImage(file) {
    if (!file || uploading) return;
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
    const reqId = bumpRequest(uploadRequestRef, mode);
    const productReqId = bumpRequest(productUploadRequestRef, mode);
    setMsg("");
    setWorkspacePatch({ uploading: true }, mode);
    try {
      const uploaded = await api.uploadImage(file);
      if (!isRequestCurrent(productUploadRequestRef, mode, productReqId)) return;
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
        productProfiling: false,
        variationSource: null,
      }, mode);
      setRefOpen(true);
      prefetchProductProfile(mode, asset, productReqId);
    } catch (e) {
      if (isModeVisible(mode)) setMsg(e.message);
    } finally {
      if (isRequestCurrent(uploadRequestRef, mode, reqId)) {
        setWorkspacePatch({ uploading: false }, mode);
        resetInput(productUploadInputRef);
      }
    }
  }

  async function doUploadVideo(file) {
    if (!file || uploading) return;
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
    setWorkspacePatch({ uploading: true }, targetMode);
    try {
      const uploaded = await api.uploadVideo(file);
      if (!isRefVersionCurrent(targetMode, refVersion)) return;
      const { previewUrl, asset } = buildDisplayAsset(uploaded, file, { thumb: uploaded.thumb });
      rememberUploadedObjectUrl(targetMode, previewUrl);
      assignUploadedReferenceAsset(targetMode, asset);
    } catch (e) {
      if (isRefVersionCurrent(targetMode, refVersion) && isModeVisible(targetMode)) setMsg(e.message);
    } finally {
      if (isRequestCurrent(uploadRequestRef, targetMode, reqId)) {
        setWorkspacePatch({ uploading: false }, targetMode);
        resetInput(videoUploadInputRef);
      }
    }
  }

  return {
    imageUploadInputRef,
    productUploadInputRef,
    videoUploadInputRef,
    bumpUploadRequest: (mode = creationMode) => bumpRequest(uploadRequestRef, mode),
    bumpProductUploadRequest: (mode = creationMode) => bumpRequest(productUploadRequestRef, mode),
    revokeUploadedObjectUrls,
    revokeProductObjectUrl,
    revokeProductObjectUrls,
    doUploadImage,
    doUploadProductImage,
    doUploadVideo,
  };
}
