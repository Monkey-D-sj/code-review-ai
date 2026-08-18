#!/usr/bin/env python3
"""Regenerate ``benchmarks/impact-coverage.json`` from the Coverage Matrix doc.

Phase 0 deliverable of ``docs/IMPACT_CONTEXT_IMPLEMENTATION_GUIDE.md``: turns the
Coverage Matrix ID catalog into a machine-readable manifest with an honest
status per item, so later phases can move items ``missing``/``partial`` ->
``covered`` and every capability commit can be gated on the IDs it closes.

The matrix is the source of truth for *which* IDs exist and what each demands;
the OVERLAY below is the source of truth for *current* status. IDs absent from
OVERLAY default to ``missing``. Statuses are limited to the guide's four values:

- covered      — end-to-end evidence (resolver + index-level impact/test-impact test)
- partial      — parser-level or single-direction support; some forms only
- missing      — not currently supported, no defined behavior
- unsupported  — declared out-of-scope for static resolution; degradation is
                 required but not yet wired (Phase 2's uncertainty contract)

Usage:
    python benchmarks/gen_impact_coverage.py [--out benchmarks/impact-coverage.json]

Regenerability: run the script, then ``git diff`` the JSON to confirm only
intended changes.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MATRIX = ROOT / "docs" / "IMPACT_CONTEXT_COVERAGE_MATRIX.md"
DEFAULT_OUT = ROOT / "benchmarks" / "impact-coverage.json"

# `| ID | 情况 | 应有行为 | 分级 |` — level may be combined like A/B or D/E.
# ID shape: prefix-SECTIONnumber, e.g. COM-N01, PY-F11, JS-M22, TS-Y01, JAVA-S04.
_ROW = re.compile(
    r"^\|\s*((?:COM|PY|JS|TS|JAVA)-[A-Z]+[0-9]+)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*([A-E](?:/[A-E])*)\s*\|$"
)

STATUSES = {"missing", "partial", "covered", "unsupported"}

_MISSING = "无现有实现证据"


def _cov(reason, evidence):
    return "covered", reason, list(evidence)


def _part(reason, evidence):
    return "partial", reason, list(evidence)


def _unsup(reason):
    return "unsupported", reason, []


# ── status overlay: ID -> (status, reason, evidence) ───────────────────────
# Evidence entries are repo-relative test file paths. Absent IDs default to
# ("missing", _MISSING, []).
OVERLAY: dict[str, tuple[str, str, list[str]]] = {
    # ── COM-N: node & change location ──────────────────────────────────────
    "COM-N01": _cov("changes 将函数/方法体 hunk 定位到所属节点",
                    ["tests/test_changes.py", "tests/test_incremental.py"]),
    "COM-N02": _cov("签名变更检出后按调用图召回全部 caller（direct first）",
                    ["tests/test_impact.py", "tests/test_changes.py"]),
    "COM-N03": _cov("新增函数建节点并建立新调用边",
                    ["tests/test_incremental.py"]),
    "COM-N04": _cov("删除函数写 tombstone，旧上游从 tombstone 恢复",
                    ["tests/test_incremental.py"]),
    "COM-N05": _cov("删除整文件写 tombstone 并恢复文件内被删节点",
                    ["tests/test_incremental.py"]),
    "COM-N06": _part("类节点被跟踪；受影响成员范围未显式返回",
                     ["tests/test_changes.py"]),
    "COM-N07": _part("继承边存在；无子类/实现召回索引",
                     ["tests/test_parser.py", "tests/test_resolver.py"]),
    "COM-N08": _part("import 边可解析；无『因 import 变更而改绑的调用点』查询",
                     ["tests/test_resolver.py", "tests/test_incremental.py"]),
    "COM-N09": _part("decorator 持久化且入口重算；DI/route 语义边不随注解变更重算",
                     ["tests/test_incremental.py", "tests/test_parser.py"]),
    "COM-N10": _part("module 节点存在且模块级 hunk 进入 uncovered；importers→module 未作为 impact 暴露",
                     ["tests/test_changes.py", "tests/test_resolver.py"]),
    "COM-N11": _cov("跨多节点 hunk 拆分并报告部分覆盖",
                    ["tests/test_changes.py"]),
    "COM-N12": ("missing", "文件重命名/移动未跟踪（需关联旧新节点）", []),
    "COM-N13": _part("low-risk 变更分类存在；『无行为影响』保证不完整",
                     ["tests/test_changes.py"]),
    "COM-N14": _cov("exclude 配置过滤 generated/vendored/依赖目录",
                    ["tests/test_changes.py"]),
    "COM-N15": _cov("二进制与不支持扩展名进入 uncovered，不静默忽略",
                    ["tests/test_changes.py"]),

    # ── COM-C: explicit call graph ────────────────────────────────────────
    "COM-C01": _cov("同文件直接调用解析", ["tests/test_resolver.py"]),
    "COM-C02": _cov("跨文件直接调用经 import 解析", ["tests/test_resolver.py"]),
    "COM-C03": _cov("三级以上传递调用（flow 线性链）", ["tests/test_flow_builder.py"]),
    "COM-C04": _cov("多个直接调用方，direct caller 优先",
                    ["tests/test_impact.py"]),
    "COM-C05": _part("flow 按入口生成；入口发现限于配置的 decorator",
                     ["tests/test_flow_builder.py", "tests/test_parser.py"]),
    "COM-C06": _cov("一个 caller 调用多个 changed symbols 合并 covers",
                    ["tests/test_testimpact.py"]),
    "COM-C07": _cov("菱形去重，visited 集防路径爆炸", ["tests/test_flow_builder.py"]),
    "COM-C08": _cov("调用环有界遍历不死循环", ["tests/test_flow_builder.py"]),
    "COM-C09": _part("环被有界遍历；递归/互递归环证据未暴露",
                     ["tests/test_flow_builder.py"]),
    "COM-C10": _cov("同名符号按模块/类作用域区分",
                    ["tests/test_resolver.py", "tests/test_incremental.py"]),
    "COM-C11": _cov("无 caller 不伪造 upstream（off-flow 回退到 edges）",
                    ["tests/test_impact.py"]),
    "COM-C12": _cov("外部库/builtin 标 unresolved 且不进 flow",
                    ["tests/test_flow_builder.py", "tests/test_resolver.py"]),
    "COM-C13": _cov("obj.method() 标 dynamic 并保留原始表达式",
                    ["tests/test_resolver.py"]),
    "COM-C14": ("missing", "无 candidate 多候选边模型（Phase 2）", []),
    "COM-C15": _cov("静态上保留分支内全部调用（无 CFG，保守保留）",
                    ["tests/test_parser.py", "tests/test_resolver.py"]),
    "COM-C16": _part("基础模式保守保留；CFG 不可达区分不存在",
                     ["tests/test_resolver.py"]),

    # ── COM-M: module / inheritance / type ────────────────────────────────
    "COM-M01": _cov("普通 import → module/importer 与 imported symbol 边",
                    ["tests/test_resolver.py"]),
    "COM-M02": _cov("alias import 绑定真实符号", ["tests/test_resolver.py"]),
    "COM-M03": _part("Python __init__ re-export 覆盖；TS/JS barrel 未覆盖",
                     ["tests/test_resolver.py", "tests/test_parser_ts.py"]),
    "COM-M04": _part("star import 抽取但标 unresolved；__all__ 未参与",
                     ["tests/test_resolver.py", "tests/test_parser.py"]),
    "COM-M05": _cov("跨包/跨模块继承经 import 解析", ["tests/test_resolver.py"]),
    "COM-M06": ("missing", "无 override 索引", []),
    "COM-M07": ("missing", "无接口/抽象分派候选", []),
    "COM-M08": ("missing", "无泛型/模板实例化处理", []),
    "COM-M09": _part("多重继承/多接口边全部保留",
                     ["tests/test_parser.py", "tests/test_resolver.py"]),
    "COM-M10": _part("外部父类/接口保持 unresolved；无 external 标签",
                     ["tests/test_resolver.py"]),

    # ── COM-I: callback / async / IoC ─────────────────────────────────────
    "COM-I01": ("missing", "函数作为参数传入的 registration 边未建", []),
    "COM-I02": ("missing", "无匿名/lambda 节点稳定身份", []),
    "COM-I03": ("missing", "无 promise/future continuation 边", []),
    "COM-I04": ("missing", "无事件注册/触发边", []),
    "COM-I05": ("missing", "无队列 producer/consumer 边", []),
    "COM-I06": ("missing", "无定时任务/调度 entry", []),
    "COM-I07": ("missing", "无 hook/plugin 注册边", []),
    "COM-I08": _part("Spring 构造/字段注入与 FastAPI Depends 有 DI 边",
                     ["tests/test_resolver.py", "tests/test_resolver_java.py"]),
    "COM-I09": _cov("route 声明→handler 合成边",
                    ["tests/test_resolver.py", "tests/test_java_routing.py"]),
    "COM-I10": _cov("测试框架收集 → is_test 标记",
                    ["tests/test_parser.py", "tests/test_testimpact.py"]),

    # ── COM-T: test impact contract ───────────────────────────────────────
    "COM-T01": _cov("测试直接调用 changed symbol 召回", ["tests/test_testimpact.py"]),
    "COM-T02": _cov("测试经业务层传递召回（transitive）", ["tests/test_testimpact.py"]),
    "COM-T03": _cov("多 changed symbols 合并 covers", ["tests/test_testimpact.py"]),
    "COM-T04": _cov("全部覆盖测试被召回", ["tests/test_testimpact.py"]),
    "COM-T05": _part("无覆盖时返回空；『建议全量回退』字段是 Phase 2 契约",
                     ["tests/test_testimpact.py"]),
    "COM-T06": _cov("symbol 不存在返回 not_found，不误选测试",
                    ["tests/test_testimpact.py"]),
    "COM-T07": ("missing", "匿名/动态生成测试函数未处理", []),
    "COM-T08": ("missing", "参数化/动态测试未归一到稳定单位", []),
    "COM-T09": ("missing", "fixture/setup 间接覆盖未处理", []),
    "COM-T10": _part("MockMvc 测试经 route 语义边可达（Java）",
                     ["tests/test_java_routing.py", "tests/test_incremental.py"]),
    "COM-T11": ("missing", "仅引用类型/常量不当作行为覆盖的弱候选未建", []),
    "COM-T12": _part("MCP 启动有 stale 检查与 catch-up rebuild；显式全量回退字段是 Phase 2 契约",
                     ["tests/test_mcp_server.py"]),

    # ── PY-S: Python definitions, scope, call syntax ──────────────────────
    "PY-S01": _cov("顶层 def 节点与调用关系", ["tests/test_parser.py", "tests/test_resolver.py"]),
    "PY-S02": _part("async def 作为函数解析；async 语义未区分",
                    ["tests/test_parser.py"]),
    "PY-S03": _cov("类/实例方法；self.m() 绑定本类", ["tests/test_resolver.py"]),
    "PY-S04": _cov("@classmethod / cls.m() 绑定本类方法", ["tests/test_resolver.py"]),
    "PY-S05": _part("C.m() 经模块/类查找绑定；staticmethod 未区分",
                    ["tests/test_resolver.py"]),
    "PY-S06": _part("嵌套作用域解析；closure 内外调用关系未显式",
                     ["tests/test_parser.py", "tests/test_resolver.py"]),
    "PY-S07": ("missing", "lambda 无稳定匿名节点", []),
    "PY-S08": ("missing", "callable obj() 无 __call__ 绑定", []),
    "PY-S09": _cov("构造 C() → class/__init__/__new__",
                   ["tests/test_resolver.py", "tests/test_resolver_java.py"]),
    "PY-S10": ("missing", "super().m() 无 MRO 解析", []),
    "PY-S11": ("missing", "property descriptor 未处理", []),
    "PY-S12": ("missing", "magic method 语法操作未连接", []),
    "PY-S13": ("missing", "with 上下文 __enter__/__exit__ 未连接", []),
    "PY-S14": ("missing", "generator/yield 语义未处理", []),
    "PY-S15": _part("comprehension 内调用抽取并归属外层函数",
                    ["tests/test_parser.py"]),
    "PY-S16": _cov("decorator 抽取与入口标记", ["tests/test_parser.py"]),
    "PY-S17": _part("默认参数/annotation 中调用被抽取；定义时/调用时未区分",
                    ["tests/test_parser.py"]),
    "PY-S18": ("missing", "functools.partial 未处理", []),
    "PY-S19": ("missing", "singledispatch 未处理", []),
    "PY-S20": _part("模块级作用域避免跨模块同名串边；无完整 LEGB 模型",
                    ["tests/test_resolver.py", "tests/test_incremental.py"]),

    # ── PY-M: Python import / package / type ──────────────────────────────
    "PY-M01": _cov("import a → a.fn() 绑定模块成员", ["tests/test_resolver.py"]),
    "PY-M02": _cov("import a.b → a.b.fn() 子模块成员", ["tests/test_resolver.py"]),
    "PY-M03": _cov("import a as x alias 绑定", ["tests/test_resolver.py"]),
    "PY-M04": _cov("from a import f 裸调用绑定", ["tests/test_resolver.py"]),
    "PY-M05": _cov("from a import f as g alias 绑定", ["tests/test_resolver.py"]),
    "PY-M06": _cov("相对 import 按 package 归一", ["tests/test_resolver.py"]),
    "PY-M07": _cov("__init__.py re-export 沿包转发", ["tests/test_resolver.py"]),
    "PY-M08": _part("from a import * 抽取但 unresolved；__all__ 未参与",
                    ["tests/test_parser.py", "tests/test_resolver.py"]),
    "PY-M09": _cov("src layout/namespace package 正确推导 module",
                   ["tests/test_parser.py", "tests/test_resolver.py"]),
    "PY-M10": ("missing", ".pyi stub 未索引/未作类型源", []),
    "PY-M11": ("missing", "ABC/Protocol 实现候选未建", []),
    "PY-M12": ("missing", "类型注解 receiver 未用于缩小目标", []),
    "PY-M13": ("missing", "union/generic/type alias 未展开", []),
    "PY-M14": ("missing", "返回值链 factory().run() 未绑定", []),

    # ── PY-F: Python framework semantics ──────────────────────────────────
    "PY-F01": _cov("Flask @app.route → route entry", ["tests/test_resolver.py"]),
    "PY-F02": _cov("Flask Blueprint @bp.route 默认 *.route 覆盖",
                   ["tests/test_resolver.py", "tests/test_parser.py"]),
    "PY-F03": _part("FastAPI @app.get/@router.get 不在默认入口 decorator；仅作普通调用解析",
                    ["tests/test_resolver.py"]),
    "PY-F04": ("missing", "include_router + prefix 未组合", []),
    "PY-F05": _cov("FastAPI Depends(provider) → dependency 边", ["tests/test_resolver.py"]),
    "PY-F06": ("missing", "middleware/exception handler/lifespan 未处理", []),
    "PY-F07": ("missing", "Django URLConf 未处理", []),
    "PY-F08": ("missing", "Django CBV as_view 未处理", []),
    "PY-F09": ("missing", "Django signal 未处理", []),
    "PY-F10": ("missing", "Django ORM manager/queryset 未处理", []),
    "PY-F11": _part("celery.task 默认入口；.delay/.apply_async 绑定未建",
                    ["tests/test_parser.py", "tests/test_resolver.py"]),
    "PY-F12": ("missing", "Celery 字符串 task 名未映射", []),
    "PY-F13": _part("click.command 默认 CLI 入口", ["tests/test_parser.py"]),
    "PY-F14": ("missing", "argparse set_defaults(func=...) 未处理", []),
    "PY-F15": ("missing", "asyncio create_task/gather 未处理", []),
    "PY-F16": ("missing", "logging/plugin handler 配置未映射", []),
    "PY-F17": ("missing", "SQLAlchemy event/listener 未处理", []),
    "PY-F18": ("missing", "Pydantic validator/serializer 未处理", []),

    # ── PY-T: Python test ecosystem ───────────────────────────────────────
    "PY-T01": _cov("pytest test_* 测试节点/文件识别", ["tests/test_parser.py"]),
    "PY-T02": ("missing", "fixture 参数注入未处理", []),
    "PY-T03": ("missing", "fixture 依赖图未建", []),
    "PY-T04": ("missing", "autouse fixture 未处理", []),
    "PY-T05": ("missing", "conftest.py 作用域绑定未处理", []),
    "PY-T06": ("missing", "parametrize 未归一", []),
    "PY-T07": ("missing", "pytest hook 未处理", []),
    "PY-T08": _cov("unittest TestCase.test_* 测试识别", ["tests/test_parser.py"]),
    "PY-T09": ("missing", "setUp/tearDown 生命周期未处理", []),
    "PY-T10": ("missing", "mock/patch target string 未建弱关系", []),

    # ── PY-D: Python dynamic boundary ─────────────────────────────────────
    "PY-D01": _part("obj.method() 标 dynamic；getattr 常量名候选未建",
                    ["tests/test_resolver.py"]),
    "PY-D02": ("missing", "importlib/__import__ 未处理", []),
    "PY-D03": _unsup("eval/exec 运行时代码边界，负向降级测试待 Phase 2"),
    "PY-D04": _unsup("monkey patch 行为不可可靠确定，降级契约待 Phase 2"),
    "PY-D05": _unsup("metaclass 动态生成成员，边界待 Phase 2"),
    "PY-D06": _unsup("descriptor/__getattr__ 需 candidate/dynamic，未实现"),
    "PY-D07": ("missing", "entry-point/plugin metadata 未连接", []),
    "PY-D08": _unsup("pickle/字符串 qname 外部数据不可知，边界待 Phase 2"),

    # ── JS-S: TS/JS definitions and call syntax ───────────────────────────
    "JS-S01": _cov("function declaration 节点与调用", ["tests/test_parser_ts.py"]),
    "JS-S02": _part("async function 作为函数解析；async 语义未区分",
                    ["tests/test_parser_ts.py"]),
    "JS-S03": _part("generator function 解析；generator 语义未区分",
                    ["tests/test_parser_ts.py"]),
    "JS-S04": _cov("arrow function 变量名对应函数节点", ["tests/test_parser_ts.py"]),
    "JS-S05": _cov("function expression 变量名对应函数节点",
                   ["tests/test_parser_ts.py"]),
    "JS-S06": ("missing", "匿名 callback 无稳定身份", []),
    "JS-S07": _part("嵌套函数/closure 保留词法作用域", ["tests/test_parser_ts.py"]),
    "JS-S08": _part("object literal method 解析", ["tests/test_parser_ts.py"]),
    "JS-S09": _part("object property arrow/function 解析", ["tests/test_parser_ts.py"]),
    "JS-S10": _cov("class method 包含于 class", ["tests/test_parser_ts.py"]),
    "JS-S11": _part("new C() 构造调用解析；构造器绑定一致性跨语言", ["tests/test_parser_ts.py"]),
    "JS-S12": _part("this.m() 绑定当前类/对象", ["tests/test_parser_ts.py"]),
    "JS-S13": ("missing", "super.m()/super() 未处理", []),
    "JS-S14": _part("C.m() 静态绑定", ["tests/test_parser_ts.py"]),
    "JS-S15": _part("class field arrow 解析", ["tests/test_parser_ts.py"]),
    "JS-S16": ("missing", "#private 成员作用域未处理", []),
    "JS-S17": ("missing", "getter/setter accessor 未处理", []),
    "JS-S18": _part("optional chaining 解析为调用/动态", ["tests/test_parser_ts.py"]),
    "JS-S19": _part("computed property 变量名标 dynamic；常量名候选未建",
                    ["tests/test_parser_ts.py"]),
    "JS-S20": ("missing", "tagged template 未处理", []),
    "JS-S21": _part("IIFE 匿名函数解析", ["tests/test_parser_ts.py"]),
    "JS-S22": _part("decorator 抽取（class/method）；application 边未建",
                    ["tests/test_parser_ts.py"]),
    "JS-S23": _part("顶层调用归属 module 节点", ["tests/test_parser_ts.py"]),
    "JS-S24": ("missing", "overload/声明合并未处理", []),

    # ── JS-M: ESM / CommonJS / project module resolution ─────────────────
    "JS-M01": _cov("ESM named import 绑定真实 export", ["tests/test_resolver.py"]),
    "JS-M02": _part("default import/export 解析；default 绑定未全验证",
                    ["tests/test_parser_ts.py", "tests/test_resolver.py"]),
    "JS-M03": _part("namespace import ns.f() 解析", ["tests/test_resolver.py"]),
    "JS-M04": _part("side-effect import 无 module-init 边", ["tests/test_resolver.py"]),
    "JS-M05": _part("export {x} 关联本地定义", ["tests/test_parser_ts.py"]),
    "JS-M06": ("missing", "export {x} from re-export 未处理", []),
    "JS-M07": ("missing", "export * barrel 未处理", []),
    "JS-M08": _cov("相对路径 ./ ../ 归一模块", ["tests/test_resolver.py"]),
    "JS-M09": _part("省略扩展名解析未验证全链", ["tests/test_resolver.py"]),
    "JS-M10": _part("目录 index 解析未验证", ["tests/test_resolver.py"]),
    "JS-M11": ("missing", "package.json main/module/types 未读", []),
    "JS-M12": ("missing", "package exports/imports 条件未读", []),
    "JS-M13": _part("tsconfig baseUrl 归一未全验证", ["tests/test_parser_ts.py"]),
    "JS-M14": _cov("tsconfig paths 多 target 按匹配顺序解析",
                   ["tests/test_resolver.py", "tests/test_parser_ts.py"]),
    "JS-M15": ("missing", "tsconfig extends 未合并", []),
    "JS-M16": ("missing", "project references 未处理", []),
    "JS-M17": ("missing", "monorepo/workspace package 未处理", []),
    "JS-M18": ("missing", "CommonJS require() 未处理", []),
    "JS-M19": ("missing", "module.exports/exports.x 未处理", []),
    "JS-M20": ("missing", "ESM/CJS interop 未处理", []),
    "JS-M21": ("missing", "dynamic import() 常量未处理", []),
    "JS-M22": _cov("tsconfig paths 别名与 path_aliases 归一",
                   ["tests/test_resolver.py", "tests/test_parser_ts.py"]),
    "JS-M23": ("missing", ".d.ts/type-only import 未处理", []),

    # ── TS-Y: TypeScript type-aided resolution ────────────────────────────
    "TS-Y01": _part("参数显式类型 receiver 绑定（Java 已验证；TS 未全验证）",
                    ["tests/test_resolver_java.py"]),
    "TS-Y02": _part("字段/局部变量类型 var_types 抽取", ["tests/test_parser_ts.py"]),
    "TS-Y03": ("missing", "构造赋值推断 new C() 未绑定", []),
    "TS-Y04": ("missing", "返回类型链未绑定", []),
    "TS-Y05": ("missing", "interface ↔ implementations 候选未建", []),
    "TS-Y06": ("missing", "abstract method overrides 未处理", []),
    "TS-Y07": ("missing", "union/intersection 未处理", []),
    "TS-Y08": ("missing", "generic type parameter 未处理", []),
    "TS-Y09": ("missing", "type alias 未展开", []),
    "TS-Y10": ("missing", "overload signatures 未处理", []),
    "TS-Y11": ("missing", "structural typing 候选未建", []),
    "TS-Y12": ("missing", "enum/namespace 声明合并未处理", []),

    # ── JS-I: async / callback / event ───────────────────────────────────
    "JS-I01": ("missing", "Promise .then/.catch 未处理", []),
    "JS-I02": ("missing", "setTimeout/setInterval 未处理", []),
    "JS-I03": ("missing", "EventEmitter on/emit 未处理", []),
    "JS-I04": ("missing", "DOM addEventListener 未处理", []),
    "JS-I05": ("missing", "array HOF map/filter/reduce 未处理", []),
    "JS-I06": ("missing", "callback 变量传递未追踪", []),
    "JS-I07": ("missing", "async queue/job processor 未处理", []),
    "JS-I08": ("missing", "worker/child process 未处理", []),
    "JS-I09": ("missing", "RxJS pipeline 未处理", []),

    # ── JS-F: frontend / Node framework semantics ─────────────────────────
    "JS-F01": ("missing", "Express/Koa route 未处理", []),
    "JS-F02": ("missing", "NestJS Controller route 未处理", []),
    "JS-F03": ("missing", "NestJS constructor/token DI 未处理", []),
    "JS-F04": ("missing", "NestJS module wiring 未处理", []),
    "JS-F05": ("missing", "React 组件节点未建", []),
    "JS-F06": ("missing", "JSX <Child/> 未处理", []),
    "JS-F07": ("missing", "JSX event handler 未处理", []),
    "JS-F08": ("missing", "hook callback/dependency 未处理", []),
    "JS-F09": ("missing", "Next.js file routes 未处理", []),
    "JS-F10": ("missing", "Next.js server action/API 未处理", []),
    "JS-F11": _cov("Vue <script setup> 进入模块图", ["tests/test_parser_ts.py"]),
    "JS-F12": _part("Vue template 子组件边未建；仅 SFC 解析", ["tests/test_parser_ts.py"]),
    "JS-F13": _part("Vue template event → handler 未建", ["tests/test_parser_ts.py"]),
    "JS-F14": ("missing", "Vue computed/watch/ref 未处理", []),
    "JS-F15": ("missing", "Angular 未处理", []),
    "JS-F16": ("missing", "Angular template 未处理", []),
    "JS-F17": ("missing", "Redux 未处理", []),
    "JS-F18": ("missing", "GraphQL resolver 未处理", []),

    # ── JS-T: TS/JS test ecosystem ────────────────────────────────────────
    "JS-T01": _part("test glob 标记 *.test/*.spec/__tests__；callback 节点未建",
                    ["tests/test_parser.py"]),
    "JS-T02": ("missing", "describe 层级未保留", []),
    "JS-T03": ("missing", "beforeEach 等生命周期未处理", []),
    "JS-T04": ("missing", "test.each 未归一", []),
    "JS-T05": ("missing", "jest.mock factory 未处理", []),
    "JS-T06": ("missing", "spies/mocked methods 未建弱关系", []),
    "JS-T07": ("missing", "RTL render/event 未处理", []),
    "JS-T08": ("missing", "Supertest 未处理", []),
    "JS-T09": ("missing", "Playwright/Cypress 未处理", []),
    "JS-T10": _cov("test filename/glob 变体识别", ["tests/test_parser.py"]),

    # ── JS-D: TS/JS dynamic boundary ──────────────────────────────────────
    "JS-D01": _unsup("eval/new Function 运行时代码边界，降级测试待 Phase 2"),
    "JS-D02": _unsup("Proxy 动态成员，不伪解析，边界待 Phase 2"),
    "JS-D03": _part("computed property 变量名标 dynamic；常量名候选未建",
                    ["tests/test_parser_ts.py"]),
    "JS-D04": _unsup("prototype monkey patch 无法唯一绑定，边界待 Phase 2"),
    "JS-D05": _part("动态 import 路径保留为 unresolved", ["tests/test_resolver.py"]),
    "JS-D06": ("missing", "runtime module loader/plugin 未连接", []),
    "JS-D07": ("missing", "DI string/symbol token 未处理", []),
    "JS-D08": _unsup("bundler codegen/macros，边界待 Phase 2"),

    # ── JAVA-S: Java types, members, call syntax ──────────────────────────
    "JAVA-S01": _cov("class/interface/enum/record 类型节点与 contains",
                     ["tests/test_parser_java.py"]),
    "JAVA-S02": _part("annotation type 解析；使用关系未连接",
                      ["tests/test_parser_java.py"]),
    "JAVA-S03": _cov("method/constructor 独立成员节点", ["tests/test_parser_java.py"]),
    "JAVA-S04": _part("overload 同名 qname 碰撞；symbol_key 未建",
                      ["tests/test_parser_java.py"]),
    "JAVA-S05": _cov("裸同文件调用绑定当前类成员", ["tests/test_resolver_java.py"]),
    "JAVA-S06": _part("this.m() 绑定当前类成员", ["tests/test_resolver_java.py"]),
    "JAVA-S07": ("missing", "super.m()/super() 未处理", []),
    "JAVA-S08": _part("C.m() 静态调用绑定", ["tests/test_resolver_java.py"]),
    "JAVA-S09": _cov("static import 裸调用绑定静态成员", ["tests/test_resolver_java.py"]),
    "JAVA-S10": _cov("new C() → class/匹配构造器", ["tests/test_resolver_java.py"]),
    "JAVA-S11": ("missing", "anonymous class 未建匿名类型", []),
    "JAVA-S12": _part("local/nested/inner class 作用域", ["tests/test_parser_java.py"]),
    "JAVA-S13": ("missing", "enum constant class body 未处理", []),
    "JAVA-S14": ("missing", "record ctor/accessor 未处理", []),
    "JAVA-S15": _cov("declared type receiver 绑定", ["tests/test_resolver_java.py"]),
    "JAVA-S16": _part("var_types 抽取；var 推断进 receiver 绑定未验证",
                      ["tests/test_parser_java.py"]),
    "JAVA-S17": ("missing", "chained return 未绑定", []),
    "JAVA-S18": ("missing", "array/collection receiver 未绑定", []),
    "JAVA-S19": ("missing", "lambda 未建可调用节点", []),
    "JAVA-S20": ("missing", "method reference 未处理", []),
    "JAVA-S21": _part("initializer/static initializer 解析", ["tests/test_parser_java.py"]),
    "JAVA-S22": ("missing", "try-with-resources 未处理", []),

    # ── JAVA-M: Java package / module / inheritance ───────────────────────
    "JAVA-M01": _cov("package 声明 → FQCN/qname", ["tests/test_parser_java.py"]),
    "JAVA-M02": _cov("无 package 的路径回退", ["tests/test_parser_java.py"]),
    "JAVA-M03": _cov("普通 import 类型/静态成员绑定", ["tests/test_resolver_java.py"]),
    "JAVA-M04": _part("wildcard import 抽取；唯一命中时解析、否则无候选集",
                      ["tests/test_parser_java.py"]),
    "JAVA-M05": _cov("fully-qualified call/type 精确绑定", ["tests/test_resolver_java.py"]),
    "JAVA-M06": ("missing", "Maven/Gradle source sets 未处理", []),
    "JAVA-M07": ("missing", "multi-module 依赖解析未处理", []),
    "JAVA-M08": ("missing", "JPMS module-info 未处理", []),
    "JAVA-M09": _cov("class extends → base", ["tests/test_resolver_java.py"]),
    "JAVA-M10": _cov("interface extends/implements 类型关系闭合",
                     ["tests/test_resolver_java.py"]),
    "JAVA-M11": ("missing", "default interface method 未处理", []),
    "JAVA-M12": ("missing", "abstract method dispatch 候选未建", []),
    "JAVA-M13": ("missing", "runtime polymorphism 候选未建", []),
    "JAVA-M14": ("missing", "sealed class permits 未处理", []),
    "JAVA-M15": ("missing", "generic/bridge method 未处理", []),
    "JAVA-M16": _part("外部 JAR 不索引；无 external 标签", ["tests/test_resolver_java.py"]),

    # ── JAVA-I: Java callback / concurrency / stdlib ──────────────────────
    "JAVA-I01": ("missing", "Stream map/filter/forEach 未处理", []),
    "JAVA-I02": ("missing", "Optional callbacks 未处理", []),
    "JAVA-I03": ("missing", "CompletableFuture chain 未处理", []),
    "JAVA-I04": ("missing", "Executor/Thread/Runnable 未处理", []),
    "JAVA-I05": ("missing", "Timer/scheduler 未处理", []),
    "JAVA-I06": ("missing", "listener registration 未处理", []),
    "JAVA-I07": ("missing", "ServiceLoader 未处理", []),
    "JAVA-I08": ("missing", "serialization callbacks 未处理", []),

    # ── JAVA-F: Spring / Jakarta semantics ────────────────────────────────
    "JAVA-F01": _cov("@Controller/@RestController mapping → HTTP entry",
                     ["tests/test_java_routing.py"]),
    "JAVA-F02": _cov("类级+方法级 RequestMapping 合并 path/method",
                     ["tests/test_java_routing.py"]),
    "JAVA-F03": _part("mapping 捕获；path 模板/参数归一证据未全",
                      ["tests/test_java_routing.py"]),
    "JAVA-F04": _cov("constructor injection → dependency type",
                     ["tests/test_resolver_java.py", "tests/test_java_routing.py"]),
    "JAVA-F05": _cov("field/setter injection（按配置注解过滤）",
                     ["tests/test_resolver_java.py"]),
    "JAVA-F06": ("missing", "qualifier/primary/named bean 未处理", []),
    "JAVA-F07": ("missing", "@Bean method 未处理", []),
    "JAVA-F08": ("missing", "component scan 未处理", []),
    "JAVA-F09": ("missing", "Spring Data Repository proxy 未处理", []),
    "JAVA-F10": ("missing", "derived query method 未处理", []),
    "JAVA-F11": ("missing", "@Valid/Validator 未处理", []),
    "JAVA-F12": ("missing", "@ModelAttribute/@InitBinder 未处理", []),
    "JAVA-F13": ("missing", "@ControllerAdvice/@ExceptionHandler 未处理", []),
    "JAVA-F14": ("missing", "filter/interceptor/security chain 未处理", []),
    "JAVA-F15": ("missing", "@EventListener/publishEvent 未处理", []),
    "JAVA-F16": ("missing", "@Scheduled 未处理", []),
    "JAVA-F17": ("missing", "@Async 未处理", []),
    "JAVA-F18": ("missing", "@Transactional 未处理", []),
    "JAVA-F19": ("missing", "AOP advice/pointcut 未处理", []),
    "JAVA-F20": ("missing", "JPA callbacks 未处理", []),
    "JAVA-F21": ("missing", "WebFlux route/WebTestClient 未处理", []),
    "JAVA-F22": ("missing", "RestTemplate/WebClient/Feign 未处理", []),
    "JAVA-F23": ("missing", "Spring Integration/Kafka/JMS listener 未处理", []),
    "JAVA-F24": ("missing", "config properties/string bean names 未处理", []),

    # ── JAVA-T: Java test ecosystem ───────────────────────────────────────
    "JAVA-T01": _cov("JUnit @Test 测试节点识别", ["tests/test_parser.py"]),
    "JAVA-T02": _part("@ParameterizedTest 默认 decorator；归一未建",
                      ["tests/test_parser.py"]),
    "JAVA-T03": ("missing", "@RepeatedTest/@TestFactory/@TestTemplate 未处理", []),
    "JAVA-T04": ("missing", "@Nested 层级未处理", []),
    "JAVA-T05": ("missing", "Before/After 生命周期未处理", []),
    "JAVA-T06": ("missing", "JUnit extension 未处理", []),
    "JAVA-T07": ("missing", "Mockito @Mock/@Spy/@InjectMocks 未处理", []),
    "JAVA-T08": ("missing", "when/verify 弱测试上下文未建", []),
    "JAVA-T09": ("missing", "@MockBean 未处理", []),
    "JAVA-T10": _cov("MockMvc request 捕获（method/path）", ["tests/test_java_routing.py"]),
    "JAVA-T11": ("missing", "WebTestClient 未处理", []),
    "JAVA-T12": ("missing", "repository/service integration test DI 未处理", []),
    "JAVA-T13": _cov("test source-set/file 命名识别", ["tests/test_parser.py"]),

    # ── JAVA-D: Java dynamic boundary ─────────────────────────────────────
    "JAVA-D01": ("missing", "reflection 常量类/方法名候选未建；动态值 unresolved", []),
    "JAVA-D02": _unsup("dynamic proxy 实现候选未建，边界待 Phase 2"),
    "JAVA-D03": _unsup("bytecode generation/instrumentation，边界待 Phase 2"),
    "JAVA-D04": _unsup("JNI/native 方法，标 native/external 待 Phase 2"),
    "JAVA-D05": ("missing", "外部 DI/configuration 不可见时候选未列", []),
    "JAVA-D06": ("missing", "runtime classpath/service provider 未解析", []),
    "JAVA-D07": _unsup("SpEL 字符串表达式，外部输入 dynamic 待 Phase 2"),
}


def parse_matrix(path: Path) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _ROW.match(line)
        if match:
            cid, desc, behavior, level = match.groups()
            rows[cid] = {
                "level": level,
                "desc": desc.strip(),
                "behavior": behavior.strip(),
            }
    return rows


def build(path: Path, baseline: dict) -> dict:
    rows = parse_matrix(path)
    if not rows:
        raise SystemExit(f"no coverage IDs parsed from {path}")
    items: dict[str, dict] = {}
    for cid in sorted(rows):
        status, reason, evidence = OVERLAY.get(cid, ("missing", _MISSING, []))
        if status not in STATUSES:
            raise SystemExit(f"{cid}: invalid status {status!r}")
        items[cid] = dict(rows[cid], status=status, reason=reason, evidence=evidence)
    extras = set(OVERLAY) - set(rows)
    if extras:
        raise SystemExit(f"overlay IDs not in matrix: {sorted(extras)}")
    counts = {s: sum(1 for item in items.values() if item["status"] == s) for s in STATUSES}
    return {
        "schema_version": 1,
        "generated_by": "benchmarks/gen_impact_coverage.py",
        "matrix_source": "docs/IMPACT_CONTEXT_COVERAGE_MATRIX.md",
        "baseline": baseline,
        "status_values": sorted(STATUSES),
        "summary": {"total": len(items), **counts},
        "items": items,
    }


# Baseline frozen at Phase 0 (commit e93e759, full suite green).
BASELINE: dict = {
    "commit": "e93e759",
    "tests": {
        "command": "uv run pytest",
        "passed": 343,
        "failed": 0,
        "skipped": 0,
        "duration_seconds": 151.68,
    },
    "historical_benchmark": {
        "manifest": "benchmarks/historical-suite-50.json",
        "test_recall_at_10": 0.2667,
        "test_recall_at_all": 0.4467,
        "source": "benchmarks/BASELINE.md (2026-08-06)",
    },
}


def main(argv: list[str]) -> int:
    out_path = DEFAULT_OUT
    if argv:
        if argv[0] == "--out" and len(argv) >= 2:
            out_path = Path(argv[1])
        else:
            print(f"usage: {sys.argv[0]} [--out PATH]", file=sys.stderr)
            return 2
    matrix = DEFAULT_MATRIX
    if not matrix.exists():
        print(f"matrix doc not found: {matrix}", file=sys.stderr)
        return 2
    manifest = build(matrix, BASELINE)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = manifest["summary"]
    print(
        f"wrote {out_path}: {summary['total']} IDs "
        f"(covered={summary['covered']}, partial={summary['partial']}, "
        f"missing={summary['missing']}, unsupported={summary['unsupported']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
