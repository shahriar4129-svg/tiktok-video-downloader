/**
 * Build-time generator for src/config/ads.js.
 *
 * The real ad configuration is gitignored. When a Git-based deployment (for
 * example Vercel's GitHub integration) checks out the repo, ads.js is absent,
 * so this script recreates it from the base64-encoded IDs supplied through
 * ADS_CLIENT_B64 and ADS_SLOT_B64. Local builds keep their existing ads.js.
 */
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const target = path.join(root, 'src', 'config', 'ads.js')

if (existsSync(target)) {
  console.log('ads.js already present — keeping the local ad config.')
  process.exit(0)
}

const isBase64 = (value) => /^[A-Za-z0-9+/=]*$/.test(value || '')
const client = (process.env.ADS_CLIENT_B64 || '').trim()
const slot = (process.env.ADS_SLOT_B64 || '').trim()

if (!isBase64(client) || !isBase64(slot)) {
  console.warn('ADS_CLIENT_B64 / ADS_SLOT_B64 missing or invalid — using placeholders.')
}

const PLACEHOLDER_CLIENT = 'Y2EtcHViLVhYWFhYWFhYWFhYWFhYWFg='
const PLACEHOLDER_SLOT = 'WU9VUl9BRF9TTE9UX0lE'
const template = path.join(root, 'src', 'config', 'ads.example.js')
let source = readFileSync(template, 'utf8')

source = source
  .replace(`decodeAdValue('${PLACEHOLDER_CLIENT}')`, `decodeAdValue('${client || PLACEHOLDER_CLIENT}')`)
  .replace(`decodeAdValue('${PLACEHOLDER_SLOT}')`, `decodeAdValue('${slot || PLACEHOLDER_SLOT}')`)

writeFileSync(target, source)
console.log('Generated src/config/ads.js from build environment variables.')
