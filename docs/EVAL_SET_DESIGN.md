# 评测集设计:Native 工具链 vs Code-Review-Graph

> 目标:建立一套可复现的评测集,量化"LLM agent 自带的工具链(native:
> `rg`/`grep` + `read` 文件)"与"code-review-graph 图查询"在代码 review /
> 代码导航场景下的 **token 成本、时间、正确性**差异。
>
> 本文档只定义方法论与**具体场景**,不绑定任何真实仓库。

---

## 1. 为什么不能只挑"native 找不到的题"

评测集的设计必须避免两个对称的作弊方向:

| 错误做法 | 结果 |
|---|---|
| 全选简单题(唯一符号、直接调用、一跳) | 图看起来毫无价值,"白建了" |
| 全选难题(别名、回调、纯 NL) | 图看起来无所不能,100% 碾压 |

真实 review 场景是**混合分布**的。评测集应当**按真实任务的难度分布分层采样**,并**分档报告**——这样得到的不是一句"图更好"的口号,而是:

> **平凡场景 native 持平或略省;中等场景图开始省;困难场景 native 在原理上失败。crossover 点在哪,就是图的适用边界。**

这个 crossover 点本身就是评测最有价值的产出。

---

## 2. 每条评测题记录什么

每条题目的最小记录结构:

```text
id                 # 唯一编号
tier               # trivial | medium | hard
description        # 一句自然语言描述(reviewer 会怎么问)
ground_truth       # 正确答案(来自真实提交/git 历史,不来自图)

# native 侧
native_command     # 具体命令,如 rg "\bparse\b" src/
native_tokens      # 该命令输出 + 为确认真调用而读的文件,折算 token
native_correct     # true / false / unsolvable
native_time_ms

# graph 侧
graph_call         # 具体图调用,如 query_graph(pattern=callers_of, target=...)
graph_tokens       # 图返回的序列化上下文折算 token
graph_correct      # true / false
graph_time_ms

# 汇总
ratio              # native_tokens / graph_tokens(仅当双方都 correct 时算)
```

**关键规则:native 侧必须认真实现。** 反作弊在于"给 native 用最好的 grep 写法、
该读的文件都读了",而不是故意给它一个差的解法。评测的是工具能力差异,
不是"我能不能把 native 写得蠢一点"。

---

## 3. 三档难度与具体场景

### Tier 1 — trivial(平凡):native 应当持平或更省

> 这一档的存在意义:诚实承认 native 的简单场景优势,确认图不做无用功。
> 预期 ratio ≈ 1 或 < 1。

**S1.1 唯一符号直接调用**

```python
# a.py
def parse(text):          # 改了这里的逻辑
    ...

# b.py
def load(path):
    ...
    result = parse(content)   # 唯一的真调用
```

- native:`rg "\bparse\b"` → 命中 b.py 一处,读 b.py 上下文确认。一次搜索 + 一次局部读。
- graph:一次 `query_graph(callers_of, target=a.py::parse)`。
- 预期:native 输出更短,图持平或略高。**这是图的正常成本**,不是缺陷。

**S1.2 单文件内小改动**

```python
# utils.py
def _hash_key(k):
    ...
    return f"{k}:{v}"      # 私有辅助,调用点都在同一文件内
```

- native:同文件内 `rg "_hash_key"` 几个命中,读同一文件即可。
- graph:一次图查询。
- 预期:trivial,双方成本都极低。

**S1.3 全局唯一配置常量**

```python
# config.py
TIMEOUT_SECONDS = 30        # 被改成 5

# 各业务文件引用 TIMEOUT_SECONDS
```

- native:`rg "TIMEOUT_SECONDS"` → 命中行即全部使用点,输出本身就是答案。
- graph:一次查询。
- 预期:native 持平。

---

### Tier 2 — medium(中等):图开始有优势

> 优势来源:**文本匹配 ≠ 结构分析**。native 的 token 成本花在"读文件确认、
> 多次重搜、人工拼调用链"上,这些图一次调用就返回了。

**S2.1 多跳调用链(反复重搜)**

```python
# leaf.py
def compute_total(cart):      # 改了这里
    ...

# checkout.py
def place_order(user, cart):
    total = compute_total(cart)    # 1 跳

# api/orders.py
@app.post("/orders")
def create_order(user, cart):
    return place_order(user, cart) # 2 跳,真正的入口
```

