"""Tests for Spring MockMvc route-edge synthesis."""
from code_review_ai.parser import parse_file
from code_review_ai.resolver import resolve_edges


def _routing_repo(tmp_path):
    ctrl = tmp_path / "HomeController.java"
    ctrl.write_text(
        "package com.example;\n"
        "class HomeController {\n"
        "    @GetMapping(\"/owners\")\n"
        "    public String list() { return null; }\n"
        "    @GetMapping(\"/owners/{ownerId}\")\n"
        "    public String show(int ownerId) { return null; }\n"
        "    @PostMapping(\"/owners\")\n"
        "    public String create() { return null; }\n"
        "}\n",
        encoding="utf-8",
    )
    test = tmp_path / "HomeControllerTests.java"
    test.write_text(
        "package com.example;\n"
        "class HomeControllerTests {\n"
        "    void listOk() { mockMvc.perform(get(\"/owners?page=1\")); }\n"
        "    void showOk() { mockMvc.perform(get(\"/owners/7\")); }\n"
        "    void createOk() { mockMvc.perform(post(\"/owners\")); }\n"
        "}\n",
        encoding="utf-8",
    )
    return [parse_file(str(ctrl), str(tmp_path)),
            parse_file(str(test), str(tmp_path))]


def test_route_edges_synthesized(tmp_path):
    files = _routing_repo(tmp_path)
    qnames = {n.qualified_name for f in files for n in f.nodes}
    edges = resolve_edges(files, qnames)
    by = {(e.source, e.target, e.kind, e.resolution) for e in edges}
    assert ("com.example::HomeControllerTests.listOk",
            "com.example::HomeController.list", "call", "resolved") in by
    # {ownerId} 模板段匹配字面段 7
    assert ("com.example::HomeControllerTests.showOk",
            "com.example::HomeController.show", "call", "resolved") in by
    assert ("com.example::HomeControllerTests.createOk",
            "com.example::HomeController.create", "call", "resolved") in by


def test_path_normalization_and_mismatch(tmp_path):
    from code_review_ai.java_routing import _normalize_path, _segments_match
    assert _normalize_path("/owners?page=1") == ["owners"]
    assert _normalize_path("/owners#top") == ["owners"]
    assert _segments_match(["owners", "1"], ["owners", "{ownerId}"]) is True
    assert _segments_match(["owners"], ["owners", "new"]) is False
    assert _segments_match(["owners", "1", "edit"], ["owners", "1"]) is False


def test_route_edges_with_class_prefix(tmp_path):
    ctrl = tmp_path / "PetController.java"
    ctrl.write_text(
        "package com.example;\n"
        "@RequestMapping(\"/owners/{ownerId}\")\n"
        "class PetController {\n"
        "    @GetMapping(\"/pets/new\")\n"
        "    public String initCreationForm() { return null; }\n"
        "    @PostMapping(\"/pets/new\")\n"
        "    public String processCreationForm() { return null; }\n"
        "}\n",
        encoding="utf-8",
    )
    test = tmp_path / "PetControllerTests.java"
    test.write_text(
        "package com.example;\n"
        "class PetControllerTests {\n"
        "    void newForm() { mockMvc.perform(get(\"/owners/7/pets/new\")); }\n"
        "    void create() { mockMvc.perform(post(\"/owners/7/pets/new\")); }\n"
        "}\n",
        encoding="utf-8",
    )
    files = [parse_file(str(ctrl), str(tmp_path)),
             parse_file(str(test), str(tmp_path))]
    qnames = {n.qualified_name for f in files for n in f.nodes}
    edges = resolve_edges(files, qnames)
    by = {(e.source, e.target, e.resolution) for e in edges}
    assert ("com.example::PetControllerTests.newForm",
            "com.example::PetController.initCreationForm", "resolved") in by
    assert ("com.example::PetControllerTests.create",
            "com.example::PetController.processCreationForm", "resolved") in by
