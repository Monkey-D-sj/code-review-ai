# full-agent-eval 运行指南（Windows 一次跑成功）

> 目标：复制粘贴即可跑通，避开 Windows 上踩过的所有坑。
> 验证于 2026-08-27，case-backend alias case，`deepseek-v4-flash`。

## 0. 直接可用的命令模板

```bash
uv run --no-sync python -m code_review_ai.cli full-agent-eval \
  --cases benchmarks/case-backend-cases.json \
  --case-ids case-backend-decrypt-password-alias \
  --agent-command "C:\Users\HMG-BA110\Desktop\code-review-ai\.venv\Scripts\python.exe -m code_review_ai.agent_adapter claude" \
  --model deepseek-v4-flash \
  --modes full_project_core \
  --work-dir eval-results/<run-name> \
  -o eval-results/<run-name>/report.json
```

**`--agent-command` 的三个硬性要求：**

1. **必须用 venv python 的绝对路径**（`.venv\Scripts\python.exe`），不能用裸 `python`。
2. 不能加 `-p`（adapter 内部会加）。
3. 不能包 `uv run` 前缀（见陷阱 2）。

## 1. 陷阱表（症状 → 根因 → 修复）

| # | 症状 | 根因 | 修复 |
|---|---|---|---|
| 1 | 两臂都 `returncode 1`、~70ms 秒挂、stderr `ModuleNotFoundError: code_review_ai` | agent 子进程 cwd=worktree，`subprocess.run(["python",...])` 走 Windows CreateProcess"父进程加载目录"规则，解析到 `pyvenv.cfg home` 指向的 uv 基座 python（无 venv site-packages） | `--agent-command` 用 `.venv\Scripts\python.exe` 绝对路径 |
| 2 | `uv run` 前缀下新建了错误 venv（CPython 3.12） | case worktree 自带 `pyproject.toml` + `requirements.txt`，`uv run` 从 worktree cwd 找到它，创建新 venv | 绝对路径 venv python，绕过 uv 项目探测 |
| 3 | `error: local repo seed has no build_repo.py` | patch-mode case（有 `source_dir`、`patch` 字段）不走 git-history 流程 | **不要**传 `--local-repo`；patch case 自动复制 source_dir 到 worktree |
| 4 | `UnicodeDecodeError`（读 JSON）、`UnicodeEncodeError`（打印中文/`\ufffd`） | Windows 默认 GBK 编解码 | 读文件显式 `encoding='utf-8'`；诊断脚本写临时 .py 文件跑，console 输出保持 ASCII-safe |
| 5 | `claude` 命令 returncode 127 | Windows 无法直接解析 `claude` | 用 `python -m code_review_ai.agent_adapter claude`（内部 `_resolve_claude_executable` 找 `.cmd` shim） |
| 6 | 跑成功但 **MCP 全 0 调用**（`mcp_adoption_rate=0`），agent 退回原生 grep/读，cost 反升 | `48e3b32` 起 `mcp_server.py` 模块级 `import toon_format`；venv 若在添加该 git 依赖前 sync，`uv run --no-sync` 不装它 → MCP 子进程在 worktree cwd 启动即崩 → 0 工具注册（主 CLI 不 import，所以 CLI 正常、只有 server 挂） | `uv sync --extra dev`（装 `toon-format` git 依赖）；之后 `uv sync` 会顺带卸掉 community 可选依赖，需 community 时补 `--extra community`。预检：`venv-python -c "import toon_format"`，或对 worktree `list_tools` 应看到 3 个核心工具 |
| 7 | report 里 `tool_trace[].response` 的中文变 `�`（如 `msg="�洢Դ…"`），但模型侧与 `agent_review` 都是干净中文，**评分不受影响** | claude CLI 在 Windows 上把工具结果内容按控制台代码页（GBK）重新序列化进 `--output-format stream-json` 观察流；适配器 `encoding='utf-8', errors='replace'` 解码 → 无效字节被替换成 U+FFFD。模型内部走另一条通道（干净 UTF-8），不经过这个损坏 | **不需要修**（遥测伪影）。要证明模型侧干净：跑一个「逐字引用 get_impact call_site 中文」的探针，回文 U+FFFD 计数=0 即模型看到干净文本。**别用 Bash 控制台看中文**（Windows 下是 GBK，干净文本也显示成 `�` 会误判）——写 UTF-8 文件再 Read，或查码点。详见 `eval-results/alias-core-synced/`（`_raw_toon.txt` 干净 / `_toon_dump.txt` 乱码 / `_probe_msg_utf8.txt` 模型引用干净） |

