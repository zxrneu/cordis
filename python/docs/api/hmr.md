# Python 代码热重载 API

[返回 API 索引](../api.md) · [配置与加载器](loader.md)

源码：[`hmr.py`](../../src/cordis/hmr.py)。HMR 重新导入修改过的 Python 模块，并替换受影响的插件挂载。只修改运行配置时，使用 `Fiber.update()` 或 `Loader.reconcile()`。

## HotReloader

```text
HotReloader(loader: Loader[ContextT], *, externals: Iterable[str] = ())
```

| 参数 / 属性 | 含义 |
| --- | --- |
| `loader` | 被管理的 Loader；递归查找 group/include 条目与可重新导入的类型化 mount |
| `externals` | 不能跨越的模块名集合，构造时复制为 set；默认空集合 |

依赖感知重载使用字符串定位符或模块名/限定名重新导入插件。模块顶层定义的 Component 和函数可自动匹配；局部闭包、lambda 和无可导入名称的对象不自动匹配，应改用稳定的模块级入口或字符串定位符。

### reload_changed

```python
async def reload_changed(changed: Iterable[str | Path]) -> tuple[str, ...]: ...
```

接收修改后的文件路径或已加载模块名，合并当前进程的静态 import 关系与插件加载窗口中捕获的动态导入，找到它们的直接和间接调用模块，再重载匹配的条目。返回被处理的条目路径元组（嵌套条目如 `group/worker`）；文件未映射到已加载模块，或没有匹配条目时返回 `()`。

例如，在宿主已有 loader 时：

```python
from cordis import HotReloader

hmr = HotReloader(loader, externals={"my_app.process_state"})
entry_ids = await hmr.reload_changed(["my_app/shared.py"])
```

`externals` 按**完整模块名精确匹配**，不是包名前缀过滤。接受的包会先扩展为其已缓存的子模块，再检查 externals；扩展后的受影响集合与它相交时，修改模块缓存前抛出 `FullRestartRequired`。

### reload_entries

```python
async def reload_entries(
    entry_ids: Iterable[str],
    *,
    accepted: set[str] | None = None,
) -> None: ...
```

显式重载条目，重复 id 会去重，未知 id 抛出 `KeyError`。

`accepted` 是本次允许从模块缓存中移除并重新导入的模块名集合。默认以所选字符串插件的入口模块为集合，**不会自动把入口模块的所有依赖一起重导入**；显式空集合也按默认处理。若集合包含包，会包含其已缓存子模块，防止重新导入包时复用旧子模块。通常用 `reload_changed()` 自动计算正确的受影响集合。

这是低层入口：它不会执行 `externals` 检查。需要边界保护时，应通过 `reload_changed()` 调用。停用的条目不会在替换阶段启动。

### watch

```python
async def watch(
    paths: Iterable[str | Path],
    *,
    interval: float = 0.5,
    stop: asyncio.Event | None = None,
    on_error: Callable[[Exception], None] | None = None,
    ready: asyncio.Event | None = None,
) -> None: ...
```

监听文件或目录，目录递归扫描 .py 文件。记录时间和大小，新增、修改、删除时调用 reload_changed()。ready Event 在初始快照完成后置位。

```python
watcher = root.spawn(
    hmr.watch(["my_app/plugins.py", "my_app/shared.py"], interval=0.5),
    policy="cancel",
    name="cordis:hmr",
)
```

初次调用不重载。interval 必须为有限正数，stop 可中断轮询等待。重载失败、语法错误和 FullRestartRequired 交给 on_error，默认记录到运行时；监听继续，下一次源码变化可重新尝试。FullRestartRequired 仍由宿主决定如何重启。

## 重载与恢复范围

重载会备份受影响模块及父包属性绑定，清除相应缓存和仍指向被移除模块的父包属性，再导入候选插件。候选导入成功后才卸载旧挂载，启动启用的替代挂载。

