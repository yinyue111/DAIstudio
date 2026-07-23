"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { reportBackgroundError } from "../../lib/errorHandling";
import {
  normalizeVideoWaveform,
  videoTimelineDuration,
  videoTimelineFrameTimes,
} from "./videoSourceTimeline";

const FRAME_SEEK_TIMEOUT_MS = 5000;
const WAVEFORM_BAR_COUNT = 64;
const WAVEFORM_PLAYBACK_RATE = 16;

function effectiveVideoDuration(mediaDuration, configuredDuration) {
  const measured = videoTimelineDuration(mediaDuration);
  const configured = videoTimelineDuration(configuredDuration);
  // Native metadata is authoritative. A persisted duration can be stale after
  // an asset is replaced, and must not silently cut the editable timeline.
  if (measured) return measured;
  return measured || configured;
}

function seekVideo(video, time) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      video.removeEventListener("seeked", onSeeked);
      video.removeEventListener("error", onError);
      if (error) reject(error);
      else resolve();
    };
    const onSeeked = () => finish();
    const onError = () => finish(new Error("视频帧解码失败"));
    const timeout = setTimeout(
      () => finish(new Error("视频帧读取超时")),
      FRAME_SEEK_TIMEOUT_MS,
    );
    video.addEventListener("seeked", onSeeked, { once: true });
    video.addEventListener("error", onError, { once: true });
    try {
      video.currentTime = time;
    } catch (error) {
      finish(error);
    }
  });
}

function canvasSize(video) {
  const width = Math.max(1, Number(video.videoWidth) || 16);
  const height = Math.max(1, Number(video.videoHeight) || 9);
  const targetWidth = 240;
  return {
    width: targetWidth,
    height: Math.max(80, Math.round(targetWidth * height / width)),
  };
}

async function captureVideoFrames(video, duration, runIsCurrent) {
  const times = videoTimelineFrameTimes(duration, 8);
  const canvas = document.createElement("canvas");
  const size = canvasSize(video);
  canvas.width = size.width;
  canvas.height = size.height;
  const context = canvas.getContext("2d", { alpha: false });
  if (!context) throw new Error("当前浏览器无法创建视频帧画布");
  const frames = [];
  for (const time of times) {
    if (!runIsCurrent()) return [];
    await seekVideo(video, time);
    if (!runIsCurrent()) return [];
    context.drawImage(video, 0, 0, canvas.width, canvas.height);
    frames.push({
      id: `frame-${time.toFixed(3)}`,
      time_seconds: time,
      src: canvas.toDataURL("image/jpeg", 0.78),
    });
  }
  return frames;
}

function mediaElementWaveform(src, audioContext, signal) {
  return new Promise((resolve, reject) => {
    const video = document.createElement("video");
    const analyser = audioContext.createAnalyser();
    const gain = audioContext.createGain();
    const samples = Array.from({ length: WAVEFORM_BAR_COUNT }, () => 0);
    const timeDomain = new Float32Array(256);
    let sampleTimer = null;
    let settled = false;
    let sourceNode = null;

    analyser.fftSize = timeDomain.length;
    gain.gain.value = 0;
    video.src = src;
    video.preload = "auto";
    video.playsInline = true;
    video.playbackRate = WAVEFORM_PLAYBACK_RATE;

    const cleanup = () => {
      if (sampleTimer) clearTimeout(sampleTimer);
      signal.removeEventListener("abort", onAbort);
      video.pause();
      video.removeEventListener("ended", onEnded);
      video.removeEventListener("error", onError);
      sourceNode?.disconnect();
      analyser.disconnect();
      gain.disconnect();
      video.removeAttribute("src");
      video.load();
    };
    const finish = (error) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (error) reject(error);
      else resolve(normalizeVideoWaveform(samples, WAVEFORM_BAR_COUNT));
    };
    const onAbort = () => finish(new DOMException("音轨读取已取消", "AbortError"));
    const onEnded = () => finish();
    const onError = () => finish(new Error("视频音轨播放失败"));
    const sample = () => {
      if (settled) return;
      analyser.getFloatTimeDomainData(timeDomain);
      let sum = 0;
      for (const value of timeDomain) sum += value * value;
      const amplitude = Math.sqrt(sum / timeDomain.length);
      const duration = videoTimelineDuration(video.duration);
      const index = duration
        ? Math.min(WAVEFORM_BAR_COUNT - 1, Math.floor(video.currentTime / duration * WAVEFORM_BAR_COUNT))
        : 0;
      samples[index] = Math.max(samples[index], amplitude);
      sampleTimer = setTimeout(sample, 16);
    };

    signal.addEventListener("abort", onAbort, { once: true });
    video.addEventListener("ended", onEnded, { once: true });
    video.addEventListener("error", onError, { once: true });
    try {
      sourceNode = audioContext.createMediaElementSource(video);
      sourceNode.connect(analyser);
      analyser.connect(gain);
      gain.connect(audioContext.destination);
      sample();
      video.play().catch(finish);
    } catch (error) {
      finish(error);
    }
  });
}

