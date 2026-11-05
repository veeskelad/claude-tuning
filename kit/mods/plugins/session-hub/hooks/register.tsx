import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { HubRow, HubSelf, HubState } from '../types'
import { loadTune, pressTune, readTune, tuneSection } from './tuning'

const PANE = 'session-hub'
const SERVER = 'ccd_session_mgmt'
const POLL_BUSY = 5_000
const POLL_IDLE = 15_000
const BAND_DONE_MAX = 10
// $.store общий для всех диалогов: отметки «видел» и выбор панели совпадают везде
const K_SEEN = 'seen'
const K_BASE = 'baseline'
const K_PANE = 'paneOpen'
// В `open` уходит только ссылка приложения на локальную сессию
const LINK_RE = /^claude:\/\/claude\.ai\/epitaxy\/local_[0-9a-f-]{36}$/
// Колонки плашки постоянной ширины: время и «✓» стоят ровно во всех плашках
const TIME_W = 9
const READ_W = 4
const MIN_PILL = 36

const TONE = {
  running: { name: 'В работе', bg: '#16a34a14', hover: '#16a34a2e', badge: '#16a34a29', ink: '#15803d', dot: '#16a34a' },
  waiting: { name: 'Ждут ответа', bg: '#d977061a', hover: '#d9770638', badge: '#d9770633', ink: '#b45309', dot: '#d97706' },
  done: { name: 'Завершены', bg: '#71717a0f', hover: '#71717a24', badge: '#71717a24', ink: '#71717a', dot: '#a1a1aa' },
} as const

const rows = atom({ plugin: 'session-hub', key: 'rows' } as const, [] as HubRow[], { shape: 'rows-2' })
const self = atom({ plugin: 'session-hub', key: 'self' } as const, { title: '', isRunning: false, since: null } as HubSelf)
const error = atom({ plugin: 'session-hub', key: 'error' } as const, '')
const isListOpen = atom({ plugin: 'session-hub', key: 'isListOpen' } as const, false)
const isDoneShown = atom({ plugin: 'session-hub', key: 'isDoneShown' } as const, false)
const isPaneOpen = atom({ plugin: 'session-hub', key: 'isPaneOpen' } as const, false)

// Живут, пока жив процесс диалога; всё, что должно пережить сон, — в $.store
let timer: { cancel: () => void } | null = null
let raw: Record<string, unknown>[] = []
const runningSince: Record<string, number> = {}
// 'app' — инструменты сессий приложения Claude; 'local' — файлы ~/.claude/sessions/<pid>.json, которые пишет
// каждая живая сессия Claude Code на машине: так список есть и в терминале, и в VS Code
let source: 'app' | 'local' = 'app'
// VS Code не рисует полосу над полем ввода: там панель сбоку открывается сама, числа — в строке статуса
let isVsCode = false
let isVsPaneShown = false
let osName = ''
let selfKey = ''

const cut = (t: string, n: number) => (t.length > n ? t.slice(0, Math.max(1, n - 1)) + '…' : t)
const projectOf = (cwd: string) => cwd.split('/').filter(Boolean).slice(-1)[0] ?? ''

function ago(now: number, ms: number) {
  const min = Math.max(0, Math.floor((now - ms) / 60000))
  if (min < 1) return 'сейчас'
  if (min < 60) return `${min} мин`
  if (min < 1440) return `${Math.floor(min / 60)} ч`
  return `${Math.floor(min / 1440)} дн`
}

async function seenMap($: EngineInterface): Promise<Record<string, number>> {
  const v = await $.store.get(K_SEEN)
  return v && typeof v === 'object' ? (v as Record<string, number>) : {}
}

async function baseline($: EngineInterface, now: number): Promise<number> {
  const v = await $.store.get(K_BASE)
  if (typeof v === 'number') return v
  // первый запуск: всё, что было до него, считаем просмотренным — иначе «ждут» все старые
  await $.store.set(K_BASE, now)
  return now
}

