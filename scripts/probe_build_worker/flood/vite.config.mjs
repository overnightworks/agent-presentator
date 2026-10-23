// Slidev loads this file from the deck root while `slidev build` runs, so the
// hook below runs as the build user, in the step's process. The driver publishes
// the mode into the life's /tmp before the STEP; the hook hands it to the venv
// Python child that does the flooding, and then fails the build, so a step that
// survives its flood still ends as `failed`.
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

// The life's shared 1777 tmpfs, by design: the rendezvous with the driver.
const TEMPORARY = '/tmp'
const PYTHON = '/opt/venv/bin/python'
const ACTIONS = '/work/deck/flood_actions.py'

function publish(name, content) {
  const pending = path.join(TEMPORARY, `.${name}.${process.pid}`)
  fs.writeFileSync(pending, content)
  fs.renameSync(pending, path.join(TEMPORARY, name))
}

const plugin = {
  name: 'flood',
  buildStart() {
    const mode = fs.readFileSync(path.join(TEMPORARY, 'flood.mode'), 'utf8').trim()
    publish('deck.pid', String(process.pid))
    const done = spawnSync(PYTHON, ['-I', ACTIONS, mode], { stdio: 'inherit' })
    throw new Error(`flood ${mode} ended with ${done.status ?? done.signal}`)
  },
}

export default { plugins: [plugin] }
