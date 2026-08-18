---
name: code-review-typescript
description: 按 TypeScript 审核规范审查 TypeScript 代码（.ts/.tsx）。含安全、正确性、性能、架构与 TypeScript 语言特有检查点，发现标注 error/warning/info。
---

# TypeScript 代码审核规范

## 审核方式

通读待审代码，逐类对照以下检查点；每条发现标注严重级（error / warning / info）+ `文件:行号` + 修复建议。本 skill 只输出规则与发现，不生成报告框架（报告交给 `code-review`）。

## 安全

- error：硬编码密码、密钥、Token。
- error：字符串拼接 SQL（如 `SELECT ... + userInput`）。
- error：日志输出密码、身份证、银行卡、Token 等敏感信息。
- error：把不可信输入传入 `eval` / `new Function`。
- error：XSS 注入点——把不可信输入直接放入 `innerHTML` / `dangerouslySetInnerHTML` / `document.write`（应使用 `textContent` 或转义）。

## 正确性

- error：空 `catch`，或捕获后既不处理也不重新抛出。
- error：文件句柄、数据库连接、`EventSource`/`WebSocket` 等资源未关闭。
- error：共享可变状态缺少同步；未处理的异步 rejection 被吞掉。
- warning：`catch (error)` 类型过宽，丢失类型信息。
- warning：`null` 与 `undefined` 混用/未区分。

## 性能

- warning：循环内查询数据库或发起网络请求（N+1）。
- warning：渲染热点中无必要的重计算（缺 memoization）或大数组频繁全量拷贝。

## 架构

- warning：函数体超过 50 行、类超过 300 行。
- warning：逻辑 ≥3 步或嵌套 ≥2 层未拆子函数；组件/模块单一职责被破坏。
- info：单字母变量名；魔法数字；未使用变量/导入；冗余代码。

## 语言特有

- 开启 `strict` 模式；`strictNullChecks` 开启，不依赖隐式 `any`。
- 避免 `any` / `as any` 逃逸类型安全；公共 API 显式标注返回类型。
- 区分 `null` 与 `undefined`，用可选链 `?.` 与空值合并 `??`。
- 不可变量用 `readonly` / `const` 建模。
- async 错误用 try/catch 传播，`Promise.all` 失败要处理，不吞 rejection。
- 用 discriminated union 收窄类型，避免过度断言。
