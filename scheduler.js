import { mkdir, readdir, readFile, unlink, stat } from 'node:fs/promises'
import { join, dirname, basename } from 'node:path'
import { fileURLToPath } from 'node:url'
import { ask, textPart } from './brain.js'
import { mimeFor } from './mime.js'
import { record as recordSent } from './sent.js'

async function sendMsg(sock, jid, content, opts) {
  const res = await sock.sendMessage(jid, content, opts)
  if (Array.isArray(res)) {
    for (const r of res) recordSent(r?.key?.id)
  } else if (res?.key?.id) {
    recordSent(res.key.id)
  }
  return res
}

const __dirname = dirname(fileURLToPath(import.meta.url))
const QUEUE_DIR = join(__dirname, 'task-queue')
const activity = new Map()

// Task files are unlinked only after their job runs, and a burst of 10 messages
// takes ~13s (paced at ~1.2s each) while the queue is scanned every 10s. Without
// this guard a second scan can pick up the still-lying file and fire the burst
// again — a 10-message task silently becoming 20+.
const inFlight = new Set()

// Repeated/burst sends, for when the user asks to be spammed. Paced on purpose:
// Baileys is an unofficial client, and back-to-back identical sends are the
// pattern that gets a number flagged and banned — which takes the whole bot
// down. Spacing keeps the burst looking like fast typing rather than a flood.
const MAX_BURST = Number(process.env.MAX_BURST || 50)
const MIN_BURST_GAP_MS = Number(process.env.MIN_BURST_GAP_MS || 1200)

function burstMessages(task) {
  if (Array.isArray(task.texts) && task.texts.length) {
    return task.texts.slice(0, MAX_BURST).map((t) => String(t))
  }
  const repeat = Math.min(Math.max(1, Number(task.repeat) || 1), MAX_BURST)
  return Array.from({ length: repeat }, () => String(task.text ?? 'Done.'))
}

async function sendBurst(sock, jid, messages, everySeconds) {
  const gap = Math.max(MIN_BURST_GAP_MS, (Number(everySeconds) || 0) * 1000)
  for (let i = 0; i < messages.length; i++) {
    if (i > 0) await new Promise((r) => setTimeout(r, gap + Math.round(Math.random() * 400)))
    try {
      await sendMsg(sock, jid, { text: messages[i] }, {})
    } catch (err) {
      log('burst send failed at', i + 1, 'of', messages.length, ':', err.message)
      break
    }
  }
  log('burst sent', messages.length, 'message(s) ->', jid)
}

let deliverFn = null
let targetResolver = null
let burstGate = null

export function setDeliver(fn) {
  deliverFn = fn
}

export function setTargetResolver(fn) {
  targetResolver = fn
}

// Decides whether a chat may receive a repeated burst rather than one message.
export function setBurstGate(fn) {
  burstGate = fn
}

export function noteUserMessage(jid) {
  activity.set(jid, Date.now())
}

export function noteReply() {}

export async function startScheduler(sock) {
  await mkdir(QUEUE_DIR, { recursive: true })
  const tick = async () => {
    await runDueTasks(sock)
  }
  setInterval(tick, 10000)
  tick()
}

export function log(...args) {
  console.log(new Date().toISOString(), '[scheduler]', ...args)
}

function dueAt(task) {
  if (typeof task.at === 'string' && task.at.startsWith('+')) {
    const secs = Number(task.at.slice(1))
    return Number.isFinite(secs) ? Date.now() + secs * 1000 : null
  }
  const t = Date.parse(task.at)
  return Number.isNaN(t) ? null : t
}

async function resolveTarget(task, fp) {
  if (task.targetJid && task.targetJid.includes('@')) return task.targetJid
  if (task.target && targetResolver) {
    const digits = String(task.target).replace(/[^0-9]/g, '')
    const lid = targetResolver(digits)
    if (lid) return lid
  }
  try {
    const st = await stat(fp)
    const mtime = st.mtimeMs
    let best = null
    let bestDiff = Infinity
    for (const [jid, ts] of activity) {
      const diff = Math.abs(ts - mtime)
      if (diff < bestDiff) {
        bestDiff = diff
        best = jid
      }
    }
    return best && bestDiff < 60_000 ? best : null
  } catch {
    return null
  }
}

async function execute(sock, jid, task) {
  if (task.action === 'brain') {
    const out = await ask(jid, [textPart(task.text || '(your scheduled task)')])
    if (deliverFn) await deliverFn(jid, out)
    else await sendMsg(sock, jid, { text: out.text || 'Done.' }, {})
    noteReply(jid)
  } else if (task.file) {
    try {
      const st = await stat(task.file)
      if (st.size > 60 * 1024 * 1024) throw new Error('file too large')
      const buf = await readFile(task.file)
      const fileName = basename(task.file)
      const mt = mimeFor(fileName)
      const content = mt.startsWith('image/')
        ? { image: buf, caption: task.text || fileName }
        : {
            document: buf,
            fileName,
            mimetype: mt,
            ...(task.text ? { caption: task.text } : {}),
          }
      let sent = false
      for (let i = 1; i <= 3 && !sent; i++) {
        try {
          await sendMsg(sock, jid, content)
          sent = true
        } catch (err) {
          log('file send attempt', i, 'failed:', err.message)
          await new Promise((r) => setTimeout(r, i * 2000))
        }
      }
      if (!sent) throw new Error('send failed after 3 attempts')
      log('file sent:', fileName)
    } catch (err) {
      log('file send failed:', err.message)
      await sendMsg(sock, jid, { text: `(Could not send ${task.file}: ${err.message})` }, {})
    }
    noteReply(jid)
  } else {
    const messages = burstMessages(task)
    if (messages.length > 1) {
      // A burst reaches a real person repeatedly, so it is limited to chats the
      // owner already has a relationship with: their own chats, approved groups,
      // and known contacts. Anything else still gets the message, just once.
      if (burstGate && !burstGate(jid)) {
        log('burst not allowed for', jid, '- sending a single message instead')
        await sendMsg(sock, jid, { text: messages[0] }, {})
      } else {
        await sendBurst(sock, jid, messages, task.everySeconds)
      }
    } else {
      await sendMsg(sock, jid, { text: messages[0] }, {})
    }
    noteReply(jid)
  }
}

async function runDueTasks(sock) {
  let files
  try {
    files = await readdir(QUEUE_DIR)
  } catch (err) {
    log('queue read failed:', err.message)
    return
  }
  const now = Date.now()
  for (const f of files) {
    if (!f.startsWith('task-') || !f.endsWith('.json')) continue
    const fp = join(QUEUE_DIR, f)
    if (inFlight.has(fp)) continue
    try {
      const task = JSON.parse(await readFile(fp, 'utf8'))
      const at = dueAt(task)
      if (at === null) throw new Error('bad "at": ' + task.at)
      if (at > now + 5000) continue
      const jid = await resolveTarget(task, fp)
      if (!jid) throw new Error('cannot resolve target chat')
      inFlight.add(fp)
      try {
        await execute(sock, jid, task)
        log('ran task', task.id || f, '->', jid, task.action)
        await unlink(fp).catch(() => {})
      } finally {
        inFlight.delete(fp)
      }
    } catch (err) {
      log('task failed:', f, err.message)
      await unlink(fp).catch(() => {})
    }
  }
}