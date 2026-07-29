"""Read-model assembly for user-owned projects and asset folders."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

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
    UserDraft,
    WorkflowRun,
)
from . import project_collection, task_center, user_assets


def project_draft_key(project_id: int) -> str:
    return f"project-{int(project_id)}"


def _truncate(value: object, limit: int = 180) -> str | None:
    text = " ".join(str(value or "").split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 3] + "..."


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
    version_map = {(int(row.recipe_id), int(row.version)): row for row in versions}
    output: list[dict] = []
    for link in links:
        recipe = recipes.get(int(link.recipe_id))
        version = (
            version_map.get((int(recipe.id), int(recipe.current_version)))
            if recipe is not None
            else None
        )
        output.append(
            {
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
            }
        )
    return output


def _task_rows_by_kind(
    db: Session,
    project: MediaProject,
    links: list[MediaProjectTask],
) -> tuple[dict[int, GenTask], dict[int, ReverseOperation], dict[int, ParseRecord], dict[int, WorkflowRun]]:
    by_kind = {
        kind: [int(link.task_id) for link in links if link.task_kind == kind]
        for kind in ("generation", "reverse", "parse", "workflow")
    }
    generation_rows = (
        {
            int(row.id): row
            for row in db.scalars(
                select(GenTask).where(
                    GenTask.user_id == project.user_id,
                    GenTask.id.in_(by_kind["generation"]),
                )
            )
        }
        if by_kind["generation"]
        else {}
    )
    reverse_rows = (
        {
            int(row.id): row
            for row in db.scalars(
                select(ReverseOperation).where(
                    ReverseOperation.user_id == project.user_id,
                    ReverseOperation.id.in_(by_kind["reverse"]),
                )
            )
        }
        if by_kind["reverse"]
        else {}
    )
    parse_rows = (
        {
            int(row.id): row
            for row in db.scalars(
                select(ParseRecord).where(
                    ParseRecord.user_id == project.user_id,
                    ParseRecord.id.in_(by_kind["parse"]),
                )
            )
        }
        if by_kind["parse"]
        else {}
    )
    workflow_rows = (
        {
            int(row.id): row
            for row in db.scalars(
                select(WorkflowRun).where(
                    WorkflowRun.user_id == project.user_id,
                    WorkflowRun.id.in_(by_kind["workflow"]),
                )
            )
        }
        if by_kind["workflow"]
        else {}
    )
    return generation_rows, reverse_rows, parse_rows, workflow_rows


def _workflow_read_rows(
    db: Session,
    workflow_rows: dict[int, WorkflowRun],
) -> tuple[dict[int, ToolRun], dict[int, ToolDefinition], dict[int, list[ToolNodeRun]]]:
    if not workflow_rows:
        return {}, {}, {}
    tool_run_ids = [int(row.tool_run_id) for row in workflow_rows.values()]
    tool_runs = {
        int(row.id): row
        for row in db.scalars(select(ToolRun).where(ToolRun.id.in_(tool_run_ids)))
    }
    tool_definition_ids = {int(row.tool_definition_id) for row in tool_runs.values()}
    tools_by_id = {
        int(row.id): row
        for row in db.scalars(
            select(ToolDefinition).where(ToolDefinition.id.in_(tool_definition_ids))
        )
    }
    tools: dict[int, ToolDefinition] = {}
    for run in workflow_rows.values():
        tool_run = tool_runs.get(int(run.tool_run_id))
        if tool_run is not None:
            tool = tools_by_id.get(int(tool_run.tool_definition_id))
            if tool is not None:
                tools[int(run.id)] = tool
    nodes: dict[int, list[ToolNodeRun]] = {}
    for node in db.scalars(
        select(ToolNodeRun)
        .where(ToolNodeRun.workflow_run_id.in_(workflow_rows))
        .order_by(ToolNodeRun.workflow_run_id, ToolNodeRun.topological_index, ToolNodeRun.id)
    ):
        nodes.setdefault(int(node.workflow_run_id), []).append(node)
    return tool_runs, tools, nodes


def _generation_result_refs(
    db: Session,
    generation_rows: dict[int, GenTask],
) -> dict[int, list[str]]:
    output: dict[int, list[str]] = {}
    if not generation_rows:
        return output
    for task_id, asset_id in db.execute(
        select(GenAsset.task_id, GenAsset.id)
        .where(GenAsset.task_id.in_(generation_rows))
        .order_by(GenAsset.id)
    ):
        output.setdefault(int(task_id), []).append(
            user_assets.generated_asset_ref(int(asset_id))
        )
    return output


def _generation_task_row(link: MediaProjectTask, task: GenTask | None, refs: list[str]) -> dict:
    prompt = task.prompt if task is not None and isinstance(task.prompt, dict) else {}
    terminal = task is not None and task.status in {"succeeded", "failed", "canceled"}
    return {
        "id": link.id,
        "task_kind": link.task_kind,
        "task_id": link.task_id,
        "created_at": link.created_at,
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
            prompt.get("final_text") or prompt.get("instruction") or prompt.get("optimized_text")
        ),
        "result_refs": refs,
        "cost_frozen": max(0, int(task.cost_frozen or 0)) if task is not None else 0,
        "cost_settled": max(0, int(task.cost_settled or 0)) if task is not None else 0,
        "error": task.error if task is not None else None,
        "finished_at": task.finished_at if task is not None else None,
    }


def _reverse_task_row(link: MediaProjectTask, task: ReverseOperation | None) -> dict:
    terminal = task is not None and task.status in {"succeeded", "failed", "canceled"}
    return {
        "id": link.id,
        "task_kind": link.task_kind,
        "task_id": link.task_id,
        "created_at": link.created_at,
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
    }


def _workflow_task_row(
    db: Session,
    project: MediaProject,
    link: MediaProjectTask,
    run: WorkflowRun | None,
    tool_run: ToolRun | None,
    tool: ToolDefinition | None,
    nodes: list[ToolNodeRun],
) -> dict:
    refs = task_center.workflow_output_asset_refs(tool_run.output if tool_run is not None else None)
    frozen, settled = project_collection.workflow_run_cost_totals(
        db,
        user_id=int(project.user_id),
        tool_run=tool_run,
        nodes=nodes,
    )
    return {
        "id": link.id,
        "task_kind": link.task_kind,
        "task_id": link.task_id,
        "created_at": link.created_at,
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
        "workflow_current_node_key": run.current_node_key if run is not None else None,
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
    }


def _parse_task_row(link: MediaProjectTask, task: ParseRecord | None) -> dict:
    return {
        "id": link.id,
        "task_kind": link.task_kind,
        "task_id": link.task_id,
        "created_at": link.created_at,
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
    }


def _project_task_rows(
    db: Session,
    project: MediaProject,
    links: list[MediaProjectTask],
) -> list[dict]:
    generation_rows, reverse_rows, parse_rows, workflow_rows = _task_rows_by_kind(
        db, project, links
    )
    workflow_tool_runs, workflow_tools, workflow_nodes = _workflow_read_rows(
        db, workflow_rows
    )
    result_refs = _generation_result_refs(db, generation_rows)
    output: list[dict] = []
    for link in links:
        task_id = int(link.task_id)
        if link.task_kind == "generation":
            output.append(
                _generation_task_row(link, generation_rows.get(task_id), result_refs.get(task_id, []))
            )
        elif link.task_kind == "reverse":
            output.append(_reverse_task_row(link, reverse_rows.get(task_id)))
        elif link.task_kind == "workflow":
            run = workflow_rows.get(task_id)
            tool_run = workflow_tool_runs.get(int(run.tool_run_id)) if run is not None else None
            output.append(
                _workflow_task_row(
                    db,
                    project,
                    link,
                    run,
                    tool_run,
                    workflow_tools.get(task_id),
                    workflow_nodes.get(task_id, []),
                )
            )
        else:
            output.append(_parse_task_row(link, parse_rows.get(task_id)))
    return output


def project_out(db: Session, project: MediaProject, *, detail: bool) -> dict:
    assets = list(
        db.scalars(
            select(MediaProjectAsset)
            .where(MediaProjectAsset.project_id == project.id)
            .order_by(
                MediaProjectAsset.sort_order,
                MediaProjectAsset.created_at,
                MediaProjectAsset.id,
            )
        )
    )
    recipes = list(
        db.scalars(
            select(MediaProjectRecipe)
            .where(MediaProjectRecipe.project_id == project.id)
            .order_by(MediaProjectRecipe.created_at, MediaProjectRecipe.id)
        )
    )
    tasks = list(
        db.scalars(
            select(MediaProjectTask)
            .where(MediaProjectTask.project_id == project.id)
            .order_by(MediaProjectTask.created_at, MediaProjectTask.id)
        )
    )
    draft_key = project_draft_key(int(project.id))
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
    asset_rows: list[dict] = []
    recipe_rows: list[dict] = []
    task_rows: list[dict] = []
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


def folder_out(db: Session, folder: AssetFolder, *, detail: bool) -> dict:
    items = list(
        db.scalars(
            select(AssetFolderItem)
            .where(AssetFolderItem.folder_id == folder.id)
            .order_by(AssetFolderItem.created_at, AssetFolderItem.id)
        )
    )
    item_rows: list[dict] = []
    if detail and items:
        asset_views = user_assets.asset_items_for_refs(
            db,
            int(folder.user_id),
            [item.asset_ref for item in items],
        )
        for item in items:
            view = asset_views.get(item.asset_ref) or {}
            item_rows.append(
                {
                    "asset_ref": item.asset_ref,
                    "type": view.get("type"),
                    "url": view.get("url"),
                    "preview_url": view.get("preview_url"),
                    "thumb": view.get("thumb"),
                    "filename": view.get("filename"),
                    "available": bool(view.get("available")),
                    "created_at": item.created_at,
                }
            )
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
