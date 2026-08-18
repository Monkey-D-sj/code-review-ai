---
name: code-review-javascript
description: 按 JavaScript 审核规范审查 JavaScript 代码（.js/.jsx/.mjs/.cjs）。含安全、正确性、性能、架构与 JavaScript 语言特有检查点，发现标注 error/warning/info。
---

# JavaScript 代码审核规范

## 审核方式

通读待审代码，逐类对照以下检查点；每条发现标注严重级（error / warning / info）+ `文件:行号` + 修复建议。本 skill 只输出规则与发现，不生成报告框架（报告交给 `code-review`）。

## 安全

- error：硬编码密码、密钥、Token。
- error：字符串拼接 SQL。
- error：日志输出密码、身份证、银行卡、Token 等敏感信息。
- error：把不可信输入传入 `eval` / `new Function`。
- error：XSS——不可信输入直接写入 `innerHTML` / `document.write`（用 `textContent` 或转义）。
- error：合并对象时原型污染——对不可信输入用 `{...obj}` / `Object.assign` 覆盖 `__proto__`。

## 正确性

- error：空 `catch`，捕获后不处理不抛出。
- error：资源（文件流、WebSocket、EventSource）未关闭。
- error：未处理/被吞掉的异步 rejection（`unhandledRejection`）。
- warning：共享可变状态、模块级可变全局缺约束。
- warning：`NaN` 判断用 `==`（应用 `Number.isNaN`）；浮点相等直接比较（`0.1 + 0.2 === 0.3`）。

## 性能

- warning：循环内 DB/网络（N+1）；DOM 查询在循环内重复执行。
- warning：渲染/计算热点无谓重算（缺缓存）。

## 架构

- warning：函数体超过 50 行、类超过 300 行；回调嵌套 ≥3 层（回调地狱）。
- warning：≥3 步或嵌套 ≥2 层逻辑未拆子函数。
- info：隐式全局变量；未使用变量/导入；魔法数字；冗余代码。

## 语言特有

- 用 `const` / `let`，弃用 `var`。
- 用严格相等 `===` / `!==`，避免隐式类型转换。
- 异步优先 async/await 或 Promise，注册 `unhandledRejection` 兜底。
- 模块化：用 import/export，避免隐式全局与命名空间污染。
- DOM 写入优先 `textContent` / 建节点，避免 `innerHTML` 拼接不可信数据。
