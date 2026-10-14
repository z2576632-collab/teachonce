"""Tests for the OpenAdapt GUI-recording adapter.

Offline: recording exports are plain dicts / temp JSON files; no OpenAdapt
package is installed or required. Consent is exercised via monkeypatched
environment.
"""

from __future__ import annotations

import json

import pytest

from teachonce.adapters.openadapt import (
    CONSENT_ENV,
    gui_observation_consented,
    import_gui_recording,
)
from teachonce.errors import ConsentRequiredError, SchemaValidationError, SensitiveContentError


@pytest.fixture(autouse=True)
def _consent(monkeypatch):
    monkeypatch.setenv(CONSENT_ENV, "1")


RECORDING = {
    "name": "把下载文件夹里的课程资料整理到 文档/课程/",
    "events": [
        {"type": "mouse_move", "x": 10, "y": 20},
        {"type": "left_click", "target": "下载文件夹"},
        {"type": "keypress", "text": "数据结构", "target": "新建文件夹命名框"},
        {"type": "drag", "target": "ch3_slides.pdf -> 数据结构/课件"},
    ],
}


def test_import_recording_dict(store):
    demo = import_gui_recording(store, RECORDING)
    assert demo.task_goal == RECORDING["name"]
    assert [s.action for s in demo.steps] == [
        "left_click",
        "keypress: 数据结构",
        "drag",
    ]
    assert demo.steps[1].target == "新建文件夹命名框"
    assert "gui-recording" in demo.tags and "openadapt" in demo.tags
    assert store.get_demonstration(demo.id).id == demo.id


def test_import_recording_from_file(store, tmp_path):
    path = tmp_path / "rec.json"
    path.write_text(json.dumps(RECORDING), encoding="utf-8")
    demo = import_gui_recording(store, path, context="用户授权导入")
    assert demo.context == "用户授权导入"
    assert len(demo.steps) == 3


def test_consent_is_required(store, monkeypatch):
    monkeypatch.delenv(CONSENT_ENV)
    assert not gui_observation_consented()
    with pytest.raises(ConsentRequiredError, match=CONSENT_ENV):
        import_gui_recording(store, RECORDING)


def test_consent_rejects_lookalike_values(store, monkeypatch):
    monkeypatch.setenv(CONSENT_ENV, "enabled")
    with pytest.raises(ConsentRequiredError):
        import_gui_recording(store, RECORDING)


def test_empty_recording_rejected(store):
    with pytest.raises(SchemaValidationError, match="no importable"):
        import_gui_recording(store, {"events": [{"type": "mouse_move"}]})


def test_missing_event_list_rejected(store):
    with pytest.raises(SchemaValidationError, match="events"):
        import_gui_recording(store, {"foo": "bar"})


def test_invalid_json_file_rejected(store, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SchemaValidationError, match="not valid JSON"):
        import_gui_recording(store, path)


def test_missing_task_goal_rejected(store):
    with pytest.raises(SchemaValidationError, match="task_goal"):
        import_gui_recording(store, {"events": [{"type": "click", "target": "x"}]})


def test_secrets_in_recording_are_blocked(store):
    recording = {
        "name": "登录内部系统",
        "events": [{"type": "keypress", "text": "password=Hunter2!", "target": "密码框"}],
    }
    with pytest.raises(SensitiveContentError):
        import_gui_recording(store, recording)
    assert store.list_demonstrations() == []
