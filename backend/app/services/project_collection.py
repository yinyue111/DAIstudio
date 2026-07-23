"""Automatic task and asset collection for user-owned media projects."""
from __future__ import annotations

import json
import re
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models import (
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
    UploadedAsset,
    UserDraft,
    WorkflowRun,
)
from . import retention, storage, task_center, user_assets
from .asset_output import is_generated_asset_task, is_settled_generated_asset_task
from .ssrf import local_storage_key_from_user_asset_url

_PROJECT_EXPORT_MAX_MEDIA_BYTES = 512 * 1024 * 1024
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")


class ProjectNotFound(LookupError):
    pass


@dataclass(frozen=True)
class ProjectExportBundle:
    path: Path
    filename: str
    manifest: dict


def require_owned_project(db: Session, user_id: int, project_id: int) -> MediaProject:
    project = db.scalar(
        select(MediaProject).where(
            MediaProject.id == int(project_id),
            MediaProject.user_id == int(user_id),
        )
    )
    if project is None:
        raise ProjectNotFound("项目不存在")
    return project


def primary_project_id_for_task(
    db: Session,
    *,
    user_id: int,
    task_kind: str,
    task_id: int,
) -> int | None:
    """Return the newest still-owned project associated with a task."""
    return db.scalar(
        select(MediaProjectTask.project_id)
        .join(MediaProject, MediaProject.id == MediaProjectTask.project_id)
        .where(
            MediaProjectTask.task_kind == task_kind,
            MediaProjectTask.task_id == int(task_id),
            MediaProject.user_id == int(user_id),
        )
        .order_by(MediaProjectTask.created_at.desc(), MediaProjectTask.id.desc())
        .limit(1)
    )


def _touch(project: MediaProject) -> None:
    project.updated_at = datetime.now(timezone.utc)


def attach_task(
    db: Session,
    *,
    project: MediaProject,
    task_kind: str,
    task_id: int,
) -> None:
    existing = db.scalar(
        select(MediaProjectTask.id).where(
            MediaProjectTask.project_id == int(project.id),
            MediaProjectTask.task_kind == task_kind,
            MediaProjectTask.task_id == int(task_id),
        )
    )
    if existing is None:
        db.add(
            MediaProjectTask(
                project_id=int(project.id),
                task_kind=task_kind,
                task_id=int(task_id),
            )
        )
    _touch(project)


def _root_upload_key(db: Session, user_id: int, key: str) -> str | None:
    candidates: list[str] = []
    if key.startswith(("upload/", "upload_video/")):
        candidates.append(key)
    stem = Path(key).stem
    if key.startswith(("upload_preview/", "upload_model_ref/", "preview/", "model_ref/")):
        candidates.append(f"upload/{stem}.png")
    if key.startswith("upload_video_preview/"):
        candidates.append(f"upload_video/{stem}.mp4")
    for candidate in candidates:
        row = db.get(UploadedAsset, candidate)
        if row is not None and int(row.user_id) == int(user_id):
            return candidate
    return None


def asset_ref_for_url(db: Session, user_id: int, url: str | None) -> str | None:
    """Resolve an owned local media URL to the existing unified asset namespace."""
    value = str(url or "").strip()
    if not value:
        return None
    key = local_storage_key_from_user_asset_url(value)
    candidates = {value}
    if key:
        public_url = storage.public_url(key)
        upload_url = storage.upload_api_url(key)
        if public_url:
            candidates.add(public_url)
        if upload_url:
            candidates.add(upload_url)
    generated = db.scalar(
        select(GenAsset)
        .where(
            GenAsset.user_id == int(user_id),
            or_(
                GenAsset.preview_url.in_(candidates),
                GenAsset.hd_url.in_(candidates),
            ),
        )
        .order_by(GenAsset.id.desc())
        .limit(1)
    )
    if generated is not None:
        return user_assets.generated_asset_ref(int(generated.id))
    if key:
        root_key = _root_upload_key(db, user_id, key)
        if root_key:
            return user_assets.uploaded_asset_ref(root_key)
    return None


