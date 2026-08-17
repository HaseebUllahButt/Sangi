import makeWASocket, {
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  downloadMediaMessage,
  getContentType,
} from '@whiskeysockets/baileys'
import qrcode from 'qrcode-terminal'
import QRCode from 'qrcode'
import { writeFile, mkdir, readFile, stat, readdir, unlink } from 'node:fs/promises'
import { join, dirname, basename } from 'node:path'
import { appendFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { transcribe, extForMimetype } from './whisper.js'
import { mimeFor } from './mime.js'
import { record as recordSent, wasSent } from './sent.js'
import { ask, textPart, filePart, deleteSession } from './brain.js'
import {
  startScheduler,
  setDeliver,
  setTargetResolver,
  setBurstGate,
  noteUserMessage,
  noteReply,
} from './scheduler.js'

const PUBLIC_QR = '/tmp/qr/qr.png'

async function writeQrPublic(qr) {
  await mkdir(dirname(PUBLIC_QR), { recursive: true })
  await writeFile(PUBLIC_QR, await new Promise((resolve, reject) => {
    QRCode.toDataURL(qr, { width: 320, margin: 1 }, (err, url) => (err ? reject(err) : resolve(url.split(',')[1])))
  }), 'base64')
  await writeFile('/tmp/qr/raw.txt', qr, 'utf8')
}

const __dirname = dirname(fileURLToPath(import.meta.url))
const DOWNLOADS = join(__dirname, 'downloads')
const CREDS = join(__dirname, 'creds')
const MAX_TEXT = 3900
const MAX_FILE = 60 * 1024 * 1024
const MAX_DOCX_CHARS = 12000
const LID_MAP_FILE = join(CREDS, 'lid-map.json')

async function extractDocxText(file) {
  try {
    const mammoth = await import('mammoth')
    const result = await mammoth.extractRawText({ path: file })
    const text = (result.value || '').trim()
    if (!text) return null
    return text.length > MAX_DOCX_CHARS ? text.slice(0, MAX_DOCX_CHARS) + '\n…' : text
  } catch (err) {
    log('docx extraction failed:', err.message)
    return null
  }
}

const allowedNumbers = (process.env.ALLOWED_NUMBERS || '')
  .split(',')
  .map((n) => n.trim().replace(/[+\s-]/g, ''))
  .filter(Boolean)

const ALLOWED_GROUPS_FILE = join(__dirname, 'creds', 'allowed-groups.json')
const BEHAVIOR_FILE = join(__dirname, 'creds', 'behavior.json')
let allowedGroups = new Set()
let behavior = { imageCaption: false, notes: [] }

let botNum = ''
const myJids = new Set()

async function loadAllowedGroups() {
  try {
    allowedGroups = new Set(JSON.parse(await readFile(ALLOWED_GROUPS_FILE, 'utf8')))
  } catch {
    allowedGroups = new Set()
  }
}

async function saveAllowedGroups() {
  await mkdir(dirname(ALLOWED_GROUPS_FILE), { recursive: true })
  await writeFile(ALLOWED_GROUPS_FILE, JSON.stringify([...allowedGroups], null, 2))
}

async function loadBehavior() {
  try {
    const saved = JSON.parse(await readFile(BEHAVIOR_FILE, 'utf8'))
    behavior = {
      imageCaption: saved.imageCaption === true,
      notes: Array.isArray(saved.notes) ? saved.notes.filter(Boolean).slice(-12) : [],
    }
  } catch {
    behavior = { imageCaption: false, notes: [] }
  }
}

async function saveBehavior() {
  await mkdir(dirname(BEHAVIOR_FILE), { recursive: true })
  await writeFile(BEHAVIOR_FILE, JSON.stringify(behavior, null, 2) + '\n')
}

function behaviorContextPart() {
  const lines = [
    '[Persistent behavior preferences from the user — follow these on every reply.]',
    behavior.imageCaption
      ? '- Include a short filename caption when sending generated images.'
      : '- Send generated images without a caption message or filename caption.',
  ]
  for (const note of behavior.notes) lines.push(`- ${note}`)
  return textPart(lines.join('\n'))
}

function behaviorText(body) {
  return String(body || '').replace(/@\d{5,20}\b/g, '').replace(/\s+/g, ' ').trim()
}

async function handleBehaviorRequest(jid, body, quoted) {
  const clean = behaviorText(body)
  const lower = clean.toLowerCase()
  const asksNoCaption =
    /(?:no|without|dont|don't|do not|remove|stop).*caption/.test(lower) &&
    /(?:image|picture|generated|send|message)/.test(lower)
  const asksCaption =
    /(?:include|add|show|with|send).*caption/.test(lower) &&
    /(?:image|picture|generated|send|message)/.test(lower)
  const explicitBehavior =
    /(?:adjust|change|modify|update|from now on|always|never|remember|make it so)/.test(lower) &&
    /(?:behavio|prefer|reply|respond|send|image|caption|message)/.test(lower)
  if (!asksNoCaption && !asksCaption && !explicitBehavior) return false

  if (asksNoCaption) {
    behavior.imageCaption = false
    behavior.notes = behavior.notes.filter((note) => !/caption/i.test(note))
    await saveBehavior()
    await sendText(jid, 'Got it. Generated images will be sent without a caption from now on.', quoted)
    return true
  }
  if (asksCaption) {
    behavior.imageCaption = true
    await saveBehavior()
    await sendText(jid, 'Got it. Generated images can include a caption from now on.', quoted)
    return true
  }

  const note = clean.slice(0, 300)
  if (note && !behavior.notes.includes(note)) behavior.notes.push(note)
  behavior.notes = behavior.notes.slice(-12)
  await saveBehavior()
  await sendText(jid, "Got it. I'll keep that as a standing preference.", quoted)
  return true
}

const startedAt = Date.now()
const seen = new Set()
const queues = new Map()
const lidToPn = new Map()
const pnToLid = new Map()
const recentGroupSenders = new Map()
const pendingImageRequests = []
const PENDING_IMAGE_TTL_MS = 90 * 1000
const recentReferences = new Map()
const REFERENCE_TTL_MS = 10 * 60 * 1000
const TASK_QUEUE = join(__dirname, 'task-queue')

function numberDigits(value) {
  return String(value || '').replace(/[^0-9]/g, '')
}

function rememberObservedGroupSender(jid, senderPn, participant) {
  if (!jid?.endsWith('@g.us')) return
  const candidate = senderPn || lidToPn.get(String(participant || '').toLowerCase()) || ''
  const digits = numberDigits(candidate)
  if (digits) recentGroupSenders.set(jid, { pn: candidate, at: Date.now() })
}

function inferredGroupSender(jid) {
  const observed = recentGroupSenders.get(jid)
  if (!observed || Date.now() - observed.at > 15_000) return ''
  return observed.pn
}

function looksLikeImageRequest(text) {
  return /\b(generate|create|make|image|images|picture|pictures|pic|photo|portrait|art|feet|reference|ref)\b/i.test(String(text || ''))
}

function rememberPendingImageRequest(jid, senderPn, text) {
  if (!jid?.endsWith('@g.us') || !looksLikeImageRequest(text)) return
  const now = Date.now()
  for (let i = pendingImageRequests.length - 1; i >= 0; i--) {
    if (now - pendingImageRequests[i].at > PENDING_IMAGE_TTL_MS) pendingImageRequests.splice(i, 1)
  }
  const pn = numberDigits(senderPn || inferredGroupSender(jid))
  const item = { jid, pn, text: String(text).trim(), at: now }
  const existing = pendingImageRequests.findIndex((p) => p.jid === jid && (!pn || p.pn === pn))
  if (existing >= 0) pendingImageRequests.splice(existing, 1)
  pendingImageRequests.push(item)
  while (pendingImageRequests.length > 20) pendingImageRequests.shift()
  log('pending image request:', jid, pn || '(sender unresolved)')
}

function consumePendingImageRequest(senderPn) {
  const now = Date.now()
  for (let i = pendingImageRequests.length - 1; i >= 0; i--) {
    if (now - pendingImageRequests[i].at > PENDING_IMAGE_TTL_MS) pendingImageRequests.splice(i, 1)
  }
  if (pendingImageRequests.length === 0) return null
  const pn = numberDigits(senderPn)
  let index = pn
    ? pendingImageRequests.map((item) => item.pn).lastIndexOf(pn)
    : -1
  if (index < 0 && pendingImageRequests.length === 1) index = 0
  if (index < 0) return null
  return pendingImageRequests.splice(index, 1)[0]
}

function discardPendingImageRequest(senderPn) {
  const pn = numberDigits(senderPn)
  if (!pn) return
  for (let i = pendingImageRequests.length - 1; i >= 0; i--) {
    if (pendingImageRequests[i].pn === pn) pendingImageRequests.splice(i, 1)
  }
}

function referenceKey(jid, senderPn, participant) {
  const candidate = senderPn || lidToPn.get(String(participant || '').toLowerCase()) || (jid?.endsWith('@g.us') ? inferredGroupSender(jid) : '')
  return numberDigits(candidate) || `jid:${String(jid || '').toLowerCase()}`
}

function rememberReference(m, senderPn, file, replyJid) {
  if (!file) return
  recentReferences.set(referenceKey(m.key.remoteJid, senderPn, m.key.participant), {
    path: file,
    replyJid,
    at: Date.now(),
  })
}

function rememberGeneratedReference(jid, senderPn, participant, replyJid, files) {
  const workspaceRoot = `${join(__dirname, 'workspace')}/`
  const generated = (files || []).filter((file) => {
    const path = String(file?.path || '')
    return path.startsWith(workspaceRoot) && /\.(?:jpe?g|png|webp|bmp)$/i.test(path)
  })
  const file = generated.at(-1)?.path
  if (!file) return
  recentReferences.set(referenceKey(jid, senderPn, participant), {
    path: file,
    replyJid,
    at: Date.now(),
  })
  log('remembered generated image as follow-up reference:', file)
}

function getRecentReference(m, senderPn) {
  const key = referenceKey(m.key.remoteJid, senderPn, m.key.participant)
  const reference = recentReferences.get(key)
  if (!reference || Date.now() - reference.at > REFERENCE_TTL_MS) {
    recentReferences.delete(key)
    return null
  }
  return reference
}

// Every image that arrives is remembered, so recall has to be the strict side.
// This used to fire on any generation verb — "make", "create", "image", "photo" —
// which meant an unrelated picture sent minutes earlier got silently attached as
// a Flow ingredient to a brand-new request. Require the user to actually point at
// an earlier image: a bare "generate an image of a car" references nothing.
const REFERENCE_FOLLOWUP_PATTERNS = [
  /\breferences?\b/i,
  /\bref\b/i,
  /\bsame\s+(?:one|image|pic|picture|photo|subject|person|face|character|guy|girl|woman|man|lady|dude)\b/i,
  /\b(?:this|that|the|above|previous|last|earlier)\s+(?:image|pic|picture|photo)\b/i,
  /\b(?:edit|change|adjust|remix|modify|redo|regenerate|make|turn|convert)\s+(?:it|this|that|him|her|them)\b/i,
  /\b(?:use|keep|preserve|match)\s+(?:it|this|that)\b/i,
  /\bsend\s+(?:it|that)\s+again\b/i,
]

function looksLikeReferenceFollowup(text) {
  const value = String(text || '').trim()
  if (/^(where|did|has|is there|any).{0,24}(image|pic|photo|output)/i.test(value)) return false
  return REFERENCE_FOLLOWUP_PATTERNS.some((pattern) => pattern.test(value))
}

function appendRecentReference(parts, text, reference) {
  if (!reference || !looksLikeReferenceFollowup(text)) return
  const request = String(text || '').trim()
  parts.push(textPart(`[Reference-image execution rule: use the user's recent reference image as a Flow ingredient for this new image request. Exact reference file: ${reference.path}. Pass this exact path to generate_image.py with --reference. The user's current request text is: ${JSON.stringify(request)}. Treat that request as authoritative: the generated prompt must explicitly include every requested subject and action (for example, if the user asks for feet, it must ask for feet). Do not replace the request with a generic "recreate the reference" prompt, scenery, or another subject. Preserve the referenced subject/person only when the user asks for that.]`))
}

async function loadLidMap() {
  try {
    const saved = JSON.parse(await readFile(LID_MAP_FILE, 'utf8'))
    for (const [lid, pn] of Object.entries(saved)) {
      const lidKey = String(lid || '').toLowerCase()
      const pnValue = String(pn || '')
      if (!lidKey || !pnValue) continue
      lidToPn.set(lidKey, pnValue)
      const digits = pnValue.replace(/[^0-9]/g, '')
      if (digits) pnToLid.set(digits, lidKey)
    }
  } catch {}
}

async function saveLidMap() {
  await mkdir(dirname(LID_MAP_FILE), { recursive: true })
  await writeFile(LID_MAP_FILE, JSON.stringify(Object.fromEntries(lidToPn), null, 2) + '\n')
}

function rememberLidMap(lid, pn) {
  const lidKey = String(lid || '').toLowerCase()
  const pnValue = String(pn || '')
  if (lidKey) lidToPn.set(lidKey, pnValue)
  const digits = pnValue.replace(/[^0-9]/g, '')
  if (digits) pnToLid.set(digits, lid)
  saveLidMap().catch((err) => log('LID map save failed:', err.message))
}

function parseReminder(text) {
  const t = text.trim()
  const inRe = /^remind(?: me)?\s+(?:in\s+)?(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)?\b\s*(?:to\s+)?(.*)$/is
  const atRe = /^remind(?: me)?\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:to\s+)?(.*)$/is
  const units = { s: 1, sec: 1, secs: 1, second: 1, seconds: 1, m: 60, min: 60, mins: 60, minute: 60, minutes: 60, h: 3600, hr: 3600, hrs: 3600, hour: 3600, hours: 3600, d: 86400, day: 86400, days: 86400 }
  let m = inRe.exec(t)
  let due
  if (m) {
    const n = Number(m[1])
    const mult = units[(m[2] || 'm').toLowerCase()]
    if (!mult || n <= 0) return null
    due = Date.now() + n * mult * 1000
    return { due, text: m[3].trim() }
  }
  m = atRe.exec(t)
  if (m) {
    let h = Number(m[1])
    const min = Number(m[2] || 0)
    const ap = (m[3] || '').toLowerCase()
    if (h > 23 || min > 59) return null
    if (ap === 'pm' && h < 12) h += 12
    if (ap === 'am' && h === 12) h = 0
    const d = new Date()
    d.setHours(h, min, 0, 0)
    if (d.getTime() <= Date.now()) d.setDate(d.getDate() + 1)
    return { due: d.getTime(), text: m[4].trim() }
  }
  return null
}

async function scheduleReminder(jid, due, text) {
  await mkdir(TASK_QUEUE, { recursive: true })
  const file = join(TASK_QUEUE, `task-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.json`)
  await writeFile(file, JSON.stringify({
    id: `remind-${Date.now()}`,
    at: new Date(due).toISOString(),
    action: 'send',
    text: text || 'Your reminder.',
    targetJid: jid,
  }))
  return new Date(due)
}

// Repeated bursts land on a real person over and over, so they stay inside the
// owner's existing circle: their own chats, groups they approved, and numbers
// already known to the bot. Everyone else can still be messaged — once.
function canBurst(jid) {
  const target = String(jid || '').toLowerCase()
  if (!target) return false
  if (target.endsWith('@g.us')) return allowedGroups.has(target)
  const digits = target.split('@')[0].split(':')[0].replace(/[^0-9]/g, '')
  if (!digits) return false
  if (allowedNumbers.some((a) => digits.endsWith(a) || a.endsWith(digits))) return true
  if (lidToPn.has(target)) return true
  for (const pn of pnToLid.keys()) {
    if (pn.endsWith(digits) || digits.endsWith(pn)) return true
  }
  return false
}

function isAllowed(jid, senderPn) {
  if (jid.endsWith('@broadcast')) return false
  if (jid.endsWith('@g.us')) return allowedGroups.has(jid)
  if (allowedNumbers.length === 0) return true
  const nums = [jid.split('@')[0]]
  if (senderPn) nums.push(String(senderPn).replace(/[^0-9]/g, ''))
  return nums.some((num) => allowedNumbers.some((a) => num.endsWith(a) || a.endsWith(num)))
}

function isApprovedNumber(num) {
  if (allowedNumbers.length === 0) return false
  const digits = String(num || '').replace(/[^0-9]/g, '')
  if (!digits) return false
  return allowedNumbers.some((a) => digits.endsWith(a) || a.endsWith(digits))
}

function isTrustedCommandSender(jid, senderPn, message) {
  const participant = message?.key?.participant || ''
  const candidate = senderPn || (jid.endsWith('@g.us') ? participant : jid.split('@')[0])
  const resolved = lidToPn.get(String(candidate).toLowerCase()) || candidate
  return isApprovedNumber(resolved)
}

function firstTextOf(m) {
  const msg = m.message || {}
  return msg.conversation || msg.extendedTextMessage?.text || msg.imageMessage?.caption || msg.videoMessage?.caption || msg.documentMessage?.caption || ''
}

function contextInfoOf(m) {
  const msg = m.message || {}
  return msg.extendedTextMessage?.contextInfo || msg.imageMessage?.contextInfo || msg.videoMessage?.contextInfo || msg.documentMessage?.contextInfo || {}
}

function hasPendingImageForGroup(m) {
  if (!m.key.remoteJid?.endsWith('@g.us') || !m.message?.imageMessage) return false
  const sender = numberDigits(
    m.key.senderPn
      || lidToPn.get(String(m.key.participant || '').toLowerCase())
      || inferredGroupSender(m.key.remoteJid),
  )
  const matches = pendingImageRequests.filter((item) => item.jid === m.key.remoteJid)
  if (matches.length === 0) return false
  if (sender) return matches.some((item) => item.pn === sender)
  return matches.length === 1
}

function isMyJid(j) {
  const n = String(j || '').toLowerCase()
  return myJids.has(n) || n.startsWith(botNum)
}

function myDigits() {
  const set = new Set()
  if (botNum) set.add(botNum)
  for (const j of myJids) {
    const d = j.split('@')[0].split(':')[0]
    if (d) set.add(d)
  }
  return set
}

function textMentionsMe(m) {
  const digits = myDigits()
  if (digits.size === 0) return false
  const text = firstTextOf(m)
  const ats = [...String(text).matchAll(/@(\d+)\b/g)].map((x) => x[1])
  return ats.some((d) => digits.has(d) || digits.has(d.split(':')[0]))
}

function shouldHandleGroupMsg(m) {
  if (!m.key.remoteJid.endsWith('@g.us')) return true
  if (firstTextOf(m).trim().startsWith('!')) return true
  if (hasPendingImageForGroup(m)) return true
  const ci = contextInfoOf(m)
  const mentionsMe = (ci.mentionedJid || []).some((j) => isMyJid(j)) || textMentionsMe(m)
  const replyToBot = wasSent(ci.stanzaId) || (!!ci.quotedMessage && isMyJid(ci.participant || ci.quotedParticipant))
  log('groupgate', m.key.remoteJid, 'mentionedJid=', JSON.stringify(ci.mentionedJid), 'participant=', ci.participant, 'quoted=', ci.quotedParticipant, 'myJids=', [...myJids], 'botNum=', botNum, '→', mentionsMe || replyToBot)
  return mentionsMe || replyToBot
}

function enqueue(chatId, fn) {
  const prev = queues.get(chatId) || Promise.resolve()
  const next = prev
    .then(fn)
    .catch((err) => log('error in queue for ' + chatId + ':', err?.stack || err?.message))
  queues.set(chatId, next.catch(() => {}))
}

function log(...args) {
  console.log(new Date().toISOString(), ...args)
}

const LOGS_DIR = join(__dirname, 'logs')
const INCOMING_LOG = join(LOGS_DIR, 'incoming.jsonl')
const LAST_MSG_LOG = join(LOGS_DIR, 'last-message.json')

async function logIncoming(m) {
  try {
    await mkdir(LOGS_DIR, { recursive: true })
    const entry = { ts: Date.now(), jid: m.key.remoteJid, id: m.key.id, type: getContentType(m.message), message: m.message }
    await writeFile(LAST_MSG_LOG, JSON.stringify(entry, null, 2))
    await appendFile(INCOMING_LOG, JSON.stringify(entry) + '\n')
  } catch (err) {
    log('incoming log failed:', err.message)
  }
}

async function downloadMedia(m) {
  const buffer = await downloadMediaMessage(m, 'buffer', {}, { reuploadRequest: sock.updateMediaMessage })
  if (!buffer) throw new Error('Could not download media')
  return buffer
}

function mediaExtension(msg) {
  const mimetype = String(msg?.mimetype || '').toLowerCase()
  const exact = {
    'image/jpeg': 'jpg',
    'image/png': 'png',
    'image/webp': 'webp',
    'image/gif': 'gif',
    'video/mp4': 'mp4',
    'video/quicktime': 'mov',
    'video/webm': 'webm',
    'application/pdf': 'pdf',
  }
  if (exact[mimetype]) return exact[mimetype]
  if (mimetype.startsWith('image/')) return mimetype.slice('image/'.length) || 'jpg'
  if (mimetype.startsWith('video/')) return mimetype.slice('video/'.length) || 'mp4'
  return extForMimetype(mimetype)
}

async function saveMedia(m, base) {
  const buffer = await downloadMedia(m)
  await mkdir(DOWNLOADS, { recursive: true })
  const msg = m.message[getContentType(m.message)]
  const ext = mediaExtension(msg)
  const file = join(DOWNLOADS, `${base}.${ext}`)
  await writeFile(file, buffer)
  return file
}

function mimeOf(m) {
  const msg = m.message[getContentType(m.message)]
  return msg?.mimetype || 'application/octet-stream'
}

async function sendMsg(jid, content, opts) {
  const res = await sock.sendMessage(jid, content, opts)
  if (Array.isArray(res)) {
    for (const r of res) recordSent(r?.key?.id)
  } else if (res?.key?.id) {
    recordSent(res.key.id)
  }
  return res
}

async function sendText(jid, text, quoted) {
  if (!text) return
  const chunks = []
  let cur = ''
  for (const line of text.split('\n')) {
    if (cur.length + line.length + 1 > MAX_TEXT) {
      chunks.push(cur)
      cur = line
    } else {
      cur = cur ? `${cur}\n${line}` : line
    }
  }
  if (cur) chunks.push(cur)
  for (const chunk of chunks) {
    await sendMsg(jid, { text: chunk }, { quoted })
  }
}

function quotedImageMessage(m) {
  const context = m.message?.extendedTextMessage?.contextInfo
  const quoted = context?.quotedMessage
  if (!quoted?.imageMessage) return null
  return {
    key: {
      remoteJid: m.key.remoteJid,
      id: context.stanzaId,
      participant: context.participant || context.quotedParticipant,
      fromMe: isMyJid(context.participant || context.quotedParticipant),
    },
    message: { imageMessage: quoted.imageMessage },
  }
}

async function buildParts(m, pendingImageInstruction = '', senderPn = '', recentReference = null, replyJid = m.key.remoteJid) {
  const msg = m.message
  if (msg.conversation) {
    const parts = [textPart(msg.conversation)]
    appendRecentReference(parts, msg.conversation, recentReference)
    return parts
  }
  if (msg.extendedTextMessage?.text) {
    const parts = [textPart(msg.extendedTextMessage.text)]
    const quotedImage = quotedImageMessage(m)
    if (quotedImage) {
      try {
        const file = await saveMedia(quotedImage, `quoted-img-${Date.now()}`)
        parts.push(
          textPart(`[Quoted image available as the exact visual reference for Flow image generation. Reference file: ${file}. Do not try to inspect or attach this image in the chat model; if the user requests generation, pass this exact path to generate_image.py with --reference.]`),
        )
        rememberReference(m, senderPn, file, replyJid)
      } catch (err) {
        log('quoted image download failed:', err.message)
        parts.push(textPart('[The quoted image could not be downloaded.]'))
      }
    }
    if (!quotedImage) appendRecentReference(parts, msg.extendedTextMessage.text, recentReference)
    return parts
  }

  if (msg.imageMessage) {
    const file = await saveMedia(m, `img-${Date.now()}`)
    rememberReference(m, senderPn, file, replyJid)
    const caption = msg.imageMessage.caption?.trim()
    const instruction = caption || pendingImageInstruction
    const instructionText = instruction
      ? ` User instruction: ${instruction}. This requested subject/action is authoritative and must remain explicit in the generated prompt; do not replace it with a generic recreation of the reference or scenery.`
      : ' No generation instruction was attached. Do not infer one from earlier conversation; ask the user what they want generated with this reference.'
    return [
      textPart(`[Image sent by the user as the exact visual reference for Flow image generation. Reference file: ${file}. Do not try to inspect or attach this image in the chat model; if the user requests generation, pass this exact path to generate_image.py with --reference.]${instructionText}`),
    ]
  }

  if (msg.audioMessage || msg.pttMessage) {
    const file = await saveMedia(m, `audio-${Date.now()}`)
    await sock.sendPresenceUpdate('recording', m.key.remoteJid).catch(() => {})
    const text = await transcribe(file, mimeOf(m))
    return [textPart(text ? `[Voice note from the user: "${text}"]` : '[Voice note (unintelligible)]')]
  }

  if (msg.videoMessage || msg.documentMessage) {
    const file = await saveMedia(m, `file-${Date.now()}`)
    const parts = [
      textPart(`[File sent by the user: ${file}]`),
      filePart(file),
    ]
    if (file.toLowerCase().endsWith('.docx')) {
      const text = await extractDocxText(file)
      if (text) parts.push(textPart(`[Docx contents:\n${text}]`))
    }
    return parts
  }

  if (msg.locationMessage) {
    const { degreesLatitude, degreesLongitude } = msg.locationMessage
    return [textPart(`[Location: ${degreesLatitude}, ${degreesLongitude}]`)]
  }

  return null
}

async function deliver(jid, out, quoted) {
  if (!out.text && out.images.length === 0 && out.files.length === 0) {
    await sendText(jid, 'Done.', quoted)
    return
  }
  await sendText(jid, out.text, quoted)
  const items = []
  for (const url of out.images) {
    try {
      if (url.startsWith('data:')) {
        const match = /^data:([^;,]+);base64,(.+)$/.exec(url)
        const ext = (match[1].split('/')[1] || 'png').split('+')[0]
        const p = join(DOWNLOADS, `img-${Date.now()}-${Math.random().toString(36).slice(2, 7)}.${ext}`)
        await writeFile(p, Buffer.from(match[2], 'base64'))
        items.push({ path: p, name: basename(p) })
      } else {
        const filePath = url.replace(/^file:\/\//, '')
        items.push({ path: filePath, name: filePath.split('/').pop() || `image-${Date.now()}.png` })
      }
    } catch (err) {
      log('image prepare failed:', err.message)
      await sendText(jid, `(Could not prepare an image: ${err.message})`, quoted)
    }
  }
  for (const file of out.files) items.push({ path: file.path, name: file.name })
  const seen = new Set()
  for (const it of items) {
    const key = it.path
    if (seen.has(key)) continue
    seen.add(key)
    await sendQueuedMedia(jid, it.path, it.name)
  }
}

const OUTBOX = join(__dirname, 'outbox')
const MAX_IMAGE = 16 * 1024 * 1024
const OUTBOX_RETRY_MS = 60 * 1000
const OUTBOX_MAX_AGE_MS = 6 * 60 * 60 * 1000

async function readStable(path) {
  let prevSize = -1
  for (let i = 0; i < 20; i++) {
    const st = await stat(path).catch(() => null)
    if (!st) return null
    if (st.size > 0 && st.size === prevSize) break
    prevSize = st.size
    await new Promise((r) => setTimeout(r, 500))
  }
  return readFile(path)
}

async function sendQueuedMedia(jid, path, name) {
  const st = await stat(path).catch(() => null)
  // These used to return silently: the agent believed it had delivered
  // something and the user was left waiting for a file that never arrived.
  if (!st) {
    log('media missing, skipping:', path)
    await sendText(jid, `⚠️ I made ${name} but it went missing before I could send it.`).catch(() => {})
    return
  }
  if (st.size > MAX_FILE) {
    log('media too large:', name, st.size)
    await sendText(jid, `⚠️ ${name} is too big to send (${Math.round(st.size / 1024 / 1024)} MB).`).catch(() => {})
    return
  }
  await mkdir(OUTBOX, { recursive: true })
  const rec = { jid, path, name, mt: mimeFor(name), size: st.size, createdAt: Date.now() }
  const recPath = join(OUTBOX, `out-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.json`)
  await writeFile(recPath, JSON.stringify(rec))
  if (await trySend(rec)) await unlink(recPath).catch(() => {})
}

async function trySend(rec) {
  const buf = await readStable(rec.path)
  if (!buf) return false
  const isImage = rec.mt.startsWith('image/')
  let sendBuf = buf
  if (isImage) {
    try {
      const sharp = (await import('sharp')).default
      sendBuf = await sharp(buf).png().toBuffer()
    } catch (err) {
      log('image re-encode failed for', rec.name, ':', err.message)
      return false
    }
  }
  let content = isImage && sendBuf.length <= MAX_IMAGE
    ? { image: sendBuf }
    : { document: sendBuf, fileName: rec.name, mimetype: rec.mt }
  if (isImage && sendBuf.length <= MAX_IMAGE && behavior.imageCaption) content.caption = rec.name
  for (let i = 1; i <= 3; i++) {
    try {
      await sendMsg(rec.jid, content)
      log('sent', rec.name, '->', rec.jid)
      return true
    } catch (err) {
      log('send attempt', i, 'failed for', rec.name, ':', err.message)
      if (isImage && content.image && i === 1) {
        content = { document: sendBuf, fileName: rec.name, mimetype: rec.mt }
        continue
      }
      await new Promise((r) => setTimeout(r, i * 2000))
    }
  }
  return false
}

let flushing = false

async function flushOutbox() {
  if (flushing) return
  flushing = true
  try {
    let files
    try {
      files = await readdir(OUTBOX)
    } catch {
      return
    }
    for (const f of files) {
      const fp = join(OUTBOX, f)
      try {
        const rec = JSON.parse(await readFile(fp, 'utf8'))
        if (await trySend(rec)) {
          await unlink(fp).catch(() => {})
        } else if (Date.now() - (rec.createdAt || 0) > OUTBOX_MAX_AGE_MS) {
          // A file WhatsApp keeps rejecting would otherwise retry forever and
          // still never be reported. Give up loudly instead.
          await unlink(fp).catch(() => {})
          log('outbox gave up on:', rec.name)
          await sendText(rec.jid, `⚠️ Couldn't send ${rec.name} — gave up after repeated attempts.`).catch(() => {})
        } else {
          log('outbox keep for later:', f)
        }
      } catch (err) {
        log('outbox flush failed:', f, err.message)
      }
    }
  } finally {
    flushing = false
  }
}

// A media send that fails mid-session leaves its record in the outbox. That was
// only retried on the next reconnect, so an image could sit undelivered for hours.
setInterval(() => {
  flushOutbox().catch((e) => log('outbox flush error:', e.message))
}, OUTBOX_RETRY_MS).unref()

async function handleCommand(jid, cmd, quoted, senderPn) {
  const body = cmd.trim()
  if (body.startsWith('!') && !isTrustedCommandSender(jid, senderPn, quoted)) {
    log('blocked command from', jid, 'pn:', senderPn, 'command:', body)
    return true
  }
  if (body === '!status') {
    const up = Math.round((Date.now() - startedAt) / 1000 / 60)
    await sendText(jid, `Bot online for ${up} min. Brain: ${process.env.OPENCODE_URL || 'http://127.0.0.1:4096'}`, quoted)
    return true
  }
  if (body === '!reset') {
    await deleteSession(jid)
    await sendText(jid, 'Conversation memory cleared.', quoted)
    return true
  }
  if (body === '!help') {
    await sendText(
      jid,
      'Commands:\n!status — bot status\n!reset — clear my memory of our conversation\n!help — this message\n\nGroup control:\n!addgroup — approve THIS group (must be sent from an approved number inside the group)\n!remgroup — revoke access for THIS group\n!groups — list approved groups\n\n"remind me in 10 min to <thing>" or "remind me at 5pm <thing>" — I ping you later.\n\nEverything else goes to the AI agent: text, images, voice notes, files, and I reply with text, images and documents. Links in replies are clickable.',
      quoted,
    )
    return true
  }
  if (body === '!addgroup' && jid.endsWith('@g.us')) {
    allowedGroups.add(jid)
    await saveAllowedGroups()
    await sendText(jid, 'Group approved. Anyone here can use me now.', quoted)
    return true
  }
  if (body === '!remgroup' && jid.endsWith('@g.us')) {
    allowedGroups.delete(jid)
    await saveAllowedGroups()
    await sendText(jid, 'Group access revoked.', quoted)
    return true
  }
  if (body === '!groups') {
    const list = [...allowedGroups].map((g) => g.split('@')[0]).join('\n') || '(none)'
    await sendText(jid, `Approved groups:\n${list}`, quoted)
    return true
  }
  if (body.startsWith('remind ')) {
    const r = parseReminder(body)
    if (!r || !r.text) {
      await sendText(jid, 'Remind you of what? Try "remind me in 10 min to call mom" or "remind me at 5pm to water plants".', quoted)
      return true
    }
    await scheduleReminder(jid, r.due, r.text)
    const hm = new Date(r.due).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    await sendText(jid, `Set. I'll ping you at ${hm}: ${r.text}`, quoted)
    return true
  }
  return false
}

const HEARTBEAT_MSGS = [
  'Still on it.',
  'Working.',
  'One sec.',
  'Almost there.',
  'Still going.',
  'Hang tight.',
]

async function askWithFeedback(jid, parts, sessionId = jid) {
  const presence = setInterval(() => {
    sock.sendPresenceUpdate('composing', jid).catch(() => {})
  }, 10000)
  let n = 0
  const beats = setInterval(() => {
    n += 1
    if (n <= 6) {
      const msg = HEARTBEAT_MSGS[(n - 1) % HEARTBEAT_MSGS.length]
      sock.sendMessage(jid, { text: msg }).catch(() => {})
    }
  }, 120000)
  try {
    return await ask(sessionId, parts)
  } finally {
    clearInterval(presence)
    clearInterval(beats)
  }
}

async function handleMessage(m, senderPn) {
  const jid = m.key.remoteJid
  const quoted = m
  const caption = m.message?.imageMessage?.caption?.trim() || ''
  if (m.message?.imageMessage && caption) discardPendingImageRequest(senderPn)
  const pendingImage = m.message?.imageMessage && !caption
    ? consumePendingImageRequest(senderPn)
    : null
  const recentReference = getRecentReference(m, senderPn)
  const hasCurrentReference = Boolean(m.message?.imageMessage || quotedImageMessage(m))
  const isReferenceFollowup = !hasCurrentReference && recentReference && looksLikeReferenceFollowup(firstTextOf(m))
  const replyJid = pendingImage?.jid || isReferenceFollowup?.replyJid || jid
  const replyQuoted = replyJid === jid ? quoted : undefined
  const hasReferenceImage = hasCurrentReference || Boolean(isReferenceFollowup)
  const isolatedSessionId = hasReferenceImage ? `image-request:${replyJid}:${m.key.id}` : replyJid
  await sock.readMessages([m.key]).catch(() => {})
  await sock.sendPresenceUpdate('composing', replyJid).catch(() => {})

  try {
    const parts = await buildParts(m, pendingImage?.text || '', senderPn, isReferenceFollowup ? recentReference : null, replyJid)
    if (!parts) {
      await sendText(replyJid, 'I can handle text, images, voice notes and files.', replyQuoted)
      await sock.sendPresenceUpdate('available', replyJid).catch(() => {})
      return
    }
    const firstText = parts.find((p) => p.type === 'text')?.text || ''
    if (firstText && !firstText.startsWith('[') && await handleBehaviorRequest(replyJid, firstText, replyQuoted)) {
      noteReply(replyJid)
      await sock.sendPresenceUpdate('available', replyJid).catch(() => {})
      return
    }
    if (firstText && !firstText.startsWith('[')) {
      if (await handleCommand(replyJid, firstText.trim(), replyQuoted, senderPn)) {
        noteReply(replyJid)
        await sock.sendPresenceUpdate('available', replyJid).catch(() => {})
        return
      }
    }
    const out = await askWithFeedback(replyJid, [behaviorContextPart(), ...parts], isolatedSessionId)
    rememberGeneratedReference(replyJid, senderPn, m.key.participant, replyJid, out.files)
    await deliver(replyJid, out, replyQuoted)
    noteReply(replyJid)
    await sock.sendPresenceUpdate('available', replyJid).catch(() => {})
  } catch (err) {
    log('message failed:', err)
    await sock.sendPresenceUpdate('available', replyJid).catch(() => {})
    const msg = err.message || 'Something went wrong.'
    await sendText(replyJid, `⚠️ ${msg}`, replyQuoted)
  } finally {
    if (hasReferenceImage) await deleteSession(isolatedSessionId).catch(() => {})
  }
}

let sock
let reconnectTimer = null
let reconnectAttempt = 0
const RECONNECT_BASE_MS = 1000
const RECONNECT_MAX_MS = 60 * 1000

// A dropped connection used to call start() immediately, with no delay, no
// catch, and no guard. Each attempt built another socket on top of the last, so
// a DNS outage turned into ~225 reconnects a second until the network returned.
function scheduleReconnect(reason) {
  if (reconnectTimer) return
  reconnectAttempt += 1
  const capped = Math.min(RECONNECT_BASE_MS * 2 ** (reconnectAttempt - 1), RECONNECT_MAX_MS)
  const delay = Math.round(capped * (0.5 + Math.random() / 2))
  log(`reconnecting in ${(delay / 1000).toFixed(1)}s (attempt ${reconnectAttempt}, ${reason})`)
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    start().catch((err) => scheduleReconnect(`start failed: ${err.message}`))
  }, delay)
}

