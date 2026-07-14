export const API_ROOT = '/api/admin/v1'

let csrfToken: string | undefined

export class ApiError extends Error {
  status: number
  code?: string
  details?: unknown

  constructor(message: string, status: number, code?: string, details?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
  }
}

function csrfFromCookie(): string | undefined {
  if (typeof document === 'undefined') return undefined
  for (const name of ['vpn_admin_csrf', 'csrf_token', 'XSRF-TOKEN', 'csrftoken']) {
    const item = document.cookie
      .split('; ')
      .find((value) => value.startsWith(`${name}=`))
    if (item) return decodeURIComponent(item.slice(name.length + 1))
  }
  return undefined
}

function isUnsafe(method: string) {
  return !['GET', 'HEAD', 'OPTIONS'].includes(method.toUpperCase())
}

function errorMessage(body: unknown, status: number): { message: string; code?: string; details?: unknown } {
  if (typeof body === 'string' && body.trim()) return { message: body }
  if (body && typeof body === 'object') {
    const record = body as Record<string, unknown>
    const nested = record.error && typeof record.error === 'object'
      ? record.error as Record<string, unknown>
      : record
    const message = nested.message ?? nested.detail ?? record.detail
    return {
      message: typeof message === 'string' ? message : `Запрос завершился с ошибкой (${status})`,
      code: typeof nested.code === 'string' ? nested.code : undefined,
      details: nested.details ?? record.errors,
    }
  }
  return { message: `Запрос завершился с ошибкой (${status})` }
}

function unwrap<T>(body: unknown): T {
  if (body && typeof body === 'object' && 'data' in body) {
    return (body as { data: T }).data
  }
  return body as T
}

export interface RequestOptions extends Omit<RequestInit, 'body'> {
  body?: unknown
  idempotencyKey?: string
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? 'GET'
  const headers = new Headers(options.headers)
  headers.set('Accept', 'application/json')
  if (options.body !== undefined) headers.set('Content-Type', 'application/json')
  const token = csrfToken ?? csrfFromCookie()
  if (isUnsafe(method) && token) headers.set('X-CSRF-Token', token)
  if (options.idempotencyKey) headers.set('Idempotency-Key', options.idempotencyKey)

  const response = await fetch(`${API_ROOT}${path}`, {
    ...options,
    method,
    headers,
    credentials: 'include',
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  })

  const responseCsrf = response.headers.get('X-CSRF-Token')
  if (responseCsrf) csrfToken = responseCsrf

  const contentType = response.headers.get('content-type') ?? ''
  const body = response.status === 204
    ? undefined
    : contentType.includes('json')
      ? await response.json().catch(() => undefined)
      : await response.text().catch(() => undefined)

  if (!response.ok) {
    const failure = errorMessage(body, response.status)
    if (response.status === 401 && typeof window !== 'undefined') {
      window.dispatchEvent(new CustomEvent('admin:unauthorized'))
    }
    throw new ApiError(failure.message, response.status, failure.code, failure.details)
  }

  const value = unwrap<T>(body)
  if (value && typeof value === 'object' && 'csrf_token' in value) {
    const nextToken = (value as { csrf_token?: unknown }).csrf_token
    if (typeof nextToken === 'string') csrfToken = nextToken
  }
  return value
}

export function queryString(params: Record<string, string | number | boolean | null | undefined>) {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') search.set(key, String(value))
  })
  const encoded = search.toString()
  return encoded ? `?${encoded}` : ''
}

export function normalizePage<T>(payload: unknown, fallbackLimit = 25, fallbackOffset = 0): import('./types').Page<T> {
  if (Array.isArray(payload)) {
    return { items: payload as T[], total: payload.length, limit: fallbackLimit, offset: fallbackOffset }
  }
  const record = (payload ?? {}) as Record<string, unknown>
  const items = (record.items ?? record.results ?? record.rows ?? []) as T[]
  const meta = (record.meta ?? record.pagination ?? {}) as Record<string, unknown>
  return {
    items: Array.isArray(items) ? items : [],
    total: Number(record.total ?? record.count ?? meta.total ?? (Array.isArray(items) ? items.length : 0)),
    limit: Number(record.limit ?? record.page_size ?? meta.limit ?? meta.page_size ?? fallbackLimit),
    offset: Number(record.offset ?? meta.offset ?? fallbackOffset),
    snapshot_id: record.snapshot_id === undefined || record.snapshot_id === null ? undefined : Number(record.snapshot_id),
  }
}

export function resetCsrfToken() {
  csrfToken = undefined
}