def _add_asset(
    db: Session,
    *,
    project: MediaProject,
    asset_ref: str,
    role: str,
) -> None:
    existing = db.scalar(
        select(MediaProjectAsset.id).where(
            MediaProjectAsset.project_id == int(project.id),
            MediaProjectAsset.asset_ref == asset_ref,
        )
    )
    if existing is not None:
        return
    max_sort = db.scalar(
        select(func.max(MediaProjectAsset.sort_order)).where(
            MediaProjectAsset.project_id == int(project.id)
        )
    )
    db.add(
        MediaProjectAsset(
            project_id=int(project.id),
            asset_ref=asset_ref,
            role=(str(role or "source").strip() or "source")[:32],
            sort_order=(int(max_sort) + 1) if max_sort is not None else 0,
        )
    )
    if not project.cover_asset_ref:
        project.cover_asset_ref = asset_ref
    _touch(project)


def attach_input_urls(
    db: Session,
    *,
    project: MediaProject,
    user_id: int,
    inputs: list[tuple[str | None, str]],
) -> None:
    seen: set[str] = set()
    for url, role in inputs:
        asset_ref = asset_ref_for_url(db, user_id, url)
        if not asset_ref or asset_ref in seen:
            continue
        seen.add(asset_ref)
        _add_asset(db, project=project, asset_ref=asset_ref, role=role)


def attach_asset_refs(
    db: Session,
    *,
    project: MediaProject,
    asset_refs: list[str],
    role: str,
) -> None:
    for asset_ref in dict.fromkeys(str(item).strip() for item in asset_refs if str(item).strip()):
        _add_asset(db, project=project, asset_ref=asset_ref, role=role)


def _generated_asset_ids(asset_refs: list[str]) -> set[int]:
    result: set[int] = set()
    for ref in asset_refs:
        for prefix in ("g.", "generated:"):
            if not ref.startswith(prefix):
                continue
            try:
                asset_id = int(ref[len(prefix) :])
            except ValueError:
                break
            if asset_id > 0:
                result.add(asset_id)
            break
    return result


def workflow_run_cost_totals(
    db: Session,
    *,
    user_id: int,
    tool_run: ToolRun | None,
    nodes: list[ToolNodeRun],
) -> tuple[int, int]:
    """Sum unique billable domain tasks represented by one workflow run."""
    generation_ids, reverse_ids = task_center.workflow_external_task_ids(nodes)
    refs = task_center.workflow_output_asset_refs(tool_run.output if tool_run is not None else None)
    generated_asset_ids = _generated_asset_ids(refs)
    if generated_asset_ids:
        generation_ids.update(
            int(task_id)
            for task_id in db.scalars(
                select(GenAsset.task_id).where(
                    GenAsset.user_id == int(user_id),
                    GenAsset.id.in_(generated_asset_ids),
                )
            )
        )
    frozen = 0
    settled = 0
    if generation_ids:
        for task in db.scalars(
            select(GenTask).where(
                GenTask.user_id == int(user_id),
                GenTask.id.in_(generation_ids),
            )
        ):
            frozen += max(0, int(task.cost_frozen or 0))
            settled += max(0, int(task.cost_settled or 0))
    if reverse_ids:
        for operation in db.scalars(
            select(ReverseOperation).where(
                ReverseOperation.user_id == int(user_id),
                ReverseOperation.id.in_(reverse_ids),
            )
        ):
            frozen += max(0, int(operation.cost_frozen or 0))
            settled += max(0, int(operation.cost_settled or 0))
    return frozen, settled


