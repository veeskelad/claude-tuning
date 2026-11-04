import { expect, mock, test } from 'claude-code/testing'

const BAND = { plugin: "session-hub", component: "AbovePrompt", props: { hasSurvey: false, isWorking: false, bodyColumns: 100 } as any } as const
const id = (n: number) => `local_0000000${n}-0000-4000-8000-000000000000`

test('полоса: числа в кнопке, раскрытие, подпись влезает, ✓ переносит в завершённые', async ($, on) => {
  mock.store(on)
  const clock = mock.clock(on)
  const iso = (minAgo: number) => new Date(clock.now() - minAgo * 60000).toISOString()
  const long = 'Проверить, не оставляет ли Hermes неубранные git worktree после задач'
  let sessions = [
    { sessionId: id(1), title: long, cwd: '/Users/x/Work/aisy', isRunning: true, lastActivityAt: iso(0), link: `claude://x/${id(1)}` },
    { sessionId: id(2), title: 'Ответила', cwd: '/Users/x/Work', isRunning: true, lastActivityAt: iso(1), link: `claude://x/${id(2)}` },
  ]
  on('command.register', () => ({ value: { command: 'sessions' } }))
  mock.env(on, { HOME: '/home/t' })
  const files = new Map<string, string>()
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'cli-test' }))
  on('session.surfaces', () => ({ value: ['desktop'] }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.log', () => ({ value: undefined }))
  on('ui.panes', () => ({ value: [] }))
  on('fs.read', (_, e: any) => (files.has(e.path) ? { value: files.get(e.path) } : { deny: 'ENOENT' }) as any)
  on('fs.write', (_, e: any) => (files.set(e.path, String(e.content ?? e.data)), { value: undefined }) as any)
  on('fs.stat', (_, e: any) => (files.has(e.path) ? { value: { mtimeMs: 1 } } : { deny: 'ENOENT' }) as any)
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.close', () => ({ value: undefined }))
  on('mcp.call', (_, e) => ({
    value: { content: [{ type: 'text', text: JSON.stringify(e.tool === 'get_session' ? { title: 'Я' } : e.tool === 'get_layout' ? { views: [1] } : sessions) }], isError: false },
  }))
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })

  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await clock.advance(1000)
  // вторая закончила ход — ждёт ответа
  sessions = [sessions[0]!, { ...sessions[1]!, isRunning: false, lastActivityAt: iso(0) }]
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
  await ui.press({ key: `seen-${id(2)}` })
  expect(await texts()).toMatch(/Ждут ответа  0 /)
  // ушла в завершённые
  expect(await texts()).toMatch(/Завершены/)
  await ui.unmount()
})

test('терминал: список из ~/.claude/sessions, свой диалог и умершие процессы не показываются', async ($, on) => {
  mock.store(on)
  const clock = mock.clock(on, { now: Date.parse('2026-11-04T09:00:00Z') })
  mock.env(on, { HOME: '/home/t' })
  const dir = '/home/t/.claude/sessions'
  const files: Record<string, unknown> = {
    'a.json': { pid: 11, sessionId: 'me', name: 'Я сам', cwd: '/w/a', kind: 'interactive', status: 'busy', pidDomain: 'darwin', statusUpdatedAt: clock.now() },
    'b.json': { pid: 22, sessionId: 'b', hostSessionId: 'local_b', name: 'Работает', cwd: '/w/proj', kind: 'interactive', status: 'busy', pidDomain: 'darwin', statusUpdatedAt: clock.now() },
    'c.json': { pid: 33, sessionId: 'c', name: 'Умерла', cwd: '/w/x', kind: 'interactive', status: 'busy', pidDomain: 'darwin', statusUpdatedAt: clock.now() },
    'd.json': { pid: 44, sessionId: 'd', name: 'Сервер', cwd: '/w/y', kind: 'interactive', status: 'busy', pidDomain: 'linux', statusUpdatedAt: clock.now() },
  }
  on('command.register', () => ({ value: { command: 'sessions' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'me' }))
  on('session.surfaces', () => ({ value: ['terminal'] }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.log', () => ({ value: undefined }))
  on('ui.panes', () => ({ value: [] }))
  on('fs.list', () => ({ value: Object.keys(files).map(name => ({ name, kind: 'file', size: 1, mtimeMs: 1, isLink: false })) }) as any)
  on('fs.read', (_, e: any) => {
    const name = String(e.path).replace(`${dir}/`, '')
    return (name in files ? { value: JSON.stringify(files[name]) } : { deny: 'ENOENT' }) as any
  })
  on('fs.write', () => ({ value: undefined }) as any)
  on('fs.stat', () => ({ deny: 'ENOENT' }) as any)
  on('process.run', (_, e: any) => ({
    value: { exitCode: 0, stdout: e.argv[0] === 'uname' ? 'Darwin\n' : '   11\n   22\n   44\n', stderr: '' },
  }) as any)
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(2000)
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  const texts = async () =>
    [...(await ui.findAll({ type: 'Text' })).map(t => t.text), ...(await ui.findAll({ type: 'Button' })).map((b: any) => b.label ?? b.text)].join('|')
  // свой диалог — не из файла, а из своих ходов (turn.start): здесь хода нет
  expect(await texts()).toMatch(/В работе 1/)
  await ui.press({ key: 'band-list' })
  const t = await texts()
  expect(t).toMatch(/Работает/)
  expect(t).not.toMatch(/Я сам/)
  expect(t).not.toMatch(/Умерла/)
  expect(t).not.toMatch(/Сервер/)
  await ui.unmount()
})

test('VS Code: панель открывается сама, числа в строке статуса', async ($, on) => {
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
  expect(opened).toContain('session-hub')
  expect(status).toMatch(/в работе 1 · ждут ответа 0/)
})