async function classify($: EngineInterface) {
  const now = await $.clock.now()
  const seen = await seenMap($)
  const base = await baseline($, now)
  const next: HubRow[] = raw.map(s => {
    const id = String(s.sessionId ?? '')
    const at = Date.parse(String(s.lastActivityAt ?? '')) || 0
    const isRunning = s.isRunning === true
    if (isRunning) runningSince[id] ??= now
    else delete runningSince[id]
    const state: HubState = isRunning ? 'running' : at > Math.max(seen[id] ?? 0, base) ? 'waiting' : 'done'
    return {
      id,
      title: String(s.title || 'без названия'),
      project: projectOf(String(s.cwd ?? '')),
      link: typeof s.link === 'string' && LINK_RE.test(s.link) ? s.link : null,
      state,
      at,
      runningSince: isRunning ? (runningSince[id] ?? now) : null,
    }
  })
  // перерисовка только при настоящем изменении: каждая перерисовка может съесть нажатие
  const before = await read($, rows)
  if (JSON.stringify(before) !== JSON.stringify(next)) await update($, rows, () => next)
  if (isVsCode) {
    const me = await read($, self)
    const run = next.filter(r => r.state === 'running').length + (me.isRunning ? 1 : 0)
    $.ui.status(`Диалоги: в работе ${run} · ждут ответа ${next.filter(r => r.state === 'waiting').length}`)
  }
}

async function listApp($: EngineInterface): Promise<Record<string, unknown>[]> {
  const res = await $.mcp.call(SERVER, 'list_sessions', { limit: 40 })
  const text = res.content.map(c => ('text' in c ? c.text : '')).join('')
  if (res.isError) throw new Error(text.slice(0, 160))
  const list = JSON.parse(text.slice(Math.max(0, text.indexOf('[')))) as Record<string, unknown>[]
  return list.filter(s => s.isArchived !== true)
}

async function listLocal($: EngineInterface): Promise<Record<string, unknown>[]> {
  const home = ((await $.env.get('HOME')) ?? (await $.env.get('USERPROFILE')) ?? '.').replace(/\\/g, '/')
  const dir = `${home}/.claude/sessions`
  if (!osName) osName = ((await $.process.run(['uname', '-s']).catch(() => null))?.stdout ?? '').trim().toLowerCase()
  type Entry = { pid?: number; sessionId?: string; hostSessionId?: string; name?: string; cwd?: string; kind?: string; status?: string; pidDomain?: string; statusUpdatedAt?: number; updatedAt?: number }
  const entries: Entry[] = []
  for (const f of await $.fs.list(dir).catch(() => [])) {
    if (!f.name.endsWith('.json')) continue
    try {
      const d = JSON.parse(String(await $.fs.read(`${dir}/${f.name}`))) as Entry
      // каталог может синкаться между машинами: процессы другой ОС отсекает pidDomain
      if (d.kind === 'interactive' && d.sessionId && d.pid && (!d.pidDomain || !osName || d.pidDomain === osName)) entries.push(d)
    } catch {
      // файл пишется прямо сейчас — прочтём на следующем опросе
    }
  }
  // файл умершего процесса может остаться: живые сверяем с ps одним вызовом
  const alive = new Set<number>()
  if (entries.length) {
    const ps = await $.process.run(['ps', '-o', 'pid=', '-p', entries.map(d => d.pid).join(',')]).catch(() => null)
    for (const line of (ps?.stdout ?? '').split('\n')) if (line.trim()) alive.add(Number(line.trim()))
  }
  const me = entries.find(d => d.sessionId === selfKey)
  if (me?.name && (await read($, self)).title !== me.name) await update($, self, s => ({ ...s, title: String(me.name) }))
  return entries
    .filter(d => alive.has(d.pid!) && d.sessionId !== selfKey)
    .map(d => ({
      sessionId: d.hostSessionId ?? d.sessionId,
      title: d.name,
      cwd: d.cwd,
      isRunning: d.status === 'busy',
      lastActivityAt: new Date(d.statusUpdatedAt ?? d.updatedAt ?? 0).toISOString(),
    }))
}

