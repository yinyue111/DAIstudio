export type StudioCreationMode = "image" | "image_edit" | "video" | "video_edit";

export type StudioWorkflowPreset = {
  key: string;
  versionId: number;
  version: number;
  creationMode: StudioCreationMode;
  analysisFocus?: string;
  outputPurpose?: string;
  referenceRoles?: string[];
  message: string;
};

export type StudioWorkflowResolution = {
  status:
    | "absent"
    | "ready"
    | "unknown"
    | "disabled"
    | "unsupported_renderer"
    | "no_active_version"
    | "invalid_workflow";
  slug: string;
  message: string;
  preset?: StudioWorkflowPreset;
};

export type CatalogModelIntentResolution = {
  status: "ready" | "invalid_model" | "unsupported_capability" | "incompatible_mode";
  message: string;
  modelConfigId?: number;
  modelUse?: "vision" | "image" | "video" | "prompt";
  creationMode?: StudioCreationMode;
  matchedCapability?: string;
};

const CREATION_MODES = new Set<StudioCreationMode>([
  "image",
  "image_edit",
  "video",
  "video_edit",
]);
const MODEL_USES = new Set(["vision", "image", "video", "prompt"]);

function cleanPositiveInteger(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

function cleanString(value: unknown): string {
  return String(value || "").trim();
}

function workflowFailure(
  status: StudioWorkflowResolution["status"],
  slug: string,
  message: string,
): StudioWorkflowResolution {
  return { status, slug, message: `${message}当前草稿未改变。` };
}

export function studioWorkflowSlug(search: string): string {
  const query = search.startsWith("?") ? search.slice(1) : search;
  return cleanString(new URLSearchParams(query).get("workflow")).toLowerCase();
}

export function resolveStudioWorkflowPreset(
  search: string,
  tool: Record<string, any> | null | undefined,
): StudioWorkflowResolution {
  const slug = studioWorkflowSlug(search);
  if (!slug) return { status: "absent", slug: "", message: "" };
  if (!tool || cleanString(tool.slug).toLowerCase() !== slug) {
    return workflowFailure("unknown", slug, `工作流「${slug}」不存在或已下架。`);
  }
  if (tool.enabled === false) {
    return workflowFailure("disabled", slug, `工作流「${tool.name || slug}」已禁用。`);
  }
  if (cleanString(tool.renderer).toLowerCase() !== "studio") {
    return workflowFailure(
      "unsupported_renderer",
      slug,
      `工作流「${tool.name || slug}」使用了当前客户端不支持的渲染器。`,
    );
  }

  const activeVersion = tool.active_version;
  const versionId = cleanPositiveInteger(activeVersion?.id);
  const version = cleanPositiveInteger(activeVersion?.version);
  if (!activeVersion || activeVersion.is_active === false || !versionId || !version) {
    return workflowFailure("no_active_version", slug, `工作流「${tool.name || slug}」没有可用的活动版本。`);
  }

  const workflow = activeVersion.workflow;
  const workflowType = cleanString(workflow?.type);
  const studioPreset = workflowType === "studio_preset"
    ? workflow
    : workflowType === "workflow.v1"
      ? workflow?.studio_preset
      : null;
  const creationMode = cleanString(studioPreset?.creation_mode) as StudioCreationMode;
  if (
    !workflow
    || typeof workflow !== "object"
    || Array.isArray(workflow)
    || !studioPreset
    || typeof studioPreset !== "object"
    || Array.isArray(studioPreset)
    || !CREATION_MODES.has(creationMode)
  ) {
    return workflowFailure(
      "invalid_workflow",
      slug,
      `工作流「${tool.name || slug}」的活动版本无法由 Studio 执行。`,
    );
  }

  const referenceRoles = Array.isArray(studioPreset.reference_roles)
    ? studioPreset.reference_roles.map(cleanString).filter(Boolean).slice(0, 20)
    : undefined;
  const isVideo = creationMode.startsWith("video");
  const analysisFocus = cleanString(studioPreset.analysis_focus) || undefined;
  const outputPurpose = cleanString(studioPreset.output_purpose)
    || (studioPreset.reverse === true ? (isVideo ? "storyboard" : "generation") : "");
  const defaultMessage = studioPreset.reverse === true
    ? `已进入${tool.name || (isVideo ? "视频反推" : "图片反推")}，请添加素材并设置分析范围。`
    : `已进入${tool.name || slug}，请继续配置创作参数。`;

  return {
    status: "ready",
    slug,
    message: "",
    preset: {
      key: slug,
      versionId,
      version,
      creationMode,
      ...(analysisFocus ? { analysisFocus } : {}),
      ...(outputPurpose ? { outputPurpose } : {}),
      ...(referenceRoles?.length ? { referenceRoles } : {}),
      message: cleanString(studioPreset.message) || defaultMessage,
    },
  };
}

function capabilityIsTrue(capabilities: unknown, key: string): boolean {
  return Boolean(
    capabilities
    && typeof capabilities === "object"
    && !Array.isArray(capabilities)
    && (capabilities as Record<string, unknown>)[key] === true,
  );
}

function modelFailure(
  status: CatalogModelIntentResolution["status"],
  message: string,
): CatalogModelIntentResolution {
  return { status, message };
}

export function resolveCatalogModelIntent(
  model: Record<string, any> | null | undefined,
  requestedMode: string = "",
): CatalogModelIntentResolution {
  const modelConfigId = cleanPositiveInteger(model?.id ?? model?.model_config_id);
  const modelUse = cleanString(model?.use) as CatalogModelIntentResolution["modelUse"];
  const mode = cleanString(requestedMode) as StudioCreationMode;
  if (!modelConfigId || !MODEL_USES.has(modelUse || "")) {
    return modelFailure("invalid_model", "模型目录参数无效，未修改当前模型。");
  }
  if (mode && !CREATION_MODES.has(mode)) {
    return modelFailure("incompatible_mode", "目标创作模式无效，未修改当前模型。");
  }

  const capabilities = model?.capabilities;
  const candidates: Array<{ mode?: StudioCreationMode; capability: string }> = modelUse === "image"
    ? [
        { mode: "image", capability: "text_to_image" },
        { mode: "image_edit", capability: "image_to_image" },
      ]
    : modelUse === "video"
      ? [
          { mode: "video", capability: "text_to_video" },
          { mode: "video", capability: "image_to_video" },
          { mode: "video", capability: "video_reference" },
          { mode: "video", capability: "video_edit" },
          { mode: "video", capability: "video_to_video" },
          { mode: "video", capability: "first_last_frame" },
        ]
      : modelUse === "vision"
        ? [
            { mode: "image_edit", capability: "image_analysis" },
            { mode: "video", capability: "video_analysis" },
          ]
        : [{ capability: "prompt_optimization" }];
  const compatible = candidates.filter((candidate) => (
    (!mode || candidate.mode === mode) && capabilityIsTrue(capabilities, candidate.capability)
  ));
  const selected = compatible[0];
  if (!selected) {
    const hasAnyDeclaredCapability = candidates.some((candidate) => (
      capabilityIsTrue(capabilities, candidate.capability)
    ));
    return modelFailure(
      mode && hasAnyDeclaredCapability ? "incompatible_mode" : "unsupported_capability",
      mode && hasAnyDeclaredCapability
        ? "该模型不支持链接指定的创作模式，未修改当前模型。"
        : "该模型未明确声明可直达的 Studio 能力，未修改当前模型。",
    );
  }
  return {
    status: "ready",
    message: "",
    modelConfigId,
    modelUse,
    ...(selected.mode ? { creationMode: selected.mode } : {}),
    matchedCapability: selected.capability,
  };
}

export function catalogModelStudioHref(
  model: Record<string, any> | null | undefined,
  requestedMode: string = "",
): CatalogModelIntentResolution & { href?: string } {
  const resolution = resolveCatalogModelIntent(model, requestedMode);
  if (resolution.status !== "ready") return resolution;
  const params = new URLSearchParams({
    model_config_id: String(resolution.modelConfigId),
    model_use: String(resolution.modelUse),
  });
  if (resolution.creationMode) params.set("studio_mode", resolution.creationMode);
  return { ...resolution, href: `/?${params.toString()}` };
}
