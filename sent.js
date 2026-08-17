const ids = new Set()

export function record(id) {
  if (!id) return
  ids.add(id)
  if (ids.size > 5000) {
    const first = ids.values().next().value
    ids.delete(first)
  }
}

export function wasSent(id) {
  return Boolean(id) && ids.has(id)
}