async function refresh($: EngineInterface) {
  try {
    raw = source === 'local' ? await listLocal($) : await listApp($)
    await classify($)
    if ((await read($, error)) !== '') await update($, error, () => '')
  } catch (err) {
    const msg = `список диалогов недоступен: ${err instanceof Error ? err.message : String(err)}`
    if ((await read($, error)) !== msg) await update($, error, () => msg)
  }
}

async function poll($: EngineInterface) {
  await refresh($)
  const busy = (await read($, rows)).some(r => r.state === 'running')
  timer?.cancel()
  timer = $.clock.after(busy ? POLL_BUSY : POLL_IDLE, () => void poll($))
}

async function loadSelf($: EngineInterface) {
  try {
    const res = await $.mcp.call(SERVER, 'get_session', { session_id: 'self' })
    const text = res.content.map(c => ('text' in c ? c.text : '')).join('')
    const me = JSON.parse(text.slice(Math.max(0, text.indexOf('{')))) as { title?: string }
    if (me.title) await update($, self, s => ({ ...s, title: String(me.title) }))
  } catch {
    // инструменты сессий ещё не подключены — заголовок подтянется со следующим стартом
  }
}

async function markSeen($: EngineInterface, ids: string[]) {
  const now = await $.clock.now()
  const seen = await seenMap($)
  for (const id of ids) seen[id] = now
  // не копить вечно: только диалоги из текущего списка
  const live = new Set(raw.map(s => String(s.sessionId ?? '')))
  await $.store.set(K_SEEN, Object.fromEntries(Object.entries(seen).filter(([k]) => live.has(k) || ids.includes(k))))
  await classify($)
}

async function openRow($: EngineInterface, id: string) {
  const r = (await read($, rows)).find(x => x.id === id)
  if (!r) return
  if (r.link === null || !LINK_RE.test(r.link)) {
    // список из ~/.claude/sessions: переключить окно отсюда нечем — подсказать, где диалог
    $.ui.toast(`«${r.title}» — ${r.project}`)
  } else {
    const run = await $.process.run(['open', r.link])
    if (run.exitCode !== 0) $.ui.toast(`Не удалось открыть: ${run.stderr.trim().slice(0, 120)}`)
  }
  if (r.state === 'waiting') await markSeen($, [r.id])
}

async function setPane($: EngineInterface, open: boolean) {
  if (open) await $.ui.open({ id: PANE, title: 'Диалоги' })
  else await $.ui.close({ id: PANE })
  await update($, isPaneOpen, () => open)
  // выбор VS Code свой у диалога: общий флаг (на десктопе панель обычно закрыта) закрыл бы её
  if (!isVsCode) await $.store.set(K_PANE, open)
}

async function activate($: EngineInterface) {
  selfKey = await $.session.id()
  const surfaces = await $.session.surfaces()
  if (surfaces.length > 0 && surfaces.every(s => s === 'terminal' || s === 'vscode')) source = 'local'
  isVsCode = surfaces.length > 0 && surfaces.every(s => s === 'vscode')
  if (source === 'app') void loadSelf($)
  if (isVsCode) {
    if (!isVsPaneShown) {
      isVsPaneShown = true
      await setPane($, true)
    }
  } else if ((await $.store.get(K_PANE)) === true) await setPane($, true)
  void poll($)
}

