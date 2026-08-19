"""Parser var_types collection for Python/TS receiver typing (Slice 1, PY-M12).

`ParsedFile.var_types` is `{function_or_module_qname: {var_name: declared_type}}`
— declared annotations the resolver can use to bind `w.run()` when `w: Widget`.
Only simple identifier types are recorded; union/generic/subscript/string
annotations and untyped variables are skipped so the resolver never guesses.
"""

from code_review_ai.parser import parse_file


def _parse(tmp_path, name, content):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return parse_file(str(path), str(tmp_path))


def test_python_params_and_locals(tmp_path):
    pf = _parse(tmp_path, "svc.py",
        "class Widget:\n"
        "    def run(self): ...\n"
        "def use(w: Widget):\n"
        "    x: Widget = w\n"
        "    w.run()\n")
    assert pf.var_types["svc::use"] == {"w": "Widget", "x": "Widget"}


def test_python_class_field_via_self(tmp_path):
    pf = _parse(tmp_path, "svc.py",
        "class Widget:\n"
        "    def run(self): ...\n"
        "class Holder:\n"
        "    w: Widget\n"
        "    def go(self):\n"
        "        self.w.run()\n")
    assert pf.var_types["svc::Holder.go"] == {"self.w": "Widget"}


def test_python_module_scope(tmp_path):
    pf = _parse(tmp_path, "svc.py",
        "class Widget:\n"
        "    def run(self): ...\n"
        "w: Widget = Widget()\n"
        "w.run()\n")
    assert pf.var_types["svc"] == {"w": "Widget"}


def test_python_skips_union_subscript_string_and_untyped(tmp_path):
    pf = _parse(tmp_path, "svc.py",
        "class Widget:\n"
        "    def run(self): ...\n"
        "def use(w: Widget, u: Widget | None, s: 'Widget', plain):\n"
        "    x: Widget = w\n"
        "    y: list[Widget] = []\n"
        "    w.run()\n")
    # only simple-identifier annotations survive; union/string/subscript/untyped drop
    assert pf.var_types["svc::use"] == {"w": "Widget", "x": "Widget"}


def test_ts_params_locals_fields(tmp_path):
    pf = _parse(tmp_path, "svc.ts",
        "export class Widget {\n"
        "  run(): void {}\n"
        "}\n"
        "export class Holder {\n"
        "  w: Widget;\n"
        "  go(w2: Widget): void {\n"
        "    const x: Widget = w2;\n"
        "    w2.run();\n"
        "    this.w.run();\n"
        "  }\n"
        "}\n"
        "export function use(w: Widget): void {\n"
        "  w.run();\n"
        "}\n")
    assert pf.var_types["svc::use"] == {"w": "Widget"}
    assert pf.var_types["svc::Holder.go"] == {
        "this.w": "Widget", "w2": "Widget", "x": "Widget"}


def test_ts_arrow_function(tmp_path):
    pf = _parse(tmp_path, "svc.ts",
        "export class Widget {\n"
        "  run(): void {}\n"
        "}\n"
        "const go = (w: Widget): void => {\n"
        "  w.run();\n"
        "};\n")
    assert pf.var_types["svc::go"] == {"w": "Widget"}


def test_js_collects_nothing(tmp_path):
    pf = _parse(tmp_path, "svc.js",
        "const go = (w) => {\n"
        "  w.run();\n"
        "};\n")
    assert pf.var_types == {}