def attach_workflow_outputs(
    db: Session,
    *,
    project: MediaProject,
    workflow_run_id: int,
) -> None:
    """Collect currently available, owner-resolvable workflow result media."""
    run = db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.id == int(workflow_run_id),
            WorkflowRun.user_id == int(project.user_id),
        )
    )
    if run is None:
        return
    tool_run = db.get(ToolRun, int(run.tool_run_id))
    refs = task_center.workflow_output_asset_refs(
        tool_run.output if tool_run is not None else None
    )
    owned_refs: list[str] = []
    for ref in refs:
        try:
            user_assets.resolve_asset_ref(db, int(project.user_id), ref)
        except (user_assets.InvalidAssetRef, user_assets.AssetNotFound):
            continue
        owned_refs.append(ref)
    attach_asset_refs(db, project=project, asset_refs=owned_refs, role="output")


def collect_generation_outputs(db: Session, task_id: int) -> None:
    links = list(
        db.execute(
            select(MediaProjectTask, MediaProject)
            .join(MediaProject, MediaProject.id == MediaProjectTask.project_id)
            .where(
                MediaProjectTask.task_kind == "generation",
                MediaProjectTask.task_id == int(task_id),
            )
        ).all()
    )
    if not links:
        return
    assets = list(
        db.scalars(
            select(GenAsset)
            .where(GenAsset.task_id == int(task_id))
            .order_by(GenAsset.id)
        )
    )
    for _, project in links:
        refs = [user_assets.generated_asset_ref(int(asset.id)) for asset in assets]
        attach_asset_refs(db, project=project, asset_refs=refs, role="output")


def collect_parse_outputs(db: Session, parse_id: int, assets: list[dict] | None) -> None:
    links = list(
        db.execute(
            select(MediaProjectTask, MediaProject)
            .join(MediaProject, MediaProject.id == MediaProjectTask.project_id)
            .where(
                MediaProjectTask.task_kind == "parse",
                MediaProjectTask.task_id == int(parse_id),
            )
        ).all()
    )
    if not links:
        return
    refs = [
        str(item.get("asset_ref") or "").strip()
        for item in assets or []
        if isinstance(item, dict) and item.get("asset_ref")
    ]
    for _, project in links:
        attach_asset_refs(db, project=project, asset_refs=refs, role="output")


def project_cost_totals(
    db: Session,
    *,
    project_id: int,
    user_id: int,
) -> tuple[int, int]:
    links = list(
        db.scalars(
            select(MediaProjectTask).where(MediaProjectTask.project_id == int(project_id))
        )
    )
    generation_ids = {
        int(link.task_id) for link in links if link.task_kind == "generation"
    }
    reverse_ids = {int(link.task_id) for link in links if link.task_kind == "reverse"}
    workflow_ids = {int(link.task_id) for link in links if link.task_kind == "workflow"}
    if workflow_ids:
        workflow_rows = list(
            db.scalars(
                select(WorkflowRun).where(
                    WorkflowRun.user_id == int(user_id),
                    WorkflowRun.id.in_(workflow_ids),
                )
            )
        )
        nodes_by_run: dict[int, list[ToolNodeRun]] = {}
        for node in db.scalars(
            select(ToolNodeRun).where(ToolNodeRun.workflow_run_id.in_(workflow_ids))
        ):
            nodes_by_run.setdefault(int(node.workflow_run_id), []).append(node)
        tool_runs = {
            int(row.id): row
            for row in db.scalars(
                select(ToolRun).where(
                    ToolRun.id.in_([int(run.tool_run_id) for run in workflow_rows])
                )
            )
        }
        output_asset_ids: set[int] = set()
        for run in workflow_rows:
            child_generation_ids, child_reverse_ids = task_center.workflow_external_task_ids(
                nodes_by_run.get(int(run.id), [])
            )
            generation_ids.update(child_generation_ids)
            reverse_ids.update(child_reverse_ids)
            tool_run = tool_runs.get(int(run.tool_run_id))
            refs = task_center.workflow_output_asset_refs(
                tool_run.output if tool_run is not None else None
            )
            output_asset_ids.update(_generated_asset_ids(refs))
        if output_asset_ids:
            generation_ids.update(
                int(task_id)
                for task_id in db.scalars(
                    select(GenAsset.task_id).where(
                        GenAsset.user_id == int(user_id),
                        GenAsset.id.in_(output_asset_ids),
                    )
                )
            )
    frozen = 0
    settled = 0
    if generation_ids:
        for task in db.scalars(
            select(GenTask).where(
                GenTask.user_id == int(user_id),
                GenTask.id.in_(generation_ids),
            )
        ):
            frozen += max(0, int(task.cost_frozen or 0))
            settled += max(0, int(task.cost_settled or 0))
    if reverse_ids:
        for task in db.scalars(
            select(ReverseOperation).where(
                ReverseOperation.user_id == int(user_id),
                ReverseOperation.id.in_(reverse_ids),
            )
        ):
            frozen += max(0, int(task.cost_frozen or 0))
            settled += max(0, int(task.cost_settled or 0))
    return frozen, settled


