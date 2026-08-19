"""SourceSpan provenance on parser IR (Phase 2, guide §3.1).

Every evidence-carrying IR record (RawCall, RawInherit, ImportEntry, DiDecl)
must be locatable to a file and a line. These tests pin the exact 1-based
line numbers, including the .vue script-block offset so spans always point at
the original file, not the extracted script.
"""

from code_review_ai.parser import parse_file


def test_raw_call_spans_pin_call_site(tmp_path):
    mod = tmp_path / "app.py"
    mod.write_text(
        "import auth\n"
        "from auth import login\n"
        "\n"
        "def main():\n"
        "    a = login()\n"
        "    b = auth.login()\n"
        "    return a, b\n",
        encoding="utf-8",
    )
    pf = parse_file(str(mod), str(tmp_path))
    by_expr = {c.target_expr: c for c in pf.raw_calls}
    simple = by_expr["login"]
    assert simple.span is not None
    assert simple.span.file_path == str(mod)  # the exact file the parser read
    assert simple.span.start_line == 5
    dotted = by_expr["auth.login"]
    assert dotted.span is not None
    assert dotted.span.start_line == 6
    # the span covers the call expression: start is never after end
    assert simple.span.start_line <= simple.span.end_line


def test_every_raw_call_carries_a_span(tmp_path):
    """No call is ever dropped without a location — the near-miss guard."""
    mod = tmp_path / "app.py"
    mod.write_text(
        "def main():\n"
        "    x = [1, 2].pop()\n"
        "    y = (lambda: 1)()\n"
        "    return x + y\n",
        encoding="utf-8",
    )
    pf = parse_file(str(mod), str(tmp_path))
    assert pf.raw_calls
    assert all(c.span is not None for c in pf.raw_calls)


def test_import_span_points_at_statement(tmp_path):
    mod = tmp_path / "app.py"
    mod.write_text(
        "import auth\n"
        "from auth import login as do_login\n",
        encoding="utf-8",
    )
    pf = parse_file(str(mod), str(tmp_path))
    by_local = {i.local_name: i for i in pf.imports}
    assert by_local["auth"].span.start_line == 1
    assert by_local["do_login"].span.start_line == 2


def test_inherit_span_points_at_base_clause(tmp_path):
    mod = tmp_path / "app.py"
    mod.write_text(
        "class Base:\n"
        "    pass\n"
        "\n"
        "class Child(Base):\n"
        "    pass\n",
        encoding="utf-8",
    )
    pf = parse_file(str(mod), str(tmp_path))
    assert pf.inherits
    inh = pf.inherits[0]
    assert inh.span is not None
    assert inh.span.start_line == 4
    assert inh.span.file_path == str(mod)


def test_java_di_decl_and_import_spans(tmp_path):
    mod = tmp_path / "Main.java"
    mod.write_text(
        "package com.foo;\n"
        "\n"
        "import com.foo.other.Service;\n"
        "\n"
        "class MyController {\n"
        "    @Autowired\n"
        "    private Service service;\n"
        "\n"
        "    public MyController(Service service) {\n"
        "        this.service = service;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    pf = parse_file(str(mod), str(tmp_path))
    assert pf.language == "java"
    assert pf.di_decls
    by_mech = {d.mechanism: d for d in pf.di_decls}
    # the field span covers the whole declaration, starting at the
    # @Autowired annotation — the injection evidence anchor
    assert by_mech["field"].span.start_line == 6
    assert by_mech["constructor"].span.start_line == 9
    service = next(i for i in pf.imports if i.local_name == "Service")
    assert service.span.start_line == 3


def test_vue_span_applies_script_block_offset(tmp_path):
    mod = tmp_path / "comp.vue"
    mod.write_text(
        "<template>\n"
        "  <div></div>\n"
        "</template>\n"
        "\n"
        "<script setup>\n"
        "import { ref } from 'vue'\n"
        "const n = ref(0)\n"
        "const go = () => { hello() }\n"
        "</script>\n",
        encoding="utf-8",
    )
    pf = parse_file(str(mod), str(tmp_path))
    hello = [c for c in pf.raw_calls if c.target_expr == "hello"]
    assert hello and hello[0].span is not None
    # script-relative line 3 + 4-line block offset -> original .vue line 8
    assert hello[0].span.start_line == 8
    # the import inside the script block points at the original .vue file too
    vue_import = next(i for i in pf.imports if i.module == "vue")
    assert vue_import.span.start_line == 6
