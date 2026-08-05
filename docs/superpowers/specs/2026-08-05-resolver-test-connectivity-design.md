# 设计：修复调用解析器，让测试文件连进图（resolver test-connectivity）

日期：2026-08-05

## 背景与问题

SWE-bench 30-case 评测（`benchmarks/swe-bench-verified-30.json`，`run_benchmark` +
`scripts/run_swebench_suite.py`）当前 recall@10 = **0.20**（30 个中 6 命中）。逐一归因后，
24 个 miss 由解析器/parser 的 3 个根因导致 —— 测试文件**明明直接调用被改符号**，却进不了
`get_impact` 的候选集：

1. **`src/` 布局前缀**：`_module_qname` 把 `src/` 目录当包名 → qname 变成
   `src._pytest.config::PytestPluginManager`，而测试 import 的是 `_pytest.config`，两边永远
   对不上 → 影响 pytest 11 + flask 1 = **12 个 case**。实证：pytest-5840 测试直接
   `PytestPluginManager()`、flask-5014 直接 `flask.Blueprint(...)`，但两个类节点在索引里
   **0 个 incoming caller**。
2. **相对导入解析错误**：tree-sitter 把前导点嵌套在 `relative_import → import_prefix`
   节点里（`_extract_imports_python` 用 `for c in node.children if c.type == "."` 数点，
   永远数不到），而 `module_name` 字段是整棵 `relative_import`（text=`.sessions`）→
   `from .sessions import Session` 解析出 module=`.sessions`（带前导点、永不匹配真实模块）
   → 包内 re-export 绑定全断。
3. **`__init__.py` re-export 链不解析**：`requests.Session()` 生成 `requests::Session`，
   而真实节点是 `requests.sessions::Session` → unresolved（即使相对导入修好，也缺这一步）。
4. （次要）构造器调用 `X(...)` 只连到类节点，不连 `X.__init__` —— 当 changed_symbols 只含
   `__init__` 时（如 flask-5014 的 `Blueprint.__init__`），构造点无法溯源。

6 个命中全是"测试直接走真实模块路径 import 后构造/调用"的幸运儿（如 `requests.models::PreparedRequest`
直接构造、`as_compatible_data` 模块级函数直接调用）。

**明确不做**：动态实例调用类型跟踪（`s.get()` / `self.dv.quantile()` 需作用域级赋值推断，
成本高、有误报风险）—— 作为下一轮独立迭代（方案 2），不混进本设计。

## 目标

修解析器/parser 根因，使「测试 → 被改符号」的静态调用边真实存在，从而 `get_impact` 的候选
自然包含测试文件。**不改 benchmark 的候选生成逻辑**（`benchmark._candidate_files` 不动），
用同一个 `run_benchmark` 重跑 30 case，纯量化解析器改进的收益。

**验收线**：`uv run pytest` 全绿；重跑后 recall ≥ **0.5**（≥15/30 命中），并比对
precision 是否回退（recall 上升但 precision 明显掉 → 说明是在往候选里灌噪声，不可接受）。

## 修复 1：`_module_qname` 剥掉 `src/`（`parser.py`）

```python
def _module_qname(file_path: str, repo_root: str) -> str:
    rel = Path(file_path).resolve().relative_to(Path(repo_root).resolve())
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if parts and parts[0] == "src":          # ← 新增：src-layout 根目录不是包
        parts = parts[1:]
    return ".".join(parts)
```

- `src/_pytest/config/__init__.py` → `_pytest.config`（原来 `src._pytest.config`）；
  `src/flask/blueprints.py` → `flask.blueprints`。
- 效果：pytest 测试 `from _pytest.config import PytestPluginManager` 与类节点对齐。
- 顺带修了「qname 不是 python 可见模块名」的潜在 bug；fixture repo 无 `src/`，现有测试不受影响。
- `.vue` 文件同样走此函数，行为一致。

## 修复 2：相对导入按包正确解析（`parser._extract_imports_python`）

**AST 事实**（tree-sitter python）：
```
from .sessions import Session
  import_from_statement
    from
    relative_import            # text = ".sessions" —— module_name 字段指到这
      import_prefix
        .                      # 点嵌套在这里，不在 import_from_statement 直接 children
      dotted_name              # text = "sessions"
    import
    dotted_name
```
现有代码 `dots = sum(c.type == "." for c in node.children)` 对带子模块名的相对导入恒为 0，
于是落到 `module = sub = ".sessions"` —— 废值。且相对基准算错：`pkg = parts[:-1]` 对
`__init__.py` 少了包自己这一层（`requests/__init__.py` 的 `from .sessions` 应得
`requests.sessions`，不是 `sessions`）。

改动：
- `_extract_imports(root, module_qname, lang, lang_name)` 增加 `file_path` 参数（`parse_file`
  调用处传入），据此判 `is_init = Path(file_path).name == "__init__.py"`。
- 相对基准 `pkg`：`is_init` 时用 `parts`（包自己），否则用 `parts[:-1]`（父包）。
- 相对 module 计算改为解析 `relative_import` 的文本：
  ```python
  mod_node = node.child_by_field_name("module_name")
  sub = _dotted(mod_node)
  if mod_node is not None and mod_node.type == "relative_import":
      rel = sub                       # 例 ".sessions" / "..m" / "."
      leading = len(rel) - len(rel.lstrip("."))
      rest = rel[leading:]            # "sessions" / "m" / ""
      up = leading - 1
      base = pkg[: len(pkg) - up] if up <= len(pkg) else []
      module = ".".join(base + ([rest] if rest else []))
  else:
      module = sub                    # 绝对导入，原样
  ```

