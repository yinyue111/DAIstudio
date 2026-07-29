"""CSV serialization for reverse-operation quality reports."""
from __future__ import annotations

import csv
import io

from fastapi.responses import StreamingResponse

from .reporting_helpers import csv_cell


def reverse_quality_csv(payload: dict) -> StreamingResponse:
    summary = payload["summary"]
    quality = payload["quality"]
    economics = payload["economics"]
    all_row = {
        "operation_count": summary["operation_count"],
        "succeeded": summary["succeeded"],
        "success_rate": summary["success_rate"],
        "adoption_rate": quality["adoption_rate"],
        "edit_rate": quality["edit_rate"],
        "avg_edit_ratio": quality["avg_edit_ratio"],
        "avg_evidence_coverage": quality["avg_evidence_coverage"],
        "generation_conversion_rate": quality["generation_conversion_rate"],
        "recipe_conversion_rate": quality["recipe_conversion_rate"],
        "useful_rate": quality["useful_rate"],
        "settled_credits": economics["revenue_credits"],
        "cost_status": economics["cost_status"],
        "provider_cost_credits": economics["provider_cost_credits"],
        "gross_profit_credits": economics["gross_profit_credits"],
        "gross_margin_rate": economics["gross_margin_rate"],
        "cost_coverage_rate": economics["gateway_cost_coverage_rate"],
    }
    dimensions = [
        ("all", "all", all_row),
        *(("media_type", row.get("media_type"), row) for row in payload["by_media_type"]),
        *(("model", row.get("model"), row) for row in payload["by_model_quality"]),
        *(("focus", row.get("focus"), row) for row in payload["by_focus"]),
    ]
    columns = [
        "dimension",
        "value",
        "operation_count",
        "succeeded",
        "success_rate",
        "avg_evidence_coverage",
        "adoption_rate",
        "edit_rate",
        "avg_edit_ratio",
        "generation_conversion_rate",
        "recipe_conversion_rate",
        "useful_rate",
        "settled_credits",
        "cost_status",
        "provider_cost_credits",
        "gross_profit_credits",
        "gross_margin_rate",
        "cost_coverage_rate",
    ]

    def iter_csv():
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        def emit(row):
            buffer.seek(0)
            buffer.truncate(0)
            writer.writerow([csv_cell(value) for value in row])
            return buffer.getvalue()

        yield "\ufeff" + emit(columns)
        for dimension, value, row in dimensions:
            yield emit([
                dimension,
                value,
                *(row.get(column, "") for column in columns[2:]),
            ])

    return StreamingResponse(
        iter_csv(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=reverse_quality_report.csv",
        },
    )
