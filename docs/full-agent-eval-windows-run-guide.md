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