def archive_inactive_projects(
    db: Session,
    *,
    now: datetime | None = None,
    limit: int = 1000,
) -> dict[str, object]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    candidates = list(
        db.scalars(
            select(MediaProject)
            .where(
                MediaProject.status == "active",
                MediaProject.auto_archive_after_days.is_not(None),
            )
            .order_by(MediaProject.updated_at, MediaProject.id)
            .limit(max(1, min(10_000, int(limit))))
        )
    )
    archived_ids: list[int] = []
    for project in candidates:
        activity_at = project.updated_at or project.created_at
        if activity_at is None:
            continue
        if activity_at.tzinfo is None:
            activity_at = activity_at.replace(tzinfo=timezone.utc)
        else:
            activity_at = activity_at.astimezone(timezone.utc)
        inactive_seconds = (current - activity_at).total_seconds()
        required_seconds = int(project.auto_archive_after_days or 0) * 86400
        if required_seconds <= 0 or inactive_seconds < required_seconds:
            continue
        project.status = "archived"
        project.updated_at = current
        archived_ids.append(int(project.id))
    db.commit()
    return {
        "evaluated": len(candidates),
        "archived": len(archived_ids),
        "project_ids": archived_ids,
    }


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        normalized = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return normalized.astimezone(timezone.utc).isoformat()
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        default=_json_default,
    ).encode("utf-8")


def _safe_filename(value: str | None, fallback: str) -> str:
    candidate = _SAFE_FILENAME_RE.sub("_", str(value or "").strip()).strip("._")
    return (candidate[:120] or fallback).replace("..", "_")


def _generation_task_export(task: GenTask) -> dict:
    return {
        "kind": "generation",
        "id": int(task.id),
        "category": task.category,
        "stage": task.stage,
        "status": task.status,
        "phase": task.phase,
        "prompt": task.prompt,
        "params": task.params,
        "source_asset_url": task.source_asset_url,
        "source_type": task.source_type,
        "model_config_id": task.model_config_id,
        "quote_id": task.quote_id,
        "cost_frozen": max(0, int(task.cost_frozen or 0)),
        "cost_settled": max(0, int(task.cost_settled or 0)),
        "error": task.error,
        "created_at": task.created_at,
        "finished_at": task.finished_at,
    }


def _reverse_task_export(task: ReverseOperation) -> dict:
    return {
        "kind": "reverse",
        "id": int(task.id),
        "target": task.target,
        "status": task.status,
        "phase": task.phase,
        "progress": max(0, min(100, int(task.progress or 0))),
        "analysis_focus": task.analysis_focus,
        "analysis_precision": task.analysis_precision,
        "output_purpose": task.output_purpose,
        "custom_instruction": task.custom_instruction,
        "include_audio": bool(task.include_audio),
        "source_range": task.source_range,
        "source_ranges": task.source_ranges,
        "request_context": task.request_context,
        "model_snapshot": task.model_snapshot,
        "template_snapshot": task.template_snapshot,
        "pricing_snapshot": task.pricing_snapshot,
        "normalized_result": task.normalized_result or task.result,
        "result_schema_version": task.result_schema_version,
        "cost_frozen": max(0, int(task.cost_frozen or 0)),
        "cost_settled": max(0, int(task.cost_settled or 0)),
        "error_code": task.error_code,
        "error": task.error,
        "created_at": task.created_at,
        "finished_at": task.finished_at,
    }


