import { expect, mock, test } from 'claude-code/testing'

const BAND = { plugin: "session-hub", component: "AbovePrompt", props: { hasSurvey: false, isWorking: false, bodyColumns: 100 } as any } as const
const id = (n: number) => `local_0000000${n}-0000-4000-8000-000000000000`

test('полоса: числа в кнопке, раскрытие, подпись влезает, ✓ переносит в завершённые', async ($, on) => {
  mock.store(on)
  const clock = mock.clock(on)
  const long = 'Проверить, не оставляет ли Hermes неубранные git worktree после задач'
  // В приложении список тоже из ~/.claude/sessions: инструменты сессий приложения спрашивают разрешение
  const entry = (n: number, name: string, cwd: string, busy: boolean) => ({
    pid: 10 + n, sessionId: `cli-${n}`, hostSessionId: id(n), name, cwd, kind: 'interactive',
    status: busy ? 'busy' : 'idle', pidDomain: 'darwin', statusUpdatedAt: clock.now(),
  })
  const local: Record<string, unknown> = {
    '11.json': entry(1, long, '/Users/x/Work/app', true),
    '12.json': entry(2, 'Ответила', '/Users/x/Work', true),
  }
  const dir = '/home/t/.claude/sessions'
  let mcpCalls = 0
  on('command.register', () => ({ value: { command: 'sessions' } }))
  mock.env(on, { HOME: '/home/t' })
  const files = new Map<string, string>()
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'cli-test' }))
  on('session.surfaces', () => ({ value: ['desktop'] }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.log', () => ({ value: undefined }))
  on('ui.panes', () => ({ value: [] }))
  on('fs.list', () => ({ value: Object.keys(local).map(name => ({ name, kind: 'file', size: 1, mtimeMs: 1, isLink: false })) }) as any)
  on('fs.read', (_, e: any) => {
    const name = String(e.path).replace(`${dir}/`, '')
    if (name in local) return { value: JSON.stringify(local[name]) } as any
    return (files.has(e.path) ? { value: files.get(e.path) } : { deny: 'ENOENT' }) as any
  })
  on('fs.write', (_, e: any) => (files.set(e.path, String(e.content ?? e.data)), { value: undefined }) as any)
  on('fs.stat', (_, e: any) => (files.has(e.path) ? { value: { mtimeMs: 1 } } : { deny: 'ENOENT' }) as any)
  on('process.run', (_, e: any) => ({
    value: { exitCode: 0, stdout: e.argv[0] === 'uname' ? 'Darwin\n' : '   11\n   12\n', stderr: '' },
  }) as any)
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.close', () => ({ value: undefined }))
  on('mcp.call', () => (mcpCalls++, { deny: 'мод не должен звать приложение' }) as any)
  on('tool.call', () => (mcpCalls++, { deny: 'мод не должен просить разрешений' }) as any)
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })

  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await clock.advance(1000)
  // вторая закончила ход — ждёт ответа
  local['12.json'] = { ...(local['12.json'] as object), status: 'idle', statusUpdatedAt: clock.now() }
  await clock.advance(16000)

  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  const texts = async () =>
    [...(await ui.findAll({ type: 'Text' })).map(t => t.text), ...(await ui.findAll({ type: 'Button' })).map((b: any) => b.label ?? b.text)].join('|')
  expect(await texts()).toMatch(/В работе  1 .*Ждут ответа  1 /)
  expect(await texts()).not.toMatch(/Ответила/)
  await ui.press({ key: 'band-list' })
  expect(await texts()).toMatch(/Ответила/)
  // длинное название обрезано под плашку, а не переносит время на новую строку
  expect(await texts()).toMatch(/Проверить, не оставляет.*…/)
  expect(await texts()).not.toMatch(/после задач/)
  await ui.press({ key: `read-${id(2)}` })
  expect(await texts()).toMatch(/Ждут ответа  0 /)
  // ушла в завершённые
  expect(await texts()).toMatch(/Завершены/)
  expect(mcpCalls).toBe(0)
  await ui.unmount()
})

test('терминал: мода нет — ни полосы, ни команды, ни опроса', async ($, on) => {
  mock.store(on)
  const clock = mock.clock(on, { now: Date.parse('2026-11-04T09:00:00Z') })
  mock.env(on, { HOME: '/home/t' })
  const commands: string[] = []
  let reads = 0
  const file = { pid: 22, sessionId: 'b', name: 'Работает', cwd: '/w/proj', kind: 'interactive', status: 'busy', pidDomain: 'darwin', statusUpdatedAt: clock.now() }
  on('command.register', (_, e: any) => (commands.push(e.name), { value: { command: e.name } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'me' }))
  on('session.surfaces', () => ({ value: ['terminal'] }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.log', () => ({ value: undefined }))
  on('ui.panes', () => ({ value: [] }))
  on('fs.list', () => ((reads++), { value: [{ name: 'b.json', kind: 'file', size: 1, mtimeMs: 1, isLink: false }] }) as any)
  on('fs.read', (_, e: any) => (String(e.path).endsWith('b.json') ? { value: JSON.stringify(file) } : { deny: 'ENOENT' }) as any)
  on('fs.write', () => ({ value: undefined }) as any)
  on('fs.stat', () => ({ deny: 'ENOENT' }) as any)
  on('process.run', (_, e: any) => ({ value: { exitCode: 0, stdout: e.argv[0] === 'uname' ? 'Darwin\n' : '22\n', stderr: '' } }) as any)
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(10_000)
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  const texts = (await ui.findAll({ type: 'Text' })).map(t => t.text).join('|')
  expect(texts).toBe('engine')
  expect(commands).not.toContain('sessions')
  expect(reads).toBe(0)
  await ui.unmount()
})

test('VS Code: мода нет — панель не открывается, строки статуса нет', async ($, on) => {
  mock.store(on)
  const clock = mock.clock(on, { now: Date.parse('2026-11-04T09:00:00Z') })
  mock.env(on, { HOME: '/home/t' })
  const opened: string[] = []
  let status: string | undefined
  const file = { pid: 22, sessionId: 'b', name: 'Работает', cwd: '/w/proj', kind: 'interactive', status: 'busy', pidDomain: 'darwin', statusUpdatedAt: clock.now() }
  on('command.register', () => ({ value: { command: 'sessions' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'me' }))
  on('session.surfaces', () => ({ value: ['vscode'] }))
  on('ui.status', (_, e: any) => ((status = e.text), { value: undefined }))
  on('ui.log', () => ({ value: undefined }))
  on('ui.panes', () => ({ value: [] }))
  on('ui.open', (_, e: any) => (opened.push(e.id), { value: { isPlaced: true } }))
  on('fs.list', () => ({ value: [{ name: 'b.json', kind: 'file', size: 1, mtimeMs: 1, isLink: false }] }) as any)
  on('fs.read', (_, e: any) => (String(e.path).endsWith('b.json') ? { value: JSON.stringify(file) } : { deny: 'ENOENT' }) as any)
  on('fs.write', () => ({ value: undefined }) as any)
  on('fs.stat', () => ({ deny: 'ENOENT' }) as any)
  on('process.run', (_, e: any) => ({ value: { exitCode: 0, stdout: e.argv[0] === 'uname' ? 'Darwin\n' : '22\n', stderr: '' } }) as any)

  await $.session.start({ cwd: '/', surface: 'vscode', isInteractive: true })
  await clock.advance(2000)
  expect(opened).not.toContain('session-hub')
  expect(status ?? '').not.toMatch(/в работе/)
})
