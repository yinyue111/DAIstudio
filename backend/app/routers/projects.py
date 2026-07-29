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
    MediaProject,
    MediaProjectAsset,
    MediaProjectRecipe,
    MediaProjectTask,
    User,
    UserDraft,
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
from ..services import (
    audit,
    project_access,
    project_collection,
    project_read_models,
    user_assets,
)

router = APIRouter(prefix="/api", tags=["projects"])


def _project_draft_key(project_id: int) -> str:
    return project_read_models.project_draft_key(project_id)


def _owned_project(db: Session, user_id: int, project_id: int) -> MediaProject:
    try:
        return project_access.owned_project(db, user_id, project_id)
    except project_access.ProjectAccessError as exc:
        raise HTTPException(exc.status_code, exc.detail) from None


def _owned_folder(db: Session, user_id: int, folder_id: int) -> AssetFolder:
    try:
        return project_access.owned_folder(db, user_id, folder_id)
    except project_access.ProjectAccessError as exc:
        raise HTTPException(exc.status_code, exc.detail) from None


def _validate_asset_refs(db: Session, user_id: int, asset_refs: list[str]) -> None:
    try:
        project_access.validate_asset_refs(db, user_id, asset_refs)
    except project_access.ProjectAccessError as exc:
        raise HTTPException(exc.status_code, exc.detail) from None


def _project_out(db: Session, project: MediaProject, *, detail: bool) -> dict:
    return project_read_models.project_out(db, project, detail=detail)


def _project_task_rows(
    db: Session,
    project: MediaProject,
    links: list[MediaProjectTask],
) -> list[dict]:
    return project_read_models._project_task_rows(db, project, links)


def _folder_out(db: Session, folder: AssetFolder, *, detail: bool) -> dict:
    return project_read_models.folder_out(db, folder, detail=detail)


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
    return project_access.owned_task_ids(db, user_id, kind, task_ids)


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
    try:
        project_access.assert_folder_parent(db, user_id, folder_id, parent_id)
    except project_access.ProjectAccessError as exc:
        raise HTTPException(exc.status_code, exc.detail) from None


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
