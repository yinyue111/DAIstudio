"""Application composition root for production workflow node adapters."""
from __future__ import annotations

from .routers import generate, parse, prompt, tasks
from .services import workflow_node_adapters


def register_production_workflow_adapters(*, overwrite: bool = True) -> None:
    workflow_node_adapters.configure_workflow_node_dependencies(
        submit_parse=parse.submit_parse,
        create_reverse_operation=prompt.create_reverse_operation,
        quote_generation=generate.quote_generation,
        generate=generate.generate,
        build_task_out=tasks.build_task_out,
        cancel_task=tasks.cancel_task,
    )
    workflow_node_adapters.register_production_workflow_adapters(overwrite=overwrite)
