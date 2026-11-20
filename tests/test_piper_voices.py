"""Piper voice discovery and config resolution. Never touches the real data dir or models."""
import pytest

from src.services import piper_voices


@pytest.fixture
def voices_dir(monkeypatch, tmp_path):
    folder = tmp_path / "voices"
    folder.mkdir()
    monkeypatch.setattr(piper_voices, "default_voices_dir", lambda: folder)
    return folder


def test_language_detection_uses_arabic_script():
    assert piper_voices.language_for_text("مرحبا بك") == "ar"
    assert piper_voices.language_for_text("Hello there") == "en"
    assert piper_voices.language_for_text("") == "en"
    assert piper_voices.language_for_text(None) == "en"


def test_default_models_are_the_documented_voices(voices_dir):
    assert piper_voices.default_model_file("en").name == "en_US-joe-medium.onnx"
    assert piper_voices.default_model_file("ar").name == "ar_JO-kareem-medium.onnx"


def test_no_model_and_no_defaults_means_no_piper(voices_dir):
    assert piper_voices.resolve_model_path("en") == ""
    assert piper_voices.resolve_model_path("ar") == ""


def test_default_model_is_used_when_downloaded(voices_dir):
    (voices_dir / "en_US-joe-medium.onnx").write_bytes(b"x")
    assert piper_voices.resolve_model_path("en") == str(voices_dir / "en_US-joe-medium.onnx")
    assert piper_voices.resolve_model_path("ar") == ""  # Arabic voice not downloaded


def test_explicit_path_wins_over_default(voices_dir, tmp_path):
    (voices_dir / "en_US-joe-medium.onnx").write_bytes(b"x")
    custom = tmp_path / "mine.onnx"
    custom.write_bytes(b"x")
    assert piper_voices.resolve_model_path("en", str(custom)) == str(custom)


def test_explicit_missing_path_does_not_silently_use_default(voices_dir, tmp_path):
    (voices_dir / "en_US-joe-medium.onnx").write_bytes(b"x")
    assert piper_voices.resolve_model_path("en", str(tmp_path / "nope.onnx")) == ""


def test_length_scale_default_and_clamping():
    assert piper_voices.parse_length_scale(None) == pytest.approx(1.06)
    assert piper_voices.parse_length_scale("") == pytest.approx(1.06)
    assert piper_voices.parse_length_scale("abc") == pytest.approx(1.06)
    assert piper_voices.parse_length_scale("nan") == pytest.approx(1.06)
    assert piper_voices.parse_length_scale("1.2") == pytest.approx(1.2)
    assert piper_voices.parse_length_scale("9") == pytest.approx(piper_voices.MAX_LENGTH_SCALE)
    assert piper_voices.parse_length_scale("0.01") == pytest.approx(piper_voices.MIN_LENGTH_SCALE)
