import re


def parse_cents(value: str) -> int:
    """Parse positive decimal money without floating-point rounding."""
    value = value.strip()
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", value):
        raise ValueError("金额必须是最多两位小数的正数")
    whole, _, fraction = value.partition(".")
    cents = int(whole) * 100 + int(fraction.ljust(2, "0"))
    if not 0 < cents <= 2**63 - 1:
        raise ValueError("金额必须大于零且在 64 位有符号整数范围内")
    return cents


def format_cents(value: int) -> str:
    return f"{value // 100}.{value % 100:02d}"