def _parse_task_export(task: ParseRecord) -> dict:
    return {
        "kind": "parse",
        "id": int(task.id),
        "url": task.url,
        "status": task.status,
        "assets": task.assets,
        "error": task.error,
        "created_at": task.created_at,
    }


def _workflow_task_export(db: Session, project: MediaProject, run: WorkflowRun) -> dict:
    tool_run = db.get(ToolRun, int(run.tool_run_id))
    tool = (
        db.get(ToolDefinition, int(tool_run.tool_definition_id))
        if tool_run is not None
        else None
    )
    nodes = list(
        db.scalars(
            select(ToolNodeRun)
            .where(ToolNodeRun.workflow_run_id == int(run.id))
            .order_by(ToolNodeRun.topological_index, ToolNodeRun.id)
        )
    )
    frozen, settled = workflow_run_cost_totals(
        db,
        user_id=int(project.user_id),
        tool_run=tool_run,
        nodes=nodes,
    )
    return {
        "kind": "workflow",
        "id": int(run.id),
        "tool_run_id": int(run.tool_run_id),
        "tool_definition": (
            {
                "id": int(tool.id),
                "slug": tool.slug,
                "name": tool.name,
                "category": tool.category,
                "entry_path": tool.entry_path,
            }
            if tool is not None
            else None
        ),
        "tool_version_id": int(tool_run.tool_version_id) if tool_run is not None else None,
        "status": run.status,
        "current_node_key": run.current_node_key,
        "progress": task_center.workflow_node_progress(nodes),
        "workflow_schema_version": run.workflow_schema_version,
        "workflow_snapshot": run.workflow_snapshot,
        "input": tool_run.input_snapshot if tool_run is not None else None,
        "output": tool_run.output if tool_run is not None else None,
        "result_refs": task_center.workflow_output_asset_refs(
            tool_run.output if tool_run is not None else None
        ),
        "nodes": [
            {
                "key": node.node_key,
                "type": node.node_type,
                "depends_on": list(node.depends_on or []),
                "status": node.status,
                "input": node.input_snapshot,
                "output": node.output,
                "error_code": node.error_code,
                "error": node.error,
                "external_kind": node.external_kind,
                "external_id": node.external_id,
                "attempt_count": max(0, int(node.attempt_count or 0)),
                "max_attempts": max(1, int(node.max_attempts or 1)),
                "compensation_status": node.compensation_status,
                "compensation_error": node.compensation_error,
                "started_at": node.started_at,
                "finished_at": node.finished_at,
            }
            for node in nodes
        ],
        "cost_frozen": frozen,
        "cost_settled": settled,
        "error_code": run.error_code,
        "error": run.error,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "updated_at": run.updated_at,
    }


def _project_task_exports(db: Session, project: MediaProject) -> list[dict]:
    links = list(
        db.scalars(
            select(MediaProjectTask)
            .where(MediaProjectTask.project_id == int(project.id))
            .order_by(MediaProjectTask.created_at, MediaProjectTask.id)
        )
    )
    output: list[dict] = []
    for link in links:
        model = {
            "generation": GenTask,
            "reverse": ReverseOperation,
            "parse": ParseRecord,
            "workflow": WorkflowRun,
        }[link.task_kind]
        task = db.scalar(
            select(model).where(
                model.id == int(link.task_id),
                model.user_id == int(project.user_id),
            )
        )
        if task is None:
            output.append({
                "kind": link.task_kind,
                "id": int(link.task_id),
                "status": "missing",
            })
        elif link.task_kind == "generation":
            output.append(_generation_task_export(task))
        elif link.task_kind == "reverse":
            output.append(_reverse_task_export(task))
        elif link.task_kind == "workflow":
            output.append(_workflow_task_export(db, project, task))
        else:
            output.append(_parse_task_export(task))
    return output


