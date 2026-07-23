"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import {
  AlertTriangle,
  ArrowRight,
  Check,
  Clock3,
  FileText,
  GitBranch,
  ImageIcon,
  LogIn,
  Play,
  RefreshCw,
  ShieldCheck,
  Video,
} from "lucide-react";
import Nav from "../../../../components/Nav";
import { STUDIO_DRAFT_PROMPT_KEY } from "../../../../components/PromptLibraryBrowser";
import { useToast } from "../../../../components/ToastProvider";
import { api, loginPath } from "../../../../lib/api";
import {
  buildCreationRecipeStudioDraft,
  creationRecipeSharePath,
} from "../../../../lib/creationRecipeTransfer";
import { formatLocalDateTime } from "../../../../lib/datetime";
import { saveStudioUserDraft } from "../../../../lib/studioSession";

function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function recipePreview(recipe) {
  const payload = objectValue(recipe?.version?.payload);
  const snapshot = objectValue(payload.reverse_snapshot_v3);
  const reverseResult = objectValue(payload.reverse_result);
  return {
    payload,
    prompt: String(payload.prompt || snapshot.final_text || reverseResult.final_text || ""),
    negative: String(payload.negative || ""),
    structured: objectValue(payload.structured),
    generation: {
      ...objectValue(payload.generation_params),
      ...objectValue(snapshot.generation),
      ...objectValue(payload.generation),
    },
    access: objectValue(payload.public_asset_access),
  };
}

