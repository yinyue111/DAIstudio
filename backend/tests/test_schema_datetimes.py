import json
from datetime import datetime

from app.schemas import TaskOut


def test_task_response_serializes_naive_database_times_as_utc():
    task = TaskOut(
        id=1,
        category="image",
        stage="preview",
        status="succeeded",
        cost_frozen=0,
        cost_settled=0,
        created_at=datetime(2026, 7, 10, 8, 30),
        finished_at=datetime(2026, 7, 10, 8, 31),
    )

    payload = json.loads(task.model_dump_json())

    assert payload["created_at"] == "2026-07-10T08:30:00+00:00"
    assert payload["finished_at"] == "2026-07-10T08:31:00+00:00"
