# 10. Cordis 中的 TypeScript 高级类型与语法全解

Cordis 之所以能够兼具**极强的动态扩展性**与**极致的类型安全（Type Safety）**，很大程度上归功于其对 TypeScript 现代类型系统与高级语法的深度运用。

本章系统归纳并在实战 Demo 及 Cordis 源码中用到的所有 TypeScript 核心概念与高级语法，帮助你不仅掌握 Cordis 的使用，更能理解其底层优雅的类型体操与架构设计。

---

## 目录导航

1. [模块扩充与声明合并 (Module Augmentation & Declaration Merging)](#1-模块扩充与声明合并-module-augmentation)
2. [计算属性名与 `unique symbol` 类型体系](#2-计算属性名与-unique-symbol-类型体系)
3. [条件类型与 `infer` 逆向类型推导 (Conditional Types & Type Inference)](#3-条件类型与-infer-逆向类型推导)
4. [映射类型与键重映射 (Mapped Types & Key Remapping with `as`)](#4-映射类型与键重映射-key-remapping)
5. [泛型型变标注 (`out` 协变关键字 - TS 4.7+)](#5-泛型型变标注-out-协变关键字)
6. [函数重载与 `NoInfer<T>` 阻断推断 (TS 5.4+)](#6-函数重载与-noinfer-阻断推断)
7. [多态 `this` 类型与上下文绑定 (Polymorphic `this`)](#7-多态-this-类型与上下文绑定)
8. [多态插件模式与类型收窄 (Discriminated Unions & Type Narrowing)](#8-多态插件模式与类型收窄)
9. [Stage 3 原生装饰器类型系统 (Decorators & Context API)](#9-stage-3-原生装饰器类型系统)
10. [高级异步与工具类型 (`PromiseLike`, `PromiseWithResolvers`)](#10-高级异步与工具类型)

---

## 1. 模块扩充与声明合并 (Module Augmentation)

### 1.1 为什么需要？
在微内核/插件架构中，根框架 `cordis` 在编写时**无法预知**未来会有哪些业务插件、哪些自定义服务（如 `database`, `http`, `auth`）以及哪些自定义事件被注册。

如果使用传统的 `any` 或全局字典，就会丧失类型提示。Cordis 采用 **模块扩充（Module Augmentation）** 与 **接口声明合并（Interface Declaration Merging）** 彻底解决了这一难题。

### 1.2 语法与实战应用

```typescript
// 业务代码：扩充 cordis 模块内部的接口定义
declare module 'cordis' {
  // 1. 合并到 Context 接口：让 ctx.database 获得强类型
  interface Context {
    database: DatabaseService
  }

  // 2. 合并到 Events 接口：让 ctx.on / ctx.emit 获得事件名与参数提示
  interface Events {
    'user/create'(user: { id: string; name: string }): void
    'user/auth'(token: string): boolean | string
    'user/transform'(data: string, next: () => string): string
  }
}
```

### 1.3 底层原理
TypeScript 允许使用 `declare module 'pkg-name'` 对已存在的模块进行“打补丁”。由于 TypeScript 中的 `interface` 具有**自动合并（Declaration Merging）**特性：
- 插件 A 声明了 `database: DatabaseService`；
- 插件 B 声明了 `timer: TimerService`；
- 最终在用户的 IDE 中，`Context` 将同时拥有 `database` 和 `timer` 的完整属性和方法提示。

---

## 2. 计算属性名与 `unique symbol` 类型体系

### 2.1 为什么需要？
Cordis 需要定义许多框架内部专属的生命周期与元数据钩子（如 `Service.init`、`Context.isolate` 等）。如果使用普通字符串（如 `_init`），容易发生命名冲突并被外部误调用。

通过 ES `Symbol` 与 TypeScript 的 `unique symbol`，既能实现独一无二的属性键，又能在类型系统中进行精准约束。

### 2.2 语法与实战应用

```typescript
// packages/core/src/service.ts 源码简析
export abstract class Service<out T = never> {
  // 使用 unique symbol 保证该字面量类型不可篡改且全局唯一
  static readonly init: unique symbol = symbols.init
  static readonly invoke: unique symbol = symbols.invoke
  static readonly extend: unique symbol = symbols.extend

  // 使用计算属性名定义生命周期函数
  abstract [Service.init]?(): any
}

// 业务服务实现
export class DatabaseService extends Service {
  // 使用 [Service.init] 作为方法名
  async [Service.init]() {
    // 异步初始化逻辑...
    return () => {
      // 卸载清理逻辑...
    }
  }
}
```

> [!NOTE]
> `unique symbol` 只能用 `const` 或 `static readonly` 声明，它代表一个在类型层面独一无二的 Symbol 字面量类型。

---

## 3. 条件类型与 `infer` 逆向类型推导

### 3.1 核心概念
条件类型（`T extends U ? X : Y`）配合 `infer` 关键字，可以像“解构”一样提取任意复杂类型中的内部组成部分（例如函数参数、返回值、Promise 的 Resolve 类型等）。

### 3.2 实战应用：提取事件参数与返回值

在 Cordis 的事件系统（`packages/core/src/events.ts`）中：

```typescript
// 1. 提取任意函数类型的参数元组
export type Parameters<F> = F extends (...args: infer P) => any ? P : never

// 2. 提取任意函数类型的返回值
export type ReturnType<F> = F extends (...args: any) => infer R ? R : never

// 3. 提取函数内部 this 的类型
export type ThisType<F> = F extends (this: infer T, ...args: any) => any ? T : never
```

#### 在 `ctx.emit` / `ctx.bail` 中的效果：
```typescript
declare module './context' {
  interface Context {
    // 根据事件名 K，精确推导出对应监听器的参数列表
    emit<K extends keyof Events>(name: K, ...args: Parameters<Events[K]>): void
    
    // 根据事件名 K，精确推导出对应监听器的返回值类型
    bail<K extends keyof Events>(name: K, ...args: Parameters<Events[K]>): ReturnType<Events[K]>
  }
}
```
当你输入 `ctx.emit('user/create', ...)` 时，TypeScript 自动通过 `Events['user/create']` 提取出参数为 `[{ id: string, name: string }]`，提供严格的入参类型校验。

---

## 4. 映射类型与键重映射 (Key Remapping with `as`)

### 4.1 核心概念
TypeScript 4.1 引入了通过 `as` 子句对映射类型中的键进行过滤和转换（Key Remapping）。

### 4.2 实战应用：从 Context 中提取所有有效服务键

在 `packages/core/src/registry.ts` 中，Cordis 需要计算出所有携带配置项的服务名称：

```typescript
export type InjectKey = keyof {
  // 遍历 Context 上的所有键 K
  // 如果 Context[K] 包含 [symbols.config] 属性，则保留该键 K；否则重映射为 never 从而过滤掉该键
  [K in keyof Context & string as Context[K] extends { [symbols.config]: any } ? K : never]: any
}
```

**效果**：通过这一行类型体操，`InjectKey` 只会包含真正通过 `Service` 注册的服务名，排除了 `Context` 自带的普通方法（如 `on`, `emit`, `plugin` 等）。

---

## 5. 泛型型变标注 (`out` 协变关键字 - TS 4.7+)

### 5.1 语法展示
在 `packages/core/src/service.ts` 中：

```typescript
export abstract class Service<out T = never> {
  declare [symbols.config]: T
  // ...
}
```

### 5.2 知识点解析
- `out T`：声明泛型参数 `T` 处于**协变（Covariant）**位置（即作为输出/只读返回值使用）。
- 对应地，`in T` 表示**逆变（Contravariant）**（作为输入参数使用）。
- **作用**：让 TypeScript 编译器在进行深层泛型比较时跳过昂贵且不必要的双向结构性比对，显著**提升编译速度与类型推导性能**。

---

## 6. 函数重载与 `NoInfer<T>` 阻断推断 (TS 5.4+)

### 6.1 问题背景
当一个函数接受多个参数，并且这些参数都涉及到同一个泛型 `K` 时，TypeScript 会尝试从**所有**实参中推断 `K`。如果某个参数传递了错误的上下文，可能导致推导出的 `K` 变成意料之外的联合类型，从而导致类型报错不准确。

### 6.2 实战应用

在 `packages/core/src/events.ts` 中：

```typescript
export interface Context {
  // 重载 1：普通事件派发
  parallel<K extends keyof Events>(name: K, ...args: Parameters<Events[K]>): Promise<void>

  // 重载 2：显式指定 thisArg
  parallel<K extends keyof Events>(
    thisArg: NoInfer<ThisType<Events[K]>>, 
    name: K, 
    ...args: Parameters<Events[K]>
  ): Promise<void>
}
```

- `NoInfer<T>`：指示 TypeScript 编译器**不要**从该参数中逆向推导泛型 `K` 的类型，必须且仅从 `name: K` 推导事件名。

---

## 7. 多态 `this` 类型与上下文绑定 (Polymorphic `this`)

### 7.1 知识点 1：返回 `this` 实现流畅链式调用（Fluent API）

```typescript
export class Context {
  extend(meta = {}): this {
    // ...
    return self
  }

  isolate(name: string, label?: symbol): this {
    // ...
    return this.extend(...)
  }
}
```
当 `CustomContext` 继承自 `Context` 时，`ctx.extend()` 返回的类型会自动收窄为 `CustomContext`，保持子类类型不丢失。

### 7.2 知识点 2：显式 `this` 参数标注

```typescript
// 声明回调函数在执行时内部 this 的类型
this.on('internal/listener', function (this: Context, name, listener, options) {
  // 在此处使用 this.fiber 或 this.root 时享有完整的 Context 类型提示
  const hooks = this.fiber._hooks[name] ??= new DisposableList()
})
```

---

## 8. 多态插件模式与类型收窄 (Discriminated Unions & Type Narrowing)

### 8.1 语法展示
Cordis 的插件可以是一个**纯函数**、一个**类（Class）**或一个**包含 `apply` 方法的对象**：

```typescript
export type Plugin<T = any> =
  | Plugin.Function<T>
  | Plugin.Constructor<T>
  | Plugin.Object<T>

export namespace Plugin {
  export interface Function<T = any> extends Base<T> {
    (ctx: Context, config: T): any
  }

  export interface Constructor<T = any> extends Base<T> {
    new (ctx: Context, config: T): any
  }

  export interface Object<T = any> extends Base<T> {
    apply(ctx: Context, config: T): any
  }
}
```

### 8.2 提取插件配置参数的条件推导

```typescript
type GetPluginParameters<P> =
  | P extends (ctx: Context, ...args: infer R) => any ? R
  : P extends new (ctx: Context, ...args: infer R) => any ? R
  : P extends { apply(ctx: Context, ...args: infer R): any } ? R
  : never

// 最终提取出配置项类型
type GetPluginConfig<P> = GetPluginParameters<P>[0]
```
当你调用 `app.plugin(DatabaseService, config)` 时，TypeScript 自动识别 `DatabaseService` 是 `Constructor` 插件，并将构造函数的第二个参数 `DatabaseConfig` 约束为 `app.plugin` 的传参类型。

---

## 9. Stage 3 原生装饰器类型系统 (Decorators & Context API)

Cordis 4.x 全面拥抱 TC39 Stage 3 原生 Decorators（TypeScript 5.0+ 原生支持，无需 `experimentalDecorators`）：

```typescript
export function Inject<K extends InjectKey>(name: K, config?: any) {
  return function (
    value: any, 
    decorator: ClassDecoratorContext<any> | ClassMethodDecoratorContext<any>
  ) {
    if (decorator.kind === 'class') {
      // 类装饰器逻辑
    } else if (decorator.kind === 'method') {
      // 方法装饰器逻辑：利用 addInitializer 在实例化时自动注入
      decorator.addInitializer(function () {
        // ...
      })
    }
  }
}
```

- `ClassDecoratorContext` / `ClassMethodDecoratorContext`：TS 5.0 提供的强类型上下文对象，支持感知被装饰的类名、字段名及初始化钩子。

---

## 10. 高级异步与工具类型

### 10.1 `PromiseLike<T>` 与 Thenable 对象
在 `Context.plugin()` 的返回类型中：

```typescript
declare module './context' {
  export interface Context {
    plugin<P extends Plugin>(plugin: P, ...args: any[]): Fiber & PromiseLike<Fiber>
  }
}
```
- `Fiber & PromiseLike<Fiber>`：使得 `plugin()` 的返回值既可以作为同步的 `Fiber` 句柄操作（如 `fiber.dispose()`），又支持直接被 `await` 等待插件初始化完成（`await app.plugin(...)`）。

### 10.2 `Promise.withResolvers` (TS 5.4+)
在定时器与异步任务调度中：

```typescript
const { promise, resolve, reject } = Promise.withResolvers<void>()
```
直接解构获得受控的 `Promise` 及其 `resolve` / `reject` 句柄，代码比传统 `new Promise((res, rej) => ...)` 更加清晰扁平。

---

## 💡 总结与学习建议

| 语法特性 | 在 Cordis 中的核心用途 | 掌握难度 |
| :--- | :--- | :---: |
| **Module Augmentation** | 实现插件/服务的全局类型合并与声明 | ⭐️⭐️ |
| **`unique symbol`** | 保护内核生命周期钩子与私有通道不冲突 | ⭐️⭐️ |
| **`infer` 与条件类型** | 动态解构插件传参、自动推导事件签名 | ⭐️⭐️⭐️ |
| **Key Remapping (`as`)** | 在类型层面动态过滤与提取服务 Key | ⭐️⭐️⭐️ |
| **`NoInfer<T>`** | 避免函数重载中的反向类型推导污染 | ⭐️⭐️⭐️ |
| **`out T` 协变标注** | 优化大型工程中的类型推导性能 | ⭐️⭐️⭐️⭐️ |

掌握了以上 10 大 TypeScript 特性，不仅能随心所欲地开发出类型完备的 Cordis 插件与服务，也能在设计其他大型 TypeScript 库时游刃有余！
