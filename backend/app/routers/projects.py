"""User-owned media projects and folders over the unified asset-ref namespace."""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from ..db import get_db
from ..deps import get_client_ip, get_current_user
from ..models import (
    AssetFolder,
    AssetFolderItem,
    CreationRecipe,
    CreationRecipeVersion,
    GenAsset,
    GenTask,
    MediaProject,
    MediaProjectAsset,
    MediaProjectRecipe,
    MediaProjectTask,
    ParseRecord,
    ReverseOperation,
    ToolDefinition,
    ToolNodeRun,
    ToolRun,
    User,
    UserDraft,
    WorkflowRun,
)
from ..schemas import (
    AssetFolderCreateIn,
    AssetFolderOut,
    AssetFolderPatchIn,
    AssetRefsIn,
    AssetSimilarityOut,
    MediaProjectAssetsIn,
    MediaProjectCreateIn,
    MediaProjectOut,
    MediaProjectPatchIn,
    MediaProjectRecipeLinksIn,
    MediaProjectTaskLinksIn,
    UserAssetMetadataOut,
    UserAssetTagsIn,
)
from ..services import audit, project_collection, task_center, user_assets

router = APIRouter(prefix="/api", tags=["projects"])


def _project_draft_key(project_id: int) -> str:
    return f"project-{int(project_id)}"


