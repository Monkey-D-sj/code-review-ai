import json

from code_review_ai.review_agent import tools
from code_review_ai.review_agent.runner import resolve_api_key, resolve_setting


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
