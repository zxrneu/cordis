from cordis import Context


class BaseLogger:
    def info(self, msg: str) -> None:
        print(f"INFO: {msg}")


class TaggedLogger:
    def __init__(self, base: BaseLogger, tag: str) -> None:
        self.base = base
        self.tag = tag

    def info(self, msg: str) -> None:
        self.base.info(f"[{self.tag}] {msg}")


def adapt_logger(service: BaseLogger, metadata: object, _ctx: Context) -> object:
    if isinstance(metadata, dict) and "tag" in metadata:
        return TaggedLogger(service, metadata["tag"])
    return service


ctx = Context()

# 1. 注册服务，并通过 adapt 钩子提供元数据适配器 (也可在类中实现 __cordis_intercept__)
ctx.provide("logger", BaseLogger(), adapt=adapt_logger)

# 2. 默认上下文访问：直接调用基础服务
ctx.logger.info("系统初始化完成")  # 输出: INFO: 系统初始化完成

# 3. 使用 ctx.intercept() 派生附带特定元数据（如日志标签）的上下文
user_ctx = ctx.intercept("logger", {"tag": "USER"})
order_ctx = ctx.intercept("logger", {"tag": "ORDER"})

# 4. 派生上下文访问同一服务时，自动按元数据适配出定制视图
user_ctx.logger.info("用户登录成功")  # 输出: INFO: [USER] 用户登录成功
order_ctx.logger.info("订单创建完成")  # 输出: INFO: [ORDER] 订单创建完成
