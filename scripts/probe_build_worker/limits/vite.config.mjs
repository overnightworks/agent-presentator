// Slidev loads this file from the deck root while `slidev build` runs, so the
// hook below runs as the build user, in the step's process. The probe's web
// client writes the case into `limit.txt` of the run's copy of this deck before
// the STEP; after the bundle is written the hook adds what the case names to
// the talk, and the worker's own checks then judge the reply.
import fs from 'node:fs'
import path from 'node:path'

const CASE_FILE = '/work/deck/limit.txt'
// `many-entries` stays under the entries bound and takes seconds to send;
// `output-entries` is one file over it, and 301 megabytes is one over the
// default output bound.
const ENTRIES = { 'many-entries': 90_000, 'output-entries': 100_001 }
const MEGABYTE = 1024 * 1024
const OUTPUT_MEGABYTES = 301

let outDirectory = ''

function manyFiles(count) {
  const directory = path.join(outDirectory, 'files')
  fs.mkdirSync(directory)
  for (let index = 0; index < count; index += 1) {
    fs.writeFileSync(path.join(directory, String(index)), 'x')
  }
}

function manyMegabytes(count) {
  const block = Buffer.alloc(MEGABYTE)
  for (let index = 0; index < count; index += 1) {
    fs.writeFileSync(path.join(outDirectory, `block-${index}`), block)
  }
}

const plugin = {
  name: 'limits',
  configResolved(config) {
    outDirectory = path.resolve(config.root, config.build.outDir)
  },
  closeBundle() {
    const mode = fs.readFileSync(CASE_FILE, 'utf8').trim()
    if (mode in ENTRIES) {
      manyFiles(ENTRIES[mode])
    } else if (mode === 'output-megabytes') {
      manyMegabytes(OUTPUT_MEGABYTES)
    } else if (mode === 'output-shape') {
      fs.symlinkSync('/etc/passwd', path.join(outDirectory, 'link'))
    }
  },
}

export default { plugins: [plugin] }
