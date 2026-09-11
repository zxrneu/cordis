# Python 代码热重载 API

[返回 API 索引](../api.md) · [配置与加载器](loader.md)

源码：[`hmr.py`](../../src/cordis/hmr.py)。HMR 重新导入修改过的 Python 模块，并替换受影响的插件挂载。只修改运行配置时，使用 `Fiber.update()` 或 `Loader.reconcile()`。

## HotReloader

```text
HotReloader(loader: Loader[ContextT], *, externals: Iterable[str] = ())
```

| 参数 / 属性 | 含义 |
| --- | --- |
| `loader` | 被管理的 Loader；自动查找其声明式 Entry 中的字符串定位符，不处理类型化 mount |
| `externals` | 不能跨越的模块名集合，构造时复制为 set；默认空集合 |

依赖感知重载通过**字符串插件定位符**匹配条目。已直接传入的函数、Component 或对象不会自动跟随源码文件变更；要支持该路径，Entry 的 plugin 使用 `package.module:attribute`。

### reload_changed

```python
async def reload_changed(changed: Iterable[str | Path]) -> tuple[str, ...]: ...
```

接收修改后的文件路径或已加载模块名，扫描当前进程的静态 import 关系，找到它们的直接和间接调用模块，再重载匹配的条目。返回被处理的条目 id 元组；文件未映射到已加载模块，或没有匹配条目时返回 `()`。

例如，在宿主已有 loader 时：

```python
from cordis import HotReloader

hmr = HotReloader(loader, externals={"my_app.process_state"})
entry_ids = await hmr.reload_changed(["my_app/shared.py"])
```

`externals` 按**完整模块名精确匹配**，不是包名前缀过滤。受影响集合与它相交时，修改模块缓存前抛出 `FullRestartRequired`。

### reload_entries

```python
async def reload_entries(
    entry_ids: Iterable[str],
    *,
    accepted: set[str] | None = None,
) -> None: ...
```

显式重载条目，重复 id 会去重，未知 id 抛出 `KeyError`。

`accepted` 是本次允许从模块缓存中移除并重新导入的模块名集合。默认以所选字符串插件的入口模块为集合，**不会自动把入口模块的所有依赖一起重导入**；显式空集合也按默认处理。通常用 `reload_changed()` 自动计算正确的受影响集合。

这是低层入口：它不会执行 `externals` 检查。需要边界保护时，应通过 `reload_changed()` 调用。停用的条目不会在替换阶段启动。

### watch

```python
async def watch(
    paths: Iterable[str | Path],
    *,
    interval: float = 0.5,
    stop: asyncio.Event | None = None,
) -> None: ...
```

对给定的一组**现有文件**记录修改时间并持续轮询，发生变化时调用 `reload_changed()`。只检查传入的固定文件集合，不递归扫描目录，也不自动发现新文件。

```python
watcher = root.spawn(
    hmr.watch(["my_app/plugins.py", "my_app/shared.py"], interval=0.5),
    policy="cancel",
    name="cordis:hmr",
)
```

初次调用不重载。interval 由调用方保证为有限正数；stop 在循环边界检查。文件删除、读取失败、`FullRestartRequired` 或重载失败都会使 watcher 结束，宿主负责恢复或重启；它没有防抖、退避或自动重试。

## 重载与恢复范围

重载会备份受影响模块、清除相应缓存，再导入候选插件。候选导入成功后才卸载旧挂载，启动启用的替代挂载。

| 失败位置 | 当前处理 |
| --- | --- |
| 导入候选模块失败 | 恢复备份的模块缓存并抛出异常；原 Fiber 尚未替换 |
| 新插件初始化失败 | 清理替代挂载、恢复模块缓存，并尝试用旧 Component 重新挂载 |
| 恢复旧插件也失败 | 使用 `ExceptionGroup` 报告重载和恢复错误 |

恢复成功也会抛出原重载失败，便于宿主知道新版本未生效。替换后应重新通过 `loader.get(id)` 获取当前 Fiber，不要继续把旧 Fiber 当作现有挂载。

HMR 只恢复模块缓存和受管插件资源，不会撤销模块顶层的文件写入、网络请求或进程全局副作用。依赖图只识别 AST 中的静态 import；动态导入、外部扩展和复杂包重导出不能据此获得完整覆盖。

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
| `imports` | `dict[str, set[str]]` | 每个模块静态导入的已加载模块 |

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