def _project_recipe_exports(db: Session, project: MediaProject) -> list[dict]:
    links = list(
        db.scalars(
            select(MediaProjectRecipe)
            .where(MediaProjectRecipe.project_id == int(project.id))
            .order_by(MediaProjectRecipe.created_at, MediaProjectRecipe.id)
        )
    )
    exports: list[dict] = []
    for link in links:
        recipe = db.scalar(
            select(CreationRecipe).where(
                CreationRecipe.id == int(link.recipe_id),
                CreationRecipe.user_id == int(project.user_id),
                CreationRecipe.deleted_at.is_(None),
            )
        )
        if recipe is None:
            exports.append({"recipe_id": int(link.recipe_id), "status": "missing"})
            continue
        version = db.scalar(
            select(CreationRecipeVersion).where(
                CreationRecipeVersion.recipe_id == int(recipe.id),
                CreationRecipeVersion.version == int(recipe.current_version),
            )
        )
        exports.append({
            "recipe_id": int(recipe.id),
            "title": recipe.title,
            "category": recipe.category,
            "visibility": recipe.visibility,
            "current_version": int(recipe.current_version),
            "schema_version": version.schema_version if version is not None else None,
            "payload": version.payload if version is not None else None,
            "created_at": recipe.created_at,
            "updated_at": recipe.updated_at,
        })
    return exports


def _authorized_export_media(
    db: Session,
    resolved: user_assets.ResolvedAsset,
) -> tuple[Path | None, str | None, str | None]:
    row = resolved.row
    retention_days = retention.get_retention_days(db)
    if row.retained_at is None and retention.is_expired(row.created_at, retention_days):
        return None, None, "素材已超过保留期"
    if resolved.origin == "uploaded":
        assert isinstance(row, UploadedAsset)
        key = row.key
        filename = _safe_filename(row.original_filename, Path(key).name)
    else:
        assert isinstance(row, GenAsset)
        if row.moderation_status != "active":
            return None, None, "素材已下架"
        task = db.get(GenTask, row.task_id) if row.task_id else None
        authorized = bool(row.unlocked) or (
            is_generated_asset_task(task) and is_settled_generated_asset_task(task)
        )
        if not authorized:
            return None, None, "原始媒体未解锁或任务尚未结算"
        key = storage.key_from_url(str(row.hd_url or row.preview_url or ""))
        if not key:
            return None, None, "媒体不在平台受控存储中"
        filename = _safe_filename(Path(key).name, f"asset-{row.id}")
    try:
        path = storage.local_path(key)
    except Exception as error:  # noqa: BLE001
        return None, None, (str(error).strip() or "媒体读取失败")[:180]
    if (
        not path.exists()
        or not path.is_file()
        or not storage.materialized_path_matches_key(path, key)
    ):
        return None, None, "平台原始媒体文件不存在"
    return path, filename, None


