import json

import pytest

from code_review_ai.review_agent import tools
from code_review_ai.review_agent.runner import (
    _create_model,
    _numeric_setting,
    resolve_api_key,
    resolve_setting,
)


def test_resolve_api_key_reads_local_dotenv_without_overriding_process(tmp_path,
                                                                        monkeypatch):
    (tmp_path / ".env").write_text("LOCAL_KEY=from-dotenv\n", encoding="utf-8")
    assert resolve_api_key(str(tmp_path), "LOCAL_KEY") == "from-dotenv"
    monkeypatch.setenv("LOCAL_KEY", "from-process")
    assert resolve_api_key(str(tmp_path), "LOCAL_KEY") == "from-process"
    assert resolve_setting(str(tmp_path), "LOCAL_KEY") == "from-process"


def test_read_file_is_bounded_line_numbered_and_contained(tmp_path):
    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "app.py"
    source.write_text("one\ntwo\nthree\n", encoding="utf-8")

    assert tools.read_file(str(tmp_path), "src/app.py", 2, 3) == "2: two\n3: three"
    outside = json.loads(tools.read_file(str(tmp_path), "../outside.py", 1, 1))
    assert outside["status"] == "rejected_policy"
    assert "error" in tools.read_file(str(tmp_path), "src", 1, 1)
    assert "exceed" in tools.read_file(str(tmp_path), "src/app.py", 1, 201)


def test_search_code_marks_invalid_scope_as_policy_rejection(tmp_path):
    result = json.loads(tools.search_code(str(tmp_path), "needle", "../outside"))
    assert result["status"] == "rejected_policy"


def test_search_code_uses_fixed_non_shell_argv_and_normalizes_hits(tmp_path, monkeypatch):
    source = tmp_path / "app.py"
    source.write_text("TOKEN = 1\n", encoding="utf-8")
    observed = {}

    def fake_run(args, **kwargs):
        observed["args"] = args
        observed.update(kwargs)
        class Completed:
            returncode = 0
            stdout = f"{source}:1:TOKEN = 1\n"
            stderr = ""
        return Completed()

    monkeypatch.setattr(tools.shutil, "which", lambda name: "rg.exe")
    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    output = tools.search_code(str(tmp_path), "TOKEN", ".", "*.py", 10)

    assert output == "app.py:1:TOKEN = 1"
    assert observed["shell"] is False
    assert "--fixed-strings" in observed["args"]
    assert "TOKEN" in observed["args"]
    assert "--pre" not in observed["args"]


def test_search_code_treats_no_matches_as_success(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda name: "rg")
    monkeypatch.setattr(tools.subprocess, "run", lambda *args, **kwargs: type(
        "Completed", (), {"returncode": 1, "stdout": "", "stderr": ""})())
    assert tools.search_code(str(tmp_path), "missing") == "(no matches)"
    assert json.loads(tools.search_code(str(tmp_path), "", "."))["error"]


def test_search_code_uses_bounded_python_fallback_when_rg_is_unavailable(tmp_path,
                                                                           monkeypatch):
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("TOKEN = 1\n", encoding="utf-8")
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)

    assert tools.search_code(str(tmp_path), "TOKEN", ".", "*.py") == (
        "src/app.py:1:TOKEN = 1")


def test_read_file_bounds_long_lines_and_total_output(tmp_path):
    source = tmp_path / "bundle.js"
    minified = "x" * 4_000
    source.write_text(f"{minified}\n{minified}\n", encoding="utf-8")

    output = tools.read_file(str(tmp_path), "bundle.js", 1, 2)

    assert len(output) < tools._MAX_READ_CHARS + 200
    assert "chars truncated" in output
    assert output.startswith("1: xxx")


def test_read_file_truncates_when_the_range_exceeds_the_character_cap(tmp_path):
    source = tmp_path / "wide.py"
    source.write_text("\n".join("y" * 400 for _ in range(200)), encoding="utf-8")

    output = tools.read_file(str(tmp_path), "wide.py", 1, 200)

    assert len(output) < tools._MAX_READ_CHARS + 200
    assert "output truncated at" in output
    assert "re-read from line" in output


def test_read_file_refuses_a_file_larger_than_the_size_guard(tmp_path, monkeypatch):
    source = tmp_path / "huge.py"
    source.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(tools, "_MAX_READ_FILE_BYTES", 4)

    rejected = json.loads(tools.read_file(str(tmp_path), "huge.py", 1, 1))

    assert rejected["status"] == "rejected_policy"
    assert "larger than" in rejected["error"]


def test_created_model_bounds_every_request_and_retries_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_KEY", "sk-test")

    model = _create_model("gpt-x", None, "MODEL_KEY", str(tmp_path))

    # LangChain leaves request_timeout=None by default, which discards the OpenAI
    # SDK default too and leaves a stalled socket hanging the whole review.
    assert model.request_timeout == 180.0
    assert model.max_retries == 3
    assert model.root_client._client.timeout.read == 180.0


def test_request_bounds_are_overridable_without_touching_code(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_KEY", "sk-test")
    (tmp_path / ".env").write_text("CRAI_REVIEW_MAX_RETRIES=1\n",
                                   encoding="utf-8")
    monkeypatch.setenv("CRAI_REVIEW_TIMEOUT_SECONDS", "12.5")

    model = _create_model("gpt-x", None, "MODEL_KEY", str(tmp_path))

    assert model.request_timeout == 12.5
    assert model.max_retries == 1


def test_numeric_setting_rejects_unusable_overrides(tmp_path, monkeypatch):
    assert _numeric_setting(str(tmp_path), "CRAI_MISSING", 7.0, float) == 7.0
    monkeypatch.setenv("CRAI_BAD", "not-a-number")
    with pytest.raises(ValueError, match="must be a number"):
        _numeric_setting(str(tmp_path), "CRAI_BAD", 7.0, float)
    monkeypatch.setenv("CRAI_BAD", "0")
    with pytest.raises(ValueError, match="greater than zero"):
        _numeric_setting(str(tmp_path), "CRAI_BAD", 7.0, float)