function sorted(list: HubRow[], state: HubState) {
  const xs = list.filter(r => r.state === state)
  // работающие — по началу работы: время активности у них меняется каждую секунду, строки прыгали бы
  return state === 'running' ? xs.sort((a, b) => (a.runningSince ?? 0) - (b.runningSince ?? 0)) : xs.sort((a, b) => b.at - a.at)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'sessions', description: 'Боковая панель диалогов: открыть или закрыть' })
    if (e.isInteractive) {
      void activate($)
      void $.clock.after(5_000, () => void loadTune($))
    }
    return next(e)
  })

  // приложение показало диалог (в том числе давно запущенный): подтянуть выбор панели и свежий список
  on('session.attach', async ($, e, next) => {
    void activate($)
    void loadTune($)
    return next(e)
  })

  on('command.run', { command: 'sessions' }, async $ => {
    const open = !(await read($, isPaneOpen))
    await setPane($, open)
    return { text: open ? 'Панель диалогов открыта.' : 'Панель диалогов закрыта. Открыть снова: /sessions' }
  })

  on('ui.close', async ($, e, next) => {
    // крестик панели — то же, что выключить её в полосе
    if (e.id === PANE && e.origin.kind !== 'unload') {
      await update($, isPaneOpen, () => false)
      if (!isVsCode) await $.store.set(K_PANE, false)
    }
    return next(e)
  })

  // этого диалога нет в списке — его статус из собственных ходов
  on('turn.start', async ($, e, next) => {
    const now = await $.clock.now()
    await update($, self, s => ({ ...s, isRunning: true, since: s.since ?? now }))
    return next(e)
  })
  on('turn.complete', async ($, e, next) => {
    if (!e.agentId) await update($, self, s => ({ ...s, isRunning: false, since: null }))
    return next(e)
  })

  // Нажатия — по ключу кнопки, а не по обработчику: после перерисовки обработчик другой, ключ тот же
  on('ui.press', { plugin: 'session-hub' }, async ($, e, next) => {
    const k = e.element
    if (k.startsWith('open-')) await openRow($, k.slice(5))
    else if (k === 'seen-all') await markSeen($, (await read($, rows)).filter(r => r.state === 'waiting').map(r => r.id))
    else if (k.startsWith('seen-')) await markSeen($, [k.slice(5)])
    else if (k === 'band-list') await update($, isListOpen, v => !v)
    else if (k === 'band-done' || k === 'pane-done') {
      const show = !(await read($, isDoneShown))
      await update($, isDoneShown, () => show)
      if (show && k === 'band-done') await update($, isListOpen, () => true)
    } else if (k === 'band-pane' || k === 'band-more') await setPane($, !(await read($, isPaneOpen)))
    else if (k === 'self') return { element: k }
    else if (!(await pressTune($, k))) return next(e)
    return { element: k }
  })

  // Полоса над полем ввода (приложение и терминал). Свёрнута — одна строка с числами; раскрыта — плашки.
  // Десктоп рисует у кнопки только подпись (label), детей-Text отбрасывает: числа, точки и проект стоят
  // рядом с кнопкой отдельным текстом.
  //  ▸ Диалоги  В работе 2  Ждут ответа 1  Завершены 7                                   панель
  //  ● Рефакторинг модов · mods        12 мин  ✓   ● Анализ платформы · ai           сейчас
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    // полоса общая с другими модами: своё сверху, их часть под ним
    const below = await next(e)
    if (e.props.hasSurvey || (e.surface !== 'desktop' && e.surface !== 'terminal')) return below
    const list = await read($, rows)
    const err = await read($, error)
    const me = await read($, self)
    if (list.length === 0 && err === '' && !me.isRunning) return below
    const { Box, Button, Text } = $.ui.resolve(e)
    // терминал не смешивает полупрозрачные цвета: там без заливок и рамок, только цвет текста
    const isDesktop = e.surface === 'desktop'
    const now = await $.clock.now()
    const isOpen = await read($, isListOpen)
    const showDone = await read($, isDoneShown)
    const paneOpen = await read($, isPaneOpen)
    const by = { running: sorted(list, 'running'), waiting: sorted(list, 'waiting'), done: sorted(list, 'done') }
    const noop = () => undefined
    const runCount = by.running.length + (me.isRunning ? 1 : 0)

    const badge = (state: HubState, n: number) => {
      const t = n > 0 ? TONE[state] : TONE.done
      return isDesktop ? (
        <Text backgroundColor={t.badge} color={t.ink} bold>{` ${n} `}</Text>
      ) : (
        <Text color={n > 0 ? t.dot : undefined} dimColor={n === 0} bold>{String(n)}</Text>
      )
    }

    // сетка: столько колонок, сколько влезает плашек не уже MIN_PILL
    const avail = Math.max(MIN_PILL, (e.props.bodyColumns || 80) - 2)
    const per = Math.max(1, Math.floor((avail + 1) / (MIN_PILL + 1)))
    const pillW = Math.floor((avail - (per - 1)) / per)
    // рамка и поля 4, точка 2, колонки времени и «✓»; на десктопе ещё поля нативной кнопки
    const room = Math.max(8, pillW - (isDesktop ? 9 : 6) - TIME_W - READ_W)

    const pill = (r: HubRow | null) => {
      const state: HubState = r ? r.state : 'running'
      const t = TONE[state]
      const title = cut(r ? r.title : me.title || 'Этот диалог', room)
      const projectRoom = room - title.length - 3
      const project = r ? r.project : 'этот диалог'
      const when = r ? (r.state === 'running' ? ago(now, r.runningSince ?? now) : ago(now, r.at)) : ago(now, me.since ?? now)
      return (
        <Box
          key={`pill-${r ? r.id : 'self'}`}
          width={pillW}
          flexDirection="row"
          paddingX={1}
          overflow="hidden"
          {...(isDesktop
            ? {
                backgroundColor: t.bg,
                // скругление: рамка round цвета фона — её не видно, остаются мягкие углы
                borderStyle: 'round' as const,
                borderColor: t.bg,
                ...(r ? { hover: { backgroundColor: t.hover, borderColor: t.hover } } : {}),
              }
            : {})}
        >
          <Box flexShrink={0}>
            <Text color={t.dot}>● </Text>
          </Box>
          <Box flexShrink={0}>
            <Button key={r ? `open-${r.id}` : 'self'} plain label={title} dimColor={state === 'done'} onPress={noop} />
          </Box>
          <Box flexGrow={1} flexShrink={1} minWidth={0}>
            <Text dimColor wrap="truncate-end">
              {projectRoom >= 6 ? ` · ${cut(project, projectRoom)}` : ''}
            </Text>
          </Box>
          <Box width={TIME_W} flexShrink={0} justifyContent="flex-end">
            <Text color={t.ink}>{when}</Text>
          </Box>
          <Box width={READ_W} flexShrink={0} justifyContent="center">
            {r && r.state === 'waiting' ? <Button key={`seen-${r.id}`} plain label="✓" dimColor onPress={noop} /> : null}
          </Box>
        </Box>
      )
    }

    return (
      <Box flexDirection="column">
        <Box flexDirection="row" justifyContent="space-between" paddingX={1}>
          <Box flexDirection="row" flexShrink={1} minWidth={0} gap={2}>
            <Button key="band-list" plain label={`${isOpen ? '▾' : '▸'} Диалоги`} onPress={noop} />
            <Text>
              {'В работе '}
              {badge('running', runCount)}
            </Text>
            <Text>
              {'Ждут ответа '}
              {badge('waiting', by.waiting.length)}
            </Text>
            <Box flexDirection="row" flexShrink={0}>
              <Button key="band-done" plain label={showDone && isOpen ? 'Скрыть завершённые' : 'Завершены'} dimColor={!(showDone && isOpen)} onPress={noop} />
              <Text> </Text>
              {badge('done', by.done.length)}
            </Box>
          </Box>
          <Box flexShrink={0} marginLeft={2}>
            <Button key="band-pane" plain label={paneOpen ? 'закрыть панель' : 'панель'} dimColor onPress={noop} />
          </Box>
        </Box>
        {err !== '' && (
          <Box paddingX={1}>
            <Text color="error" wrap="truncate-end">{err}</Text>
          </Box>
        )}
        {isOpen && (
          <Box flexDirection="row" flexWrap="wrap" columnGap={1} rowGap={isDesktop ? 1 : 0} paddingX={1} marginTop={isDesktop ? 1 : 0}>
            {by.waiting.map(pill)}
            {me.isRunning && pill(null)}
            {by.running.map(pill)}
            {showDone && by.done.slice(0, BAND_DONE_MAX).map(pill)}
            {runCount + by.waiting.length === 0 && !showDone && <Text dimColor>никто не работает и не ждёт ответа</Text>}
          </Box>
        )}
        {isOpen && showDone && by.done.length > BAND_DONE_MAX && (
          <Box paddingX={1}>
            <Button key="band-more" plain label={`ещё ${by.done.length - BAND_DONE_MAX} — в панели`} dimColor onPress={noop} />
          </Box>
        )}
        {below}
      </Box>
    )
  })

  // Боковая панель — по кнопке «панель» или /sessions; в VS Code открывается сама. Карточки по разделам
  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Button, Text } = $.ui.resolve(e)
    const isDesktop = e.surface === 'desktop'
    const list = await read($, rows)
    const me = await read($, self)
    const err = await read($, error)
    const now = await $.clock.now()
    const showDone = await read($, isDoneShown)
    const tuneView = await readTune($)
    const noop = () => undefined
    const width = Math.max(20, (e.props.bodyColumns ?? 40) - 8)
    const frame = (bg: string, hover?: string) =>
      isDesktop
        ? { backgroundColor: bg, borderStyle: 'round' as const, borderColor: bg, ...(hover ? { hover: { backgroundColor: hover, borderColor: hover } } : {}) }
        : {}

    const card = (r: HubRow) => {
      const t = TONE[r.state]
      const when = r.state === 'running' ? `работает ${ago(now, r.runningSince ?? now)}` : ago(now, r.at)
      return (
        <Box key={`card-${r.id}`} flexDirection="column" paddingX={1} marginBottom={1} {...frame(t.bg, t.hover)}>
          <Button key={`open-${r.id}`} plain label={cut(r.title, width)} dimColor={r.state === 'done'} onPress={noop} />
          <Box flexDirection="row">
            <Box flexGrow={1} flexShrink={1} minWidth={0}>
              <Text wrap="truncate-end">
                <Text color={t.dot}>● </Text>
                <Text dimColor>{r.project}</Text>
              </Text>
            </Box>
            <Box flexShrink={0} marginLeft={1}>
              <Text color={t.ink}>{when}</Text>
            </Box>
            {r.state === 'waiting' && (
              <Box flexShrink={0} marginLeft={1}>
                <Button key={`seen-${r.id}`} plain label="✓ прочитано" dimColor onPress={noop} />
              </Box>
            )}
          </Box>
        </Box>
      )
    }

    const section = (state: HubState, items: HubRow[], count: number, action?: { key: string; label: string }) => (
      <Box key={`sec-${state}`} flexDirection="column" marginBottom={1}>
        <Box flexDirection="row" justifyContent="space-between" marginBottom={1}>
          <Text>
            <Text bold>{TONE[state].name} </Text>
            {isDesktop ? (
              <Text backgroundColor={TONE[state].badge} color={TONE[state].ink} bold>{` ${count} `}</Text>
            ) : (
              <Text color={TONE[state].dot} bold>{String(count)}</Text>
            )}
          </Text>
          {action && <Button key={action.key} plain label={action.label} dimColor onPress={noop} />}
        </Box>
        {items.map(card)}
      </Box>
    )

    const waiting = sorted(list, 'waiting')
    const running = sorted(list, 'running')
    const done = sorted(list, 'done')
    return (
      <Box flexDirection="column">
        {err !== '' && <Text color="error">{err}</Text>}
        <Box key="card-self" flexDirection="column" paddingX={1} marginBottom={1} {...frame(me.isRunning ? TONE.running.bg : TONE.done.bg)}>
          <Text wrap="truncate-end">{me.title || 'Этот диалог'}</Text>
          <Text dimColor>этот диалог · {me.isRunning ? `работает ${ago(now, me.since ?? now)}` : 'ждёт тебя'}</Text>
        </Box>
        {/* советы раз в неделю — сверху: под длинным списком их не видно */}
        {tuneSection(tuneView, { Box, Text, Button }, { isDesktop, width })}
        {section('waiting', waiting, waiting.length, waiting.length > 1 ? { key: 'seen-all', label: 'прочитать все' } : undefined)}
        {section('running', running, running.length)}
        {section('done', showDone ? done.slice(0, 30) : [], done.length, { key: 'pane-done', label: showDone ? 'скрыть' : 'показать' })}
      </Box>
    )
  })
}
