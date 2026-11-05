import { expect, mock, test } from 'claude-code/testing'

const BAND = { plugin: "session-hub", component: "AbovePrompt", props: { hasSurvey: false, isWorking: false, bodyColumns: 100 } as any } as const
const id = (n: number) => `local_0000000${n}-0000-4000-8000-000000000000`

test('полоса: числа в кнопке, раскрытие, подпись влезает, ✓ переносит в завершённые', async ($, on) => {
  mock.store(on)
  const clock = mock.clock(on)
  const iso = (minAgo: number) => new Date(clock.now() - minAgo * 60000).toISOString()
  const long = 'Проверить, не оставляет ли сборщик неубранные git worktree после задач'
  let sessions = [
    { sessionId: id(1), title: long, cwd: '/home/x/work/app', isRunning: true, lastActivityAt: iso(0), link: `claude://x/${id(1)}` },
    { sessionId: id(2), title: 'Ответила', cwd: '/home/x/work', isRunning: true, lastActivityAt: iso(1), link: `claude://x/${id(2)}` },
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

// Терминал и VS Code (и терминал IDE): мод молчит — ни полосы, ни панели, ни строки статуса, ни опроса
for (const surface of ['terminal', 'vscode'] as const) {
  test(`${surface}: мода нет`, async ($, on) => {
    mock.store(on)
    const clock = mock.clock(on, { now: Date.parse('2026-11-04T09:00:00Z') })
    const commands: string[] = []
    const opened: string[] = []
    let status: string | undefined
    let calls = 0
    on('command.register', (_, e: any) => (commands.push(e.name), { value: { command: e.name } }))
    on('session.start', (_, e) => ({ cwd: e.cwd }))
    on('session.id', () => ({ value: 'me' }))
    on('session.surfaces', () => ({ value: [surface] }))
    on('ui.status', (_, e: any) => ((status = e.text), { value: undefined }))
    on('ui.log', () => ({ value: undefined }))
    on('ui.panes', () => ({ value: [] }))
    on('ui.open', (_, e: any) => (opened.push(e.id), { value: { isPlaced: true } }))
    on('fs.list', () => ((calls++), { value: [] }) as any)
    on('mcp.call', () => ((calls++), { deny: 'мод не должен звать приложение' }) as any)
    on('ui.render', ($, e) => {
      const { Text } = $.ui.resolve(e)
      return <Text>engine</Text>
    })

    await $.session.start({ cwd: '/', surface, isInteractive: true })
    await $.session.attach({ surface } as any).catch(() => undefined)
    await clock.advance(10_000)
    expect(commands).not.toContain('sessions')
    expect(opened).not.toContain('session-hub')
    expect(status ?? '').not.toMatch(/в работе/)
    expect(calls).toBe(0)
    if (surface === 'terminal') {
      const ui = await $.ui.mount({ ...BAND, surface })
      expect((await ui.findAll({ type: 'Text' })).map(t => t.text).join('|')).toBe('engine')
      await ui.unmount()
    }
  })
}
