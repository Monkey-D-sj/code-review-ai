# Spring Boot / FastAPI 框架语义覆盖报告

本报告与 215 项纯语法目录分开统计。

## 当前状态

| 框架 | 完整支持 | 部分支持 | 缺失 |
|---|---:|---:|---:|
| FastAPI | 3 | 1 | 0 |
| Spring Boot | 7 | 0 | 0 |

## 已支持

- FastAPI `@app.get`、`@router.post` 等路由装饰器作为业务入口。
- FastAPI `Depends(provider)` 和 `Security(provider)` 依赖边，并支持递归依赖链。
- Spring Boot `@RestController`、类级/方法级 `@RequestMapping` 与 `@GetMapping` 路由映射。
- Spring `@Autowired` 字段注入、构造器注入和 Repository 依赖链。
- Spring `@Bean` 方法入口识别。
- 已有 MockMvc 请求到 Controller 映射的框架边。

## 当前限制

FastAPI `include_router()` 的 prefix 组合暂未物化为独立 route 节点；Spring Security/filter/interceptor chain、WebFlux functional route、Kafka/JMS listener、复杂条件化 Bean 与完整 Spring Boot 配置仍需后续增加。