验证样例：
- `requests/__init__.py` 内 `from .sessions import Session` → `requests.sessions` ✓
- `a/b/c.py` 内 `from ..m import y` → `a.m` ✓
- `a/b/c.py` 内 `from . import y` → `a.b` ✓

## 修复 3：re-export 链解析（`resolver`）

前提是修复 2 让每个模块的 import 绑定变正确。思路：当 `qname.join(mod, name)` 不存在时，
沿 `mod` **自身** 的 import 绑定找 re-export 源头 —— 完全跟随真实 re-export，非启发式。

`resolve_calls` 里构建全局绑定一次：
```python
all_import_maps = {pf.module_qname: _import_map(pf) for pf in parsed_files}
```
新增辅助函数（防环）。绑定元组 `(module, imported_name, is_star)` 中 `imported_name` 是
**导出名**——重导出可能带别名（`from .sessions import Session as S`），所以递归必须用
`binding[1]` 而非原 `name`：
```python
def _resolve_reexport(current: str, name: str, all_import_maps: dict,
                      existing: set[str], seen: set[str] | None = None) -> str | None:
    tgt = qname.join(current, name)
    if tgt in existing:
        return tgt
    if current in (seen or set()):
        return None                       # 环
    binding = (all_import_maps.get(current) or {}).get(name)
    if not binding or not binding[1]:
        return None                       # 无绑定 / 模块 import / star import
    return _resolve_reexport(binding[0], binding[1], all_import_maps,
                             existing, (seen or set()) | {current})
```
挂在 `_resolve_one`（签名加 `all_import_maps`）的两条路径，仅当直连目标不在 existing 时兜底：
- CALL_SIMPLE：`from requests import Session` → `requests::Session` 不存在 →
  `_resolve_reexport("requests", "Session")` → 绑到 `("requests.sessions","Session")` →
  `requests.sessions::Session` ✓
- CALL_ATTRIBUTE：`requests.Session()`（head=imported module，imp_name=None）同理。

`_resolved` 语义不变：目标存在才标 `resolved`，否则 `unresolved`。

## 修复 4：构造器补 `__init__` 边（`resolver.resolve_calls`）

`X(...)` 解析到类节点后，若 `qname.join(cls_qn, "__init__")` 存在于全集，额外发一条
`source → cls.__init__` 的 call 边（resolution=`resolved`）：
```python
class_qnames = {n.qualified_name for f in parsed_files for n in f.nodes if n.kind == "class"}
# 在 resolve_calls 主循环里，对每个 resolved 且 target∈class_qnames 的边：
init_qn = qname.join(edge.target, "__init__")
if init_qn in existing:
    edges.append(Edge(source=edge.source, target=init_qn, kind="call",
                      file_path=edge.file_path, call_line=edge.call_line,
                      resolution="resolved"))
```
当 changed_symbols 只含 `__init__`（flask-5014 的 `Blueprint.__init__`）时，构造点可溯源。

## 验证

1. **现有测试**：`uv run pytest` 全绿。现有 `test_resolver.py` 锁定的行为（fixture repo 无
   src、无相对导入、`obj.run` 保持 dynamic、`vals[0]` 保持 unresolved）不受这组修复影响。
2. **新增单测**（每个修复一个）：
   - 修复 1：src 布局文件 → module qname 无 `src.` 前缀。
   - 修复 2：`from .m import X` 在普通模块与 `__init__.py` 中解析出正确绝对 module。
   - 修复 3：模拟 `requests/__init__.py` re-export，`from requests import Session` 与
     `requests.Session()` 均解析到 `requests.sessions::Session`。
   - 修复 4：`X(...)` 同时产出类边与 `__init__` 边。
   - 端到端：pytest-5840 式（src 布局 + 测试直接构造类）→ `get_impact` 上游含测试文件。
3. **重跑评测**：
   `uv run python scripts/run_swebench_suite.py --cases benchmarks/swe-bench-verified-30.json
   --out benchmark-results/swe-bench-verified-30.json`
   （repo 已缓存在 `.benchmark-cache/repos/`，只重索引 + 评估，每 case ~3-6s）。
   结果覆写提交，recall/precision 前后对比写进 commit message 或 PR。

## 风险与边界

- **`src/` 被误剥**：若某 repo 的顶层包真的叫 `src`（非 layout 用法），修复 1 会剥错。罕见，
  按 python de-facto 约定接受；若出现可改为「仅当 `src/` 下恰好一个顶层包」的启发式。
- **存量索引迁移**：修复 1 改变 qname 语义，升级后存量 `.code-review-ai/index.db` 需全量
  rebuild（benchmark 本身每次全量重建，不受影响；MCP 侧升级后建议 `rebuild_index` 一次）。
- **修复 4 的误连**：`X(...)` 若 `X` 是重载/元类构造，`__init__` 边可能不是真实执行路径；
  属保守过度连接，但同类边不参与 impact 的「受影响入口」判定，仅作上下游候选，风险可接受。
- **precision 监控**：recall 上升的同时若 precision 显著下降（候选灌满无关测试），
  需回退或加候选去噪，验收线已把这一点列为硬性条件。
