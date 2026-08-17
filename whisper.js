import { readFile } from 'node:fs/promises'
import { basename } from 'node:path'

const GROQ_URL = 'https://api.groq.com/openai/v1/audio/transcriptions'
const MAX_BYTES = 24 * 1024 * 1024

export function extForMimetype(mimetype = '') {
  const map = {
    'audio/ogg': 'ogg', 'audio/opus': 'opus', 'application/ogg': 'ogg',
    'audio/mp4': 'm4a', 'audio/x-m4a': 'm4a',
    'audio/mpeg': 'mp3', 'audio/mp3': 'mp3',
    'audio/wav': 'wav', 'audio/x-wav': 'wav',
    'audio/webm': 'webm', 'video/webm': 'webm',
  }
  for (const [k, v] of Object.entries(map)) if (mimetype.startsWith(k)) return v
  return 'm4a'
}

export async function transcribe(audioPath, mimetype) {
  const key = process.env.GROQ_API_KEY
  if (!key) throw new Error('GROQ_API_KEY is not set in .env — voice notes need it')
  const buffer = await readFile(audioPath)
  if (buffer.length > MAX_BYTES) throw new Error('Audio file too large for Groq free tier')

  const form = new FormData()
  form.append('model', 'whisper-large-v3')
  const ext = extForMimetype(mimetype)
  form.append('file', new Blob([buffer], { type: mimetype || 'audio/mpeg' }), `audio.${ext}`)

  const res = await fetch(GROQ_URL, {
    method: 'POST',
    headers: { Authorization: `Bearer ${key}` },
    body: form,
  })
  if (!res.ok) {
    const body = await res.text()
    throw new Error(`Groq transcription failed (${res.status}): ${body.slice(0, 300)}`)
  }
  const json = await res.json()
  return json.text
}
