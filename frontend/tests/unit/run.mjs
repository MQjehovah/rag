// Minimal offline runner for pure-function TS unit tests.
// Bundles with the repo-local esbuild (no typecheck) then runs node:test on the emit.
// No new dependencies, no network. Output lives inside node_modules (ignored).
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import fs from 'node:fs'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(here, '../..')
const outDir = path.join(root, 'node_modules', '.unit-out')
fs.rmSync(outDir, { recursive: true, force: true })
fs.mkdirSync(outDir, { recursive: true })

const esbuild = path.join(root, 'node_modules', 'esbuild', 'bin', 'esbuild')
const entries = fs.readdirSync(here).filter((f) => f.endsWith('.test.ts')).sort()
if (!entries.length) {
  console.error('no *.test.ts files in', here)
  process.exit(1)
}
const outFiles = []
for (const file of entries) {
  const entry = path.join(here, file)
  const outFile = path.join(outDir, file.replace(/\.ts$/, '.js'))
  execFileSync(
    process.execPath,
    [esbuild, entry, '--bundle', '--format=cjs', '--platform=node',
      '--outfile=' + outFile, '--log-level=warning'],
    { stdio: 'inherit' },
  )
  outFiles.push(outFile)
}

execFileSync(process.execPath, ['--test', ...outFiles], { stdio: 'inherit' })
