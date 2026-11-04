import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Phase } from '../types'

// Раньше автосжатия: перенос успевает, пока сжатие не срезало детали
const FIRST_OFFER = 60
const SNOOZE_STEP = 10
// Сколько ждать документ: ход записи может стоять в очереди за текущим
const WAIT_MS = 15 * 60_000

const phase = atom({ plugin: 'handoff', key: 'phase' } as const, 'idle' as Phase)
const offerAt = atom({ plugin: 'handoff', key: 'offerAt' } as const, FIRST_OFFER)
const percent = atom({ plugin: 'handoff', key: 'percent' } as const, 0)
const note = atom({ plugin: 'handoff', key: 'note' } as const, '')
const doc = atom({ plugin: 'handoff', key: 'doc' } as const, '')
const askedAt = atom({ plugin: 'handoff', key: 'askedAt' } as const, 0)

// Документ пишет сама сессия: у неё весь контекст. Отдельная модель-пересказчик теряла текущий шаг,
// и чистый чат начинал с повторного изучения проекта
const writePrompt = (path: string) => `Контекст почти заполнен. После этого ответа он будет очищен, и работа продолжится в чистом чате только по документу передачи. Запиши его в файл ${path} (папку создай). Разделы по порядку:

1. Сейчас — шаг, на котором мы стоим, и первое действие следующего чата: команда, файл и строка или правка. Если пользователь проверял что-то вживую — что он видел и что ему не понравилось.
2. Последнее от пользователя — его последние 1–3 сообщения дословно (до 300 знаков каждое).
3. Цель и «готово» — одной-двумя строками; путь к чек-листу задачи, если он есть.
4. Файлы — изменённые и нужные, полными путями, по строке: что в нём и в каком он состоянии.
5. Решения — что приняли и почему; что пробовали и отбросили.
6. Проверено и нет.
7. Дальше — оставшиеся шаги по порядку.

Не пересказывай то, что уже записано в файлах проекта, — дай путь. Ключи, пароли и личные данные в документ не пиши. Ответь одной строкой: путь к документу.`

const continuePrompt = (path: string) =>
  `Прочитай ${path} и продолжай с раздела «Сейчас». Проект и сводки заново не изучай: открывай только файлы, названные в документе, и только когда они нужны для шага.`

async function docPath($: EngineInterface): Promise<string> {
  const home = ((await $.env.get('HOME')) ?? (await $.env.get('USERPROFILE')) ?? '.').replace(/\\/g, '/')
  const d = new Date(await $.clock.now())
  const p2 = (n: number) => String(n).padStart(2, '0')
  const stamp = `${d.getFullYear()}${p2(d.getMonth() + 1)}${p2(d.getDate())}-${p2(d.getHours())}${p2(d.getMinutes())}`
  return `${home}/.claude/handoffs/${stamp}-${(await $.session.id()).slice(0, 8)}.md`
}

async function fail($: EngineInterface, why: string): Promise<void> {
  await update($, phase, () => 'error')
  await update($, note, () => why)
}

// Шаг 1: заказать документ у самой сессии; шаг 2 — в turn.complete
async function start($: EngineInterface, path?: string): Promise<string> {
  const target = path ?? (await docPath($))
  const now = await $.clock.now()
  await update($, doc, () => target)
  await update($, askedAt, () => now)
  await update($, phase, () => 'writing')
  await $.prompt.submit({ text: writePrompt(target) })
  return target
}

// Шаг 2: документ записан после заказа — очистить контекст и продолжить по нему
async function check($: EngineInterface): Promise<void> {
  if ((await read($, phase)) !== 'writing') return
  const path = await read($, doc)
  const since = await read($, askedAt)
  const stat = await $.fs.stat(path).catch(() => undefined)
  if (stat && stat.mtimeMs >= since - 2000) {
    try {
      await $.command.run({ command: 'clear' })
      await update($, phase, () => 'idle')
      await update($, offerAt, () => FIRST_OFFER)
      await $.prompt.submit({ text: continuePrompt(path) })
    } catch (err) {
      await fail($, `не удалось очистить контекст: ${err instanceof Error ? err.message : String(err)}. Документ — ${path}`)
    }
  } else if ((await $.clock.now()) - since > WAIT_MS) {
    await fail($, `документ не появился за 15 минут (${path})`)
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'handoff-now', description: 'Перенести работу в чистый чат по документу передачи' })
    // Процесс перезапустился посреди переноса: документ мог успеть записаться
    void check($)
    return next(e)
  })

  on('command.run', { command: 'handoff-now' }, async $ => {
    if ((await read($, phase)) === 'writing') return { text: `Перенос уже идёт: ${await read($, doc)}` }
    // Из хука команды отправить запрос нельзя — он ждал бы ход, который держит сам хук: следующим событием
    const path = await docPath($)
    $.clock.after(1, () => void start($, path))
    return { text: `Claude пишет документ передачи (${path}); потом — чистый чат и продолжение` }
  })

  on('turn.complete', async ($, e, next) => {
    if (!e.agentId) await check($)
    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    const p = e.context.percent
    if (p !== undefined) {
      await update($, percent, () => p)
      if (p >= (await read($, offerAt)) && (await read($, phase)) === 'idle') {
        await update($, phase, () => 'offer')
        // VS Code полосу над полем ввода не рисует — там подсказка всплывающим сообщением
        $.ui.toast(`Контекст ${p}% — /handoff-now перенесёт работу в чистый чат`, { timeoutMs: 15000 })
      }
    }
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const now = await read($, phase)
    if (e.props.hasSurvey || now === 'idle') return next(e)
    const { Box, Button, Text } = $.ui.resolve(e)
    // Полоса общая с другими модами: своё сверху, их часть под ним
    const below = await next(e)
    let row
    if (now === 'writing') {
      row = <Text dimColor>Перенос в чистый чат: Claude пишет документ передачи…</Text>
    } else if (now === 'error') {
      row = (
        <Box>
          <Text color="error">Перенос не удался: {await read($, note)} </Text>
          <Button key="handoff-ok" label="Понятно" onPress={() => update($, phase, () => 'idle')} />
        </Box>
      )
    } else {
      const p = await read($, percent)
      row = (
        <Box>
          <Text color="warning">Контекст {p}%. Перенести работу в чистый чат? </Text>
          <Button key="handoff-go" label="Перенести" onPress={() => void start($)} />
          <Button
            key="handoff-later"
            label="Не сейчас"
            onPress={async () => {
              await update($, offerAt, () => p + SNOOZE_STEP)
              await update($, phase, () => 'idle')
            }}
          />
        </Box>
      )
    }
    return (
      <Box flexDirection="column">
        {row}
        {below}
      </Box>
    )
  })
}
