import { useEffect } from "react";

import { api } from "../lib/api";
import { MODEL_SELECTION_USES } from "../app/studio/StudioModelSelector";
import { normalizeReverseConfig } from "../app/studio/reverseConfig";
import { parseUnifiedTaskKey } from "../lib/unifiedTasks";
import {
  resolveCatalogModelIntent,
  resolveStudioWorkflowPreset,
  studioWorkflowSlug,
} from "../app/studio/workflowPreset";

export default function useStudioDeepLinkBootstrap({
  ownerId,
  cfg,
  workspaces,
  toolWorkflowsEnabled,
  allModelOptions,
  modelOptionsForSelection,
  creationMode,
  cloudDraftLoadedRef,
  workflowBootstrapRef,
  catalogModelBootstrapRef,
  reverseTaskBootstrapRef,
  setCreationMode,
  setWorkspacePatch,
  setRefOpen,
  setStructOpen,
  setMsg,
  notify,
  changeModelSelection,
  openRecentReverseOperation,
}) {
  useEffect(() => {
    if (!ownerId || !cloudDraftLoadedRef.current || typeof window === "undefined") return;
    const task = parseUnifiedTaskKey(new URLSearchParams(window.location.search).get("task"));
    if (!task || task.kind !== "reverse") return;
    const taskKey = `${ownerId}:${task.key}`;
    if (
      reverseTaskBootstrapRef.current === `loading:${taskKey}`
      || reverseTaskBootstrapRef.current === `done:${taskKey}`
    ) return;
    reverseTaskBootstrapRef.current = `loading:${taskKey}`;
    openRecentReverseOperation({ id: task.id })
      .finally(() => {
        reverseTaskBootstrapRef.current = `done:${taskKey}`;
      });
  }, [ownerId, workspaces, openRecentReverseOperation]);

  useEffect(() => {
    if (!ownerId || !cloudDraftLoadedRef.current || typeof window === "undefined") return;
    const slug = studioWorkflowSlug(window.location.search);
    if (!slug || !cfg) return;
    if (!toolWorkflowsEnabled) {
      if (workflowBootstrapRef.current !== `disabled:${slug}`) {
        workflowBootstrapRef.current = `disabled:${slug}`;
        const message = `工作流功能未开放，已忽略「${slug}」预设，当前草稿未改变。`;
        setMsg(message);
        notify.warn(message);
      }
      return;
    }
    const loadingKey = `loading:${slug}`;
    if (
      workflowBootstrapRef.current === loadingKey
      || workflowBootstrapRef.current === `failed:${slug}`
      || workflowBootstrapRef.current.startsWith(`applied:${slug}:`)
    ) return;
    workflowBootstrapRef.current = loadingKey;
    let active = true;

    api.toolCatalogDetail(slug)
      .then((tool) => {
        if (!active) return;
        const resolution = resolveStudioWorkflowPreset(window.location.search, tool);
        if (resolution.status !== "ready" || !resolution.preset) {
          workflowBootstrapRef.current = `failed:${slug}`;
          setMsg(resolution.message);
          notify.warn(resolution.message);
          return;
        }
        const preset = resolution.preset;
        workflowBootstrapRef.current = `applied:${slug}:${preset.versionId}`;
        const nextCategory = preset.creationMode.startsWith("video") ? "video" : "image";
        setCreationMode(preset.creationMode);
        setWorkspacePatch((current) => ({
          reverseConfig: normalizeReverseConfig({
            ...(current.reverseConfig || {}),
            ...(preset.analysisFocus ? { analysis_focus: preset.analysisFocus } : {}),
            ...(preset.outputPurpose ? { output_purpose: preset.outputPurpose } : {}),
          }, { category: nextCategory }),
          ...(preset.referenceRoles ? {
            reverseSources: preset.referenceRoles.map((role) => ({
              asset_url: "",
              source_type: "image",
              role,
            })),
          } : {}),
        }), preset.creationMode);
        setRefOpen(true);
        setStructOpen(true);
        setMsg(preset.message);
        window.requestAnimationFrame(() => {
          document.getElementById("studio-reference-panel")?.scrollIntoView({
            behavior: "smooth",
            block: "center",
          });
        });
      })
      .catch((error) => {
        if (!active) return;
        workflowBootstrapRef.current = `failed:${slug}`;
        const message = error?.status === 404
          ? `工作流「${slug}」不存在、已禁用或没有可用版本，当前草稿未改变。`
          : `工作流「${slug}」加载失败，当前草稿未改变。`;
        setMsg(message);
        notify.warn(message);
      });
    return () => {
      active = false;
      if (workflowBootstrapRef.current === loadingKey) workflowBootstrapRef.current = "";
    };
  }, [ownerId, workspaces, setWorkspacePatch, cfg, toolWorkflowsEnabled]);

  useEffect(() => {
    if (!ownerId || !cfg || typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const modelConfigId = Number(params.get("model_config_id"));
    const modelUse = String(params.get("model_use") || "");
    const requestedMode = String(params.get("studio_mode") || "");
    if (
      !Number.isInteger(modelConfigId)
      || modelConfigId <= 0
      || !MODEL_SELECTION_USES.includes(modelUse)
    ) return;
    const bootstrapKey = `${modelUse}:${modelConfigId}:${requestedMode || "auto"}`;
    if (
      catalogModelBootstrapRef.current === `done:${bootstrapKey}`
      || catalogModelBootstrapRef.current === `failed:${bootstrapKey}`
    ) return;
    const option = allModelOptions[modelUse]?.find((item) => item.id === modelConfigId);
    if (!option) {
      catalogModelBootstrapRef.current = `failed:${bootstrapKey}`;
      const message = "目录中的模型当前不可用，已保留现有模型选择。";
      setMsg(message);
      notify.warn(message);
      return;
    }
    const resolution = resolveCatalogModelIntent(option, requestedMode);
    if (resolution.status !== "ready") {
      catalogModelBootstrapRef.current = `failed:${bootstrapKey}`;
      setMsg(resolution.message);
      notify.warn(resolution.message);
      return;
    }
    if (resolution.creationMode && creationMode !== resolution.creationMode) {
      catalogModelBootstrapRef.current = `mode:${bootstrapKey}`;
      setCreationMode(resolution.creationMode);
      return;
    }
    const compatibleOption = modelOptionsForSelection[modelUse]
      ?.find((item) => item.id === modelConfigId);
    if (!compatibleOption) {
      catalogModelBootstrapRef.current = `failed:${bootstrapKey}`;
      const message = "该模型与当前 Studio 素材和能力约束不兼容，已保留现有模型选择。";
      setMsg(message);
      notify.warn(message);
      return;
    }
    changeModelSelection(modelUse, modelConfigId);
    catalogModelBootstrapRef.current = `done:${bootstrapKey}`;
    setMsg(`已选择 ${option.display_name || option.model_id}。`);
  }, [ownerId, cfg, creationMode]);
}
