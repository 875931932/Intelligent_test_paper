"""知识队列消费配置检查。"""

from __future__ import annotations

from pathlib import Path

from app.infrastructure.tasks.celery_app import celery_app


def test_knowledge_route_and_beat_schedule():
    assert celery_app.conf.task_routes["knowledge.job"]["queue"] == "knowledge"
    beat = celery_app.conf.beat_schedule["knowledge-dispatch-outbox"]
    assert beat["task"] == "knowledge.dispatch_outbox"
    assert "knowledge.dispatch_outbox" in celery_app.tasks


def test_start_script_starts_knowledge_worker():
    script = (Path(__file__).resolve().parents[2] / "start_worker.ps1").read_text(encoding="utf-8")
    assert "'-Q', 'knowledge'" in script
    assert "'beat'" in script