- native:`rg compute_total` → 找到 `place_order`(1 跳)。**影响在哪?还得再
  `rg place_order`** → 找到 `create_order` 和 worker。搜 2~3 次,每跳都要读
  文件确认,还要手工拼出"叶子 → 中间 → 入口"这条链。
- graph:一次 BFS(`get_impact_radius` max_depth=2)返回整条链 + 每个节点的
  影响分,并标记哪些是 HTTP handler 这类入口。
- 成本差异随跳数增长:**native 是 O(跳数) 次搜索 + 读文件,图是 O(1) 次调用。**

**S2.2 唯一名、但调用点极多**

```python
# db.py
def execute(sql): ...      # 改了这里,全仓库被调了 200 次
```

- native:`rg "\bexecute\b"` 命中 200 行。**但你不能直接信**——要判断哪些是
  真调用(不是字符串、不是注释、不是同名方法),得打开文件逐个确认。这 200
  次读文件/读上下文就是成本大头。
- graph:返回按影响分排序的调用者,直接标注了每个节点的 file/line,不用扫。
- 注意:名字唯一不代表 trivial——**匹配多但需要人工过滤**是另一类中等题。

**S2.3 跨文件转发 / qualified name 解析**

```python
# a.py
def run_task(task): ...

# b.py
from a import run_task      # 转发
def handler(task):
    run_task(task)

# c.py
from a import run_task as rt   # 别名转发
rt(task)
```

- native:rg `run_task` 能命中 import 行,但要**跨文件拼出** canonical 名
  (`a.py::run_task`)才能确认 b.py 和 c.py 用的是同一个函数。文本上它是散点,
  结构上它是同一实体。
- graph:图存 canonical `a.py::run_task`,`query_graph(callers_of)` 直接聚合
  两个调用点。
- 这种题是"medium"不是"hard":因为 rg 至少能命中 import 行,**不是原理性无解**,
  只是要读文件、要多步推理。

**S2.4 包装/代理函数**

```python
# metrics.py
def _raw_send(data): ...      # 改了这里

# wrappers.py
def send(data, retries=3):
    for i in range(retries):
        _raw_send(data)       # 唯一调用,但被包装层挡住
```

- native:rg `_raw_send` 只看到 `wrappers.py` 这一处。但**真正的上游是
  `send()` 的调用者**——你要先意识到"它被包装了",再去 rg `send`。这个
  "意识到被包装"的推理步骤,文本工具给不了提示。
- graph:从 `_raw_send` 沿调用链自然走到 `send` 及其调用者。

**S2.5 需要"谁测了这个函数"**

```python
# parse.py
def parse(text): ...        # 改了

# tests/test_parse.py
def test_parse_handles_empty(): ...    # 覆盖它的测试
```

- native:rg `parse` 会命中测试文件,但**无法区分"测试里调用了 parse"和
  "这个测试真正覆盖 parse"**。要看测试断言有没有真正触达,得读测试代码判断。
- graph:`TESTED_BY` 边直接回答"改了它,哪些测试会挂",并可用于 test-gap 检测。

---

### Tier 3 — hard(困难):native 在原理上失败

> 这一档是**能力差**,不是成本差。native 无解的题**不计入 token 比值**,
> 单独一列记 `unsolvable`——否则会得到一个假的"无穷大省 token"。
>
> **只保留图可靠支持的场景**。图同样可能失败的多态展开、动态构造名、
> 无边共变、依赖向量索引的自然语言查询等"双方都可能漏"的题
> **不进 hard 档**(见档末的排除说明)。

**S3.1 别名隐藏(你举的例子)**

```python
# a.py
def compute_total(cart): ...     # 改了这里

# checkout.py
from a import compute_total as ct
def place_order(user, cart):
    total = ct(cart)             # rg "compute_total" 看不到这个调用
```

- native:`rg compute_total` → **漏掉调用点**;`rg ct` → 太泛,噪声爆炸或
  全部无关。没有任何一个字符串能同时"命中真调用"+"不带来噪声"。
- graph:解析 import 别名,`query_graph(callers_of)` 直接命中 `checkout.py`。
- 判定:**native 答不全 = 正确性失败**,不是成本问题。

**S3.2 回调 / 注册表(静态调用为 0)**

```python
# discounts.py
class DiscountEngine:
    def __init__(self):
        self._strategies = {}

    def register(self, name, fn):
        self._strategies[name] = fn

    def apply(self, name, cart):
        return self._strategies[name](cart)

# 配置处
engine.register("vip", compute_total)    # 改了 compute_total,唯一的引用在这里
```

