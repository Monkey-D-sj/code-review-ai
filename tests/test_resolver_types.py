"""Resolver receiver declared-type binding (Slice 1, PY-M12).

`w.run()` where `w: Widget` binds to `widget::Widget.run` when the declared
type is a unique repo symbol; multiple reasonable targets become candidate
edges sharing a site_id; untyped/union/external receivers stay dynamic or
unresolved — the resolver must never guess a target.
"""

from code_review_ai.parser import parse_file
from code_review_ai.resolver import resolve_edges


def _build(tmp_path, files):
    parsed = []
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        parsed.append(parse_file(str(path), str(tmp_path)))
    qnames = {n.qualified_name for f in parsed for n in f.nodes}
    return resolve_edges(parsed, qnames)


def _triples(edges):
    return {(e.source, e.target, e.resolution) for e in edges}


def test_receiver_param_binds(tmp_path):
    edges = _build(tmp_path, {
        "widget.py": "class Widget:\n    def run(self): ...\n",
        "svc.py": "from widget import Widget\n"
                  "\n"
                  "def use(w: Widget):\n"
                  "    w.run()\n",
    })
    by = _triples(edges)
    assert ("svc::use", "widget::Widget.run", "resolved") in by


def test_receiver_ts_param_binds(tmp_path):
    edges = _build(tmp_path, {
        "widget.ts": "export class Widget {\n  run(): void {}\n}\n",
        "svc.ts": "import { Widget } from './widget';\n"
                  "export function use(w: Widget): void {\n"
                  "  w.run();\n"
                  "}\n",
    })
    by = _triples(edges)
    assert ("svc::use", "widget::Widget.run", "resolved") in by


def test_receiver_self_field_binds(tmp_path):
    edges = _build(tmp_path, {
        "widget.py": "class Widget:\n    def run(self): ...\n",
        "svc.py": "from widget import Widget\n"
                  "\n"
                  "class Holder:\n"
                  "    w: Widget\n"
                  "    def go(self):\n"
                  "        self.w.run()\n",
    })
    by = _triples(edges)
    assert ("svc::Holder.go", "widget::Widget.run", "resolved") in by


def test_receiver_this_field_binds_ts(tmp_path):
    edges = _build(tmp_path, {
        "widget.ts": "export class Widget {\n  run(): void {}\n}\n",
        "svc.ts": "import { Widget } from './widget';\n"
                  "export class Holder {\n"
                  "  w: Widget;\n"
                  "  go(): void {\n"
                  "    this.w.run();\n"
                  "  }\n"
                  "}\n",
    })
    by = _triples(edges)
    assert ("svc::Holder.go", "widget::Widget.run", "resolved") in by


def test_receiver_module_scope_binds(tmp_path):
    edges = _build(tmp_path, {
        "widget.py": "class Widget:\n    def run(self): ...\n",
        "svc.py": "from widget import Widget\n"
                  "\n"
                  "w: Widget = Widget()\n"
                  "w.run()\n",
    })
    by = _triples(edges)
    assert ("svc", "widget::Widget.run", "resolved") in by


def test_receiver_untyped_stays_dynamic(tmp_path):
    edges = _build(tmp_path, {
        "widget.py": "class Widget:\n    def run(self): ...\n",
        "svc.py": "from widget import Widget\n"
                  "\n"
                  "def use(w):\n"
                  "    w.run()\n",
    })
    by = _triples(edges)
    # no resolved receiver edge, the call stays dynamic (raw expr)
    assert not any(src == "svc::use" and res == "resolved" for src, _t, res in by)
    assert ("svc::use", "w.run", "dynamic") in by


def test_receiver_union_annotation_stays_dynamic(tmp_path):
    edges = _build(tmp_path, {
        "widget.py": "class Widget:\n    def run(self): ...\n",
        "svc.py": "from widget import Widget\n"
                  "\n"
                  "def use(w: Widget | None):\n"
                  "    w.run()\n",
    })
    by = _triples(edges)
    assert not any(src == "svc::use" and res == "resolved" for src, _t, res in by)


def test_receiver_external_type_stays_dynamic(tmp_path):
    edges = _build(tmp_path, {
        "svc.py": "def use(s: ExternalThing):\n"
                  "    s.run()\n",
    })
    by = _triples(edges)
    assert not any(src == "svc::use" and res == "resolved" for src, _t, res in by)


def test_receiver_barrel_multi_candidate_shares_site_id(tmp_path):
    edges = _build(tmp_path, {
        "a.py": "__all__ = ['Widget']\nclass Widget:\n    def run(self): ...\n",
        "b.py": "__all__ = ['Widget']\nclass Widget:\n    def run(self): ...\n",
        "barrel.py": "from a import *\nfrom b import *\n",
        "svc.py": "from barrel import Widget\n"
                  "\n"
                  "def use(w: Widget):\n"
                  "    w.run()\n",
    })
    cands = [e for e in edges if e.source == "svc::use"
             and e.resolution == "candidate"]
    assert {e.target for e in cands} == {"a::Widget.run", "b::Widget.run"}
    assert len({e.site_id for e in cands}) == 1  # one call site, grouped


def test_receiver_ts_barrel_multi_candidate(tmp_path):
    edges = _build(tmp_path, {
        "a.ts": "export class Widget {\n  run(): void {}\n}\n",
        "b.ts": "export class Widget {\n  run(): void {}\n}\n",
        "barrel.ts": "export * from './a';\nexport * from './b';\n",
        "svc.ts": "import { Widget } from './barrel';\n"
                 "export function use(w: Widget): void {\n"
                 "  w.run();\n"
                 "}\n",
    })
    cands = [e for e in edges if e.source == "svc::use"
             and e.resolution == "candidate"]
    assert {e.target for e in cands} == {"a::Widget.run", "b::Widget.run"}
    assert len({e.site_id for e in cands}) == 1
