import pytest

from code_review_ai.config import load_config
from code_review_ai.changes import detect_changed_symbols

from conftest import FIXTURES as FIX, Q


def test_symbols_mode_passthrough():
    cfg = load_config(FIX)
    out = detect_changed_symbols(cfg, symbols=[Q("auth","login")])
    assert out == [Q("auth","login")]


def test_files_mode_uses_git_diff(tmp_path, monkeypatch):
    cfg = load_config(FIX)
    # stub git diff to report a hunk on lines 5-6 of auth.py
    import code_review_ai.changes as ch

    monkeypatch.setattr(ch, "_git_diff", lambda base, files: {"auth.py": [(5, 6)]})
    out = detect_changed_symbols(cfg, files=["auth.py"])
    # authenticate() spans lines 2-3 in fixture; login() lines 6-7 -> line 6 hits login
    assert Q("auth","login") in out


def test_git_diff_failure_is_surfaced_not_swallowed(monkeypatch):
    """A bad diff_base must raise, not silently return an empty list."""
    cfg = load_config(FIX)
    import code_review_ai.changes as ch

    def bad_diff(base, files):
        raise RuntimeError("git diff failed (exit 128): fatal: bad revision 'origin/main'")

    monkeypatch.setattr(ch, "_git_diff", bad_diff)
    with pytest.raises(RuntimeError, match="bad revision"):
        detect_changed_symbols(cfg, files=["auth.py"])


def test_deleted_symbol_reported(tmp_path, monkeypatch):
    cfg = load_config(FIX)
    import code_review_ai.changes as ch

    monkeypatch.setattr(ch, "_git_diff", lambda base, files: {"auth.py": [(2, 3)]})
    out = detect_changed_symbols(cfg, files=["auth.py"])
    assert Q("auth","authenticate",Q("auth","UserService")) in out


def test_git_numstat_parses_text_and_binary(monkeypatch):
    import code_review_ai.changes as ch

    class _FakeResult:
        returncode = 0
        stdout = "10\t2\tauth.py\n-\t-\tlogo.png\n"
        stderr = ""
    monkeypatch.setattr(ch.subprocess, "run", lambda *args, **kwargs: _FakeResult())
    assert ch._git_numstat("origin/main") == {"auth.py": (10, 2), "logo.png": (0, 0)}
