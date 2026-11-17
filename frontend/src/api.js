async function toError(res) {
  let text = await res.text()
  try {
    const body = JSON.parse(text)
    const detail = body?.detail
    if (typeof detail === 'string') return new Error(detail)
    if (detail && typeof detail === 'object') {
      const msg = detail['消息'] || detail['原因'] || JSON.stringify(detail)
      return new Error(msg)
    }
  } catch {
    /* 非 JSON 响应，原样抛出 */
  }
  return new Error(text || `请求失败（HTTP ${res.status}）`)
}

export async function getJson(url) {
  const res = await fetch(url)
  if (!res.ok) throw await toError(res)
  return res.json()
}

export async function postJson(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  })
  if (!res.ok) throw await toError(res)
  return res.json()
}
