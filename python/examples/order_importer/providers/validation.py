from dataclasses import dataclass

from cordis import Context, plugin

from ..definitions import VALIDATOR, Order, ValidateOrderRequest
from ..money import format_cents, parse_cents


@dataclass(frozen=True)
class ValidationConfig:
    max_amount_cents: int


class AmountValidator:
    def __init__(self, config: ValidationConfig) -> None:
        self.config = config

    def validate(self, request: ValidateOrderRequest) -> Order:
        order_id, customer = request.order_id.strip(), request.customer.strip()
        if not order_id or not customer:
            raise ValueError("订单号和客户不能为空")
        cents = parse_cents(request.amount)
        if cents > self.config.max_amount_cents:
            raise ValueError(
                f"金额超过上限 {format_cents(self.config.max_amount_cents)}"
            )
        return Order(order_id, customer, cents)


@plugin(provide=[VALIDATOR])
def validation(ctx: Context, config: ValidationConfig) -> None:
    ctx.provide(VALIDATOR, AmountValidator(config))
