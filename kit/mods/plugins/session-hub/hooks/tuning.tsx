// Раздел «Подстройка» панели диалогов: что пора улучшить под человека и что давно не делалось.
// Советы собирает без модели ~/.claude/bin/tuning-suggest.py (не чаще раза в 12 часов, в фоне), ответ человека —
// тем же скриптом в ~/.claude/tuning-feedback.jsonl: по журналу скрипт решает, что предлагать снова, а self-tune —
// что человеку не нужно. Общий файл для обеих панелей (набора и личной): каждая подключает раздел и кнопки tune.*.
//  Подстройка 2
//  ╭ Ревизия под тебя: последняя 9 дн. назад                ╮
//  │ просьба ×4: «смержил, закрывай задачу»                 │
//  │ сделать  обсудить  позже  не предлагать                │
//  ╰────────────────────────────────────────────────────────╯
import { atom, read, update } from 'claude-code'
import type { EngineInterface } from 'claude-code'
import type { TuneItem, TuneView } from '../types'

const SCRIPT = '.claude/bin/tuning-suggest.py'
const DATA = '.local/state/claude-tuning/suggestions.json'
const FRESH_MS = 12 * 60 * 60_000
const K_TOASTED = 'tuneToastedAt'

const tune = atom({ plugin: 'session-hub', key: 'tune' } as const, { items: [], at: 0 } as TuneView)

let home = ''

/** Советы для отрисовки: чтение подписывает панель на их изменение */
export async function readTune($: EngineInterface): Promise<TuneView> {
  return read($, tune)
}
let isLoading = false

async function homeDir($: EngineInterface): Promise<string> {
  if (!home) home = ((await $.env.get('HOME')) ?? (await $.env.get('USERPROFILE')) ?? '.').replace(/\\/g, '/')
  return home
}

async function runScript($: EngineInterface, args: string[]): Promise<boolean> {
  const h = await homeDir($)
  if (!(await $.fs.stat(`${h}/${SCRIPT}`).catch(() => undefined))) return false
  const r = await $.process.run(['python3', `${h}/${SCRIPT}`, ...args], { timeoutMs: 120_000 }).catch(() => null)
  return r?.exitCode === 0
}

async function readData($: EngineInterface): Promise<TuneView> {
  const raw: any = JSON.parse(String(await $.fs.read(`${await homeDir($)}/${DATA}`)))
  const items: TuneItem[] = (Array.isArray(raw?.items) ? raw.items : [])
    .filter((i: any) => typeof i?.id === 'string' && typeof i?.title === 'string')
    .map((i: any) => ({ id: i.id, title: i.title, why: String(i.why ?? ''), prompt: String(i.prompt ?? '') }))
  return { items, at: Number(raw?.at) || 0 }
}

/** Советы в панель: файл старше 12 часов или его нет — пересобрать скриптом. Новый набор — одно уведомление */
export async function loadTune($: EngineInterface, isForced = false): Promise<void> {
  if (isLoading) return
  isLoading = true
  try {
    const st = await $.fs.stat(`${await homeDir($)}/${DATA}`).catch(() => undefined)
    if (isForced || !st || (await $.clock.now()) - st.mtimeMs > FRESH_MS) await runScript($, [])
    const next = await readData($).catch(() => ({ items: [], at: 0 }) as TuneView)
    const cur = await read($, tune)
    if (JSON.stringify(cur) !== JSON.stringify(next)) await update($, tune, () => next)
    const toasted = Number(await $.store.get(K_TOASTED)) || 0
    if (next.items.length > 0 && next.at > toasted) {
      await $.store.set(K_TOASTED, next.at)
      $.ui.toast(`Подстройка: ${next.items.length} — что улучшить под тебя. Панель диалогов: /sessions`)
    }
  } finally {
    isLoading = false
  }
}

const discuss = (i: TuneItem) =>
  `Обсудим совет подстройки «${i.title}»${i.why ? ` (${i.why})` : ''}. Спроси одним вопросом, что мне в нём нужно иначе, ` +
  'и по ответу поправь правило по скиллу self-tune. Если совет мне не нужен — запиши это: ' +
  `python3 ~/${SCRIPT} --answer ${i.id} never`

/** Кнопка раздела: tune.<действие>.<id>. false — кнопка не из раздела */
export async function pressTune($: EngineInterface, key: string): Promise<boolean> {
  if (!key.startsWith('tune.')) return false
  const [, act, ...rest] = key.split('.')
  const id = rest.join('.')
  const item = (await read($, tune)).items.find(i => i.id === id)
  if (!item) return true
  const answer = act === 'do' ? 'done' : act === 'talk' ? 'discuss' : act === 'later' ? 'later' : 'never'
  await update($, tune, v => ({ ...v, items: v.items.filter(i => i.id !== id) }))
  void runScript($, ['--answer', id, answer])
  const text = act === 'do' ? item.prompt : act === 'talk' ? discuss(item) : ''
  // Из обработчика нажатия сообщение отправляется уже после него: прямой вызов движок отклоняет
  if (text) void $.clock.after(0, () => void $.prompt.submit({ text }).catch(() => $.ui.toast(`Не отправилось: ${text.slice(0, 60)}`)))
  return true
}

type Els = { Box: any; Text: any; Button: any }

/** Раздел панели; советов нет — ничего */
export function tuneSection(view: TuneView, els: Els, opts: { isDesktop: boolean; width: number; inset?: number }) {
  const { Box, Text, Button } = els
  if (view.items.length === 0) return null
  const pad = opts.inset ?? 0
  const bg = '#6366f114'
  const frame = opts.isDesktop ? { backgroundColor: bg, borderStyle: 'round' as const, borderColor: bg } : {}
  const cut = (t: string, n: number) => (t.length > n ? `${t.slice(0, Math.max(1, n - 1))}…` : t)
  const noop = () => undefined
  const act = (id: string, a: string, label: string) => <Button key={`tune.${a}.${id}`} plain label={label} dimColor={a !== 'do'} onPress={noop} />
  return (
    <Box key="sec-tune" flexDirection="column" marginBottom={1}>
      <Box flexDirection="row" paddingX={pad * 2} marginBottom={1}>
        <Text>
          <Text bold>Подстройка </Text>
          {opts.isDesktop ? <Text backgroundColor="#6366f133" color="#4338ca" bold>{` ${view.items.length} `}</Text> : <Text dimColor>{String(view.items.length)}</Text>}
        </Text>
      </Box>
      {view.items.map(i => (
        <Box key={`tune-card-${i.id}`} flexDirection="column" paddingX={1} marginX={pad} marginBottom={1} {...frame}>
          <Text bold wrap="truncate-end">{cut(i.title, opts.width)}</Text>
          {i.why !== '' && <Text dimColor wrap="wrap">{i.why}</Text>}
          <Box flexDirection="row" gap={2}>
            {act(i.id, 'do', 'сделать')}
            {act(i.id, 'talk', 'обсудить')}
            {act(i.id, 'later', 'позже')}
            {act(i.id, 'never', 'не предлагать')}
          </Box>
        </Box>
      ))}
    </Box>
  )
}
