/**
 * 示例 7: 实战场景 —— 配置驱动的 CSV 订单导入与服务热替换
 * 
 * 移植自 Cordis Python 官方核心实战示例: python/examples/order_importer
 * 
 * 本示例已拆分为模块化目录结构，详见: examples/order-importer/
 * ├── definitions.ts        # 契约与领域模型定义
 * ├── money.ts              # 货币美分计算工具
 * ├── config.ts             # 配置模型与 TOML/JSON 解析器
 * ├── config.toml           # 业务配置文件
 * ├── application.ts        # 应用管理器 (负责插件热重载与生命周期编排)
 * ├── providers/            # 服务提供方
 * │   ├── validation.ts     # 金额校验服务 (AmountValidator)
 * │   ├── memory.ts         # 内存存储服务 (MemoryStore)
 * │   ├── sqlite.ts         # SQLite 存储服务 (SQLiteStore)
 * │   └── csv.ts            # CSV 导入服务 (CsvImporter)
 * ├── consumers/            # 业务消费方
 * │   ├── directory.ts      # 目录扫描监听插件 (DirectoryWatcher)
 * │   └── console.ts        # 控制台格式化日志插件 (ConsoleLogger)
 * ├── incoming/             # 待扫描 CSV 文件夹
 * │   └── orders.csv        # 测试订单 CSV 文件
 * └── index.ts              # 入口聚合与演示调度
 * 
 * 运行方式:
 *   npx tsx examples/07-csv-order-importer.ts            # 运行全自动交互演示
 *   npx tsx examples/07-csv-order-importer.ts --serve    # 常驻监听配置变动
 *   npx tsx examples/order-importer                     # 直接运行拆分后的模块目录
 */

export * from './order-importer/index'
import './order-importer/index'