function clientEventId(prefix) {
  return `${prefix}-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
}

function SharedRecipeLoading() {
  return (
    <main className="mx-auto max-w-5xl px-4 py-8 sm:px-6" aria-busy="true" aria-label="正在加载分享配方">
      <div className="skeleton h-5 w-32" />
      <div className="skeleton mt-5 h-12 max-w-xl" />
      <div className="mt-8 grid gap-5 lg:grid-cols-[minmax(0,1fr)_18rem]">
        <div className="skeleton h-96" />
        <div className="skeleton h-64" />
      </div>
    </main>
  );
}

function SharedRecipeError({ message, onRetry }) {
  return (
    <main className="mx-auto flex min-h-[70vh] max-w-xl items-center px-4 py-10 sm:px-6">
      <section className="panel w-full p-6 text-center" role="alert">
        <AlertTriangle className="mx-auto text-warn" size={34} aria-hidden="true" />
        <h1 className="mt-4 text-xl font-bold text-snow">分享链接不可用</h1>
        <p className="mt-2 text-sm leading-relaxed text-fog">{message || "配方分享不存在、已撤销或已过期。"}</p>
        <div className="mt-5 flex flex-wrap justify-center gap-2">
          <button type="button" className="btn-secondary btn-sm min-h-10" onClick={onRetry}>
            <RefreshCw size={15} aria-hidden="true" /> 重新加载
          </button>
          <Link href="/" className="btn-primary btn-sm min-h-10">进入创作台</Link>
        </div>
      </section>
    </main>
  );
}

export default function SharedCreationRecipePage() {
  const params = useParams();
  const router = useRouter();
  const notify = useToast();
  const slug = Array.isArray(params?.slug) ? params.slug[0] : String(params?.slug || "");
  const sharePath = creationRecipeSharePath(slug);
  const [me, setMe] = useState(null);
  const [shared, setShared] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [derivedRecipe, setDerivedRecipe] = useState(null);

  async function load() {
    if (!slug) {
      setError("配方分享地址无效。");
      setLoading(false);
      return;
    }
    setLoading(true);
    setError("");
    try {
      const [result, currentUser] = await Promise.all([
        api.sharedCreationRecipe(slug),
        api.me({ redirectOn401: false }).catch(() => null),
      ]);
      setShared(result);
      setMe(currentUser);
    } catch (cause) {
      setShared(null);
      setError(cause?.status === 404
        ? "配方分享不存在、已撤销或已过期。"
        : cause?.message || "分享配方加载失败，请稍后重试。");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, [slug]); // eslint-disable-line react-hooks/exhaustive-deps

  const recipe = shared?.recipe || null;
  const share = shared?.share || null;
  const preview = useMemo(() => recipePreview(recipe), [recipe]);

  function requireLogin() {
    if (me) return false;
    router.push(loginPath(sharePath));
    return true;
  }

  async function useRecipe() {
    if (requireLogin() || !recipe || !share) return;
    setBusy("apply");
    setError("");
    try {
      await api.recordCreationRecipeUsage(recipe.id, {
        event_type: "apply",
        version: share.version,
        share_slug: slug,
        client_event_id: clientEventId("recipe-share-apply"),
        context: { entry: "shared_recipe" },
      });
      const draft = buildCreationRecipeStudioDraft(recipe, recipe.version, {
        shareSlug: slug,
        source: "share",
      });
      const saved = saveStudioUserDraft(window.localStorage, STUDIO_DRAFT_PROMPT_KEY, me.id, draft);
      if (!saved) throw new Error("无法保存当前用户的创作配方草稿");
      router.push("/");
    } catch (cause) {
      const message = cause?.message || "应用分享配方失败";
      setError(message);
      notify.error(message);
    } finally {
      setBusy("");
    }
  }

  async function deriveRecipe() {
    if (requireLogin() || !recipe || !share || derivedRecipe) return;
    setBusy("clone");
    setError("");
    try {
      const cloned = await api.cloneCreationRecipe(recipe.id, {
        version: share.version,
        share_slug: slug,
      });
      setDerivedRecipe(cloned);
      notify.success("已派生到我的创作配方");
    } catch (cause) {
      const message = cause?.message || "派生创作配方失败";
      setError(message);
      notify.error(message);
    } finally {
      setBusy("");
    }
  }

  if (loading) return <><Nav me={me} /><SharedRecipeLoading /></>;
  if (!shared || error && !recipe) return <><Nav me={me} /><SharedRecipeError message={error} onRetry={load} /></>;

  const CategoryIcon = recipe.category === "video" ? Video : ImageIcon;
  const expiresAt = share.expires_at ? formatLocalDateTime(share.expires_at) : "长期有效";
  const structuredEntries = Object.entries(preview.structured);
  const generationEntries = Object.entries(preview.generation)
    .filter(([, value]) => ["string", "number", "boolean"].includes(typeof value));

  return (
    <div className="min-h-screen">
      <Nav me={me} />
      <main className="mx-auto max-w-5xl px-4 py-8 sm:px-6">
        <div className="flex flex-wrap items-center gap-2 text-xs text-fog">
          <span className="inline-flex items-center gap-1.5 text-ok"><ShieldCheck size={14} aria-hidden="true" /> 安全分享</span>
          <span>v{share.version}</span>
          <span className="inline-flex items-center gap-1"><Clock3 size={13} aria-hidden="true" /> {expiresAt}</span>
        </div>
        <div className="mt-4 flex items-start gap-3">
          <span className="flex h-11 w-11 flex-none items-center justify-center rounded-xl border border-line bg-white/5 text-mist">
            <CategoryIcon size={21} aria-hidden="true" />
          </span>
          <div className="min-w-0">
            <h1 className="break-words text-2xl font-bold text-snow sm:text-3xl">{recipe.title}</h1>
            <p className="mt-1 text-sm text-fog">{recipe.category === "video" ? "视频创作配方" : "图片创作配方"} · 分享版本只包含可公开复用的内容</p>
          </div>
        </div>

        <div className="mt-7 grid gap-5 lg:grid-cols-[minmax(0,1fr)_18rem] lg:items-start">
          <div className="space-y-5">
            <section className="border-y border-line py-5" aria-labelledby="shared-recipe-prompt">
              <h2 id="shared-recipe-prompt" className="flex items-center gap-2 text-sm font-semibold text-snow">
                <FileText size={16} aria-hidden="true" /> 提示词
              </h2>
              <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-7 text-mist">
                {preview.prompt || "该配方以结构化参数为主，未公开单独的提示词文本。"}
              </p>
              {preview.negative && (
                <div className="mt-4 border-t border-line pt-4">
                  <h3 className="text-xs font-medium text-fog">负向约束</h3>
                  <p className="mt-2 whitespace-pre-wrap break-words text-xs leading-6 text-mist">{preview.negative}</p>
                </div>
              )}
            </section>

            {structuredEntries.length > 0 && (
              <section aria-labelledby="shared-recipe-structure">
                <h2 id="shared-recipe-structure" className="text-sm font-semibold text-snow">结构化创作参数</h2>
                <dl className="mt-3 divide-y divide-line border-y border-line">
                  {structuredEntries.map(([key, value]) => (
                    <div key={key} className="grid gap-1 py-3 sm:grid-cols-[9rem_minmax(0,1fr)] sm:gap-4">
                      <dt className="break-words text-xs font-medium text-fog">{key}</dt>
                      <dd className="whitespace-pre-wrap break-words text-xs leading-6 text-mist">
                        {typeof value === "string" ? value : JSON.stringify(value, null, 2)}
                      </dd>
                    </div>
                  ))}
                </dl>
              </section>
            )}
          </div>

          <aside className="panel p-4 lg:sticky lg:top-24" aria-label="分享配方操作">
            <h2 className="text-sm font-semibold text-snow">使用这个配方</h2>
            <p className="mt-1 text-xs leading-5 text-fog">直接使用会恢复分享版本；派生会保存一份可独立编辑的私有副本。</p>
            <div className="mt-4 grid gap-2">
              <button type="button" className="btn-primary min-h-11 w-full" onClick={useRecipe} disabled={Boolean(busy)}>
                {me ? <Play size={16} aria-hidden="true" /> : <LogIn size={16} aria-hidden="true" />}
                {busy === "apply" ? "正在恢复…" : me ? "在 Studio 使用" : "登录后使用"}
              </button>
              <button type="button" className="btn-secondary min-h-11 w-full" onClick={deriveRecipe} disabled={Boolean(busy) || Boolean(derivedRecipe)}>
                {derivedRecipe ? <Check size={16} aria-hidden="true" /> : <GitBranch size={16} aria-hidden="true" />}
                {busy === "clone" ? "正在派生…" : derivedRecipe ? "已派生到我的配方" : me ? "派生到我的配方" : "登录后派生"}
              </button>
              {derivedRecipe && (
                <Link href="/prompts" className="btn-ghost btn-sm min-h-10 w-full">
                  查看我的配方 <ArrowRight size={14} aria-hidden="true" />
                </Link>
              )}
            </div>

            {generationEntries.length > 0 && (
              <div className="mt-4 border-t border-line pt-4">
                <h3 className="text-xs font-medium text-mist">生成参数</h3>
                <dl className="mt-2 space-y-2 text-xs">
                  {generationEntries.slice(0, 8).map(([key, value]) => (
                    <div key={key} className="flex items-start justify-between gap-3">
                      <dt className="break-words text-fog">{key}</dt>
                      <dd className="max-w-[9rem] break-words text-right text-mist">{String(value)}</dd>
                    </div>
                  ))}
                </dl>
              </div>
            )}

            {preview.access.status === "unavailable" && (
              <p className="mt-4 border-t border-line pt-4 text-xs leading-5 text-warn">
                <AlertTriangle className="mr-1 inline" size={13} aria-hidden="true" />
                私有源素材已脱敏，使用时需要重新选择本人的参考素材。
              </p>
            )}
            {error && <p className="mt-4 text-xs leading-5 text-bad" role="alert">{error}</p>}
          </aside>
        </div>
      </main>
    </div>
  );
}
