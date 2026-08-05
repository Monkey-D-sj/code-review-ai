---
name: code-review-python
description: 按 Python 审核规范审查 Python 代码（.py）。含安全、正确性、性能、架构与 Python 语言特有检查点，发现标注 error/warning/info。
---

# Python 代码审核规范

## 审核方式

通读待审代码，逐类对照以下检查点；每条发现标注严重级（error / warning / info）+ `文件:行号` + 修复建议。本 skill 只输出规则与发现，不生成报告框架（报告交给 `code-review`）。

## 安全

- error：硬编码密码、密钥、Token、数据库连接串。
- error：字符串拼接 SQL 或未参数化的查询（SQL 注入）。
- error：日志输出密码、身份证、银行卡、Token 等敏感信息。
- error：对不可信输入使用 `eval` / `exec`。
- error：不安全的反序列化——对不可信数据 `pickle.loads`、`yaml.load` 不带 `Loader`。

## 正确性

- error：空 `except`，或捕获后既不处理也不重新抛出。
- error：文件、网络、数据库等资源未用 `with` / `finally` 正确关闭。
- error：手动创建裸线程（`threading.Thread`）且无异常处理/守护。
- error：共享可变状态（全局 list/dict、跨线程）缺少同步。
- warning：`except Exception` 过宽 / bare `except`，掩盖具体错误。
- warning：可变默认参数（`def f(items=[])`）。

## 性能

- warning：循环内查询数据库或发起网络请求（N+1）。
- warning：热点路径用 `+` 拼接字符串（应用 f-string / `"".join`）。
- warning：无必要的深拷贝或重复计算（缺少缓存/记忆化）。

## 架构

- warning：函数体超过 50 行、类超过 300 行。
- warning：逻辑 ≥3 步或嵌套 ≥2 层未拆分为语义清晰的子函数。
- warning：主控函数直接写实现细节（应只做参数准备 → 调用子函数 → 返回）。
- info：单字母变量名（数学索引除外）；内置名当变量名（`id`/`list`/`dict`/`str`）；循环变量无语义。
- info：魔法数字、未使用变量/导入、冗余或注释掉的旧代码、缺必要注释。

## 语言特有

- 资源管理优先 `with` 语句；异常尽量捕获具体类型。
- f-string 优先于 `%` 格式化和 `.format()`。
- 遍历序列用 `enumerate`，避免 `for i in range(len(items))`。
- datetime 注意时区：naive 与 aware 不混用，推荐 `timezone.utc`。
- 模块级常量用 `UPPER_SNAKE` 命名。
- 列表推导嵌套不宜过深。
