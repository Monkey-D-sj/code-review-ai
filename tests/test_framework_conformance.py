from __future__ import annotations

import json
import subprocess
from pathlib import Path

from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import rebuild
from code_review_ai.parser import parse_file


def _build(repo: Path):
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    cfg = load_config(str(repo))
    cfg.repo_path = str(repo)
    cfg.db_path = str(repo / "framework-index.db")
    cfg.community_detection = False
    conn = connect(cfg.db_path)
    init_schema(conn)
    stats = rebuild(cfg, conn)
    return conn, stats


def _targets(conn, source: str, kind: str = "call") -> set[str]:
    return {
        row["target"] for row in conn.execute(
            "SELECT target FROM edges WHERE source=? AND kind=? "
            "AND resolution='resolved'", (source, kind))
    }


def _decorators(conn, qname: str) -> list[str]:
    row = conn.execute(
        "SELECT decorators FROM nodes WHERE qualified_name=?", (qname,)
    ).fetchone()
    return json.loads(row["decorators"]) if row and row["decorators"] else []


def test_fastapi_routes_and_recursive_dependencies(tmp_path: Path):
    source = tmp_path / "fastapi_app.py"
    source.write_text(
        "from fastapi import APIRouter, Depends, FastAPI, Security\n"
        "app = FastAPI()\n"
        "router = APIRouter()\n"
        "\n"
        "def get_db():\n"
        "    return object()\n"
        "\n"
        "def get_user(db=Depends(get_db)):\n"
        "    return db\n"
        "\n"
        "def require_admin():\n"
        "    return True\n"
        "\n"
        "@app.get('/users')\n"
        "def list_users(user=Depends(get_user), admin=Security(require_admin)):\n"
        "    return user\n"
        "\n"
        "@router.post('/users')\n"
        "def create_user():\n"
        "    return {'ok': True}\n"
        "\n"
        "def main():\n"
        "    list_users()\n"
        "    create_user()\n",
        encoding="utf-8",
    )
    conn, stats = _build(tmp_path)
    assert stats.node_count > 0 and stats.edge_count > 0
    assert "app.get" in _decorators(conn, "fastapi_app::list_users")
    assert "router.post" in _decorators(conn, "fastapi_app::create_user")
    assert _targets(conn, "fastapi_app::list_users") >= {
        "fastapi_app::get_user",
        "fastapi_app::require_admin",
    }
    assert "fastapi_app::get_db" in _targets(conn, "fastapi_app::get_user")

    flow_entries = {
        row["qualified_name"]
        for row in conn.execute(
            "SELECT DISTINCT n.qualified_name FROM flows f "
            "JOIN nodes n ON n.id=f.entry_point_id"
        )
    }
    assert {"fastapi_app::list_users", "fastapi_app::create_user"} <= flow_entries


def test_spring_boot_controller_mapping_and_di(tmp_path: Path):
    source = tmp_path / "UserController.java"
    source.write_text(
        "package com.example.app;\n"
        "import org.springframework.stereotype.Repository;\n"
        "import org.springframework.stereotype.Service;\n"
        "import org.springframework.web.bind.annotation.GetMapping;\n"
        "import org.springframework.web.bind.annotation.RequestMapping;\n"
        "import org.springframework.web.bind.annotation.RestController;\n"
        "import org.springframework.beans.factory.annotation.Autowired;\n"
        "import org.springframework.context.annotation.Bean;\n"
        "import org.springframework.context.annotation.Configuration;\n"
        "\n"
        "@RestController\n"
        "@RequestMapping(\"/users\")\n"
        "class UserController {\n"
        "    @Autowired private UserService service;\n"
        "    @GetMapping(\"/{id}\")\n"
        "    String get(String id) { return service.find(id); }\n"
        "}\n"
        "\n"
        "@Service\n"
        "class UserService {\n"
        "    @Autowired private UserRepository repository;\n"
        "    String find(String id) { return repository.find(id); }\n"
        "}\n"
        "\n"
        "@Repository\n"
        "class UserRepository {\n"
        "    String find(String id) { return id; }\n"
        "}\n"
        "\n"
        "@Configuration\n"
        "class AppConfig {\n"
        "    @Bean UserService userService() { return new UserService(); }\n"
        "}\n",
        encoding="utf-8",
    )
    conn, stats = _build(tmp_path)
    assert stats.node_count > 0 and stats.edge_count > 0
    assert _targets(conn, "com.example.app::UserController.get") >= {
        "com.example.app::UserService.find",
    }
    assert "com.example.app::UserService" in _targets(
        conn, "com.example.app::UserController", kind="call")
    assert "com.example.app::UserRepository" in _targets(
        conn, "com.example.app::UserService", kind="call")
    assert "Bean" in _decorators(conn, "com.example.app::AppConfig.userService")

    parsed = parse_file(str(source), str(tmp_path))
    mappings = {
        node.qualified_name: node.mappings for node in parsed.nodes
    }
    assert mappings["com.example.app::UserController.get"] == [
        ("GET", "/users/{id}")
    ]