- native:rg `compute_total` 只找到 `register(...)` 这一行引用,返回
  "只在一处出现"。但它**不知道** `apply()` 在运行时分发它——必须去读
  `DiscountEngine` 的实现、理解分发逻辑、再找 `apply()` 的调用者。这是
  **多文件、多步推理**,而且很容易就此打住,漏掉真实影响面。
- graph:`triggers_of` / `listeners_of` 这类注册/触发边 + 流检测,直接从入口
  (`POST /orders → apply → compute_total`)给出全链路。

**S3.3 测试兜底("改了 X,有没有测试覆盖?会挂哪些?")**

```python
# parse.py
def parse(text): ...            # 改了这里

# tests/test_parse.py
def test_parse_handles_empty():
    ...
    assert parse("") is None    # 这个测试真正触达 parse 的行为

# docs/examples.py
def demo():
    parse("x")                  # 只是"用了一下",不是测试,改挂了也不报
```

- native:rg `parse` 能列出测试文件里出现 `parse` 的行,**但无法区分"测试
  真正断言了它的行为"和"碰巧引用/演示代码用了一下"**——要判断"改了有没有
  测试兜底",得读完全部测试代码逐个验证,代价高且容易判错。
- graph:`TESTED_BY` 边直接回答"改了它,哪些测试会挂",并可用于 test-gap
  检测(`changes.py` 里 `test_gaps` 就是基于 TESTED_BY 缺失判定的)。
- 注意:`TESTED_BY` 是解析器从"测试函数调用生产函数"推断的,不是运行时
  证据——但它回答了 native 从信息源上就得不到的问题。

> **明确排除的场景**(图同样可能失败,不进 hard 档):
> - 纯自然语言问题("谁处理了未捕获的异常")——依赖向量索引 `--embed`,
>   否则 `hybrid_search` 回退到 FTS5,整句匹配不到任何节点,图同样查不到;
> - 多态 / 接口分发(`Base.save()` 运行时调到哪个 override)——项目只做
>   "声明类型解析 + 唯一实现的 Spring/Temporal resolver",不做一般多态展开;
> - 动态构造的名字(`getattr(obj, "compute_" + name)`)——静态图无法解析;
> - 无调用边的共变耦合(改了枚举、所有消费方一起动)——调用图抓不到,
>   已由 `impact_accuracy` 的 co-change benchmark 单独衡量。
>
> 这些场景放进 hard 档会得到"双方都失败"的噪声,测不出能力差异。

---

## 4. 分层采样比例

三层都测,但**数量按真实任务分布的估计**来配比(比例本身也是评测的一部分,
应写进报告并说明依据):

| Tier | 建议占比 | 说明 |
|---|---:|---|
| trivial | ~40% | 真实 review 里最常见的就是简单查找 |
| medium | ~35% | 多跳、过滤、转发,图的主要价值区间 |
| hard | ~25% | 能力差场景,数量少但决定"图的不可替代性" |

> 如果只想突出差异化,可以多放 hard 档——但必须**在报告里声明采样偏置**,
> 否则就回到了"挑题"的作弊。

---

## 5. 评分与报告

### 统一 Gold 与双层评分

`case-backend` 每个 case 只维护一个 `gold`：`root_causes` 描述独立修复单元，
`context` 描述应召回的 symbols/files/entries/tests 和显式 hard negatives。同一份
gold 产生两组独立结果：`graph_retrieval` 只评价索引检索，`agent_review` 评价根因
与 Agent 结构化报告的影响面。两组指标不能合并成一个 F1；没有标注的维度记为
`not applicable`，不能用 0 填充。旧 `gold_findings`/`gold_files` 仅用于兼容历史
manifest，不再作为 case-backend 的答案源。

### 指标

- **token ratio** = native_tokens / graph_tokens,**仅在双方都 correct 时计算**;
- **native_success** = correct 行数 / 总行数(能力差直接表现为 success 下降);
- **graph_success** = 同上;
- **unsolvable 数** = native 原理性无解的题数(单独统计,不进 ratio);
- **时间**(可选):命令墙钟时间,量级即可,机器差异大时不跨机比。

### 报告模板

