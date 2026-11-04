import { expect, mock, test } from 'claude-code/testing'

const BAND = { plugin: 'handoff', component: 'AbovePrompt', props: { hasSurvey: false, isWorking: false } as any } as const
const measure = (percent: number) => ({
  context: { tokens: percent * 2000, window: 200000, percent },
  rateLimits: [],
  changed: ['context' as const],
})

test('на 60% предлагает перенос, «Не сейчас» сдвигает порог на 10%', async ($, on) => {
  on('session.measure', (_, e) => ({ changed: e.changed }))
  on('ui.toast', () => ({ value: undefined }) as any)
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })
  await $.session.measure(measure(55))
  const quiet = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect((await quiet.find({ type: 'Text' }))?.text).toBe('engine')
  await quiet.unmount()

  await $.session.measure(measure(61))
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect((await ui.find({ type: 'Text', text: /Контекст 61%/ }))?.text).toMatch(/Перенести/)
  await ui.press({ key: 'handoff-later' })
  await ui.unmount()

  await $.session.measure(measure(65))
  const after = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect((await after.find({ type: 'Text' }))?.text).toBe('engine')
  await after.unmount()
})

test('/handoff-now: сессия пишет документ, затем /clear и продолжение по нему', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  const clock = mock.clock(on, { now: Date.parse('2026-11-04T09:00:00Z') })
  const prompts: string[] = []
  const commands: string[] = []
  let written = 0
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'abcdef1234' }))
  on('command.register', () => ({ value: { command: 'handoff-now' } }))
  on('prompt.submit', (_, e: any) => (prompts.push(e.text), { text: e.text }) as any)
  on('command.run', (_, e: any) => (commands.push(e.command), { value: undefined }) as any)
  on('turn.complete', () => ({ text: '' }) as any)
  on('fs.stat', () => (written ? { value: { mtimeMs: written } } : { deny: 'ENOENT' }) as any)

  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const r: any = await $.command.run({ command: 'handoff-now' } as any)
  await clock.advance(10)
  expect(r.text).toMatch(/\/home\/t\/\.claude\/handoffs\/20261104-\d{4}-abcdef12\.md/)
  expect(prompts[0]).toMatch(/Запиши его в файл \/home\/t\/\.claude\/handoffs\//)

  // ход закончился без документа — ждём дальше, ничего не чистим
  await $.turn.complete({ turnId: 't1', answer: '', durationMs: 1, isAborted: false, reason: 'answer' } as any)
  expect(commands).not.toContain('clear')

  written = clock.now() + 1000
  await $.turn.complete({ turnId: 't2', answer: '', durationMs: 1, isAborted: false, reason: 'answer' } as any)
  expect(commands).toContain('clear')
  expect(prompts[1]).toMatch(/продолжай с раздела «Сейчас»/)
})
