import { readFile, mkdir, stat, readdir } from 'node:fs/promises'
import { join, dirname, basename, isAbsolute, extname } from 'node:path'
import { fileURLToPath } from 'node:url'
import http from 'node:http'
import https from 'node:https'

const __dirname = dirname(fileURLToPath(import.meta.url))
const BASE = process.env.OPENCODE_URL || 'http://127.0.0.1:4096'
const SESSION_FILE = join(__dirname, 'creds', 'wa-sessions.json')
const DATA_DIR = join(__dirname, 'downloads')
const WORKSPACE = join(__dirname, 'workspace')
const SEARCH_DIRS = [WORKSPACE, DATA_DIR, __dirname]
const OPENCODE_TIMEOUT_MS = 20 * 60 * 1000
const WORKSPACE_DELIVERABLE = /\.(?:png|jpe?g|webp|bmp|gif|pdf|docx?|xlsx?|csv|txt|zip|mp3|mp4|ogg)$/i
const MAX_WORKSPACE_OUTPUTS = 3

// AGENTS.md promises that whatever the agent leaves in workspace/ comes back over
// WhatsApp. Only generate_image.py prints an IMAGE_GENERATED marker, and the agent
// is told never to name a path in its reply, so a file it produced any other way —
// a script, ImageMagick, a download — otherwise had no delivery path at all.
async function collectWorkspaceOutputs(since, already) {
  const taken = new Set((already || []).map((file) => file.path))
  let names
  try {
    names = await readdir(WORKSPACE)
  } catch {
    return []
  }
  const fresh = []
  for (const name of names) {
    if (!WORKSPACE_DELIVERABLE.test(name)) continue
    const path = join(WORKSPACE, name)
    if (taken.has(path)) continue
    try {
      const st = await stat(path)
      if (st.isFile() && st.mtimeMs >= since) fresh.push({ path, name, at: st.mtimeMs })
    } catch {}
  }
  fresh.sort((a, b) => a.at - b.at)
  return fresh.slice(-MAX_WORKSPACE_OUTPUTS).map(({ path, name }) => ({ path, name }))
}

function postJson(url, payload, timeoutMs = OPENCODE_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    const target = new URL(url)
    const client = target.protocol === 'https:' ? https : http
    const body = JSON.stringify(payload)
    const req = client.request(target, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'content-length': Buffer.byteLength(body),
      },
    }, (res) => {
      let text = ''
      res.setEncoding('utf8')
      res.on('data', (chunk) => { text += chunk })
      res.on('end', () => {
        const status = res.statusCode || 0
        resolve({
          ok: status >= 200 && status < 300,
          status,
          text: async () => text,
          json: async () => JSON.parse(text),
        })
      })
    })
    req.setTimeout(timeoutMs, () => {
      req.destroy(new Error(`OpenCode response timeout after ${Math.round(timeoutMs / 60000)} minutes`))
    })
    req.on('error', reject)
    req.write(body)
    req.end()
  })
}

async function findMentionedFiles(text) {
  const found = []
  const seen = new Set()
  const re = /file:\/\/([^\s"'`<>|)]+)|([^\s"'`<>|)]+\.(?:pdf|png|jpe?g|webp|gif|docx?|xlsx?|csv|txt|zip|mp3|mp4|ogg|html?))\b/gi
  let m
  while ((m = re.exec(text))) {
    const raw = (m[1] || m[2]).replace(/[.,;:!?]+$/, '')
    const candidates = isAbsolute(raw) ? [raw] : SEARCH_DIRS.map((d) => join(d, raw))
    for (const p of candidates) {
      try {
        const st = await stat(p)
        if (st.isFile() && !seen.has(p)) {
          seen.add(p)
          found.push(p)
        }
      } catch {}
    }
  }
  return found.slice(0, 1)
}

async function findGeneratedFiles(text) {
  const found = []
  const seen = new Set()
  const re = /^\s*(?:IMAGE|VIDEO)_GENERATED:\s*(\S+)\s*$/gim
  let m
  while ((m = re.exec(text))) {
    const raw = m[1]
    const candidates = isAbsolute(raw)
      ? [raw]
      : [join(__dirname, raw), join(join(__dirname, 'workspace'), raw)]
    for (const p of candidates) {
      try {
        const st = await stat(p)
        if (st.isFile() && !seen.has(p)) {
          seen.add(p)
          found.push(p)
        }
      } catch {}
    }
  }
  return found.slice(0, 1)
}

let sessionMap = null
async function loadSessions() {
  if (sessionMap) return sessionMap
  try {
    sessionMap = JSON.parse(await readFile(SESSION_FILE, 'utf8'))
  } catch {
    sessionMap = {}
  }
  return sessionMap
}

async function saveSessions() {
  await mkdir(dirname(SESSION_FILE), { recursive: true })
  await writeFile(SESSION_FILE, JSON.stringify(sessionMap, null, 2))
}
import { writeFile } from 'node:fs/promises'

async function ensureSession(chatId) {
  const map = await loadSessions()
  if (map[chatId]) return map[chatId]
  const res = await fetch(`${BASE}/session`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ title: `whatsapp-${chatId}` }),
  })
  if (!res.ok) throw new Error(`Failed to create opencode session (${res.status})`)
  const { id } = await res.json()
  map[chatId] = id
  await saveSessions()
  return id
}

