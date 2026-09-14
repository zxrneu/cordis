/**
 * 货币处理工具函数
 * 使用美分整数存储，避免浮点数精度失真
 */

/** 将带小数的金额字符串解析为美分整数 (例如 "25.00" -> 2500) */
export function parseCents(value: string): number {
  const trimmed = value.trim()
  if (!/^[0-9]+(?:\.[0-9]{1,2})?$/.test(trimmed)) {
    throw new Error('金额必须是最多两位小数的正数')
  }
  const [whole, fraction = ''] = trimmed.split('.')
  const cents = parseInt(whole, 10) * 100 + parseInt(fraction.padEnd(2, '0'), 10)
  if (cents <= 0 || !Number.isSafeInteger(cents)) {
    throw new Error('金额必须大于零且在安全整数范围内')
  }
  return cents
}

/** 将美分整数格式化为标准货币字符串 (例如 2500 -> "25.00") */
export function formatCents(cents: number): string {
  const whole = Math.floor(cents / 100)
  const fraction = String(cents % 100).padStart(2, '0')
  return `${whole}.${fraction}`
}