```markdown
| Tier | 题数 | native 成功 | graph 成功 | 中位 token ratio | native 无解 |
|---|---:|---:|---:|---:|---:|
| trivial | 40 | 40/40 | 40/40 | 0.9 | 0 |
| medium | 35 | 33/35 | 35/35 | 3.1 | 0 |
| hard | 25 | 8/25 | 21/25 | 8.7 | 12 |
| 总计 | 100 | 81/100 | 96/100 | — | 12 |
```

从这张表能直接读出三个结论:

1. trivial 档 ratio 0.9 → **简单场景不要用图,诚实承认**;
2. medium 档 3.1× + 2 题 native 失败 → **图的主战场**;
3. hard 档 native 12 题无解 → **图的不可替代性证据**,且这 12 题根本不该
   进 ratio(算进去就是"无穷大"的假数字)。

### 汇总陈述模板

> 平凡场景 native 与图持平(≈0.9×);中等场景图约省 3× 且多答对 2 题;
> 困难场景 native 有 12/25 题在原理上无法完成。图的适用边界在"需要
> 结构解析或语义匹配"的查询;纯文本符号查找建议直接用 native。

---

## 6. 反作弊 / 防偏清单

- [ ] **题目来自真实提交**,ground truth 取 git 历史,不取自图(否则循环)。
- [ ] **native 侧用最好的写法**,不故意写差;读文件成本如实计入。
- [ ] **分层采样并在报告中声明占比**,不挑题。
- [ ] **unsolvable 单列**,不计入 token ratio。
- [ ] **图侧成本如实计**(搜索 + 边 + 工具调用开销),不只算"核心几行"。
- [ ] **可复现**:钉死 commit、确定性排序、同一 `chars/4` 记账法。
- [ ] "双方都可能失败"的题**不进 hard 档**(或单独标注),不在图上假设必胜。

---

## 7. 落地到本项目

### 7.1 hard 档必须用 LLM Agent 测

hard 档的成本来自推理过程:读 import 后发现别名、识别包装层、决定下一轮
搜索词、跨文件排除同名噪声。固定命令序列无法公平模拟这些决策。因此主实验
不是"一条 rg 命令 vs 一次图调用",而是同一个 LLM Agent 的配对 A/B:

- `native_agent`:Read / Glob / Grep,以及命令级白名单约束的只读 Bash;
- `full_project_core`:完全相同的 native 工具，并开放 `get_impact`、
  `get_test_impact`、`get_change_summary`、`get_change_context`、
  `query_graph`、`search_symbol`、`get_symbol_detail`;不开放社区查询、外部服务、
  死代码和入口点工具;
- 同一模型、prompt、仓库快照、超时和重复次数;正式对比不设 provider
  金额上限,避免大图 case 被预算截断后记成错误。

在线评测使用工具权限层强制只读:Bash 可见,但`dontAsk + allowedTools`只预授权
`rg/grep`、`git status/rev-parse`及`ls/cat/head/tail/wc/stat/du/diff`等读取命令;
其他命令直接拒绝。真实仓库 case 通过反向恢复修复提交构造 mutation，因此显式禁止
`git log`、`git show`和所有 Bash `git diff`。完整差异已嵌入 prompt；完全禁用 `git diff`
避免前缀式权限规则把 revision 参数误判为安全。`rg --pre`可执行外部预处理器,因此单独列入 deny;
所有`>`/`<`重定向也 deny,避免`rg ... > file`绕过只读边界。解释器、包管理器、
网络命令、文件写命令和会改变仓库的 Git 命令也显式 deny。
Claude 会把`&&`、`;`、管道和换行拆为子命令逐一校验,不能在允许的`rg`后拼接
安装或写入命令。Core 的 MCP 白名单排除`rebuild_index`、`get_communities`、
`get_community`、`call_external_service`、`find_dead_code`;只在 prompt 中写
“不要修改”不视为有效隔离。

`full_project_querygraph`保留为压力/消融模式,不再作为默认产品 A/B。该模式最多
调用两次`query_graph`,每次`max_neighbors=5`:先选能代表运行时消费链和公开契约链
的结构节点,默认只查上游;只有参数、返回值或下游调用发生变化时才查下游。

native 在 hard 档失败不预先标成`unsolvable`,而按实际运行结果记录
`incorrect`、`timeout`或`provider_failure`。只有同一运行条件下重复失败,
而 graph 侧稳定答对,才能陈述为能力差。

### 7.2 已有 runner 的映射