## 2. 跑之前：廉价预检（不花 LLM 钱）

```bash
# 1) 预检：会真的复制 repo + 建索引，但不调 LLM
uv run --no-sync python -m code_review_ai.cli full-agent-eval \
  --cases benchmarks/case-backend-cases.json \
  --case-ids <case-id> --agent-command "<abs-venv-python> -m code_review_ai.agent_adapter claude" \
  --modes full_project_core --dry-run

# 2) 确认 agent-command 能从 worktree cwd 解析（代替真实 run 的秒挂诊断）
cd eval-results/<run-name>/worktrees/<case-id>-*/ && \
  /c/Users/HMG-BA110/Desktop/code-review-ai/.venv/Scripts/python.exe -m code_review_ai.agent_adapter --help
#   出现 usage 即 OK；ModuleNotFoundError 说明 python 路径不对
```

## 3. 读结果：数据格式陷阱

- **transcript 的 `stdout` 是 adapter 归一化后的最终 JSON**（单个 dict），不是 claude raw stream → **拿不到 per-turn usage**；对比 token 用 report.json 的 `usage`。
- **tool_trace 字段**：真实入参在 **`input`**（`arguments` 恒为空）；MCP 工具才有 `response` 全文，原生工具只有 `response_chars`。
- **`predicted_findings` 是 int 计数**不是 list → 遍历会 `TypeError`；真命中看 `matched_findings`。
- **对比 token 别只看 `input_tokens`**：那是 fresh（未命中缓存）部分。真实输入量 = `input_tokens + cache_read_input_tokens`；cache_read 打折计费，所以 **cost 差距 < total input 差距**（例：total 2.43×，cost 只 1.56×）。

## 4. 方法论提醒

- **n=1 方差大**：单次 run 的 cost 波动（$0.19~$0.29）和优化幅度同量级 → 关键结论必须 `--repetitions 3`。
- **单 case 不够**：alias case 是单符号局部 bug，f1 天然易 1.0；下结论前要在深链路/多符号/高 uncertainty 的 case 上抽查。
- **`--dry-run` 不是纯检查**：它会真的建 worktree + 索引（几秒，无 LLM 成本）。

## 5. 已知可复用的经验

- **core 模式最优工具轨迹**（验证过）：`get_change_summary`(第一) → `get_impact`(每变更符号一次, 默认 max_level=1) → 变更文件全文读（豁免）→ 调用方用 offset/limit 精读。**不要**用 `max_level=0` 重查同一符号/文件。
- **`get_impact` 描述里那句 "you usually don't need to Read caller files" 是关键信号**：去掉后模型会退回用 4+ 次原生读补证据，cost 从 $0.20 涨到 $0.29。改描述后必须重跑验证。
- **TOON vs JSON 序列化：同工具下格式差异 ≈ 0，别为省 token 折腾它**（2026-08 alias case，确定性同 payload 对比）。get_impact 在 eval 默认 `max_level=1` 时 TOON 反而**长 ~1%**（6522 vs 6472），全闭包下长 5%；结构字符拆解：JSON 语法开销 816（11.4%）vs TOON 缩进+标记 985（13.6%）——get_impact 4 层深嵌套（symbol→upstream→item→call_site），TOON 按行×深度付缩进费，光缩进 862 就超过它省掉的 JSON 全部引号/花括号。且 payload 里 ~23% 是两格式逐字相同的 code snippet + sig。TOON 只在**扁平** payload 上赢（get_change_summary 短 7.7%，但仅 ~600 字符，省的钱可忽略）。因此**默认串行化已改回 JSON**（`toon=false`），TOON 仅留作 ablation 开关（`CRAI_EVAL_TOON`：0=强制 JSON，1=强制 TOON，空=服务器默认 JSON；eval 臂 `full_project_core_json`/`full_project_core_toon`）。$0.007 的 core/toon 成本差是行为方差（多一次 search_symbol+Read 就 ~1650 token），不是格式。
