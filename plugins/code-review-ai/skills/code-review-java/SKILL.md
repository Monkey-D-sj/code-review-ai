---
name: code-review-java
description: 按 Java 审核规范审查 Java 代码（.java）。含安全、正确性、性能、架构与 Java 语言特有检查点，发现标注 error/warning/info。
---

# Java 代码审核规范

## 审核方式

通读待审代码，逐类对照以下检查点；每条发现标注严重级（error / warning / info）+ `文件:行号` + 修复建议。本 skill 只输出规则与发现，不生成报告框架（报告交给 `code-review`）。

## 安全

- error：硬编码密码、密钥、Token、数据库连接串。
- error：字符串拼接 SQL 或 JDBC 未参数化查询（SQL 注入）。
- error：日志输出密码、身份证、银行卡、Token 等敏感信息。
- error：对不可信输入做反序列化（`ObjectInputStream.readObject`）未做类型白名单。
- error：XML 解析未禁用外部实体/DTD（XXE）——`DocumentBuilderFactory` 未 `setFeature` 禁用 DOCTYPE/外部实体。
- error：基于不可信输入反射执行（`Class.forName` + `Method.invoke`）。

## 正确性

- error：空 `catch`，捕获后不处理不抛出。
- error：资源（`InputStream`/`Connection`/`ResultSet`）未用 try-with-resources 或 finally 关闭。
- error：对象相等用 `==`（应用 `equals`，如字符串比较）。
- error：可变共享状态缺同步（非 final 的 `static` 字段、并发使用 `SimpleDateFormat`）。
- warning：`catch (Exception e)` 过宽 / 吞异常。
- warning：整数运算未考虑溢出（大数乘法/自增）。

## 性能

- warning：循环内查询数据库或发起网络请求（N+1）。
- warning：热点路径用 `+` 拼接字符串（应用 `StringBuilder`）。
- warning：无界集合/缓存；同步块过大（应缩小锁范围或用并发集合）。

## 架构

- warning：类超过 300 行、方法超过 50 行。
- warning：逻辑 ≥3 步或嵌套 ≥2 层未拆分为语义清晰的子函数。
- warning：有状态的 `static` 万能类（工具类应无状态）。
- info：魔法数字、未使用变量/导入、冗余或注释掉的旧代码、缺必要注释。

## 语言特有

- 资源管理优先 try-with-resources，避免手写 `close`。
- 覆写 `equals` 必须同步覆写 `hashCode`。
- `Optional` 避免裸 `get()`，用 `orElse` / `orElseThrow`。
- 优先不可变集合 `List.of` / `Map.of`。
- 用 `@Override` 标记覆写方法。
- 命名：类 `UpperCamelCase`、方法/变量 `camelCase`、常量 `UPPER_SNAKE`。