export async function deleteSession(chatId) {
  const map = await loadSessions()
  const sid = map[chatId]
  if (sid) {
    await fetch(`${BASE}/session/${sid}`, { method: 'DELETE' }).catch(() => {})
    delete map[chatId]
    await saveSessions()
  }
}

export function imagePart(filePath) {
  return { type: 'image', url: `file://${filePath}` }
}

const FILE_MIME_BY_EXT = {
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.png': 'image/png',
  '.webp': 'image/webp',
  '.gif': 'image/gif',
  '.pdf': 'application/pdf',
  '.doc': 'application/msword',
  '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  '.xls': 'application/vnd.ms-excel',
  '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  '.txt': 'text/plain',
  '.csv': 'text/csv',
  '.zip': 'application/zip',
  '.mp3': 'audio/mpeg',
  '.m4a': 'audio/mp4',
  '.ogg': 'audio/ogg',
  '.mp4': 'video/mp4',
  '.webm': 'video/webm',
}

export function filePart(filePath) {
  const mime = FILE_MIME_BY_EXT[extname(filePath).toLowerCase()] || 'application/octet-stream'
  return { type: 'file', url: `file://${filePath}`, mime }
}

function textPart(text) {
  return { type: 'text', text }
}

function log(...args) {
  console.log(new Date().toISOString(), '[brain]', ...args)
}

function infoError(info) {
  if (!info?.error) return ''
  return info.error.message || info.error.data?.message || String(info.error)
}

export async function ask(chatId, parts, retried = false) {
  const turnStartedAt = Date.now() - 1000
  const sid = await ensureSession(chatId)
  let res
  try {
    res = await postJson(`${BASE}/session/${sid}/message`, {
      parts,
      model: {
        providerID: 'opencode',
        modelID: 'deepseek-v4-flash-free',
        options: { reasoningEffort: 'high' },
      },
    })
  } catch (err) {
    throw new Error(`opencode request failed: ${err.message}`)
  }
  if (!res.ok) {
    const body = await res.text()
    if (!retried && (res.status === 500 || res.status === 404)) {
      await deleteSession(chatId)
      return ask(chatId, parts, true)
    }
    throw new Error(`opencode failed (${res.status}): ${body.slice(0, 300)}`)
  }
  const body = await res.json()
  const info = body.info || {}
  const outParts = body.parts || []
  log('completion', chatId, 'finish:', info.finish, 'error:', infoError(info), 'tokens:', JSON.stringify(info.tokens || {}))

  if (info.error) {
    const msg = infoError(info)
    if (!retried) {
      await deleteSession(chatId)
      return ask(chatId, parts, true)
    }
    throw new Error(`brain error: ${msg.slice(0, 300)}`)
  }

  const truncated = ['length', 'unknown', 'error', 'aborted'].includes(info.finish)
  if (truncated && !retried) {
    log('truncated completion (finish:', info.finish + '), continuing')
    const tail = await ask(chatId, [textPart('[Your previous reply was cut off mid-message. Continue from exactly where you stopped, do not repeat anything.]')], true)
    const cur = { text: '', images: [], files: [] }
    for (const p of outParts) {
      if (p.type === 'text' && p.text) {
        if (cur.text) cur.text += '\n'
        cur.text += p.text.trim()
      }
    }
    return { text: (cur.text ? cur.text + '\n' : '') + tail.text, images: tail.images, files: tail.files }
  }

  const result = { text: '', images: [], files: [] }
  for (const p of outParts) {
    if (p.type === 'text' && p.text) {
      if (result.text) result.text += '\n'
      result.text += p.text.trim()
    } else if (p.type === 'image' && p.url) {
      result.images.push(p.url)
    } else if (p.type === 'file' && p.path) {
      result.files.push({ path: p.path, name: basename(p.path) })
    }
  }
  result.images = result.images.slice(0, 1)
  const generated = await findGeneratedFiles(result.text)
  for (const path of generated) {
    result.files.push({ path, name: basename(path) })
  }
  result.text = result.text
    .replace(/^\s*(?:IMAGE|VIDEO)_GENERATED:\s*\S+\s*$/gim, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
  if (generated.length > 0) result.text = ''
  const mentioned = await findMentionedFiles(result.text)
  for (const path of mentioned) {
    if (!result.files.some((f) => f.path === path)) {
      result.files.push({ path, name: basename(path) })
    }
  }
  const produced = await collectWorkspaceOutputs(turnStartedAt, result.files)
  if (produced.length) {
    log('delivering workspace output(s):', produced.map((file) => file.name).join(', '))
    result.files.push(...produced)
  }
  return result
}

export { textPart }
