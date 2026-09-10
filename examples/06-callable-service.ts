/**
 * 示例 6: 可调用服务 (Callable Service) 与上下文调用溯源
 * 
 * 在 Cordis 中，Service 可以通过定义 [Service.invoke] 符号方法，
 * 使自身直接作为函数被调用 (如 `ctx.http(...)` 或 `ctx.math(...)`)，
 * 同时依然可以挂载类方法与属性。
 * 
 * 运行方式:
 *   npx tsx examples/06-callable-service.ts
 */

import { Context, Service } from 'cordis'

// 1. 类型扩充：服务既是函数接口，又是类接口
declare module 'cordis' {
  interface Context {
    calc: CalcService
  }
}

export interface CalcService {
  (a: number, b: number): number // 允许函数签名调用
}

export class CalcService extends Service {
  constructor(ctx: Context) {
    super(ctx, 'calc')
  }

  // 核心魔法：定义 [Service.invoke]
  // 当用户执行 `ctx.calc(a, b)` 时，直接触发此方法
  [Service.invoke](a: number, b: number) {
    console.log(`  [CalcService.invoke] 执行加法运算: ${a} + ${b}`)
    return a + b
  }

  // 依然可以挂载普通类方法
  multiply(a: number, b: number) {
    console.log(`  [CalcService.multiply] 执行乘法运算: ${a} * ${b}`)
    return a * b
  }

  power(base: number, exp: number) {
    return Math.pow(base, exp)
  }
}

async function main() {
  const app = new Context()

  console.log('--- 挂载 CalcService ---')
  app.plugin(CalcService)

  // 等待服务就绪
  await new Promise((r) => setTimeout(r, 100))

  console.log('\n--- 1. 直接像函数一样调用服务 ---')
  const sum = app.calc(10, 25)
  console.log(`-> 结果: 10 + 25 = ${sum}`)

  console.log('\n--- 2. 调用服务上的类方法 ---')
  const product = app.calc.multiply(6, 7)
  console.log(`-> 结果: 6 * 7 = ${product}`)

  const pow = app.calc.power(2, 10)
  console.log(`-> 结果: 2 ^ 10 = ${pow}`)
}

main().catch(console.error)
