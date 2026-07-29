import { useEffect, useRef, useState } from "react";

import {
  filterModelOptions,
  MODEL_SELECTION_USES,
  normalizeModelOptions,
  readModelSelections,
  resolveModelConfigId,
  writeModelSelections,
} from "../app/studio/StudioModelSelector";
import { modelEnabledForConfig } from "../app/studio/viewModel";

const MODEL_USE_LABELS = {
  vision: "反推",
  image: "图片",
  video: "视频",
  prompt: "提示词优化",
};

export default function useStudioModelSelection({
  cfg,
  category,
  creationMode,
  selected,
  productAsset,
  subjectMode,
  ownerId,
  notify,
}) {
  const [modelSelections, setModelSelections] = useState({
    vision: null,
    image: null,
    video: null,
    prompt: null,
  });
  const modelSelectionsRef = useRef(modelSelections);
  const modelSelectionOwnerRef = useRef("");

  const allModelOptions = normalizeModelOptions(cfg);
  // Keep the user's chosen generation model visible when references make it
  // incompatible. Submission preflight provides the actionable compatibility
  // error; silently filtering here would switch models and could start a paid
  // subject-profile analysis under a model the user did not choose.
  const generationModelOptions = allModelOptions[category];
  const visionModelOptions = filterModelOptions(allModelOptions.vision, {
    use: "vision",
    creationMode,
    selected,
    productAsset,
    subjectMode,
  });
  const promptModelOptions = filterModelOptions(allModelOptions.prompt, {
    use: "prompt",
    creationMode,
    selected,
    productAsset,
  });
  const modelOptionsForSelection = {
    ...allModelOptions,
    [category]: generationModelOptions,
    vision: visionModelOptions,
    prompt: promptModelOptions,
  };
  const selectedGenerationModelConfigId = resolveModelConfigId(
    generationModelOptions,
    modelSelections[category],
  );
  const selectedVisionModelConfigId = resolveModelConfigId(
    visionModelOptions,
    modelSelections.vision,
  );
  const selectedPromptModelConfigId = resolveModelConfigId(
    promptModelOptions,
    modelSelections.prompt,
  );
  const effectiveModelSelections = {
    ...modelSelections,
    [category]: selectedGenerationModelConfigId,
    vision: selectedVisionModelConfigId,
    prompt: selectedPromptModelConfigId,
  };
  const selectedGenerationModel = generationModelOptions.find(
    (option) => option.id === selectedGenerationModelConfigId,
  ) || null;
  const selectedPromptModel = promptModelOptions.find(
    (option) => option.id === selectedPromptModelConfigId,
  ) || null;

  modelSelectionsRef.current = modelSelections;

  useEffect(() => {
    const owner = String(ownerId || "");
    if (!owner || !cfg) return;
    let preferred = modelSelectionsRef.current;
    if (modelSelectionOwnerRef.current !== owner) {
      preferred = readModelSelections(window.localStorage, owner);
      modelSelectionOwnerRef.current = owner;
    }
    const next = Object.fromEntries(MODEL_SELECTION_USES.map((use) => [
      use,
      resolveModelConfigId(modelOptionsForSelection[use], preferred?.[use]),
    ]));
    const fallbackUses = MODEL_SELECTION_USES.filter((use) => (
      preferred?.[use]
      && next[use] !== Number(preferred[use])
      && allModelOptions[use].length > 0
    ));
    modelSelectionsRef.current = next;
    setModelSelections((current) => (
      JSON.stringify(current) === JSON.stringify(next) ? current : next
    ));
    writeModelSelections(window.localStorage, owner, next);
    if (fallbackUses.length > 0) {
      const labels = fallbackUses.map((use) => MODEL_USE_LABELS[use]).join("、");
      notify.warn(`${labels}已选模型不再可用，已切换为默认模型。`);
    }
  }, [
    ownerId,
    cfg,
    creationMode,
    category,
    selected?.type,
    selected?.id,
    selected?.url,
    productAsset?.id,
    productAsset?.url,
  ]);

  function applyModelSelectionPreferences(preferred, preferredOwnerId = ownerId) {
    const next = Object.fromEntries(MODEL_SELECTION_USES.map((use) => [
      use,
      resolveModelConfigId(modelOptionsForSelection[use], preferred?.[use]),
    ]));
    modelSelectionsRef.current = next;
    setModelSelections(next);
    if (typeof window !== "undefined" && preferredOwnerId) {
      writeModelSelections(window.localStorage, String(preferredOwnerId), next);
    }
    return next;
  }

  function selectModel(use, modelConfigId) {
    const next = {
      ...effectiveModelSelections,
      [use]: resolveModelConfigId(modelOptionsForSelection[use], modelConfigId),
    };
    modelSelectionsRef.current = next;
    setModelSelections(next);
    if (typeof window !== "undefined" && ownerId) {
      writeModelSelections(window.localStorage, String(ownerId), next);
    }
    return next;
  }

  function modelEnabled(kind) {
    const use = String(kind || "").startsWith("video") ? "video" : "image";
    if (cfg?.model_options) {
      return filterModelOptions(allModelOptions[use], {
        use,
        creationMode: kind,
        selected: kind === creationMode ? selected : null,
        productAsset: kind === creationMode ? productAsset : null,
        subjectMode: kind === creationMode ? subjectMode : "general",
      }).length > 0;
    }
    return modelEnabledForConfig(cfg, kind);
  }

  return {
    allModelOptions,
    generationModelOptions,
    visionModelOptions,
    promptModelOptions,
    modelOptionsForSelection,
    selectedGenerationModelConfigId,
    selectedVisionModelConfigId,
    selectedPromptModelConfigId,
    effectiveModelSelections,
    selectedGenerationModel,
    selectedPromptModel,
    modelSelectionsRef,
    applyModelSelectionPreferences,
    selectModel,
    modelEnabled,
  };
}
