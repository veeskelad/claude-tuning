import { expect, mock, test } from 'claude-code/testing'

const PANE = {
  plugin: 'session-hub',
  component: 'Pane',
  requestId: 'session-hub',
  props: { title: 'Диалоги', isFocused: false, bodyColumns: 60, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} } as any,
} as const
const DATA = '/home/t/.local/state/claude-tuning/suggestions.json'
const SCRIPT = '/home/t/.claude/bin/tuning-suggest.py'

test('подстройка: советы в панели, «сделать» отправляет и записывает ответ, «не предлагать» убирает', async ($, on) => {
  mock.store(on)
  const clock = mock.clock(on)
  mock.env(on, { HOME: '/home/t' })
  const items = [
    { id: 'self-tune', title: 'Запомнить, о чём ты просишь снова и снова', what: 'Claude найдёт повторяющиеся просьбы', why: 'просьба «смержил, закрывай» — 4 раза', prompt: 'Проведи ревизию по скиллу self-tune' },
    { id: 'memory', title: 'Навести порядок в памяти', what: '', why: '', prompt: '/memory-consolidate' },
  ]
  const files = new Map<string, string>([[DATA, JSON.stringify({ at: 5, items })], [SCRIPT, '#']])
  const runs: string[][] = []
  const sent: string[] = []
  const toasts: string[] = []
  on('command.register', () => ({ value: { command: 'sessions' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'cli-test' }))
  on('session.surfaces', () => ({ value: ['desktop'] }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.panes', () => ({ value: [] }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.toast', (_, e: any) => (toasts.push(String(e.text ?? e)), { value: undefined }) as any)
  on('fs.read', (_, e: any) => (files.has(e.path) ? { value: files.get(e.path) } : { deny: 'ENOENT' }) as any)
  // файл советов свежий — скрипт сбора не запускается
  on('fs.stat', (_, e: any) => (files.has(e.path) ? { value: { mtimeMs: clock.now() } } : { deny: 'ENOENT' }) as any)
  on('process.run', (_, e: any) => (runs.push([...e.argv]), { value: { exitCode: 0, stdout: '', stderr: '' } }) as any)
  on('prompt.submit', (_, e: any) => (sent.push(e.text), { text: e.text }) as any)
  on('mcp.call', () => ({ value: { content: [{ type: 'text', text: '[]' }], isError: false } }))

  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await clock.advance(6000)
  expect(runs.some(r => r.includes(SCRIPT) && !r.includes('--answer'))).toBe(false)
  expect(toasts.join('|')).toMatch(/Подстройка: 2/)

  for (const surface of ['desktop', 'terminal'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    const texts = async () => (await ui.findAll({ type: 'Text' })).map(t => t.text).join('|')
    expect(await texts()).toMatch(/Подстройка/)
    expect(await texts()).toMatch(/Запомнить, о чём ты просишь снова и снова/)
    expect(await texts()).toMatch(/Claude найдёт повторяющиеся просьбы/)
    expect(await texts()).toMatch(/смержил, закрывай/)
    expect(await texts()).toMatch(/«скрыть» — больше не покажу/)
    await ui.unmount()
  }

  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  const texts = async () => (await ui.findAll({ type: 'Text' })).map(t => t.text).join('|')
  await ui.press({ key: 'tune.do.self-tune' })
  await clock.advance(10)
  expect(sent).toContain('Проведи ревизию по скиллу self-tune')
  expect(runs).toContainEqual(['python3', SCRIPT, '--answer', 'self-tune', 'done'])
  expect(await texts()).not.toMatch(/Запомнить, о чём/)

  await ui.press({ key: 'tune.never.memory' })
  await clock.advance(10)
  expect(runs).toContainEqual(['python3', SCRIPT, '--answer', 'memory', 'never'])
  expect(sent.length).toBe(1)
  // советов не осталось — раздела нет
  expect(await texts()).not.toMatch(/Подстройка/)
  await ui.unmount()
})

test('подстройка: «обсудить» отправляет вопрос с советом и способом отказаться', async ($, on) => {
  mock.store(on)
  const clock = mock.clock(on)
  mock.env(on, { HOME: '/home/t' })
  const files = new Map<string, string>([[DATA, JSON.stringify({ at: 7, items: [{ id: 'skills-idle', title: 'Выключить навыки, которыми ты не пользуешься', what: '', why: '48 навыков', prompt: 'x' }] })], [SCRIPT, '#']])
  const sent: string[] = []
  on('command.register', () => ({ value: { command: 'sessions' } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'cli-test' }))
  on('session.surfaces', () => ({ value: ['desktop'] }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.panes', () => ({ value: [] }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.toast', () => ({ value: undefined }))
  on('fs.read', (_, e: any) => (files.has(e.path) ? { value: files.get(e.path) } : { deny: 'ENOENT' }) as any)
  on('fs.stat', (_, e: any) => (files.has(e.path) ? { value: { mtimeMs: clock.now() } } : { deny: 'ENOENT' }) as any)
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }) as any)
  on('prompt.submit', (_, e: any) => (sent.push(e.text), { text: e.text }) as any)
  on('mcp.call', () => ({ value: { content: [{ type: 'text', text: '[]' }], isError: false } }))

  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await clock.advance(6000)
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'tune.talk.skills-idle' })
  await clock.advance(10)
  expect(sent[0]).toMatch(/Обсудим совет подстройки «Выключить навыки/)
  expect(sent[0]).toMatch(/--answer skills-idle never/)
  await ui.unmount()
})
