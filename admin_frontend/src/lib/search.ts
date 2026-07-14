export function withParam(current: URLSearchParams, key: string, value: string, resetOffset = false) {
  const next = new URLSearchParams(current)
  if (value) next.set(key, value)
  else next.delete(key)
  if (resetOffset) next.delete('offset')
  return next
}
