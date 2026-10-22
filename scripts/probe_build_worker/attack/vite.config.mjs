// Slidev loads this file from the deck root while `slidev build` runs, so every
// line below runs as the build user, in the step's process. Each attack records
// the kernel's errno name or `succeeded`, never what it read, into the built talk
// as `attacks.txt`. The shape and the order are the rendezvous of the probe's
// driver: publish the step, wait for the 10002 probe, attack, publish done, wait
// for the probe to finish. Every wait is bounded.
import { spawnSync } from 'node:child_process'
import dns from 'node:dns/promises'
import fs from 'node:fs'
import net from 'node:net'
import path from 'node:path'

// The life's shared 1777 tmpfs and shared memory, by design: the rendezvous of the two attackers.
const TEMPORARY = '/tmp'
const SHARED_MEMORY = '/dev/shm'
const DECK = '/work/deck'
const PYTHON = '/opt/venv/bin/python'
const ATTACKS = `${DECK}/kernel_attacks.py`
const SOCKET_DIRECTORY = '/run/presentator-build'
const UNREACHABLE_ADDRESS = '192.0.2.1'
const RESOLVABLE_NAME = 'example.com'
const WAIT_MILLISECONDS = 60_000
const POLL_MILLISECONDS = 100
const STATUS_FIELDS = new Set(['Uid', 'Gid', 'Groups', 'CapInh', 'CapPrm', 'CapEff', 'CapAmb', 'NoNewPrivs', 'Seccomp'])

const lines = []

function record(line) {
  lines.push(line)
}

function check(key, attack) {
  try {
    attack()
    record(`check ${key} succeeded`)
  } catch (refusal) {
    record(`check ${key} ${refusal.code ?? refusal.name}`)
  }
}

async function checkAsync(key, attack) {
  try {
    await attack()
    record(`check ${key} succeeded`)
  } catch (refusal) {
    record(`check ${key} ${refusal.code ?? refusal.name}`)
  }
}

function reportStatus() {
  for (const line of fs.readFileSync('/proc/self/status', 'utf8').split('\n')) {
    const [name, values] = line.split(':')
    if (STATUS_FIELDS.has(name)) {
      record(`status node ${name} ${values.trim()}`)
    }
  }
}

function publish(name, content) {
  const pending = path.join(TEMPORARY, `.${name}.${process.pid}`)
  fs.writeFileSync(pending, content)
  fs.renameSync(pending, path.join(TEMPORARY, name))
}

async function waitFor(name) {
  const deadline = Date.now() + WAIT_MILLISECONDS
  while (Date.now() < deadline) {
    try {
      const content = fs.readFileSync(path.join(TEMPORARY, name), 'utf8').trim()
      if (content) {
        return content
      }
    } catch {
      // not published yet
    }
    await new Promise((resolve) => setTimeout(resolve, POLL_MILLISECONDS))
  }
  record(`not-driven waited for ${name}`)
  return null
}

function python(...argumentsList) {
  const done = spawnSync(PYTHON, ['-I', ATTACKS, ...argumentsList], { encoding: 'utf8' })
  for (const line of done.stdout.split('\n').filter(Boolean)) {
    record(line)
  }
  if (done.status !== 0) {
    record(`not-driven python ${argumentsList[0]} exited ${done.status} ${(done.stderr ?? '').trim().split('\n').pop()}`)
  }
}

function connectOutcome(options) {
  return new Promise((resolve, reject) => {
    const connection = net.connect(options)
    connection.once('connect', () => {
      connection.destroy()
      resolve()
    })
    connection.once('error', reject)
  })
}

function bindOutcome(address) {
  return new Promise((resolve, reject) => {
    const server = net.createServer()
    server.once('listening', () => server.close(resolve))
    server.once('error', reject)
    server.listen(address)
  })
}

function markers() {
  for (const directory of [TEMPORARY, SHARED_MEMORY, '/work/home', '/work/vite', `${DECK}/node_modules`]) {
    const marker = path.join(directory, 'deck-marker')
    try {
      fs.writeFileSync(marker, 'marker')
      record(`marker ${marker}`)
    } catch (refusal) {
      record(`check marker.${marker} ${refusal.code}`)
    }
  }
}

function filesystemAttacks() {
  check('fs.write.deck', () => fs.writeFileSync(`${DECK}/attack-marker`, 'x'))
  check('fs.create.root', () => fs.writeFileSync('/attack-marker', 'x'))
  check('fs.create.frontend', () => fs.writeFileSync('/app/frontend/attack-marker', 'x'))
  check('fs.write.package-json', () => fs.closeSync(fs.openSync('/app/frontend/package.json', 'r+')))
  // Writing the manifest answers EACCES: the image's runtime user owns it and
  // its mode 0644 refuses others; owner and mode are printed beside it as an
  // observation, not a refusal.
  const manifest = fs.statSync('/app/frontend/package.json')
  const mode = (manifest.mode & 0o7777).toString(8).padStart(4, '0')
  record(`observe package-json owner=${manifest.uid} mode=${mode}`)
}

async function networkAttacks() {
  await checkAsync('bind.socket-directory', () => bindOutcome(`${SOCKET_DIRECTORY}/attack.sock`))
  await checkAsync('connect.worker-socket', () => connectOutcome({ path: `${SOCKET_DIRECTORY}/worker.sock` }))
  await checkAsync('net.connect.ip', () => connectOutcome({ host: UNREACHABLE_ADDRESS, port: 53 }))
  await checkAsync('net.dns', () => dns.lookup(RESOLVABLE_NAME))
}

async function attack() {
  reportStatus()
  publish('deck.pid', String(process.pid))
  const probe = await waitFor('probe.pid')
  if (probe !== null) {
    for (const [label, target] of [['1', 1], ['probe', Number(probe)]]) {
      for (const signal of ['SIGSTOP', 'SIGKILL']) {
        check(`kill.${label}.${signal}`, () => process.kill(target, signal))
      }
    }
    python('deck', '1', probe)
    filesystemAttacks()
    await networkAttacks()
    markers()
    python('fork')
  }
  publish('deck.done', 'done')
  if (probe !== null) {
    await waitFor('probe.done')
  }
}

const attacked = { done: false }

const plugin = {
  name: 'attack',
  async buildStart() {
    if (!attacked.done) {
      attacked.done = true
      await attack()
    }
  },
  generateBundle() {
    this.emitFile({ type: 'asset', fileName: 'attacks.txt', source: `${lines.join('\n')}\n` })
  },
}

export default { plugins: [plugin] }
