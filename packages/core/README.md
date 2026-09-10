<div align="center">
  <h1 id="satori">Cordis</h1>
</div>

A Meta-Framework of Spatiotemporal Composability.

**Cordis is under active development. The API is not yet stable and may change without notice.**

- Paper: _A Programming Paradigm for Spatiotemporal Composability_ [[arXiv](https://arxiv.org/abs/2608.25512)] [[repository](https://github.com/cordiverse/paper)]
- Documentation: [cordis-primer](https://deepseek-harness.github.io/deepseek-harness/reference/cordis-primer) (official documentation is still under construction)

## 📚 开发者指南与文档索引

### TypeScript 实现 (`packages/core`)
- **入门到精通**：[TypeScript 开发者完全指南与高频 Q&A](doc/developer-guide.md)
- **底层走读**：[TypeScript 核心原理代码走读与维护者手册](doc/internals-walkthrough.md)
- **系统知识库**：[TypeScript Wiki 知识库目录](doc/README.md)
- **实战演练示例**：[TypeScript 可执行实战示例 (`examples/01~06`)](examples/)
  - `examples/01-basic-plugin.ts`：基础插件与生命周期清理
  - `examples/02-custom-service.ts`：自定义 Service 与异步 `[Service.init]`
  - `examples/03-reactive-injection.ts`：响应式依赖注入与自愈
  - `examples/04-advanced-events.ts`：高级事件分发流 (`emit`/`parallel`/`bail`/`waterfall`)
  - `examples/05-isolation-and-realms.ts`：服务隔离域 (`ctx.isolate`) 与多租户
  - `examples/06-callable-service.ts`：函数式可调用服务 (`[Service.invoke]`)

### Python 原生实现 (`python/src/cordis`)
- **双端全方位对比**：[Python vs TS 架构对比与演进指南](python/docs/python-vs-typescript.md)
- **入门到精通**：[Python 开发者完全指南与高频 Q&A](python/docs/developer-guide.md)
- **底层走读**：[Python 核心原理代码走读与维护者手册](python/docs/internals-walkthrough.md)
- **中文入门**：[Python 快速上手（订单导入）](python/docs/zh-CN.md)
- **Python 首页**：[Cordis for Python 文档中心](python/README.md)