项目已有的`full-agent-eval`负责真实仓库、真实 Agent 在线工具调用。case schema
现已增加`difficulty = trivial | medium | hard`,每个 run、preflight 和 report
都保留该标签。`agent-eval-analyze`会同时输出全局结果和`by_difficulty`,每档包括:

- precision / recall / F1及配对 delta;
- input / output / total token配对 delta;
- elapsed time、工具调用数、读取文件数配对 delta;
- provider failure、成本和 MCP adoption。

在线 adapter 还记录有序`tool_trace`:工具名、压缩后的实际输入及对应响应字符数。
它用于定位重复图查询和大响应;provider stream 当前没有可靠的逐工具耗时,因此不
伪造`elapsed_ms`,只保留整次 run 的真实耗时。

读取成本同时记录`read_calls`、`search_calls`、`bash_calls`、
`unique_files_touched`、`native_response_chars`、`mcp_response_chars`和
`total_tool_calls`。`Read`、`Grep`以及 Bash 中带明确文件操作数的
`rg/grep/cat/head/tail`会进入唯一文件集合；Bash 的目录、stdin、变量、通配符、
隐式路径或未分类命令不会被猜测为文件，而是将`unknown_file_access`设为 true，
并在`unknown_file_access_details`保留序号、命令和原因。旧的`files_read`字段保留
为该可解析集合的兼容别名，因此它是下界而不是“所有文件访问已完整解析”的声明。

`get_change_context`默认最多选4个 symbol、每个最多5个邻居并限制为8 KB。symbol
选择先覆盖不同 changed file,再按生产调用者数量补位;构造器降权、test-only caller
默认不占图响应预算。对于 FastAPI validation-alias hard case,9个 changed symbols
会稳定选为`_compat.v2::get_schema_from_model_field`、
`dependencies.utils::_get_multidict_value`、
`openapi.utils::_get_openapi_operation_parameters`和`params::Param.__init__`,覆盖运行时
取值、OpenAPI契约、兼容层和参数声明四个层次。

旧 manifest 没有`difficulty`时标为`unclassified`,避免破坏已有历史报告;用于正式
分档结论的 manifest 必须显式标注三档之一。

### 7.3 业务形态仓库评测集

评测集中于业务形态仓库。`benchmarks/case-backend-cases.json`（source_dir 内嵌、
无需 clone）承载分层业务项目的全 agent eval 用例，`benchmarks/fast-cases.json`
是 fast-repo 上的快速回归集（--local-repo）。开源框架仓库的历史评测线已移除。

用例默认**盲评**：prompt 只给出交付物（改动破坏了什么、哪些调用方/对外入口/测试
受影响）与 diff，不点名任何符号；每个用例的 `hint` 只在 `--hinted` 消融臂下注入。
点名受影响调用者的文字对两臂是对称输入、非对称收益——它正好替 native 臂完成了图
工具存在的意义，所以 gold keywords 只取"必须沿调用链反查才能拿到"的标识符（出现在
diff 或 hint 里的关键词可以靠改写命中，而不是靠追溯），并且单次回答上限 3 条发现，
避免 f1 退化成篇幅指标。

### 7.4 推荐运行方式

```powershell
code-review-ai full-agent-eval `
  --cases benchmarks/case-backend-cases.json `
  --repetitions 3 `
  --workers 1 `
  --agent-command '<adapter command>' `
  --out benchmark-results/case-backend-tiered.json

code-review-ai agent-eval-analyze `
  --report benchmark-results/case-backend-tiered.json `
  --out benchmark-results/case-backend-tiered-analysis.json

# 将大 transcript 压缩为完整、有序、无响应正文的工具执行路线
code-review-ai eval-trace `
  benchmark-results/case-backend-tiered.json `
  --transcripts-root .code-review-ai/full-agent-eval/transcripts `
  -o benchmark-results/case-backend-tiered-routes.md
```

`full-agent-eval`传入`-o <name>.json`并成功完成时，会自动在同目录生成
`<name>-routes.md`；`eval-trace`保留用于重新解析旧报告或指定其他输出位置。

未传`--modes`时默认就是`native_agent full_project_core`;需要测原始图邻域工具时再
显式传`--modes native_agent full_project_querygraph`,并将结果标为 ablation。

建索引时间继续由`index_setup`单列且不计入 Agent 在线耗时。最终报告同时给出
cold setup、Agent A/B及按 tier 的配对结果,不能只摘 hard 档或只报告成功运行。
