"use client";

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Layers3, Plus } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import AssetPickerDialog from "../../components/AssetPickerDialog";
import Nav from "../../components/Nav";
import { STUDIO_DRAFT_PROMPT_KEY } from "../../components/PromptLibraryBrowser";
import { useToast } from "../../components/ToastProvider";
import { api } from "../../lib/api";
import { buildCreationRecipeStudioDraft } from "../../lib/creationRecipeTransfer";
import { redirectOnAuthError, reportBackgroundError } from "../../lib/errorHandling";
import { saveStudioUserDraft } from "../../lib/studioSession";
import { normalizeUnifiedAssetPage, unifiedAssetKey } from "../../lib/unifiedAssets";
import AssetFolderPanel from "./AssetFolderPanel";
import ProjectLinkPickerDialog from "./ProjectLinkPickerDialog";
import ProjectList from "./ProjectList";
import ProjectWorkspace from "./ProjectWorkspace";

function replaceById(items, value) {
  return items.map((item) => item.id === value.id ? value : item);
}

function positiveId(value) {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

function ProjectsPageContent() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const notify = useToast();
  const [me, setMe] = useState(null);
  const [projects, setProjects] = useState([]);
  const [projectId, setProjectId] = useState(null);
  const [project, setProject] = useState(null);
  const [folders, setFolders] = useState([]);
  const [folder, setFolder] = useState(null);
  const [assets, setAssets] = useState([]);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [projectTitle, setProjectTitle] = useState("");
  const [projectType, setProjectType] = useState("mixed");
  const [assetRole, setAssetRole] = useState("source");
  const [folderName, setFolderName] = useState("");
  const [picker, setPicker] = useState("");
  const [linkPicker, setLinkPicker] = useState("");
  const [draftText, setDraftText] = useState("");
  const [draftDirty, setDraftDirty] = useState(false);
  const [draftStatus, setDraftStatus] = useState("saved");
  const [similarityByRef, setSimilarityByRef] = useState({});
  const draftSaveSeqRef = useRef(0);
  const requestedProjectId = positiveId(searchParams.get("project"));
  const initialProjectIdRef = useRef(requestedProjectId);

  const assetMap = useMemo(() => new Map(assets.map((asset) => [unifiedAssetKey(asset), asset])), [assets]);

  const loadInitial = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [currentUser, projectRows, folderRows, assetPayload] = await Promise.all([
        api.me(),
        api.projects({ status: "all", limit: 100 }),
        api.assetFolders(),
        api.meAssets({ type: "all", origin: "all", retention: "all", limit: 200 }),
      ]);
      setMe(currentUser);
      setProjects(projectRows);
      setFolders(folderRows);
      setAssets(normalizeUnifiedAssetPage(assetPayload).items);
      setProjectId((current) => current || initialProjectIdRef.current || projectRows[0]?.id || null);
    } catch (loadError) {
      if (!redirectOnAuthError(loadError, router, setError, "projects session probe")) {
        setError(loadError.message || "项目加载失败");
      }
    } finally {
      setLoading(false);
    }
  }, [router]);

  useEffect(() => { loadInitial(); }, [loadInitial]);

  useEffect(() => {
    if (requestedProjectId) {
      setProjectId((current) => current === requestedProjectId ? current : requestedProjectId);
    }
  }, [requestedProjectId]);

  const selectProject = useCallback((nextProjectId) => {
    const normalizedId = positiveId(nextProjectId);
    setProjectId(normalizedId);
    const next = new URLSearchParams(searchParams.toString());
    if (normalizedId) next.set("project", String(normalizedId));
    else next.delete("project");
    router.replace(next.size ? `${pathname}?${next.toString()}` : pathname, { scroll: false });
  }, [pathname, router, searchParams]);

  useEffect(() => {
    if (!projectId) {
      setProject(null);
      return;
    }
    let canceled = false;
    setDetailLoading(true);
    api.project(projectId).then((value) => {
      if (!canceled) setProject(value);
    }).catch((loadError) => {
      if (!canceled) {
        setError(loadError.message || "项目详情加载失败");
        reportBackgroundError(loadError, "load project detail");
      }
    }).finally(() => {
      if (!canceled) setDetailLoading(false);
    });
    return () => { canceled = true; };
  }, [projectId]);

  useEffect(() => {
    draftSaveSeqRef.current += 1;
    setDraftText(String(project?.draft?.content || ""));
    setDraftDirty(false);
    setDraftStatus("saved");
    setSimilarityByRef({});
  }, [project?.id]);

  useEffect(() => {
    if (!project?.id || !project.draft_key || !draftDirty) return undefined;
    const projectIdAtSchedule = project.id;
    const seq = ++draftSaveSeqRef.current;
    const timer = window.setTimeout(async () => {
      setDraftStatus("saving");
      try {
        const saved = await api.saveDraft(project.draft_key, {
          content: draftText,
          savedAt: Date.now(),
        });
        if (draftSaveSeqRef.current !== seq || projectIdAtSchedule !== project.id) return;
        setDraftDirty(false);
        setDraftStatus("saved");
        setProject((current) => current?.id === projectIdAtSchedule
          ? { ...current, draft: saved.payload, draft_updated_at: saved.updated_at }
          : current);
      } catch (saveError) {
        if (draftSaveSeqRef.current !== seq || projectIdAtSchedule !== project.id) return;
        setDraftStatus("error");
        reportBackgroundError(saveError, "save project cloud draft");
      }
    }, 900);
    return () => window.clearTimeout(timer);
  }, [project?.id, project?.draft_key, draftText, draftDirty]);

  function publishProject(value) {
    setProject(value);
    setProjects((current) => replaceById(current, value).sort((left, right) => (
      String(right.updated_at || "").localeCompare(String(left.updated_at || ""))
    )));
  }

  async function createProject() {
    const title = projectTitle.trim();
    if (!title || busy) return;
    setBusy("create-project");
    try {
      const created = await api.createProject({ title, project_type: projectType });
      setProjects((current) => [created, ...current]);
      selectProject(created.id);
      setProject(created);
      setProjectTitle("");
      notify.success("项目已创建");
    } catch (createError) {
      setError(createError.message || "创建项目失败");
      notify.error(createError.message || "创建项目失败");
    } finally {
      setBusy("");
    }
  }

  async function updateProject(body, successMessage) {
    if (!project || busy) return;
    setBusy("update-project");
    try {
      publishProject(await api.updateProject(project.id, body));
      notify.success(successMessage);
    } catch (updateError) {
      setError(updateError.message || "项目更新失败");
      notify.error(updateError.message || "项目更新失败");
    } finally {
      setBusy("");
    }
  }

  async function deleteProject() {
    if (!project || busy || !window.confirm(`确认删除项目“${project.title}”？项目中的真实素材不会被删除。`)) return;
    setBusy("delete-project");
    try {
      await api.deleteProject(project.id);
      const next = projects.filter((item) => item.id !== project.id);
      setProjects(next);
      setProject(null);
      selectProject(next[0]?.id || null);
      notify.success("项目已删除，素材仍保留在素材库");
    } catch (deleteError) {
      setError(deleteError.message || "删除项目失败");
      notify.error(deleteError.message || "删除项目失败");
    } finally {
      setBusy("");
    }
  }

  async function removeProjectAsset(assetRef) {
    if (!project || busy) return;
    setBusy("remove-project-asset");
    try {
      publishProject(await api.removeProjectAssets(project.id, [assetRef]));
      notify.success("已从项目移除，真实素材仍保留");
    } catch (removeError) {
      notify.error(removeError.message || "移除素材失败");
    } finally {
      setBusy("");
    }
  }

  async function saveProjectAssetTags(assetRef, tags) {
    if (!project || busy) return;
    setBusy("save-project-asset-tags");
    try {
      const metadata = await api.updateProjectAssetTags(project.id, assetRef, tags);
      setProject((current) => current?.id === project.id
        ? {
            ...current,
            assets: current.assets.map((link) => (
              link.asset_ref === assetRef ? { ...link, metadata } : link
            )),
          }
        : current);
      notify.success("素材标签已保存");
    } catch (saveError) {
      notify.error(saveError.message || "素材标签保存失败");
    } finally {
      setBusy("");
    }
  }

  async function findProjectSimilarAssets(assetRef) {
    if (!project || busy) return;
    setBusy("analyze-project-asset");
    try {
      const result = await api.findProjectSimilarAssets(project.id, assetRef, { maxDistance: 8 });
      const refreshed = await api.project(project.id);
      publishProject(refreshed);
      setSimilarityByRef((current) => ({ ...current, [assetRef]: result }));
      if (result.status === "degraded") {
        notify.warn(result.message || "检测已完成，部分能力不可用");
      } else {
        notify.success(`检测完成，发现 ${result.matches.length} 个匹配素材`);
      }
    } catch (analysisError) {
      notify.error(analysisError.message || "素材查重失败");
    } finally {
      setBusy("");
    }
  }

  async function exportProject() {
    if (!project || busy) return;
    setBusy("export-project");
    try {
      const filename = await api.exportProject(project.id, { includeMedia: true });
      notify.success(`已导出 ${filename}`);
    } catch (exportError) {
      notify.error(exportError.message || "项目导出失败");
    } finally {
      setBusy("");
    }
  }

  async function removeProjectTask(task) {
    if (!project || busy) return;
    setBusy("remove-project-task");
    try {
      publishProject(await api.removeProjectTask(project.id, task.task_kind, task.task_id));
      notify.success("任务已移出项目");
    } catch (removeError) {
      notify.error(removeError.message || "移出任务失败");
    } finally {
      setBusy("");
    }
  }

  async function removeProjectRecipe(recipeId) {
    if (!project || busy) return;
    setBusy("remove-project-recipe");
    try {
      publishProject(await api.removeProjectRecipe(project.id, recipeId));
      notify.success("配方已移出项目");
    } catch (removeError) {
      notify.error(removeError.message || "移出配方失败");
    } finally {
      setBusy("");
    }
  }

  async function restoreProjectRecipe(recipeLink) {
    if (busy || !positiveId(recipeLink?.recipe_id)) return;
    setBusy("restore-project-recipe");
    try {
      const recipe = await api.creationRecipe(recipeLink.recipe_id);
      const version = recipe.version?.payload
        ? recipe.version
        : await api.creationRecipeVersion(recipe.id, recipe.current_version);
      const draft = buildCreationRecipeStudioDraft(recipe, version, { source: "owner" });
      const saved = saveStudioUserDraft(
        window.localStorage,
        STUDIO_DRAFT_PROMPT_KEY,
        me?.id,
        draft,
      );
      if (!saved) throw new Error("无法保存当前用户的创作配方草稿");
      router.push("/");
    } catch (restoreError) {
      notify.error(restoreError.message || "恢复创作配方失败");
    } finally {
      setBusy("");
    }
  }

  async function confirmProjectLinks(selectedItems) {
    if (!project || busy || !selectedItems.length) return;
    setBusy("link-project-items");
    try {
      if (linkPicker === "tasks") {
        // 后端按 task_kind 分组归集，选中多种任务时逐类提交（project_id 走路径参数）
        const idsByKind = new Map();
        for (const task of selectedItems) {
          if (!idsByKind.has(task.kind)) idsByKind.set(task.kind, []);
          idsByKind.get(task.kind).push(task.id);
        }
        let updated = null;
        for (const [taskKind, taskIds] of idsByKind) {
          updated = await api.addProjectTasks(project.id, taskKind, taskIds);
        }
        if (updated) publishProject(updated);
        notify.success(`已加入 ${selectedItems.length} 个任务`);
      } else if (linkPicker === "recipes") {
        publishProject(await api.addProjectRecipes(project.id, selectedItems.map((item) => item.id)));
        notify.success(`已加入 ${selectedItems.length} 个配方`);
      }
      setLinkPicker("");
    } catch (linkError) {
      notify.error(linkError.message || "加入项目失败");
    } finally {
      setBusy("");
    }
  }

  function changeDraft(value) {
    setDraftText(value);
    setDraftDirty(true);
    setDraftStatus("saving");
  }

  async function createFolder() {
    const name = folderName.trim();
    if (!name || busy) return;
    setBusy("create-folder");
    try {
      const created = await api.createAssetFolder({ name });
      setFolders((current) => [...current, created]);
      setFolder(created);
      setFolderName("");
      notify.success("素材文件夹已创建");
    } catch (createError) {
      notify.error(createError.message || "创建文件夹失败");
    } finally {
      setBusy("");
    }
  }

  async function selectFolder(id) {
    setBusy("open-folder");
    try {
      setFolder(await api.assetFolder(id));
    } catch (loadError) {
      notify.error(loadError.message || "文件夹加载失败");
    } finally {
      setBusy("");
    }
  }

  async function deleteFolder() {
    if (!folder || busy || !window.confirm(`确认删除文件夹“${folder.name}”？素材不会被删除。`)) return;
    setBusy("delete-folder");
    try {
      await api.deleteAssetFolder(folder.id);
      setFolders((current) => current.filter((item) => item.id !== folder.id));
      setFolder(null);
      notify.success("文件夹已删除，素材仍保留");
    } catch (deleteError) {
      notify.error(deleteError.message || "删除文件夹失败");
    } finally {
      setBusy("");
    }
  }

  async function renameFolder(nextName) {
    const trimmed = String(nextName || "").trim();
    if (!folder || !trimmed || trimmed === folder.name || busy) return;
    setBusy("rename-folder");
    try {
      const updated = await api.updateAssetFolder(folder.id, { name: trimmed });
      setFolder(updated);
      setFolders((current) => replaceById(current, updated));
      notify.success("文件夹已重命名");
    } catch (renameError) {
      notify.error(renameError.message || "重命名文件夹失败");
    } finally {
      setBusy("");
    }
  }

  async function moveFolderParent(parentId) {
    if (!folder || busy || (folder.parent_id || null) === (parentId || null)) return;
    setBusy("move-folder");
    try {
      const updated = await api.updateAssetFolder(
        folder.id,
        parentId ? { parent_id: parentId } : { move_to_root: true },
      );
      setFolder(updated);
      setFolders(await api.assetFolders());
      notify.success(parentId ? "文件夹已移动" : "文件夹已移到根目录");
    } catch (moveError) {
      notify.error(moveError.message || "移动文件夹失败");
    } finally {
      setBusy("");
    }
  }

  async function reorderFolder(direction) {
    if (!folder || busy) return;
    const siblings = folders.filter((item) => (item.parent_id || null) === (folder.parent_id || null));
    const index = siblings.findIndex((item) => item.id === folder.id);
    const targetIndex = index + (direction === "up" ? -1 : 1);
    if (index < 0 || targetIndex < 0 || targetIndex >= siblings.length) return;
    const ordered = [...siblings];
    [ordered[index], ordered[targetIndex]] = [ordered[targetIndex], ordered[index]];
    setBusy("reorder-folder");
    try {
      // 按新顺序补齐同级 sort_order，只提交发生变化的文件夹
      for (let position = 0; position < ordered.length; position += 1) {
        if ((ordered[position].sort_order ?? 0) !== position) {
          await api.updateAssetFolder(ordered[position].id, { sort_order: position });
        }
      }
      setFolders(await api.assetFolders());
      notify.success("文件夹顺序已更新");
    } catch (reorderError) {
      notify.error(reorderError.message || "调整文件夹顺序失败");
    } finally {
      setBusy("");
    }
  }

  async function removeFolderAsset(assetRef) {
    if (!folder || busy) return;
    setBusy("remove-folder-asset");
    try {
      const updated = await api.removeAssetsFromFolder(folder.id, [assetRef]);
      setFolder(updated);
      setFolders((current) => replaceById(current, updated));
    } catch (removeError) {
      notify.error(removeError.message || "移出文件夹失败");
    } finally {
      setBusy("");
    }
  }

  async function confirmAssets(picked) {
    const refs = picked.map(unifiedAssetKey).filter(Boolean);
    if (!refs.length) return;
    setBusy("link-assets");
    try {
      if (picker === "project" && project) {
        publishProject(await api.addProjectAssets(project.id, { asset_refs: refs, role: assetRole }));
        notify.success(`已添加 ${refs.length} 个项目素材`);
      } else if (picker === "folder" && folder) {
        const updated = await api.moveAssetsToFolder(folder.id, refs);
        setFolder(updated);
        setFolders((current) => replaceById(current, updated));
        notify.success(`已移动 ${refs.length} 个素材`);
      }
      setPicker("");
    } catch (linkError) {
      notify.error(linkError.message || "素材归集失败");
    } finally {
      setBusy("");
    }
  }

  const excludedRefs = picker === "project"
    ? (project?.assets || []).map((item) => item.asset_ref)
    : picker === "folder"
      ? (folder?.items || []).map((item) => item.asset_ref)
      : [];

  return (
    <div className="min-h-screen bg-base text-snow">
      <Nav me={me} active="projects" />
      <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6">
        <header className="flex flex-col gap-4 border-b border-line pb-5 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <div className="flex items-center gap-2 text-aqua"><Layers3 size={17} aria-hidden="true" /><span className="text-xs font-medium">创作归集</span></div>
            <h1 className="mt-1 text-2xl font-display font-semibold text-snow">项目</h1>
          </div>
          <form className="flex flex-col gap-2 sm:flex-row" onSubmit={(event) => { event.preventDefault(); createProject(); }}>
            <label className="sr-only" htmlFor="new-project-title">新项目名称</label>
            <input id="new-project-title" className="input h-10 min-w-56 py-1" value={projectTitle} onChange={(event) => setProjectTitle(event.target.value)} maxLength={128} placeholder="新项目名称" />
            <label className="sr-only" htmlFor="new-project-type">项目类型</label>
            <select id="new-project-type" className="input h-10 py-1" value={projectType} onChange={(event) => setProjectType(event.target.value)}>
              <option value="mixed">混合项目</option><option value="image">图片项目</option><option value="video">视频项目</option>
            </select>
            <button type="submit" className="btn-primary h-10" disabled={!projectTitle.trim() || Boolean(busy)}><Plus size={16} aria-hidden="true" /> 创建项目</button>
          </form>
        </header>

        {error && (
          <div className="mt-4 flex items-center justify-between gap-3 border-y border-bad/30 bg-bad/10 px-3 py-2 text-sm text-bad" role="alert">
            <span>{error}</span><button type="button" className="btn-ghost btn-sm" onClick={loadInitial}>重试</button>
          </div>
        )}

        <div className="mt-5 grid gap-6 lg:grid-cols-[240px_minmax(0,1fr)_260px]">
          <aside className="border-y border-line lg:border-r lg:border-y-0 lg:pr-4" aria-labelledby="project-list-title">
            <h2 id="project-list-title" className="px-3 pb-2 text-xs font-display font-semibold text-fog">全部项目 · {projects.length}</h2>
            <ProjectList projects={projects} activeId={projectId} loading={loading} onSelect={selectProject} />
          </aside>
          <ProjectWorkspace
            project={project}
            assetMap={assetMap}
            loading={detailLoading}
            busy={Boolean(busy)}
            assetRole={assetRole}
            draftText={draftText}
            draftStatus={draftStatus}
            onDraftChange={changeDraft}
            onAssetRoleChange={setAssetRole}
            onAddAssets={() => setPicker("project")}
            onAddTasks={() => setLinkPicker("tasks")}
            onAddRecipes={() => setLinkPicker("recipes")}
            onRemoveAsset={removeProjectAsset}
            onRemoveTask={removeProjectTask}
            onRemoveRecipe={removeProjectRecipe}
            onRestoreRecipe={restoreProjectRecipe}
            onSaveAssetTags={saveProjectAssetTags}
            onFindSimilar={findProjectSimilarAssets}
            similarityByRef={similarityByRef}
            onAutoArchiveChange={(days) => updateProject(
              { auto_archive_after_days: days },
              days ? `已设置 ${days} 天未更新自动归档` : "已关闭自动归档",
            )}
            onExport={exportProject}
            onArchive={() => updateProject({ status: project?.status === "archived" ? "active" : "archived" }, project?.status === "archived" ? "项目已恢复" : "项目已归档")}
            onDelete={deleteProject}
          />
          <div className="border-t border-line pt-5 lg:border-l lg:border-t-0 lg:pl-4 lg:pt-0">
            <AssetFolderPanel
              folders={folders}
              activeFolder={folder}
              name={folderName}
              onNameChange={setFolderName}
              busy={Boolean(busy)}
              onCreate={createFolder}
              onSelect={selectFolder}
              onAddAssets={() => setPicker("folder")}
              onRemoveAsset={removeFolderAsset}
              onRename={renameFolder}
              onMoveParent={moveFolderParent}
              onReorder={reorderFolder}
              onDelete={deleteFolder}
            />
          </div>
        </div>
      </main>
      <ProjectLinkPickerDialog
        open={Boolean(linkPicker)}
        mode={linkPicker || "tasks"}
        busy={Boolean(busy)}
        excludedKeys={linkPicker === "tasks"
          ? (project?.tasks || []).map((task) => `${task.task_kind}:${task.task_id}`)
          : (project?.recipes || []).map((recipe) => `recipe:${recipe.recipe_id}`)}
        onClose={() => setLinkPicker("")}
        onConfirm={confirmProjectLinks}
      />
      <AssetPickerDialog
        open={Boolean(picker)}
        role={picker === "project" ? "project_assets" : "folder_assets"}
        multiple
        maxSelection={100}
        mediaType="all"
        excludedRefs={excludedRefs}
        onClose={() => setPicker("")}
        onConfirm={confirmAssets}
      />
    </div>
  );
}

export default function ProjectsPage() {
  return (
    <Suspense fallback={<div className="min-h-screen bg-base" aria-label="项目加载中" />}>
      <ProjectsPageContent />
    </Suspense>
  );
}
