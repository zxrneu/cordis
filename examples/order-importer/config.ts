import path from 'node:path'
import { parseCents } from './money'

export interface AppConfig {
  database:
    | { provider: 'sqlite'; path: string }
    | { provider: 'memory' }
  validation: {
    maxAmount: string
  }
  importer: {
    directory: string
    interval: number
  }
  logging: {
    enabled: boolean
  }
}

/** 轻量级 TOML / JSON 配置解析器 (零外部依赖) */
export function parseConfig(content: string, basePath: string): AppConfig {
  const trimmed = content.trim()
  let raw: Record<string, Record<string, any>> = {}

  if (trimmed.startsWith('{')) {
    raw = JSON.parse(trimmed)
  } else {
    // 简易行式 TOML 节点解析
    let currentSection = ''
    for (let line of trimmed.split(/\r?\n/)) {
      line = line.trim()
      if (!line || line.startsWith('#') || line.startsWith(';')) continue

      const sectionMatch = line.match(/^\[([a-zA-Z0-9_-]+)\]$/)
      if (sectionMatch) {
        currentSection = sectionMatch[1]
        raw[currentSection] ??= {}
        continue
      }

      const kvMatch = line.match(/^([a-zA-Z0-9_-]+)\s*=\s*(.+)$/)
      if (kvMatch && currentSection) {
        const key = kvMatch[1]
        let valStr = kvMatch[2].trim()
        const hashIdx = valStr.indexOf('#')
        if (hashIdx > 0) valStr = valStr.substring(0, hashIdx).trim()

        let value: any = valStr
        if (valStr.startsWith('"') && valStr.endsWith('"')) {
          value = valStr.slice(1, -1)
        } else if (valStr === 'true') {
          value = true
        } else if (valStr === 'false') {
          value = false
        } else if (!isNaN(Number(valStr))) {
          value = Number(valStr)
        }
        raw[currentSection][key] = value
      }
    }
  }

  const dbRaw = raw.database || {}
  const valRaw = raw.validation || {}
  const impRaw = raw.importer || {}
  const logRaw = raw.logging || {}

  const provider = dbRaw.provider || 'sqlite'
  let databaseConfig: AppConfig['database']
  if (provider === 'sqlite') {
    const rawPath = String(dbRaw.path || 'var/orders.sqlite3')
    databaseConfig = {
      provider: 'sqlite',
      path: path.isAbsolute(rawPath) ? rawPath : path.resolve(basePath, rawPath),
    }
  } else if (provider === 'memory') {
    databaseConfig = { provider: 'memory' }
  } else {
    throw new Error(`database.provider 必须是 sqlite 或 memory`)
  }

  const maxAmount = String(valRaw.max_amount || '100.00')
  parseCents(maxAmount) // 验证格式合法性

  const interval = Number(impRaw.interval ?? 0.5)
  if (isNaN(interval) || interval < 0.05) {
    throw new Error('importer.interval 必须是至少 0.05 秒的有限数字')
  }

  const rawDir = String(impRaw.directory || 'incoming')
  const directory = path.isAbsolute(rawDir) ? rawDir : path.resolve(basePath, rawDir)

  return {
    database: databaseConfig,
    validation: { maxAmount },
    importer: { directory, interval },
    logging: { enabled: logRaw.enabled ?? true },
  }
}