async function start() {
  // Drop the previous socket first; otherwise its listeners keep firing against
  // a connection we have already replaced.
  if (sock) {
    try { sock.ev.removeAllListeners() } catch {}
    try { sock.end(undefined) } catch {}
  }
  await loadAllowedGroups()
  await loadLidMap()
  await loadBehavior()
  await mkdir(CREDS, { recursive: true })
  await mkdir(DOWNLOADS, { recursive: true })
  const { state, saveCreds } = await useMultiFileAuthState(CREDS)
  const { version } = await fetchLatestBaileysVersion()

  sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false,
    markOnlineOnConnect: true,
    syncFullHistory: false,
    browser: ['Mac OS', 'Chrome', '14.4.1'],
  })

  sock.ev.on('creds.update', saveCreds)

  sock.ev.on('chats.phoneNumberShare', ({ lid, jid }) => {
    rememberLidMap(lid, jid)
    log('phone number share:', lid, '->', jid)
  })

  const pairNum = process.env.PAIR_PHONE_NUMBER
  if (pairNum && !state.creds.registered) {
    setTimeout(async () => {
      try {
        const code = await sock.requestPairingCode(pairNum)
        log('===== PAIRING CODE =====')
        log('Phone: WhatsApp > Linked Devices > Link a device > "Link with phone number instead"')
        log('Enter this 8-digit code:   ' + code.match(/.{1,2}/g).join(' '))
      } catch (e) {
        log('pairing request failed:', e.message)
      }
    }, 1500)
  }

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update
    if (qr && !process.env.PAIR_PHONE_NUMBER) {
      log('Scan QR code with WhatsApp > Linked Devices > Link a device')
      qrcode.generate(qr, { small: true, color: false })
      QRCode.toFile(join(__dirname, 'qr.png'), qr, { width: 400, margin: 1 })
        .then(() => log('Cluster QR for PC: qr.png (or see public URL below)'))
        .catch((e) => log('QR PNG failed:', e.message))
      writeQrPublic(qr)
    }
    if (connection === 'open') {
      log('Connected to WhatsApp!')
      const uid = sock.user?.id?.toLowerCase()
      botNum = uid?.split('@')[0]?.split(':')[0] || ''
      if (uid) myJids.add(uid)
      const lid = sock.user?.lid?.toLowerCase()
      if (lid) myJids.add(lid)
      reconnectAttempt = 0
      flushOutbox().catch((e) => log('outbox flush error:', e.message))
    }
    if (connection === 'close') {
      const code = lastDisconnect?.error?.output?.statusCode
      const shouldReconnect = code !== DisconnectReason.loggedOut
      log('connection closed, code:', code, 'reconnecting:', shouldReconnect)
      if (shouldReconnect) scheduleReconnect(`close code ${code}`)
      else log('logged out — not reconnecting; re-pair the device')
    }
  })

  sock.ev.on('groupParticipants.update', (updates) => {
    for (const u of updates) {
      for (const p of u.participants) {
        const j = p.toLowerCase()
        if (u.action === 'remove') myJids.delete(j)
        else if (u.action === 'add') myJids.add(j)
      }
    }
  })

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    for (const m of messages) {
      if (m.key.fromMe || type !== 'notify') continue
      if (!m.message) continue
      if (seen.has(m.key.id)) continue
      seen.add(m.key.id)
      if (seen.size > 2000) seen.clear()
      const jid = m.key.remoteJid
      const participant = m.key.participant || ''
      const senderPn = m.key.senderPn || lidToPn.get(String(participant).toLowerCase()) || lidToPn.get(String(jid).toLowerCase()) || ''
      rememberObservedGroupSender(jid, senderPn, participant)
      if (senderPn) {
        if (participant) rememberLidMap(participant, senderPn)
        if (jid.endsWith('@lid')) rememberLidMap(jid, senderPn)
      }
      if (isAllowed(jid, senderPn)) {
        if (jid.endsWith('@g.us') && !shouldHandleGroupMsg(m)) {
          log('ignored group message (no mention/reply) from', jid)
          continue
        }
      } else if (!(jid.endsWith('@g.us') && isTrustedCommandSender(jid, senderPn, m) && firstTextOf(m).trim() === '!addgroup')) {
        log('blocked message from', jid, 'pn:', senderPn)
        continue
      }
      noteUserMessage(jid)
      const text = firstTextOf(m)
      log('message from', jid, 'pn:', senderPn, 'len:', text.length, m.type, '→', text)
      rememberPendingImageRequest(jid, senderPn, text)
      logIncoming(m)
      enqueue(jid, () => handleMessage(m, senderPn))
    }
  })

  setDeliver((jid, out) => deliver(jid, out, undefined))
  setTargetResolver((digits) => {
    for (const [pn, lid] of pnToLid) {
      if (pn.endsWith(digits) || digits.endsWith(pn)) return lid
    }
    // Previously this gave up, so a task could only reach someone who had
    // already messaged the bot. A plain number is a valid WhatsApp JID.
    return digits.length >= 8 ? `${digits}@s.whatsapp.net` : null
  })
  setBurstGate(canBurst)
  await startScheduler(sock)
}

// Startup hits the network (fetchLatestBaileysVersion). Exiting on a transient
// DNS failure just handed the same tight loop to systemd, so back off here too.
start().catch((err) => {
  log('initial start failed:', err.message)
  scheduleReconnect('initial start failed')
})
