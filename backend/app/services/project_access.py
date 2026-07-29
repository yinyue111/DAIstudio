"""Ownership and validation rules shared by project HTTP operations."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    AssetFolder,
    GenTask,
    MediaProject,
    ParseRecord,
    ReverseOperation,
    WorkflowRun,
)
from . import project_collection, user_assets


class ProjectAccessError(LookupError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def owned_project(db: Session, user_id: int, project_id: int) -> MediaProject:
    try:
        return project_collection.require_owned_project(db, user_id, project_id)
    except project_collection.ProjectNotFound as exc:
        raise ProjectAccessError(404, str(exc)) from None


def owned_folder(db: Session, user_id: int, folder_id: int) -> AssetFolder:
    folder = db.scalar(
        select(AssetFolder).where(
            AssetFolder.id == folder_id,
            AssetFolder.user_id == user_id,
        )
    )
    if folder is None:
        raise ProjectAccessError(404, "素材文件夹不存在")
    return folder


def validate_asset_refs(db: Session, user_id: int, asset_refs: list[str]) -> None:
    try:
        user_assets.resolve_asset_refs(db, user_id, asset_refs)
    except user_assets.InvalidAssetRef as exc:
        raise ProjectAccessError(400, str(exc)) from None
    except user_assets.AssetNotFound as exc:
        raise ProjectAccessError(404, str(exc)) from None


def owned_task_ids(
    db: Session,
    user_id: int,
    kind: str,
    task_ids: list[int],
) -> set[int]:
    model = {
        "generation": GenTask,
        "reverse": ReverseOperation,
        "parse": ParseRecord,
        "workflow": WorkflowRun,
    }[kind]
    return set(
        db.scalars(
            select(model.id).where(
                model.user_id == user_id,
                model.id.in_(task_ids),
            )
        )
    )


def assert_folder_parent(
    db: Session,
    user_id: int,
    folder_id: int,
    parent_id: int | None,
) -> None:
    current_id = parent_id
    visited = {folder_id}
    while current_id is not None:
        if current_id in visited:
            raise ProjectAccessError(409, "文件夹不能移动到自身或子文件夹")
        visited.add(current_id)
        current = owned_folder(db, user_id, current_id)
        current_id = current.parent_id