def _truncate(value: object, limit: int = 180) -> str | None:
    text = " ".join(str(value or "").split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _owned_project(db: Session, user_id: int, project_id: int) -> MediaProject:
    project = db.scalar(select(MediaProject).where(
        MediaProject.id == project_id,
        MediaProject.user_id == user_id,
    ))
    if project is None:
        raise HTTPException(404, "项目不存在")
    return project


def _owned_folder(db: Session, user_id: int, folder_id: int) -> AssetFolder:
    folder = db.scalar(select(AssetFolder).where(
        AssetFolder.id == folder_id,
        AssetFolder.user_id == user_id,
    ))
    if folder is None:
        raise HTTPException(404, "素材文件夹不存在")
    return folder


def _validate_asset_refs(db: Session, user_id: int, asset_refs: list[str]) -> None:
    try:
        user_assets.resolve_asset_refs(db, user_id, asset_refs)
    except user_assets.InvalidAssetRef as exc:
        raise HTTPException(400, str(exc)) from None
    except user_assets.AssetNotFound as exc:
        raise HTTPException(404, str(exc)) from None


def _project_out(db: Session, project: MediaProject, *, detail: bool) -> dict:
    assets = list(db.scalars(
        select(MediaProjectAsset)
        .where(MediaProjectAsset.project_id == project.id)
        .order_by(MediaProjectAsset.sort_order, MediaProjectAsset.created_at, MediaProjectAsset.id)
    ))
    recipes = list(db.scalars(
        select(MediaProjectRecipe)
        .where(MediaProjectRecipe.project_id == project.id)
        .order_by(MediaProjectRecipe.created_at, MediaProjectRecipe.id)
    ))
    tasks = list(db.scalars(
        select(MediaProjectTask)
        .where(MediaProjectTask.project_id == project.id)
        .order_by(MediaProjectTask.created_at, MediaProjectTask.id)
    ))
    asset_rows: list[dict] = []
    recipe_rows: list[dict] = []
    task_rows: list[dict] = []
    draft_key = _project_draft_key(int(project.id))
    draft_payload: dict = {}
    draft_updated_at = None
    metadata_rows = user_assets.metadata_for_asset_refs(
        db,
        int(project.user_id),
        [row.asset_ref for row in assets],
    )
    metadata_counts = _project_metadata_counts(metadata_rows)
    cost_frozen, cost_settled = project_collection.project_cost_totals(
        db,
        project_id=int(project.id),
        user_id=int(project.user_id),
    )
    if detail:
        asset_views = user_assets.asset_items_for_refs(
            db,
            int(project.user_id),
            [row.asset_ref for row in assets],
        )
        asset_rows = [
            {
                "id": row.id,
                "asset_ref": row.asset_ref,
                "role": row.role,
                "sort_order": row.sort_order,
                "note": row.note,
                "asset": asset_views.get(row.asset_ref),
                "metadata": (
                    user_assets.metadata_payload(metadata_rows[row.asset_ref])
                    if row.asset_ref in metadata_rows
                    else None
                ),
                "duplicate_count": metadata_counts.get(row.asset_ref, (0, 0))[0],
                "similar_count": metadata_counts.get(row.asset_ref, (0, 0))[1],
                "created_at": row.created_at,
            }
            for row in assets
        ]
        recipe_rows = _project_recipe_rows(db, project, recipes)
        task_rows = _project_task_rows(db, project, tasks)
        draft = db.scalar(
            select(UserDraft).where(
                UserDraft.user_id == project.user_id,
                UserDraft.key == draft_key,
            )
        )
        if draft is not None:
            draft_payload = dict(draft.payload or {})
            draft_updated_at = draft.updated_at
    return {
        "id": project.id,
        "title": project.title,
        "description": project.description,
        "project_type": project.project_type,
        "status": project.status,
        "cover_asset_ref": project.cover_asset_ref,
        "auto_archive_after_days": project.auto_archive_after_days,
        "asset_count": len(assets),
        "recipe_count": len(recipes),
        "task_count": len(tasks),
        "cost_frozen": cost_frozen,
        "cost_settled": cost_settled,
        "assets": asset_rows,
        "recipes": recipe_rows,
        "tasks": task_rows,
        "draft_key": draft_key,
        "draft": draft_payload,
        "draft_updated_at": draft_updated_at,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


def _project_metadata_counts(
    rows: dict[str, object],
    *,
    max_distance: int = 8,
) -> dict[str, tuple[int, int]]:
    counts: dict[str, tuple[int, int]] = {}
    values = list(rows.values())
    for query in values:
        exact = 0
        similar = 0
        for candidate in values:
            if query.asset_ref == candidate.asset_ref or query.media_type != candidate.media_type:
                continue
            if query.content_sha256 and query.content_sha256 == candidate.content_sha256:
                exact += 1
                continue
            if not query.perceptual_hash or not candidate.perceptual_hash:
                continue
            try:
                distance = (
                    int(query.perceptual_hash, 16) ^ int(candidate.perceptual_hash, 16)
                ).bit_count()
            except ValueError:
                continue
            if distance <= max_distance:
                similar += 1
        counts[query.asset_ref] = (exact, similar)
    return counts


def _project_recipe_rows(
    db: Session,
    project: MediaProject,
    links: list[MediaProjectRecipe],
) -> list[dict]:
    recipe_ids = [int(link.recipe_id) for link in links]
    if not recipe_ids:
        return []
    recipes = {
        int(row.id): row
        for row in db.scalars(
            select(CreationRecipe).where(
                CreationRecipe.user_id == project.user_id,
                CreationRecipe.deleted_at.is_(None),
                CreationRecipe.id.in_(recipe_ids),
            )
        )
    }
    versions = list(
        db.scalars(
            select(CreationRecipeVersion).where(
                CreationRecipeVersion.recipe_id.in_(recipe_ids)
            )
        )
    )
    version_map = {
        (int(row.recipe_id), int(row.version)): row
        for row in versions
    }
    output: list[dict] = []
    for link in links:
        recipe = recipes.get(int(link.recipe_id))
        version = (
            version_map.get((int(recipe.id), int(recipe.current_version)))
            if recipe is not None
            else None
        )
        output.append({
            "id": link.id,
            "recipe_id": link.recipe_id,
            "title": recipe.title if recipe is not None else None,
            "category": recipe.category if recipe is not None else None,
            "visibility": recipe.visibility if recipe is not None else None,
            "favorite": bool(recipe.favorite) if recipe is not None else False,
            "current_version": recipe.current_version if recipe is not None else None,
            "cover_asset_url": recipe.cover_asset_url if recipe is not None else None,
            "schema_version": version.schema_version if version is not None else None,
            "payload": version.payload if version is not None else None,
            "created_at": link.created_at,
        })
    return output


def _project_task_rows(
    db: Session,
    project: MediaProject,
    links: list[MediaProjectTask],
) -> list[dict]:
    by_kind = {
        kind: [int(link.task_id) for link in links if link.task_kind == kind]
        for kind in ("generation", "reverse", "parse", "workflow")
    }
    generation_rows = {
        int(row.id): row
        for row in db.scalars(
            select(GenTask).where(
                GenTask.user_id == project.user_id,
                GenTask.id.in_(by_kind["generation"]),
            )
        )
    } if by_kind["generation"] else {}
    reverse_rows = {
        int(row.id): row
        for row in db.scalars(
            select(ReverseOperation).where(
                ReverseOperation.user_id == project.user_id,
                ReverseOperation.id.in_(by_kind["reverse"]),
            )
        )
    } if by_kind["reverse"] else {}
    parse_rows = {
        int(row.id): row
        for row in db.scalars(
            select(ParseRecord).where(
                ParseRecord.user_id == project.user_id,
                ParseRecord.id.in_(by_kind["parse"]),
            )
        )
    } if by_kind["parse"] else {}
    workflow_rows = {
        int(row.id): row
        for row in db.scalars(
            select(WorkflowRun).where(
                WorkflowRun.user_id == project.user_id,
                WorkflowRun.id.in_(by_kind["workflow"]),
            )
        )
    } if by_kind["workflow"] else {}
    workflow_tool_runs: dict[int, ToolRun] = {}
    workflow_tools: dict[int, ToolDefinition] = {}
    workflow_nodes: dict[int, list[ToolNodeRun]] = {}
    if workflow_rows:
        tool_run_ids = [int(row.tool_run_id) for row in workflow_rows.values()]
        workflow_tool_runs = {
            int(row.id): row
            for row in db.scalars(select(ToolRun).where(ToolRun.id.in_(tool_run_ids)))
        }
        tool_definition_ids = {
            int(row.tool_definition_id) for row in workflow_tool_runs.values()
        }
        tools_by_id = {
            int(row.id): row
            for row in db.scalars(
                select(ToolDefinition).where(ToolDefinition.id.in_(tool_definition_ids))
            )
        }
        for run in workflow_rows.values():
            tool_run = workflow_tool_runs.get(int(run.tool_run_id))
            if tool_run is not None:
                tool = tools_by_id.get(int(tool_run.tool_definition_id))
                if tool is not None:
                    workflow_tools[int(run.id)] = tool
        for node in db.scalars(
            select(ToolNodeRun)
            .where(ToolNodeRun.workflow_run_id.in_(workflow_rows))
            .order_by(ToolNodeRun.workflow_run_id, ToolNodeRun.topological_index, ToolNodeRun.id)
        ):
            workflow_nodes.setdefault(int(node.workflow_run_id), []).append(node)
    result_refs: dict[int, list[str]] = {}
    if generation_rows:
        for task_id, asset_id in db.execute(
            select(GenAsset.task_id, GenAsset.id)
            .where(GenAsset.task_id.in_(generation_rows))
            .order_by(GenAsset.id)
        ):
            result_refs.setdefault(int(task_id), []).append(
                user_assets.generated_asset_ref(int(asset_id))
            )

    output: list[dict] = []
    for link in links:
        common = {
            "id": link.id,
            "task_kind": link.task_kind,
            "task_id": link.task_id,
            "created_at": link.created_at,
        }
        if link.task_kind == "generation":
            task = generation_rows.get(int(link.task_id))
            prompt = task.prompt if task is not None and isinstance(task.prompt, dict) else {}
            terminal = task is not None and task.status in {"succeeded", "failed", "canceled"}
            output.append({
                **common,
                "status": task.status if task is not None else None,
                "category": task.category if task is not None else None,
                "stage": task.stage if task is not None else None,
                "phase": task.phase if task is not None else None,
                "progress": 100 if terminal else 0,
                "title": (
                    f"{'视频' if task.category == 'video' else '图片'}生成"
                    if task is not None
                    else "生成任务已失效"
                ),
                "summary": _truncate(
                    prompt.get("final_text")
                    or prompt.get("instruction")
                    or prompt.get("optimized_text")
                ),
                "result_refs": result_refs.get(int(link.task_id), []),
                "cost_frozen": max(0, int(task.cost_frozen or 0)) if task is not None else 0,
                "cost_settled": max(0, int(task.cost_settled or 0)) if task is not None else 0,
                "error": task.error if task is not None else None,
                "finished_at": task.finished_at if task is not None else None,
            })
            continue
        if link.task_kind == "reverse":
            task = reverse_rows.get(int(link.task_id))
            terminal = task is not None and task.status in {"succeeded", "failed", "canceled"}
            output.append({
                **common,
                "status": task.status if task is not None else None,
                "category": task.target if task is not None else None,
                "stage": task.output_purpose if task is not None else None,
                "phase": task.phase if task is not None else None,
                "progress": max(int(task.progress or 0), 100 if terminal else 0) if task else 0,
                "title": (
                    f"{'视频' if task.target == 'video' else '图片'}反推"
                    if task is not None
                    else "反推任务已失效"
                ),
                "summary": _truncate(task.analysis_focus) if task is not None else None,
                "result_refs": [],
                "cost_frozen": max(0, int(task.cost_frozen or 0)) if task is not None else 0,
                "cost_settled": max(0, int(task.cost_settled or 0)) if task is not None else 0,
                "error": task.error if task is not None else None,
                "finished_at": task.finished_at if task is not None else None,
            })
            continue
        if link.task_kind == "workflow":
            run = workflow_rows.get(int(link.task_id))
            tool_run = (
                workflow_tool_runs.get(int(run.tool_run_id)) if run is not None else None
            )
            tool = workflow_tools.get(int(link.task_id))
            nodes = workflow_nodes.get(int(link.task_id), [])
            refs = task_center.workflow_output_asset_refs(
                tool_run.output if tool_run is not None else None
            )
            frozen, settled = project_collection.workflow_run_cost_totals(
                db,
                user_id=int(project.user_id),
                tool_run=tool_run,
                nodes=nodes,
            )
            output.append({
                **common,
                "status": run.status if run is not None else None,
                "category": tool.category if tool is not None else "workflow",
                "stage": tool.slug if tool is not None else None,
                "phase": run.current_node_key if run is not None else None,
                "progress": task_center.workflow_node_progress(nodes),
                "title": tool.name if tool is not None else "工作流任务已失效",
                "summary": _truncate(tool.description) if tool is not None else None,
                "result_refs": refs,
                "cost_frozen": frozen,
                "cost_settled": settled,
                "error": run.error if run is not None else None,
                "workflow_tool_slug": tool.slug if tool is not None else None,
                "workflow_entry_path": tool.entry_path if tool is not None else None,
                "workflow_current_node_key": (
                    run.current_node_key if run is not None else None
                ),
                "workflow_nodes": [
                    {
                        "key": node.node_key,
                        "type": node.node_type,
                        "status": node.status,
                        "attempt_count": max(0, int(node.attempt_count or 0)),
                        "max_attempts": max(1, int(node.max_attempts or 1)),
                        "compensation_status": node.compensation_status,
                    }
                    for node in nodes
                ],
                "finished_at": run.finished_at if run is not None else None,
            })
            continue
        task = parse_rows.get(int(link.task_id))
        output.append({
            **common,
            "status": task.status if task is not None else None,
            "category": "parse" if task is not None else None,
            "stage": None,
            "phase": None,
            "progress": (
                {"queued": 5, "running": 50, "done": 100, "failed": 100}.get(task.status, 0)
                if task is not None
                else 0
            ),
            "title": "链接解析" if task is not None else "解析任务已失效",
            "summary": _truncate(task.url) if task is not None else None,
            "result_refs": [
                str(item.get("asset_ref"))
                for item in (task.assets or [])
                if isinstance(item, dict) and item.get("asset_ref")
            ] if task is not None else [],
            "cost_frozen": 0,
            "cost_settled": 0,
            "error": task.error if task is not None else None,
            "finished_at": None,
        })
    return output


def _folder_out(db: Session, folder: AssetFolder, *, detail: bool) -> dict:
    items = list(db.scalars(
        select(AssetFolderItem)
        .where(AssetFolderItem.folder_id == folder.id)
        .order_by(AssetFolderItem.created_at, AssetFolderItem.id)
    ))
    item_rows: list[dict] = []
    if detail and items:
        # 一次性批量取素材视图补充可读字段，避免逐条查询
        asset_views = user_assets.asset_items_for_refs(
            db,
            int(folder.user_id),
            [item.asset_ref for item in items],
        )
        for item in items:
            view = asset_views.get(item.asset_ref) or {}
            item_rows.append({
                "asset_ref": item.asset_ref,
                "type": view.get("type"),
                "url": view.get("url"),
                "preview_url": view.get("preview_url"),
                "thumb": view.get("thumb"),
                "filename": view.get("filename"),
                "available": bool(view.get("available")),
                "created_at": item.created_at,
            })
    return {
        "id": folder.id,
        "name": folder.name,
        "parent_id": folder.parent_id,
        "sort_order": folder.sort_order,
        "item_count": len(items),
        "items": item_rows,
        "created_at": folder.created_at,
        "updated_at": folder.updated_at,
    }


def _touch(project: MediaProject) -> None:
    project.updated_at = datetime.now(timezone.utc)


def _commit_and_audit(
    request: Request,
    db: Session,
    user_id: int,
    action: str,
    biz_type: str,
    biz_id: int,
    detail: dict | None = None,
) -> None:
    db.commit()
    audit.log(
        db,
        user_id=user_id,
        action=action,
        biz_type=biz_type,
        biz_id=biz_id,
        ip=get_client_ip(request),
        detail=detail,
    )


@router.get("/projects", response_model=list[MediaProjectOut])
def list_projects(
    status: str = Query(default="active", pattern="^(active|archived|all)$"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stmt = select(MediaProject).where(MediaProject.user_id == user.id)
    if status != "all":
        stmt = stmt.where(MediaProject.status == status)
    rows = list(db.scalars(stmt.order_by(MediaProject.updated_at.desc(), MediaProject.id.desc()).offset(offset).limit(limit)))
    return [_project_out(db, row, detail=False) for row in rows]


@router.post("/projects", response_model=MediaProjectOut, status_code=201)
def create_project(
    body: MediaProjectCreateIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if body.cover_asset_ref:
        _validate_asset_refs(db, user.id, [body.cover_asset_ref])
    project = MediaProject(user_id=user.id, **body.model_dump())
    db.add(project)
    db.flush()
    _commit_and_audit(
        request, db, user.id, "create_media_project", "media_project", int(project.id)
    )
    db.refresh(project)
    return _project_out(db, project, detail=True)


@router.get("/projects/{project_id}", response_model=MediaProjectOut)
def get_project(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return _project_out(db, _owned_project(db, user.id, project_id), detail=True)


@router.get("/projects/{project_id}/export")
def export_project(
    project_id: int,
    request: Request,
    include_media: bool = Query(default=True),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = _owned_project(db, user.id, project_id)
    bundle = project_collection.build_project_export(
        db,
        project=project,
        include_media=include_media,
    )
    try:
        _commit_and_audit(
            request,
            db,
            user.id,
            "export_media_project",
            "media_project",
            project_id,
            {
                "asset_count": len(bundle.manifest.get("assets", [])),
                "included_media_count": bundle.manifest.get("included_media_count", 0),
                "total_media_bytes": bundle.manifest.get("total_media_bytes", 0),
            },
        )
    except Exception:
        bundle.path.unlink(missing_ok=True)
        raise
    return FileResponse(
        str(bundle.path),
        filename=bundle.filename,
        media_type="application/zip",
        background=BackgroundTask(
            lambda path: os.unlink(path) if os.path.exists(path) else None,
            str(bundle.path),
        ),
    )


@router.patch("/projects/{project_id}", response_model=MediaProjectOut)
def patch_project(
    project_id: int,
    body: MediaProjectPatchIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = _owned_project(db, user.id, project_id)
    values = body.model_dump(exclude_unset=True, exclude={"clear_cover"})
    if values.get("cover_asset_ref"):
        _validate_asset_refs(db, user.id, [values["cover_asset_ref"]])
    for key, value in values.items():
        setattr(project, key, value)
    if body.clear_cover:
        project.cover_asset_ref = None
    _touch(project)
    _commit_and_audit(
        request,
        db,
        user.id,
        "update_media_project",
        "media_project",
        project_id,
        {"fields": sorted(body.model_fields_set)},
    )
    db.refresh(project)
    return _project_out(db, project, detail=True)


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = _owned_project(db, user.id, project_id)
    db.execute(
        delete(UserDraft).where(
            UserDraft.user_id == user.id,
            UserDraft.key == _project_draft_key(project_id),
        )
    )
    db.delete(project)
    _commit_and_audit(
        request, db, user.id, "delete_media_project", "media_project", project_id
    )
    return Response(status_code=204)


@router.post("/projects/{project_id}/assets", response_model=MediaProjectOut)
def add_project_assets(
    project_id: int,
    body: MediaProjectAssetsIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = _owned_project(db, user.id, project_id)
    _validate_asset_refs(db, user.id, body.asset_refs)
    existing = {
        row.asset_ref: row
        for row in db.scalars(select(MediaProjectAsset).where(
            MediaProjectAsset.project_id == project_id,
            MediaProjectAsset.asset_ref.in_(body.asset_refs),
        ))
    }
    max_sort = db.scalar(select(func.max(MediaProjectAsset.sort_order)).where(
        MediaProjectAsset.project_id == project_id
    ))
    next_sort = int(max_sort) + 1 if max_sort is not None else 0
    for asset_ref in body.asset_refs:
        row = existing.get(asset_ref)
        if row is None:
            row = MediaProjectAsset(project_id=project_id, asset_ref=asset_ref, sort_order=next_sort)
            next_sort += 1
            db.add(row)
        row.role = body.role
        row.note = body.note
    if not project.cover_asset_ref and body.asset_refs:
        project.cover_asset_ref = body.asset_refs[0]
    _touch(project)
    _commit_and_audit(
        request,
        db,
        user.id,
        "add_media_project_assets",
        "media_project",
        project_id,
        {"asset_refs": body.asset_refs, "role": body.role},
    )
    db.refresh(project)
    return _project_out(db, project, detail=True)


@router.delete("/projects/{project_id}/assets", response_model=MediaProjectOut)
def remove_project_assets(
    project_id: int,
    body: AssetRefsIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = _owned_project(db, user.id, project_id)
    db.execute(delete(MediaProjectAsset).where(
        MediaProjectAsset.project_id == project_id,
        MediaProjectAsset.asset_ref.in_(body.asset_refs),
    ))
    if project.cover_asset_ref in body.asset_refs:
        project.cover_asset_ref = db.scalar(
            select(MediaProjectAsset.asset_ref)
            .where(MediaProjectAsset.project_id == project_id)
            .order_by(MediaProjectAsset.sort_order, MediaProjectAsset.id)
            .limit(1)
        )
    _touch(project)
    _commit_and_audit(
        request,
        db,
        user.id,
        "remove_media_project_assets",
        "media_project",
        project_id,
        {"asset_refs": body.asset_refs},
    )
    db.refresh(project)
    return _project_out(db, project, detail=True)


def _project_asset_link(
    db: Session,
    project_id: int,
    asset_ref: str,
) -> MediaProjectAsset:
    link = db.scalar(
        select(MediaProjectAsset).where(
            MediaProjectAsset.project_id == int(project_id),
            MediaProjectAsset.asset_ref == asset_ref,
        )
    )
    if link is None:
        raise HTTPException(404, "项目素材不存在")
    return link


@router.put(
    "/projects/{project_id}/assets/{asset_ref}/tags",
    response_model=UserAssetMetadataOut,
)
def update_project_asset_tags(
    project_id: int,
    asset_ref: str,
    body: UserAssetTagsIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _owned_project(db, user.id, project_id)
    _project_asset_link(db, project_id, asset_ref)
    try:
        row = user_assets.update_asset_tags(db, user.id, asset_ref, body.tags)
    except user_assets.InvalidAssetRef as exc:
        raise HTTPException(400, str(exc)) from None
    except user_assets.AssetNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    _commit_and_audit(
        request,
        db,
        user.id,
        "update_user_asset_tags",
        "user_asset",
        int(row.id),
        {"asset_ref": asset_ref, "tags": body.tags, "project_id": project_id},
    )
    db.refresh(row)
    return row


@router.post(
    "/projects/{project_id}/assets/{asset_ref}/similar",
    response_model=AssetSimilarityOut,
)
def find_project_similar_assets(
    project_id: int,
    asset_ref: str,
    request: Request,
    max_distance: int = Query(default=8, ge=0, le=64),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _owned_project(db, user.id, project_id)
    _project_asset_link(db, project_id, asset_ref)
    candidate_refs = list(
        db.scalars(
            select(MediaProjectAsset.asset_ref).where(
                MediaProjectAsset.project_id == int(project_id)
            )
        )
    )
    try:
        result = user_assets.find_similar_assets(
            db,
            user.id,
            asset_ref,
            candidate_refs,
            max_distance=max_distance,
        )
    except user_assets.InvalidAssetRef as exc:
        raise HTTPException(400, str(exc)) from None
    except user_assets.AssetNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    _commit_and_audit(
        request,
        db,
        user.id,
        "analyze_project_asset_similarity",
        "media_project",
        project_id,
        {
            "asset_ref": asset_ref,
            "status": result["status"],
            "match_count": len(result["matches"]),
        },
    )
    return result


@router.post("/projects/{project_id}/recipes", response_model=MediaProjectOut)
def add_project_recipes(
    project_id: int,
    body: MediaProjectRecipeLinksIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = _owned_project(db, user.id, project_id)
    owned_ids = set(db.scalars(select(CreationRecipe.id).where(
        CreationRecipe.user_id == user.id,
        CreationRecipe.deleted_at.is_(None),
        CreationRecipe.id.in_(body.recipe_ids),
    )))
    if owned_ids != set(body.recipe_ids):
        raise HTTPException(404, "创作配方不存在")
    existing = set(db.scalars(select(MediaProjectRecipe.recipe_id).where(
        MediaProjectRecipe.project_id == project_id,
        MediaProjectRecipe.recipe_id.in_(body.recipe_ids),
    )))
    for recipe_id in body.recipe_ids:
        if recipe_id not in existing:
            db.add(MediaProjectRecipe(project_id=project_id, recipe_id=recipe_id))
    _touch(project)
    _commit_and_audit(
        request,
        db,
        user.id,
        "add_media_project_recipes",
        "media_project",
        project_id,
        {"recipe_ids": body.recipe_ids},
    )
    db.refresh(project)
    return _project_out(db, project, detail=True)


@router.delete("/projects/{project_id}/recipes/{recipe_id}", response_model=MediaProjectOut)
def remove_project_recipe(
    project_id: int,
    recipe_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = _owned_project(db, user.id, project_id)
    db.execute(delete(MediaProjectRecipe).where(
        MediaProjectRecipe.project_id == project_id,
        MediaProjectRecipe.recipe_id == recipe_id,
    ))
    _touch(project)
    _commit_and_audit(
        request,
        db,
        user.id,
        "remove_media_project_recipe",
        "media_project",
        project_id,
        {"recipe_id": recipe_id},
    )
    db.refresh(project)
    return _project_out(db, project, detail=True)


def _owned_task_ids(db: Session, user_id: int, kind: str, task_ids: list[int]) -> set[int]:
    model = {
        "generation": GenTask,
        "reverse": ReverseOperation,
        "parse": ParseRecord,
        "workflow": WorkflowRun,
    }[kind]
    return set(db.scalars(select(model.id).where(model.user_id == user_id, model.id.in_(task_ids))))


@router.post("/projects/{project_id}/tasks", response_model=MediaProjectOut)
def add_project_tasks(
    project_id: int,
    body: MediaProjectTaskLinksIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = _owned_project(db, user.id, project_id)
    if _owned_task_ids(db, user.id, body.task_kind, body.task_ids) != set(body.task_ids):
        raise HTTPException(404, "任务不存在")
    existing = set(db.scalars(select(MediaProjectTask.task_id).where(
        MediaProjectTask.project_id == project_id,
        MediaProjectTask.task_kind == body.task_kind,
        MediaProjectTask.task_id.in_(body.task_ids),
    )))
    for task_id in body.task_ids:
        if task_id not in existing:
            db.add(MediaProjectTask(project_id=project_id, task_kind=body.task_kind, task_id=task_id))
    db.flush()
    if body.task_kind == "workflow":
        for workflow_run_id in body.task_ids:
            project_collection.attach_workflow_outputs(
                db,
                project=project,
                workflow_run_id=workflow_run_id,
            )
    _touch(project)
    _commit_and_audit(
        request,
        db,
        user.id,
        "add_media_project_tasks",
        "media_project",
        project_id,
        {"task_kind": body.task_kind, "task_ids": body.task_ids},
    )
    db.refresh(project)
    return _project_out(db, project, detail=True)


@router.delete("/projects/{project_id}/tasks/{task_kind}/{task_id}", response_model=MediaProjectOut)
def remove_project_task(
    project_id: int,
    task_kind: str,
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = _owned_project(db, user.id, project_id)
    db.execute(delete(MediaProjectTask).where(
        MediaProjectTask.project_id == project_id,
        MediaProjectTask.task_kind == task_kind,
        MediaProjectTask.task_id == task_id,
    ))
    _touch(project)
    _commit_and_audit(
        request,
        db,
        user.id,
        "remove_media_project_task",
        "media_project",
        project_id,
        {"task_kind": task_kind, "task_id": task_id},
    )
    db.refresh(project)
    return _project_out(db, project, detail=True)


@router.get("/asset-folders", response_model=list[AssetFolderOut])
def list_asset_folders(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = list(db.scalars(
        select(AssetFolder)
        .where(AssetFolder.user_id == user.id)
        .order_by(AssetFolder.sort_order, AssetFolder.created_at, AssetFolder.id)
    ))
    return [_folder_out(db, row, detail=False) for row in rows]


@router.post("/asset-folders", response_model=AssetFolderOut, status_code=201)
def create_asset_folder(
    body: AssetFolderCreateIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if body.parent_id is not None:
        _owned_folder(db, user.id, body.parent_id)
    duplicate = db.scalar(select(AssetFolder.id).where(
        AssetFolder.user_id == user.id,
        AssetFolder.parent_id == body.parent_id,
        AssetFolder.name == body.name,
    ))
    if duplicate is not None:
        raise HTTPException(409, "同级已存在同名文件夹")
    folder = AssetFolder(user_id=user.id, **body.model_dump())
    db.add(folder)
    db.flush()
    _commit_and_audit(
        request, db, user.id, "create_asset_folder", "asset_folder", int(folder.id)
    )
    db.refresh(folder)
    return _folder_out(db, folder, detail=True)


@router.get("/asset-folders/{folder_id}", response_model=AssetFolderOut)
def get_asset_folder(
    folder_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return _folder_out(db, _owned_folder(db, user.id, folder_id), detail=True)


def _assert_folder_parent(db: Session, user_id: int, folder_id: int, parent_id: int | None) -> None:
    current_id = parent_id
    visited = {folder_id}
    while current_id is not None:
        if current_id in visited:
            raise HTTPException(409, "文件夹不能移动到自身或子文件夹")
        visited.add(current_id)
        current = _owned_folder(db, user_id, current_id)
        current_id = current.parent_id


@router.patch("/asset-folders/{folder_id}", response_model=AssetFolderOut)
def patch_asset_folder(
    folder_id: int,
    body: AssetFolderPatchIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    folder = _owned_folder(db, user.id, folder_id)
    parent_id = None if body.move_to_root else (body.parent_id if body.parent_id is not None else folder.parent_id)
    _assert_folder_parent(db, user.id, folder_id, parent_id)
    name = body.name if body.name is not None else folder.name
    duplicate = db.scalar(select(AssetFolder.id).where(
        AssetFolder.user_id == user.id,
        AssetFolder.parent_id == parent_id,
        AssetFolder.name == name,
        AssetFolder.id != folder_id,
    ))
    if duplicate is not None:
        raise HTTPException(409, "同级已存在同名文件夹")
    folder.name = name
    folder.parent_id = parent_id
    if body.sort_order is not None:
        folder.sort_order = body.sort_order
    _commit_and_audit(
        request, db, user.id, "update_asset_folder", "asset_folder", folder_id
    )
    db.refresh(folder)
    return _folder_out(db, folder, detail=True)


@router.delete("/asset-folders/{folder_id}", status_code=204)
def delete_asset_folder(
    folder_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    folder = _owned_folder(db, user.id, folder_id)
    db.delete(folder)
    _commit_and_audit(
        request, db, user.id, "delete_asset_folder", "asset_folder", folder_id
    )
    return Response(status_code=204)


@router.post("/asset-folders/{folder_id}/assets", response_model=AssetFolderOut)
def move_assets_to_folder(
    folder_id: int,
    body: AssetRefsIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    folder = _owned_folder(db, user.id, folder_id)
    _validate_asset_refs(db, user.id, body.asset_refs)
    db.execute(delete(AssetFolderItem).where(
        AssetFolderItem.user_id == user.id,
        AssetFolderItem.asset_ref.in_(body.asset_refs),
    ))
    db.flush()
    db.add_all([
        AssetFolderItem(folder_id=folder_id, user_id=user.id, asset_ref=asset_ref)
        for asset_ref in body.asset_refs
    ])
    _commit_and_audit(
        request,
        db,
        user.id,
        "move_assets_to_folder",
        "asset_folder",
        folder_id,
        {"asset_refs": body.asset_refs},
    )
    db.refresh(folder)
    return _folder_out(db, folder, detail=True)


@router.delete("/asset-folders/{folder_id}/assets", response_model=AssetFolderOut)
def remove_assets_from_folder(
    folder_id: int,
    body: AssetRefsIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    folder = _owned_folder(db, user.id, folder_id)
    db.execute(delete(AssetFolderItem).where(
        AssetFolderItem.folder_id == folder_id,
        AssetFolderItem.user_id == user.id,
        AssetFolderItem.asset_ref.in_(body.asset_refs),
    ))
    _commit_and_audit(
        request,
        db,
        user.id,
        "remove_assets_from_folder",
        "asset_folder",
        folder_id,
        {"asset_refs": body.asset_refs},
    )
    db.refresh(folder)
    return _folder_out(db, folder, detail=True)
