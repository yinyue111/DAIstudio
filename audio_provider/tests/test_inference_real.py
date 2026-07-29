"""真实推理归一化逻辑的单测（不依赖模型权重）。

test_contract.py 全部注入 FakeEngine，真实的 FasterWhisperEngine /
PyannoteDiarizationEngine 分段过滤、时间戳取整、说话人重编号等纯逻辑
在 CI 里一行未执行。这里通过向引擎内部注入桩模型（而不是替换整个引擎）
来执行真实的归一化代码路径。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from audio_provider.app.config import ProviderSettings
from audio_provider.app.inference import (
    FasterWhisperEngine,
    PyannoteDiarizationEngine,
    assign_speaker_labels,
)


class _StubWhisperModel:
    """模拟 faster_whisper.WhisperModel.transcribe 的返回形状。"""

    def __init__(self, segments: list[Any], language: str | None = "zh"):
        self._segments = segments
        self._language = language
        self.calls: list[dict[str, Any]] = []

    def transcribe(self, path: str, **kwargs: Any):
        self.calls.append({"path": path, **kwargs})
        return iter(self._segments), SimpleNamespace(language=self._language)


def _segment(start: float, end: float, text: str) -> Any:
    return SimpleNamespace(start=start, end=end, text=text)


def _engine_with_model(model: Any, **settings: Any) -> FasterWhisperEngine:
    engine = FasterWhisperEngine(ProviderSettings(eager_load=False, **settings))
    # 直接注入已加载模型，绕过权重下载，但保留 transcribe 的真实归一化逻辑。
    engine._state.model = model
    engine._state.version = "stub-1.0"
    return engine


def test_transcribe_filters_invalid_segments_and_rounds_timestamps() -> None:
    model = _StubWhisperModel(
        [
            _segment(0.0, 1.23456, "  你好  "),
            _segment(1.5, 1.5, "端点相等要丢弃"),
            _segment(2.0, 1.0, "结束早于开始要丢弃"),
            _segment(-0.5, 1.0, "负时间戳要丢弃"),
            _segment(3.0, 4.0, "   "),
            _segment(4.4444444, 5.5555555, "世界"),
        ]
    )
    engine = _engine_with_model(model)

    result = engine.transcribe(Path("/tmp/fixture.wav"), language="zh", prompt=None)

    assert result["language"] == "zh"
    assert result["text"] == "你好 世界"
    assert result["segments"] == [
        {"id": 0, "start": 0.0, "end": 1.235, "text": "你好"},
        {"id": 5, "start": 4.444, "end": 5.556, "text": "世界"},
    ]


def test_transcribe_caps_segments_at_max_segments_setting() -> None:
    model = _StubWhisperModel(
        [_segment(float(i), float(i) + 0.5, f"第{i}段") for i in range(10)]
    )
    engine = _engine_with_model(model, max_segments=3)

    result = engine.transcribe(Path("/tmp/fixture.wav"), language=None, prompt=None)

    assert len(result["segments"]) == 3
    assert [row["id"] for row in result["segments"]] == [0, 1, 2]


def test_transcribe_passes_language_prompt_and_vad_to_model() -> None:
    model = _StubWhisperModel([_segment(0.0, 1.0, "ok")])
    engine = _engine_with_model(model, beam_size=7)

    engine.transcribe(Path("/tmp/fixture.wav"), language="en", prompt="品牌名不翻译")

    assert model.calls == [
        {
            "path": "/tmp/fixture.wav",
            "beam_size": 7,
            "language": "en",
            "initial_prompt": "品牌名不翻译",
            "vad_filter": True,
            "word_timestamps": False,
        }
    ]


def test_transcribe_empty_language_becomes_none() -> None:
    model = _StubWhisperModel([_segment(0.0, 1.0, "ok")], language="")
    engine = _engine_with_model(model)

    result = engine.transcribe(Path("/tmp/fixture.wav"), language=None, prompt=None)

    assert result["language"] is None


def test_transcribe_raises_when_model_unavailable() -> None:
    engine = FasterWhisperEngine(ProviderSettings(eager_load=False))
    engine._state.error = "ImportError: model initialization failed"

    with pytest.raises(RuntimeError, match="model initialization failed"):
        engine.transcribe(Path("/tmp/fixture.wav"), language=None, prompt=None)


class _StubTurn(SimpleNamespace):
    pass


class _StubAnnotation:
    def __init__(self, rows: list[tuple[float, float, str]]):
        self._rows = rows

    def itertracks(self, yield_label: bool = False):
        for start, end, label in self._rows:
            yield _StubTurn(start=start, end=end), "track", label


def test_diarize_sorts_rounds_and_drops_empty_turns() -> None:
    engine = PyannoteDiarizationEngine(ProviderSettings(eager_load=False))
    engine._state.model = lambda path: _StubAnnotation(
        [
            (5.5555555, 7.0, "SPEAKER_01"),
            (0.0, 2.1234567, "SPEAKER_00"),
            (3.0, 3.0, "SPEAKER_02"),  # 零长度必须丢弃
            (4.0, 3.5, "SPEAKER_03"),  # 结束早于开始必须丢弃
        ]
    )
    engine._state.version = "stub-1.0"

    turns = engine.diarize(Path("/tmp/fixture.wav"))

    assert turns == [
        {"start": 0.0, "end": 2.123, "speaker": "SPEAKER_00"},
        {"start": 5.556, "end": 7.0, "speaker": "SPEAKER_01"},
    ]


def test_diarize_raises_when_pipeline_unavailable() -> None:
    engine = PyannoteDiarizationEngine(ProviderSettings(eager_load=False))
    engine._state.error = "RuntimeError: diarization pipeline initialization failed"

    with pytest.raises(RuntimeError, match="pipeline initialization failed"):
        engine.diarize(Path("/tmp/fixture.wav"))


def test_assign_speaker_labels_picks_maximum_overlap_and_renumbers() -> None:
    segments = [
        {"id": 0, "start": 0.0, "end": 2.0, "text": "a"},
        {"id": 1, "start": 2.0, "end": 4.0, "text": "b"},
        {"id": 2, "start": 4.0, "end": 5.0, "text": "c"},
    ]
    turns = [
        {"start": 0.0, "end": 0.4, "speaker": "SPEAKER_09"},
        {"start": 0.4, "end": 2.2, "speaker": "SPEAKER_02"},  # 与段 0 重叠更多
        {"start": 2.2, "end": 5.0, "speaker": "SPEAKER_09"},
    ]

    labeled = assign_speaker_labels(segments, turns)

    assert labeled == 3
    # 按首次出现顺序重编号：SPEAKER_02 -> S1，SPEAKER_09 -> S2。
    assert [row["speaker_id"] for row in segments] == ["S1", "S2", "S2"]


def test_assign_speaker_labels_respects_max_speakers_cap() -> None:
    segments = [
        {"id": index, "start": float(index), "end": float(index) + 1.0, "text": "x"}
        for index in range(3)
    ]
    turns = [
        {"start": float(index), "end": float(index) + 1.0, "speaker": f"SPK_{index}"}
        for index in range(3)
    ]

    labeled = assign_speaker_labels(segments, turns, max_speakers=2)

    assert labeled == 2
    assert segments[0]["speaker_id"] == "S1"
    assert segments[1]["speaker_id"] == "S2"
    assert "speaker_id" not in segments[2]
