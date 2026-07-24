from code_review_ai.config import load_config
from code_review_ai.changes import detect_changed_symbols

from conftest import FIXTURES as FIX


def test_symbols_mode_passthrough():
    cfg = load_config(FIX)
    out = detect_changed_symbols(cfg, symbols=["auth::login"])
    assert out == ["auth::login"]


def test_files_mode_uses_git_diff(tmp_path, monkeypatch):
    cfg = load_config(FIX)
    # stub git diff to report a hunk on lines 5-6 of auth.py
    import code_review_ai.changes as ch

    monkeypatch.setattr(ch, "_git_diff", lambda base, files: {"auth.py": [(5, 6)]})
    out = detect_changed_symbols(cfg, files=["auth.py"])
    # authenticate() spans lines 2-3 in fixture; login() lines 6-7 -> line 6 hits login
    assert "auth::login" in out


def test_deleted_symbol_reported(tmp_path, monkeypatch):
    cfg = load_config(FIX)
    import code_review_ai.changes as ch

    monkeypatch.setattr(ch, "_git_diff", lambda base, files: {"auth.py": [(2, 3)]})
    out = detect_changed_symbols(cfg, files=["auth.py"])
    assert "auth::UserService.authenticate" in out