export default function useVideoSourceTimelineMedia({ src, configuredDuration }) {
  const videoRef = useRef(null);
  const captureRunRef = useRef(0);
  const waveformAbortRef = useRef(null);
  const [metadata, setMetadata] = useState({
    status: src ? "loading" : "idle",
    duration: 0,
    width: 0,
    height: 0,
    message: "",
  });
  const [thumbnails, setThumbnails] = useState([]);
  const [thumbnailStatus, setThumbnailStatus] = useState(src ? "loading" : "idle");
  const [thumbnailMessage, setThumbnailMessage] = useState("");
  const [waveform, setWaveform] = useState([]);
  const [waveformStatus, setWaveformStatus] = useState("idle");
  const [waveformMessage, setWaveformMessage] = useState("尚未读取音轨。需要波形时可手动提取。");

  useEffect(() => {
    captureRunRef.current += 1;
    waveformAbortRef.current?.abort();
    setMetadata({
      status: src ? "loading" : "idle",
      duration: 0,
      width: 0,
      height: 0,
      message: "",
    });
    setThumbnails([]);
    setThumbnailStatus(src ? "loading" : "idle");
    setThumbnailMessage("");
    setWaveform([]);
    setWaveformStatus("idle");
    setWaveformMessage("尚未读取音轨。需要波形时可手动提取。");
  }, [src]);

  const onLoadedMetadata = useCallback(async (event) => {
    const video = event.currentTarget;
    const duration = effectiveVideoDuration(video.duration, configuredDuration);
    if (!duration) {
      setMetadata({
        status: "error",
        duration: 0,
        width: 0,
        height: 0,
        message: "视频没有可读取的有效时长。",
      });
      setThumbnailStatus("error");
      return;
    }
    setMetadata({
      status: "ready",
      duration,
      width: Number(video.videoWidth) || 0,
      height: Number(video.videoHeight) || 0,
      message: "",
    });
    const runId = captureRunRef.current + 1;
    captureRunRef.current = runId;
    setThumbnailStatus("loading");
    setThumbnailMessage("");
    try {
      const frames = await captureVideoFrames(
        video,
        duration,
        () => captureRunRef.current === runId,
      );
      if (captureRunRef.current !== runId) return;
      if (frames.length === 0) throw new Error("未读取到可用视频帧");
      setThumbnails(frames);
      setThumbnailStatus("ready");
    } catch (error) {
      if (captureRunRef.current !== runId) return;
      reportBackgroundError(error, "capture source video timeline thumbnails");
      setThumbnails([]);
      setThumbnailStatus("error");
      setThumbnailMessage(error?.message || "缩略图提取失败，仍可使用时间刻度编辑。");
    }
  }, [configuredDuration]);

  const onVideoError = useCallback(() => {
    captureRunRef.current += 1;
    setMetadata({
      status: "error",
      duration: 0,
      width: 0,
      height: 0,
      message: "视频元数据加载失败，无法读取输入时间线。",
    });
    setThumbnailStatus("error");
    setThumbnailMessage("视频解码失败。请改用平台支持的 MP4、MOV 或 WebM 文件。");
  }, []);

  const analyzeWaveform = useCallback(async () => {
    if (!src || waveformStatus === "loading") return;
    waveformAbortRef.current?.abort();
    const controller = new AbortController();
    waveformAbortRef.current = controller;
    setWaveform([]);
    setWaveformStatus("loading");
    setWaveformMessage("正在读取音轨…");
    let audioContext = null;
    try {
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      if (!AudioContextClass) throw new Error("当前浏览器不支持音轨解码");
      audioContext = new AudioContextClass();
      await audioContext.resume?.();
      const nextWaveform = await mediaElementWaveform(src, audioContext, controller.signal);
      if (controller.signal.aborted) return;
      const peak = Math.max(...nextWaveform, 0);
      if (nextWaveform.length === 0 || peak <= 0.00001) {
        setWaveformStatus("no_audio");
        setWaveformMessage("未检测到有效音轨；素材可能没有音频或音轨为静音。");
        return;
      }
      setWaveform(nextWaveform.map((value) => value / peak));
      setWaveformStatus("ready");
      setWaveformMessage("已从当前视频音轨提取波形。");
    } catch (error) {
      if (error?.name === "AbortError" || controller.signal.aborted) return;
      reportBackgroundError(error, "analyze source video waveform");
      const noDecodableTrack = ["EncodingError", "NotSupportedError"].includes(error?.name)
        || /decode|encoding|音轨播放失败/i.test(String(error?.message || ""));
      setWaveformStatus(noDecodableTrack ? "no_audio" : "unavailable");
      setWaveformMessage(noDecodableTrack
        ? "未检测到浏览器可解码音轨；素材可能无音轨，或当前音频编码不受支持。"
        : (error?.message || "当前浏览器无法读取该视频音轨。"));
    } finally {
      await audioContext?.close?.().catch(() => {});
      if (waveformAbortRef.current === controller) waveformAbortRef.current = null;
    }
  }, [src, waveformStatus]);

  useEffect(() => () => {
    captureRunRef.current += 1;
    waveformAbortRef.current?.abort();
  }, []);

  return {
    videoRef,
    metadata,
    thumbnails,
    thumbnailStatus,
    thumbnailMessage,
    waveform,
    waveformStatus,
    waveformMessage,
    analyzeWaveform,
    onLoadedMetadata,
    onVideoError,
  };
}