| 失败位置 | 当前处理 |
| --- | --- |
| 导入候选模块失败 | 恢复备份的模块缓存并抛出异常；原 Fiber 尚未替换 |
| 新插件初始化失败 | 清理替代挂载、恢复模块缓存，并尝试用旧 Component 重新挂载 |
| 服务替换导致消费者初始化失败 | 恢复旧提供者，并重新启动本次更新导致失败的原消费者；包含间接依赖及其他 Loader 中的消费者 |
| 恢复旧插件也失败 | 使用 `ExceptionGroup` 报告重载和恢复错误 |

恢复成功也会抛出原重载失败，便于宿主知道新版本未生效。替换后应重新通过 `loader.get(id)` 获取当前 Fiber，不要继续把旧 Fiber 当作现有挂载。

消费者检查包含原先等待服务、在新版本中才开始激活的组件；恢复旧版本后可重新回到 `PENDING`。已有的失败不会被当成本次重载失败，也不会自动重试。消费者恢复再次失败时，会与原重载错误一起报告。

回滚同时恢复 sys.modules 和原父包属性（包括原有非模块属性），清除失败导入在接受模块的顶层命名空间内新增的模块与绑定。包子模块也受缓存一致性检查。

HMR 按稳定顺序取得涉及 Loader 的锁，与其他管理操作互斥，嵌套及类型化挂载在同一事务中替换与恢复。排队时取消不变更状态；导入、旧实例卸载或候选启动期间取消，会等待整个提交或回滚完成后再向调用方抛出 CancelledError。线程导入也必须完成后才释放锁；候选成功可能已提交，候选失败则尝试恢复旧版本。未设置统一收尾超时，阻塞导入和不结束的 cleanup 仍可能阻塞调用。

自动匹配覆盖嵌套 group/include 和可重新导入的类型化挂载。HMR 不会撤销模块顶层的文件写入、网络请求或进程全局副作用。依赖图包含 AST 静态边和插件加载期间追踪的动态边；加载窗口之外才执行的条件导入、外部扩展及复杂重导出仍需要宿主评估覆盖范围。

重复协调配置或导入已缓存的插件会保留已有动态依赖；模块被移出缓存并重新执行时，才重新收集其动态依赖。

## FullRestartRequired

从 `RuntimeError` 继承，可从 `cordis` 导入。表示 `reload_changed()` 的受影响模块跨过 externals 边界，当前调用不能安全执行局部模块替换。

它只是异常信号，**不会自动重启进程**。由宿主或进程管理器决定如何完成整体重启，也可以终止当前开发监听并重新启动应用。

## ModuleGraph

```python
ModuleGraph()
```

高级诊断接口：为当前 `sys.modules` 中可定位到现有 `.py` 源码的模块建立快照。构造时会同步读取和解析这些源码；较大进程可通过 `await asyncio.to_thread(ModuleGraph)` 避免阻塞事件循环。

| 属性 | 类型 | 含义 |
| --- | --- | --- |
| `paths` | `dict[str, Path]` | 模块名到解析后的绝对源码路径 |
| `by_path` | `dict[Path, set[str]]` | 同一路径对应的模块名集合 |
| `imports` | `dict[str, set[str]]` | 每个模块静态及已追踪动态导入的已加载模块 |

这些都是构造时的快照，后续 import 或文件变更不会自动更新它们。源码无法读取或解析时，该模块的 import 集合按空集合处理。

### modules_for

```python
def modules_for(changed: Iterable[str | Path]) -> set[str]: ...
```

把文件路径或已知模块名转换为本图中的模块名集合。不匹配的项被忽略；不会为了查找而导入未知模块。

### affected

```python
def affected(changed: set[str]) -> set[str]: ...
```

返回 changed 本身，加上通过反向 import 关系找到的全部依赖它们的模块。方向是“谁导入了被修改模块”，而不是“被修改模块导入了谁”。

```python
from cordis import ModuleGraph

graph = ModuleGraph()
changed = graph.modules_for(["my_app/shared.py"])
affected = graph.affected(changed)
```

查询图不会触发重载；真正替换插件使用 HotReloader 或 Loader 的重载方法。
