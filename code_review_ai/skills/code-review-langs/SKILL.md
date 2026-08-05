---
name: code-review-langs
description: 审查任何代码前先看本 skill——语言审核 skill 套件的入口。列出可用语言（Python/TypeScript/JavaScript）及对应规范 skill，按代码语言路由到具体 skill；不确定时用它确定该用哪套规范。
---

# 语言审核 skill 入口

本 skill 是语言审核 skill 套件的**入口路由表**：只负责决定"用哪套规范"，不包含具体规则。

## 支持的语言与对应 skill

| 语言 | 扩展名 | 审核 skill |
|---|---|---|
| Python | `.py` | `code-review-python` |
| TypeScript | `.ts` / `.tsx` | `code-review-typescript` |
| JavaScript | `.js` / `.jsx` / `.mjs` / `.cjs` | `code-review-javascript` |

## 路由规则

- 待审代码是**单一语言** → 直接调用上表对应 skill。
- **混合仓库 / 多语言变更** → 按文件扩展名逐个路由，每个文件用对应语言的 skill。
- **其他语言**（上表没有）→ 用通用工程最佳实践审查，并在报告中标注"该语言无专用规范"。

## 与 `code-review` skill 的关系

- 本套件只给"按语言审什么"的**静态规则清单**；`code-review` skill 负责 git diff 审查/评分/报告框架。两者独立，可配合使用。
- 本套件所有规则输出统一 `error` / `warning` / `info` 三级，与 `code-review` 的评分公式（得分 = max(40, 100 − error×10 − warning×3)）兼容。
