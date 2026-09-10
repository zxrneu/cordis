# 06. 声明式配置与加载器系统

在生产环境中，大型系统通常不希望硬编码 `ctx.plugin()`，而是希望通过**声明式配置文件（YAML / JSON）**驱动系统拓扑，并支持在运行期通过管理界面或 API 动态增删插件、调整配置并持久化回写。

`@cordisjs/plugin-loader` 与 `@cordisjs/plugin-include` 共同构成了 Cordis 的声明式编排底座。

---

## 1. Loader 拓扑模型：Tree、Group 与 Entry

Loader 将整个应用的插件拓扑抽象为一个**配置树**：

```
                              EntryTree (根配置树)
                                      │
                         ┌────────────┴────────────┐
                         ▼                         ▼
                  Entry: 'logger'           Entry: 'group-1' (EntryGroup)
                                                   │
                                      ┌────────────┴────────────┐
                                      ▼                         ▼
                                Entry: 'http'             Entry: 'database'
```

### 1.1 核心概念

- **`EntryTree`**：配置树的根容器，维护全局 `store`（所有 Entry 的索引字典），提供 `resolve(id)`、`create()`、`update()`、`remove()` 等拓扑操作。
- **`EntryGroup`**：分组容器。一个 Group 本身也是一个 Entry，可以嵌套包含多个子 Entry，支持批量启用、禁用或作用域隔离。
- **`Entry`**：单个插件的配置载体，包含如下核心字段：

```typescript
export interface EntryOptions {
  id: string              // 唯一标识符（若未指定则自动生成 8 位随机 hex）
  name: string            // 插件模块名称（如 'cordis:group', '@cordisjs/plugin-timer'）
  config?: any            // 传递给插件的配置对象
  disabled?: boolean      // 是否禁用该插件
  group?: boolean         // 是否为分组容器
  inject?: Inject         // 覆盖插件默认声明的依赖列表
  isolate?: Dict<string>  // 命名空间隔离规则
  intercept?: Dict        // 局部配置拦截规则
}
```

---

## 2. 空间隔离实现原理（Isolate & Realms）

Cordis 的多租户与服务隔离是通过 `isolate` 模块实现的。每个隔离的服务会被分配一个唯一的 Symbol，挂载在 `ctx[Context.isolate]` 字典中。

```
           全局上下文 (Root Context) ──> database -> Symbol(database)
                                           │
         ┌─────────────────────────────────┴─────────────────────────────────┐
         ▼                                                                   ▼
   LocalRealm (#id)                                                   GlobalRealm (@custom)
   entry.isolate: { database: true }                                  entry.isolate: { database: 'tenant_a' }
   指向 Symbol(database#1a2b3c)                                       指向 Symbol(database@tenant_a)
   仅该 Entry 及其子级独享该数据库实例                                  所有声明 @tenant_a 的 Entry 共享该数据库实例
```

### 2.1 两种隔离领域（Realms）

1. **`LocalRealm`（私有本地隔离）**：
   - 配置方式：`isolate: { database: true }`
   - 生成后缀：`#<entry.id>`
   - 作用：当前 Entry 独立拥有该服务的新实例，外部与父级完全不可见。
2. **`GlobalRealm`（共享命名空间隔离）**：
   - 配置方式：`isolate: { database: 'team_sales' }`
   - 生成后缀：`@team_sales`
   - 作用：所有标记为 `'team_sales'` 的插件共享同一个特定的服务实例，而其他未标记或标记不同 label 的插件使用不同的实例。

---

## 3. 配置文件持久化与动态集成（`Include` 插件）

`@cordisjs/plugin-include` 负责将外部文件（如 `cordis.yml`）与内存中的 `EntryTree` 进行双向同步绑定：

```yaml
# cordis.yml 示例
- name: '@cordisjs/plugin-logger-console'
  config:
    showTime: 'yyyy-MM-dd hh:mm:ss'

- name: '@cordisjs/plugin-timer'

- name: 'group:services'
  group: true
  isolate:
    database: true # 局部隔离 database 服务
  config:
    - name: './plugins/my-database'
      config:
        host: 'localhost'
        port: 5432
    - name: './plugins/my-api'
      config:
        port: 8080
```

### 3.1 双向同步流程

1. **冷启动加载**：应用启动时，`Include` 读取 YAML/JSON 文件并解析为 Entry 树结构。
2. **外部文件监听**：若外部修改了 `cordis.yml`（例如通过 Git 同步或手动编辑），`Include` 会感知文件变更并触发局部增量重载。
3. **运行时回写**：当管理员在前端控制台修改了插件配置或启用了新插件，Cordis 触发 `internal/update` 事件，`Include` 自动将最新拓扑结构原子化格式化回写至磁盘文件（采用 `.tmp` 临时文件 + rename 原子替换，防止写入中断损坏文件）。

---

## 4. 动态打补丁机制（Patches）

在多环境部署（开发环境、测试环境、生产环境）或预置发行版中，往往需要基于公共配置文件应用局部差异。`Include` 提供了强大的 Patches 能力：

```typescript
ctx.plugin(Include, {
  path: './cordis.yml',
  patches: [
    // 覆盖指定 ID 插件的配置
    {
      id: 'my-api-id',
      config: { port: 9000 },
    },
    // 动态追加新插件
    {
      insert: [
        {
          name: '@cordisjs/plugin-hmr',
          config: { debounce: 200 },
        },
      ],
    },
  ],
})
```

---

## 5. 表达式插值求值（JsExpr 与 `!js` 标签）

Cordis 支持在 YAML 配置文件中直接书写 JavaScript 表达式，动态读取环境变量或上游上下文：

```yaml
- name: './plugins/http-server'
  config:
    port: !js process.env.PORT || 3000
    secret: !js `app-${Date.now()}`
```

### 底层求值实现
在加载配置时，`interpolate` 会递归扫描配置节点，利用 `with (ctx) { return eval(expr) }` 在受控的上下文环境中完成表达式求值与注入。
