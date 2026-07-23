import type { CreationRecipe, CreationRecipeVersion } from "./api";

type JsonObject = Record<string, unknown>;

export type CreationRecipeTransferSource = "owner" | "public" | "share";

export type CreationRecipeTransferOptions = {
  shareSlug?: string | null;
  source?: CreationRecipeTransferSource;
  savedAt?: number;
};

function objectValue(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as JsonObject
    : {};
}

function cloneJsonValue<T>(value: T): T {
  if (Array.isArray(value)) {
    return value.map((item) => cloneJsonValue(item)) as T;
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as JsonObject).map(([key, item]) => [key, cloneJsonValue(item)]),
    ) as T;
  }
  return value;
}

function positiveInteger(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

function recipeVersion(recipe: CreationRecipe, version?: CreationRecipeVersion | null): CreationRecipeVersion | null {
  if (version?.payload) return version;
  return recipe.version?.payload ? recipe.version : null;
}

function recipeCreationMode(recipe: CreationRecipe, snapshot: JsonObject): string {
  const requested = String(snapshot.creation_mode || "");
  if (["image", "image_edit", "video", "video_edit"].includes(requested)) return requested;
  return recipe.category === "video" ? "video" : "image";
}

function recipePrompt(payload: JsonObject, snapshot: JsonObject, reverseResult: JsonObject): string {
  return String(payload.prompt || snapshot.final_text || reverseResult.final_text || "");
}

function recipeGeneration(payload: JsonObject, snapshot: JsonObject): JsonObject {
  return cloneJsonValue({
    ...objectValue(payload.generation_params),
    ...objectValue(snapshot.generation),
    ...objectValue(payload.generation),
  });
}

export function creationRecipeSharePath(slug: unknown): string {
  const normalized = String(slug || "").trim();
  return normalized ? `/recipes/shared/${encodeURIComponent(normalized)}` : "";
}

export function creationRecipeShareUrl(slug: unknown, origin = ""): string {
  const path = creationRecipeSharePath(slug);
  if (!path) return "";
  const normalizedOrigin = String(origin || "").replace(/\/$/, "");
  return normalizedOrigin ? `${normalizedOrigin}${path}` : path;
}

export function buildCreationRecipeStudioDraft(
  recipe: CreationRecipe,
  version?: CreationRecipeVersion | null,
  options: CreationRecipeTransferOptions = {},
): JsonObject {
  const resolvedVersion = recipeVersion(recipe, version);
  if (!resolvedVersion) throw new Error("该创作配方缺少可恢复的版本");

  const payload = objectValue(resolvedVersion.payload);
  const reverseResult = objectValue(payload.reverse_result);
  const originalSnapshot = cloneJsonValue(objectValue(payload.reverse_snapshot_v3));
  const hasReverseSnapshot = Number(originalSnapshot.version) === 3;
  const generation = recipeGeneration(payload, originalSnapshot);
  const versionNumber = positiveInteger(resolvedVersion.version)
    || positiveInteger(recipe.current_version);
  if (!versionNumber) throw new Error("该创作配方版本无效");

  const shareSlug = String(options.shareSlug || "").trim();
  const creationMode = recipeCreationMode(recipe, originalSnapshot);
  const prompt = recipePrompt(payload, originalSnapshot, reverseResult);
  const negative = String(payload.negative || generation.negative || originalSnapshot.negative || "");
  const structured = cloneJsonValue(objectValue(payload.structured));
  const reverseSnapshot = hasReverseSnapshot
    ? {
        ...originalSnapshot,
        final_text: prompt || String(originalSnapshot.final_text || ""),
        structured: Object.keys(structured).length ? structured : objectValue(originalSnapshot.structured),
        generation,
        creation_recipe_id: recipe.id,
        creation_recipe_version: versionNumber,
        creation_recipe_share_slug: shareSlug || null,
        creation_recipe_source: options.source || (shareSlug ? "share" : "owner"),
      }
    : null;

  if (!prompt.trim() && !Object.keys(structured).length && !reverseSnapshot) {
    throw new Error("该创作配方没有可恢复的提示词或结构化内容");
  }

  return {
    prompt,
    negative,
    structured,
    generation,
    category: recipe.category,
    creationMode,
    creationRecipeId: recipe.id,
    creationRecipeVersion: versionNumber,
    creationRecipeShareSlug: shareSlug || null,
    creationRecipeSource: options.source || (shareSlug ? "share" : "owner"),
    reverse_snapshot_v3: reverseSnapshot,
    savedAt: Number.isSafeInteger(options.savedAt) && Number(options.savedAt) > 0
      ? Number(options.savedAt)
      : Date.now(),
  };
}
