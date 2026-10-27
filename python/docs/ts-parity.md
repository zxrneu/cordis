# Python 与 TS 行为对齐记录

本轮补齐此前审计列出的行为差异，采用 Python 的显式类型、contextvars、不可变配置对象及异步资源管理，不模拟 JavaScript Symbol 或修改对象原型。

| 范围 | 已实现行为 |
| --- | --- |
| 服务 | 定义方依赖声明与已提交 Realm 保留；任务、监听器等副作用归调用方；支持自身 provide 的严格读取 |
| Context | 服务赋值验证所有权；显式 extend 元数据；派生视图继承子类实例状态 |
| 对象服务 | ContextAware 显式追踪、可选调用方依赖范围、foo.bar 关联、接收者 property、check 与配置合并 |
| 事件 | 同步 once 可用于 bail；异步与重入仍只触发一次；waterfall 防重复 next；同步 emit 异常向外传播 |
| 内部钩子 | plugin、service、update、get、set、listener、dispatch，加上原有 status、error |
| Loader | Entry.inject、显式配置表达式、嵌套查询与就绪门控；Fiber 更新和直接停用同步配置及持久化 |
| 配置 | 补丁字段与插入条目的所有权回写；sidecar、三方合并、冲突快照、延迟写入日志与重试 |
| HMR | 嵌套 group/include、可重新导入的类型化挂载，共同事务及回滚；目录监听和失败后继续监听 |
| 定时器与诊断 | 无回调 timeout、可关闭的异步 interval、关闭 throttle 尾部调用、只读 Effect 树快照 |

## 使用方式

服务内部的 `self.ctx` 指向本次调用者，依赖读取仍按照服务提供者的 inject 和 Realm 解析，即使调用来自根 Context 或经 extend/intercept 派生也一样：

```python
from cordis import Context, Service

class Api(Service[dict[str, object]]):
    def read(self) -> object:
        # 监听器由调用方拥有，db 来自定义方声明的依赖。
        self.ctx.on("refresh", lambda: None)
        return self.ctx.service("db")

async def run() -> None:
    async with Context() as root:
        await root.use(lambda ctx, _: ctx.provide("db", 42))
        await root.inject(["db"], lambda ctx, _: Api(ctx, "api"))
        values: list[object] = []
        client = await root.inject(["api"], lambda ctx, _: values.append(ctx.api.read()))
        await client.dispose()
```

配置清单可显式声明依赖和求值表达式：

```python
from cordis import ConfigExpression, Entry

worker = Entry(
    "worker",
    "myapp.plugins:worker",
    config={"port": ConfigExpression("ctx.port + 1")},
    inject=["db"],
)
```

文件中用 `{"__expr__": "ctx.port + 1"}` 表达同一含义。普通字符串保持字面值；不支持函数调用或语句。表达式通过 Context 读取输入，仍须使用可信配置。

## 兼容变化与边界

- 同步 emit 的监听器异常现在立即抛给调用方并中断该次派发；异步异常仍由受管任务记录。同步 once 在 emit 返回前完成，且可以用于 bail。
- 插件为已注册服务属性赋值现在执行所有权检查；临时公共元数据用 extend 显式声明。ServiceKey 描述符保持只读。
- ConfigStore 默认保持严格冲突与失败回滚；ConfigIncludes 默认采用文件优先合并及延迟回写。journal 是内存状态，宿主应在退出前 flush；失败会保留可重试信息。
- 类型化挂载 `loader.reload(id)` 保留原定义并重启；源码替换使用 HotReloader。替换后通过 loader.get() 获取当前视图。局部闭包、lambda 和无可导入名称的对象不能自动替换。
- ContextAware 是显式对象追踪入口；普通第三方对象保持原行为。Python 不会撤销模块顶层外部副作用，也不会卸载仍被其他代码引用的旧字节码。
- 主配置和 sidecar 分别原子写入，失败时尝试补偿；不提供跨文件原子提交。表达式与 HMR 的边界不等同于 JS eval 或 Node 模块加载器。

精确签名和失败语义见 [Context](api/context.md)、[Loader 与 ConfigStore](api/loader.md)、[HMR](api/hmr.md)。回归测试集中在 [test_ts_parity.py](../tests/test_ts_parity.py)，类型契约同时覆盖合法和非法用法。
