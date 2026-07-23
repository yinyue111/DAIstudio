"use client";

import { useState } from "react";
import { DEFAULT_REVERSE_CONFIG } from "../app/studio/reverseConfig";

export function createWorkspaceState() {
  return {
    prompt: "",
    negative: "",
    imageEditProductMode: false,
    editSubjectMode: "general",
    ratio: "1:1",
    imageQuality: "1k",
    n: 1,
    seed: "",
    vDuration: 5,
    vResolution: "720p",
    productVideoTemplate: "prompt_driven",
    editMaskMode: "protect_subject",
    productPixelLockMode: "auto",
    videoAnalysisPreset: "standard",
    url: "",
    appliedUrl: "",
    parsing: false,
    uploading: false,
    reversing: false,
    reverseOperation: null,
    profileOperation: null,
    assets: [],
    selected: null,
    lastFrameAsset: null,
    productAsset: null,
    productDetailAssets: [],
    productProfile: null,
    productProfileSource: "",
    portraitProfile: null,
    portraitProfileSource: "",
    productProfiling: false,
    subjectProtection: null,
    subjectProtectionLoading: false,
    subjectProtectionSource: "",
    variationSource: null,
    structured: {},
    structuredBaseline: {},
    structuredDirty: false,
    structuredSource: "",
    reverseVideoAnalysis: null,
    reverseSources: [],
    reverseConfig: { ...DEFAULT_REVERSE_CONFIG, source_ranges: [], custom_keyframes: [] },
    pendingReverseResult: null,
    reverseResultTab: "draft",
    reverseResultSchemaVersion: "",
    reverseAppliedVersion: null,
    reverseAppliedRevisionId: null,
    reverseUndoSnapshot: null,
    reverseApplyConflict: null,
    reverseResultRevisions: [],
    reverseFeedback: null,
    batchReverseAssets: [],
    creationRecipeId: null,
    creationRecipeVersion: null,
    creationRecipeShareSlug: "",
    creationRecipeSource: "",
    promptSourceSignature: "",
    negativeTouched: false,
    promptDirty: false,
  };
}

export function createModeWorkspaces(modes) {
  return Object.fromEntries(modes.map(({ key }) => [key, createWorkspaceState()]));
}

export default function useStudioWorkspaceState({ creationMode, modes }) {
  const [workspaces, setWorkspaces] = useState(() => createModeWorkspaces(modes));
  const workspace = workspaces[creationMode] || createWorkspaceState();

  function updateWorkspaceField(field, valueOrUpdater, mode = creationMode) {
    setWorkspaces((prev) => {
      const current = prev[mode] || createWorkspaceState();
      const nextValue = typeof valueOrUpdater === "function"
        ? valueOrUpdater(current[field])
        : valueOrUpdater;
      return {
        ...prev,
        [mode]: {
          ...current,
          [field]: nextValue,
        },
      };
    });
  }

  function setWorkspacePatch(patchOrUpdater, mode = creationMode) {
    setWorkspaces((prev) => {
      const current = prev[mode] || createWorkspaceState();
      const patch = typeof patchOrUpdater === "function" ? patchOrUpdater(current) : patchOrUpdater;
      return {
        ...prev,
        [mode]: {
          ...current,
          ...patch,
        },
      };
    });
  }

  const setPrompt = (value) => updateWorkspaceField("prompt", value);
  const setNegative = (value) => updateWorkspaceField("negative", value);
  const setEditSubjectMode = (value) => {
    updateWorkspaceField("editSubjectMode", value);
    if (creationMode === "image_edit") updateWorkspaceField("imageEditProductMode", value !== "general");
  };
  const setRatio = (value, mode) => updateWorkspaceField("ratio", value, mode);
  const setImageQuality = (value) => updateWorkspaceField("imageQuality", value);
  const setN = (value) => updateWorkspaceField("n", value);
  const setSeed = (value) => updateWorkspaceField("seed", value);
  const setVDuration = (value) => updateWorkspaceField("vDuration", value);
  const setVResolution = (value) => updateWorkspaceField("vResolution", value);
  const setProductVideoTemplate = (value) => updateWorkspaceField("productVideoTemplate", value);
  const setEditMaskMode = (value) => updateWorkspaceField("editMaskMode", value);
  const setProductPixelLockMode = (value) => updateWorkspaceField("productPixelLockMode", value);
  const setVideoAnalysisPreset = (value) => updateWorkspaceField("videoAnalysisPreset", value);
  const setReverseConfig = (value) => updateWorkspaceField("reverseConfig", value);
  const setUrl = (value) => updateWorkspaceField("url", value);
  const setStructured = (value) => updateWorkspaceField("structured", value);
  const setNegativeTouched = (value) => updateWorkspaceField("negativeTouched", value);
  const setPromptDirty = (value) => {
    setWorkspaces((prev) => {
      const current = prev[creationMode] || createWorkspaceState();
      const nextValue = typeof value === "function" ? value(current.promptDirty) : value;
      return {
        ...prev,
        [creationMode]: {
          ...current,
          promptDirty: nextValue,
          ...(nextValue ? { promptSourceSignature: "" } : {}),
        },
      };
    });
  };

  return {
    workspaces,
    setWorkspaces,
    workspace,
    setWorkspacePatch,
    setPrompt,
    setNegative,
    setEditSubjectMode,
    setRatio,
    setImageQuality,
    setN,
    setSeed,
    setVDuration,
    setVResolution,
    setProductVideoTemplate,
    setEditMaskMode,
    setProductPixelLockMode,
    setVideoAnalysisPreset,
    setReverseConfig,
    setUrl,
    setStructured,
    setNegativeTouched,
    setPromptDirty,
  };
}
