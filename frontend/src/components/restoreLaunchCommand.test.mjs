import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const root = fileURLToPath(new URL('../..', import.meta.url))
const server = await createServer({
  root,
  configFile: false,
  appType: 'custom',
  server: { middlewareMode: true, ws: false },
})
after(() => server.close())
const { buildRestoreLaunchCommand } = await server.ssrLoadModule('/src/pages/restoreLaunchCommand.ts')

test('restore launcher isolates the database URL in a child process and uses the selected port', () => {
  const command = buildRestoreLaunchCommand("E:\\World Engine\\author's copy", 8002)
  assert.match(command, /EnvironmentVariables\['WORLD_ENGINE_DATABASE_URL'\] = ''/)
  assert.match(command, /--data-directory "E:\\World Engine\\author''s copy" --port 8002/)
  assert.match(command, /\[System\.Diagnostics\.Process\]::Start\(\$worldEngineRestoreStart\)/)
  assert.match(command, /WaitForExit\(\)/)
  assert.doesNotMatch(command, /\$env:WORLD_ENGINE_DATABASE_URL/)
})

test('restore launcher rejects invalid port numbers', () => {
  assert.equal(buildRestoreLaunchCommand('E:\\restore', 0), '')
  assert.equal(buildRestoreLaunchCommand('E:\\restore', 65536), '')
  assert.equal(buildRestoreLaunchCommand('E:\\restore', 8001.5), '')
})