def build_project_export(
    db: Session,
    *,
    project: MediaProject,
    include_media: bool = True,
) -> ProjectExportBundle:
    links = list(
        db.scalars(
            select(MediaProjectAsset)
            .where(MediaProjectAsset.project_id == int(project.id))
            .order_by(MediaProjectAsset.sort_order, MediaProjectAsset.created_at, MediaProjectAsset.id)
        )
    )
    metadata = user_assets.metadata_for_asset_refs(
        db,
        int(project.user_id),
        [link.asset_ref for link in links],
    )
    task_exports = _project_task_exports(db, project)
    recipe_exports = _project_recipe_exports(db, project)
    draft = db.scalar(
        select(UserDraft).where(
            UserDraft.user_id == int(project.user_id),
            UserDraft.key == f"project-{int(project.id)}",
        )
    )
    cost_frozen, cost_settled = project_cost_totals(
        db,
        project_id=int(project.id),
        user_id=int(project.user_id),
    )
    asset_manifest: list[dict] = []
    resolved_by_ref: dict[str, user_assets.ResolvedAsset] = {}
    for link in links:
        try:
            resolved_by_ref[link.asset_ref] = user_assets.resolve_asset_ref(
                db,
                int(project.user_id),
                link.asset_ref,
            )
        except (user_assets.InvalidAssetRef, user_assets.AssetNotFound):
            pass
        metadata_row = metadata.get(link.asset_ref)
        asset_manifest.append({
            "asset_ref": link.asset_ref,
            "role": link.role,
            "sort_order": int(link.sort_order or 0),
            "note": link.note,
            "metadata": user_assets.metadata_payload(metadata_row) if metadata_row else None,
            "media_included": False,
            "media_path": None,
            "media_exclusion_reason": None,
            "linked_at": link.created_at,
        })

    tmp = tempfile.NamedTemporaryFile(prefix=f"project-{project.id}-", suffix=".zip", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    total_media_bytes = 0
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            project_payload = {
                "schema_version": "media-project-export.v1",
                "exported_at": datetime.now(timezone.utc),
                "project": {
                    "id": int(project.id),
                    "title": project.title,
                    "description": project.description,
                    "project_type": project.project_type,
                    "status": project.status,
                    "cover_asset_ref": project.cover_asset_ref,
                    "auto_archive_after_days": project.auto_archive_after_days,
                    "cost_frozen": cost_frozen,
                    "cost_settled": cost_settled,
                    "created_at": project.created_at,
                    "updated_at": project.updated_at,
                },
            }
            archive.writestr("project.json", _json_bytes(project_payload))
            archive.writestr("tasks/tasks.json", _json_bytes(task_exports))
            archive.writestr(
                "drafts/project-draft.json",
                _json_bytes({
                    "key": f"project-{int(project.id)}",
                    "payload": dict(draft.payload or {}) if draft is not None else {},
                    "updated_at": draft.updated_at if draft is not None else None,
                }),
            )
            archive.writestr("recipes/index.json", _json_bytes(recipe_exports))
            for recipe in recipe_exports:
                recipe_id = recipe.get("recipe_id")
                archive.writestr(
                    f"recipes/recipe-{recipe_id}.json",
                    _json_bytes(recipe),
                )

            for index, (link, manifest_row) in enumerate(zip(links, asset_manifest, strict=True), start=1):
                if not include_media:
                    manifest_row["media_exclusion_reason"] = "导出未请求原始媒体"
                    continue
                resolved = resolved_by_ref.get(link.asset_ref)
                if resolved is None:
                    manifest_row["media_exclusion_reason"] = "素材不存在或已过期"
                    continue
                path, filename, reason = _authorized_export_media(db, resolved)
                if path is None or filename is None:
                    manifest_row["media_exclusion_reason"] = reason or "原始媒体未授权"
                    continue
                item_bytes = max(0, int(path.stat().st_size))
                if total_media_bytes + item_bytes > _PROJECT_EXPORT_MAX_MEDIA_BYTES:
                    manifest_row["media_exclusion_reason"] = "项目媒体超过 512MB 导出上限"
                    continue
                arcname = f"media/{index:03d}-{filename}"
                archive.write(path, arcname=arcname)
                total_media_bytes += item_bytes
                manifest_row["media_included"] = True
                manifest_row["media_path"] = arcname
            archive.writestr("assets/manifest.json", _json_bytes(asset_manifest))
        filename = f"project-{int(project.id)}-{_safe_filename(project.title, 'export')}.zip"
        return ProjectExportBundle(
            path=tmp_path,
            filename=filename,
            manifest={
                "assets": asset_manifest,
                "included_media_count": sum(bool(item["media_included"]) for item in asset_manifest),
                "total_media_bytes": total_media_bytes,
            },
        )
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
