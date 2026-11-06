import { atom, read, update } from 'claude-code'
import type { Hook, Register, RenderChildren } from 'claude-code'

import type { HubSelf, HubSession, HubState, HubView } from '../types'
import { loadTune, pressTune, readTune, tuneSection } from './tuning'

// Список диалогов — из ~/.claude/sessions: инструменты сессий приложения спрашивают разрешение на каждый вызов

const PANE = 'session-hub'
const VERSION = '2.0.0'
const POLL_IDLE = 15_000
const POLL_BUSY = 5_000
// Новый диалог стартует раньше, чем приложение подключит ему инструменты сессий: первые неудачи — повтор, не ошибка
const POLL_RETRY = 3_000
const SILENT_FAILURES = 4
const DONE_PREVIEW = 5
const HOUR = 60 * 60_000
// Ход закончился, а активность диалога растёт — работают фоновые субагенты; столько держим его «в работе»
const BG_HOLD = 60_000
// Активность без замеченного конца хода: «ждёт ответа» только после стольких секунд тишины
const QUIET = 30_000
// Как часто живой диалог сверяется с общим переключателем панели
const SYNC_MS = 2_000
const SEEN_SELF_EVERY = 30_000
const LOG_KEEP = 120
const SEEN_KEEP = 200

// Открыта панель и скрыты ли завершённые — одно на все диалоги: файл, а не $.store, чтобы каждый
// процесс диалога читал свежее значение с диска, а не свою копию
const FLAG_FILE = '.claude/session-hub/pane.json'
const K_CACHE = 'lastSessions'
const K_SEEN = 'seenAt'
const K_BASELINE = 'baselineAt'
const K_LOG = 'log'
// Когда диалог начал работать — общий для всех панелей, чтобы секундомеры совпадали
const K_RUN = 'runStart'
// Ключи версии 1.1.0, когда открытость помнилась по диалогу
const LEGACY_PREFIX = 'open:'
const LEGACY_DEFAULT = 'defaultOpen'

// Кнопки полосы над полем ввода
const BAND_PANE = 'band-pane'
const BAND_LIST = 'band-list'
const BAND_DONE = 'band-done'
// «ещё N — в боковой панели»: своё имя, не BAND_PANE — два элемента с одним ключом приложение не рисует,
// и полоса пропадала целиком
const BAND_MORE = 'band-more'
// Название этого диалога в его же полосе: кнопка без действия, ради одинакового отступа
const BAND_SELF = 'band-self'
// Сколько завершённых показывает полоса; остальные — в боковой панели
const BAND_DONE_MAX = 10

const EMPTY: HubView = { sessions: [], error: null, isStale: false, hasData: false }

const view = atom({ plugin: 'session-hub', key: 'view' } as const, EMPTY)
const self = atom({ plugin: 'session-hub', key: 'self' } as const, {
  title: 'Этот диалог',
  isRunning: false,
  startedAt: null as number | null,
})
const showAllDone = atom({ plugin: 'session-hub', key: 'showAllDone' } as const, false)
const hideDone = atom({ plugin: 'session-hub', key: 'hideDone' } as const, false)
// Открыта ли панель в этом диалоге — по нему полоса над полем ввода подписывает кнопку
const paneOpen = atom({ plugin: 'session-hub', key: 'paneOpen' } as const, false)
// Раскрыт ли список диалогов в полосе над полем ввода — общий выбор всех диалогов, как и панель
const listOpen = atom({ plugin: 'session-hub', key: 'listOpen' } as const, false)
// Полоса над полем ввода включена: диалог в приложении Claude, инструменты сессий есть. Состояние, а не
// переменная: хук полосы читает его первым, и когда оно включается, движок перерисовывает полосу сам
const bandOn = atom({ plugin: 'session-hub', key: 'bandOn' } as const, false)
// Завершённые в полосе показаны — свой у каждого диалога, при старте скрыты
const bandShowDone = atom({ plugin: 'session-hub', key: 'bandShowDone' } as const, false)

type Dollar = Parameters<Hook<'session.start'>>[0]

type RawSession = {
  sessionId: string
  title?: string
  cwd?: string
  link?: string
  isRunning?: boolean
  isArchived?: boolean
  lastActivityAt?: string
}

type Seen = { isRunning: boolean; at: number }
type PaneFlag = { isOpen: boolean; hideDone: boolean; isListOpen: boolean; at: number; by: string }

// Модульные переменные живут, пока жив процесс диалога; приложение усыпляет простаивающие диалоги,
// и при возврате модуль загружается заново — всё, что должно пережить сон, лежит в $.store и в файле
let cliId = ''
let selfId = ''
let flagPath = ''
let isReady = false
// Опрос списка идёт: пока открыта панель или видна полоса над полем ввода
let paneUp = false
// Полоса над полем ввода живёт только в приложении Claude: там есть инструменты сессий
let bandLive = false
// Полоса сама запустила активацию — один раз на процесс (после /reload-plugins session.start может не прийти)
let isBandKicked = false
let timer: { cancel: () => void } | null = null
let syncTimer: { cancel: () => void } | null = null
let failures = 0
let cachedJson = ''
let flagMtime = -1
let selfSeenAt = 0
// Закрытие, которое сделала сама синхронизация, — не действие человека, флаг не трогает
let isSyncClosing = false
// Последний сырой список и что видели на прошлом опросе — по ним отличаем конец хода от фоновых субагентов
let raw: RawSession[] = []
const prev = new Map<string, Seen>()
// Кнопки панели между перерисовками — те же объекты. Движок выдаёт кнопке номер при её создании,
// а нажатие приходит с номером, который кнопка имела на экране. Создай её заново — и клик, пришедший
// во время перерисовки (а панель перерисовывается, например, когда забирает клавиатуру после первого
// клика), уходит по старому номеру и теряется. Пока подпись та же — отдаём прежний объект с прежним номером
const buttons = new Map<string, { sig: string; el: any }>()
const bgUntil = new Map<string, number>()
const sawStop = new Set<string>()

function projectOf(cwd: string | undefined): string {
  if (!cwd) return '—'
  const parts = cwd.split('/').filter(Boolean)
  const last = parts[parts.length - 1] ?? cwd
  // Сессии без папки живут во временном каталоге приложения
  return cwd.includes('/scratch-workspaces/') ? 'без папки' : last
}

// Крупные ступени: текст меняется редко — и панель не перерисовывается каждую минуту.
// Любая перерисовка выдаёт кнопкам новые адреса, и нажатие, пришедшее по старому, теряется
function ago(ms: number): string {
  const min = Math.max(0, Math.floor(ms / 60_000))
  if (min < 5) return 'сейчас'
  if (min < 60) return `${Math.floor(min / 5) * 5} мин`
  const h = Math.floor(min / 60)
  if (h < 24) return `${h} ч`
  return `${Math.floor(h / 24)} дн`
}

// Сколько работает — минутами: меняется раз в минуту, и панель перерисовывается так же редко
function workedFor(ms: number): string {
  const min = Math.max(0, Math.floor(ms / 60_000))
  // Короткие подписи: колонка времени в плашке полосы фиксированной ширины (TIME_W)
  if (min < 1) return 'сейчас'
  if (min < 60) return `${min} мин`
  return `${Math.floor(min / 60)} ч ${min % 60} м`
}

function activityOf(s: RawSession): number {
  return s.lastActivityAt ? Date.parse(s.lastActivityAt) : 0
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

let osDomain = ''

type LocalEntry = {
  pid?: number
  sessionId?: string
  hostSessionId?: string
  name?: string
  cwd?: string
  kind?: string
  status?: string
  pidDomain?: string
  statusUpdatedAt?: number
  updatedAt?: number
}

async function listLocal($: Dollar): Promise<RawSession[]> {
  const home = (await $.env.get('HOME')) ?? (await $.env.get('USERPROFILE')) ?? '.'
  const dir = `${home.replace(/\\/g, '/')}/.claude/sessions`
  if (!osDomain) {
    const u = await $.process.run(['uname', '-s']).catch(() => null)
    osDomain = (u?.stdout ?? '').trim().toLowerCase() || 'unknown'
  }
  const entries: LocalEntry[] = []
  for (const f of await $.fs.list(dir)) {
    if (!f.name.endsWith('.json')) continue
    try {
      const d = JSON.parse(String(await $.fs.read(`${dir}/${f.name}`))) as LocalEntry
      // Каталог синкается между машинами: чужие процессы отсекает pidDomain
      if (d.kind === 'interactive' && d.sessionId && d.pid && (!d.pidDomain || d.pidDomain === osDomain)) entries.push(d)
    } catch {
      // файл пишется прямо сейчас — следующий опрос прочтёт
    }
  }
  // Файл умершего процесса может остаться: живые сверяем с ps одним вызовом
  const alive = new Set<number>()
  if (entries.length) {
    const ps = await $.process.run(['ps', '-o', 'pid=', '-p', entries.map(d => d.pid).join(',')]).catch(() => null)
    for (const line of (ps?.stdout ?? '').split('\n')) if (line.trim()) alive.add(Number(line.trim()))
  }
  const me = entries.find(d => d.sessionId === cliId)
  if (me) {
    selfId = me.hostSessionId ?? me.sessionId ?? cliId
    await setSelf($, { title: me.name || 'Этот диалог' })
  }
  return entries
    .filter(d => alive.has(d.pid!) && d.sessionId !== cliId)
    .map(d => ({
      sessionId: d.hostSessionId ?? d.sessionId!,
      title: d.name,
      cwd: d.cwd,
      isRunning: d.status === 'busy',
      lastActivityAt: new Date(d.statusUpdatedAt ?? d.updatedAt ?? 0).toISOString(),
    }))
}

async function setView($: Dollar, next: HubView): Promise<void> {
  const cur = await read($, view)
  if (JSON.stringify(cur) === JSON.stringify(next)) return
  await update($, view, () => next)
}

async function setSelf($: Dollar, patch: Partial<HubSelf>): Promise<void> {
  const cur = await read($, self)
  if (Object.entries(patch).every(([k, v]) => (cur as any)[k] === v)) return
  await update($, self, s => ({ ...s, ...patch }))
}

async function seenMap($: Dollar): Promise<Record<string, number>> {
  const v: any = await $.store.get(K_SEEN)
  return v && typeof v === 'object' ? v : {}
}

async function markSeen($: Dollar, marks: Record<string, number>): Promise<void> {
  const seen = { ...(await seenMap($)) }
  for (const [id, at] of Object.entries(marks)) {
    if (id && at > (seen[id] ?? 0)) seen[id] = at
  }
  const kept = Object.entries(seen)
    .sort((a, b) => b[1] - a[1])
    .slice(0, SEEN_KEEP)
  await $.store.set(K_SEEN, Object.fromEntries(kept))
}

// Раскладка по секциям из последнего сырого списка; зовётся после опроса и после «Прочитано»
async function classify($: Dollar): Promise<void> {
  const now = await $.clock.now()
  const seen = await seenMap($)
  const baseline = Number(await $.store.get(K_BASELINE)) || 0
  const stored: any = await $.store.get(K_RUN)
  const runStart: Record<string, number> = stored && typeof stored === 'object' ? { ...stored } : {}
  let isRunChanged = false
  const sessions: HubSession[] = raw.map(s => {
    const at = activityOf(s)
    let state: HubState
    if (s.isRunning) state = 'running'
    else if ((bgUntil.get(s.sessionId) ?? 0) > now) state = 'background'
    else if (at > (seen[s.sessionId] ?? baseline)) {
      // Конец хода видели — ждёт сразу; не видели — ждёт, когда активность затихнет
      state = sawStop.has(s.sessionId) || now - at >= QUIET ? 'waiting' : 'background'
    } else state = 'done'
    const isLive = state === 'running' || state === 'background'
    if (isLive && !runStart[s.sessionId]) {
      runStart[s.sessionId] = now
      isRunChanged = true
    } else if (!isLive && runStart[s.sessionId]) {
      delete runStart[s.sessionId]
      isRunChanged = true
    }
    return {
      id: s.sessionId,
      title: s.title || 'Без названия',
      project: projectOf(s.cwd),
      link: s.link ?? null,
      state,
      when: isLive ? workedFor(now - (runStart[s.sessionId] ?? now)) : ago(now - at),
      startedAt: isLive ? (runStart[s.sessionId] ?? null) : null,
    }
  })
  if (isRunChanged) await $.store.set(K_RUN, runStart)
  await setView($, { sessions, error: null, isStale: false, hasData: true })
}

// Диалог на экране — значит, его последний ответ видели: в панелях других диалогов он не «ждёт ответа»
async function markSelfSeenIfShown($: Dollar): Promise<void> {
  if (!selfId) return
  const now = await $.clock.now()
  if (now - selfSeenAt < SEEN_SELF_EVERY) return
  selfSeenAt = now
  await markSeen($, { [selfId]: now })
}

async function refresh($: Dollar, isManual = false): Promise<boolean> {
  try {
    const list: RawSession[] = await listLocal($)
    const now = await $.clock.now()
    raw = list.filter(s => !s.isArchived && s.sessionId !== selfId)

    for (const s of raw) {
      const id = s.sessionId
      const at = activityOf(s)
      const p = prev.get(id)
      if (s.isRunning) {
        sawStop.delete(id)
        bgUntil.delete(id)
      } else if (p?.isRunning) {
        sawStop.add(id)
      } else if (p && at > p.at) {
        // Ход давно кончился, а активность растёт: это фоновые субагенты, а не ответ человеку
        bgUntil.set(id, now + BG_HOLD)
        sawStop.delete(id)
      }
      prev.set(id, { isRunning: Boolean(s.isRunning), at })
    }

    failures = 0
    await classify($)

    const json = JSON.stringify(raw)
    if (json !== cachedJson) {
      cachedJson = json
      const cur = await read($, view)
      await $.store.set(K_CACHE, { sessions: cur.sessions, updatedAt: now })
    }
    return true
  } catch (err) {
    failures++
    if (isManual || failures > SILENT_FAILURES) {
      const cur = await read($, view)
      await setView($, { ...cur, error: errorText(err) })
    }
    return false
  }
}

function schedule($: Dollar, ms: number): void {
  timer?.cancel()
  timer = $.clock.after(ms, () => void tick($))
}

async function tick($: Dollar): Promise<void> {
  timer = null
  if (!paneUp) return
  const ok = await refresh($)
  if (!paneUp) return
  void markSelfSeenIfShown($)

  const v = await read($, view)
  const isBusy = (await read($, self)).isRunning || v.sessions.some(s => s.state === 'running' || s.state === 'background')
  const next = !ok && failures <= SILENT_FAILURES ? POLL_RETRY  : isBusy ? POLL_BUSY : POLL_IDLE
  schedule($, next)
}

function startPolling($: Dollar): void {
  paneUp = true
  failures = 0
  schedule($, 1)
}

function stopPolling(): void {
  paneUp = false
  timer?.cancel()
  timer = null
}

// Короткий журнал открытий и закрытий в $.store (ключ log) — чтобы видеть, кто и почему закрыл панель
// Записи идут по очереди: параллельные «прочитать — дописать — записать» затирали друг друга,
// и при быстрых кликах журнал терял строки
let logChain: Promise<void> = Promise.resolve()
function logEvent($: Dollar, event: string): Promise<void> {
  logChain = logChain.then(async () => {
    const list: any = await $.store.get(K_LOG)
    const entry = `${new Date(await $.clock.now()).toISOString()} ${cliId.slice(0, 8)} v${VERSION} ${event}`
    await $.store.set(K_LOG, [...(Array.isArray(list) ? list : []), entry].slice(-LOG_KEEP))
  }).catch(() => undefined)
  return logChain
}

async function readFlag($: Dollar): Promise<PaneFlag> {
  try {
    const flag = JSON.parse(String(await $.fs.read(flagPath)))
    if (typeof flag?.isOpen === 'boolean') return { hideDone: false, isListOpen: false, at: 0, by: '', ...flag }
  } catch {
    // файла ещё нет — панель закрыта: полоса над полем ввода уже показывает диалоги
  }
  return { isOpen: false, hideDone: false, isListOpen: false, at: 0, by: '' }
}

async function writeFlag($: Dollar, patch: Partial<Pick<PaneFlag, 'isOpen' | 'hideDone' | 'isListOpen'>>): Promise<void> {
  // Путь к файлу появляется в ensureReady; до неё писать некуда
  if (!flagPath) return
  const flag: PaneFlag = { ...(await readFlag($)), ...patch, at: await $.clock.now(), by: cliId }
  await $.fs.write(flagPath, JSON.stringify(flag))
  flagMtime = -1
}

async function setPaneOpen($: Dollar, isOpen: boolean): Promise<void> {
  if ((await read($, paneOpen)) !== isOpen) await update($, paneOpen, () => isOpen)
}

async function showPane($: Dollar): Promise<void> {
  await $.ui.open({ id: PANE, title: 'Диалоги' })
  await setPaneOpen($, true)
  if (!paneUp) startPolling($)
}

// Открыть или закрыть панель — сразу во всех диалогах (через общий файл); true — теперь открыта
async function togglePane($: Dollar, why: string): Promise<boolean> {
  await ensureReady($)
  if ((await $.ui.panes()).some(p => p.id === PANE)) {
    // Флаг закрытия пишет хук ui.close
    await $.ui.close({ id: PANE })
    return false
  }
  await writeFlag($, { isOpen: true })
  await showPane($)
  if (!syncTimer) scheduleSync($)
  void logEvent($, `open (${why})`)
  return true
}

async function closePaneBySync($: Dollar): Promise<void> {
  isSyncClosing = true
  try {
    await $.ui.close({ id: PANE })
  } finally {
    isSyncClosing = false
  }
}

// Привести панель этого диалога к общему переключателю
async function applyFlag($: Dollar, why: string): Promise<void> {
  const flag = await readFlag($)
  if (flag.hideDone !== (await read($, hideDone))) await update($, hideDone, () => flag.hideDone)
  const isUp = (await $.ui.panes()).some(p => p.id === PANE)
  await setPaneOpen($, isUp)
  if (flag.isOpen && !isUp) {
    await showPane($)
    void logEvent($, `open (${why})`)
  } else if (!flag.isOpen && isUp) {
    await closePaneBySync($)
    void logEvent($, `close (${why})`)
  } else if (isUp && !paneUp) {
    startPolling($)
  }
}

// Живые диалоги сверяются с файлом раз в 2 с: открыли или закрыли в одном — то же во всех.
// Усыплённые приложением диалоги сверятся, когда проснутся или когда их снова откроют
function scheduleSync($: Dollar): void {
  syncTimer?.cancel()
  syncTimer = $.clock.after(SYNC_MS, () => void syncTick($))
}

async function syncTick($: Dollar): Promise<void> {
  try {
    const stat = await $.fs.stat(flagPath).catch(() => undefined)
    const mtime = stat?.mtimeMs ?? 0
    if (mtime !== flagMtime) {
      flagMtime = mtime
      await applyFlag($, 'sync')
    }
  } finally {
    scheduleSync($)
  }
}

async function dropLegacyKeys($: Dollar): Promise<void> {
  for (const key of await $.store.keys()) {
    if (key.startsWith(LEGACY_PREFIX) || key === LEGACY_DEFAULT) await $.store.delete(key)
  }
}

// Один раз на процесс; зовётся из session.start и из любых событий, что придут раньше него
// (диалог, которому обновление плагина досталось без нового session.start)
async function ensureReady($: Dollar): Promise<void> {
  if (isReady) return
  isReady = true
  cliId = await $.session.id()
  const home = (await $.env.get('HOME')) ?? (await $.env.get('USERPROFILE')) ?? '.'
  flagPath = `${home.replace(/\\/g, '/')}/${FLAG_FILE}`
  // Первый запуск мода: «ждут ответа» — то, что закончило ход за последний час
  if (typeof (await $.store.get(K_BASELINE)) !== 'number') await $.store.set(K_BASELINE, (await $.clock.now()) - HOUR)
  void dropLegacyKeys($)

  // Пока свежий список не пришёл — последний известный, с пометкой «из кэша»
  if (!(await read($, view)).hasData) {
    const cached: any = await $.store.get(K_CACHE)
    if (Array.isArray(cached?.sessions) && typeof cached.sessions[0]?.state === 'string') {
      await setView($, { ...EMPTY, sessions: cached.sessions, isStale: true, hasData: true })
    }
  }
}

// Мод работает только в приложении Claude; в терминале и в VS Code (и терминале IDE) он ничего не показывает
async function isInApp($: Dollar): Promise<boolean> {
  return (await $.session.surfaces()).includes('desktop')
}

// Подхватить общий переключатель и начать сверку
async function activate($: Dollar, why: string): Promise<void> {
  if (!(await isInApp($))) return
  await ensureReady($)
  await applyFlag($, why)
  flagMtime = (await $.fs.stat(flagPath).catch(() => undefined))?.mtimeMs ?? 0
  if (!syncTimer) scheduleSync($)
  // Полосе нужны свежие числа и при закрытой панели
  bandLive = true
  if (!(await read($, bandOn))) await update($, bandOn, () => true)
  if (!paneUp) startPolling($)
}

async function openSession($: Dollar, s: HubSession): Promise<void> {
  const t0 = await $.clock.now()
  if (!s.link) {
    // Список из ~/.claude/sessions: переключить окно отсюда нечем — подсказать, где диалог
    if (s.state === 'waiting') await markRead($, [s.id])
    $.ui.toast(`«${s.title}» — ${s.project}`)
    return
  }
  // Сначала открыть, потом отметки: так перерисовка от отметки не мешает самому переходу
  const isWindows = (await $.env.get('OS')) === 'Windows_NT'
  // macOS — open; Windows — системный обработчик ссылок (у Windows нет команды open)
  const argv = isWindows ? ['rundll32', 'url.dll,FileProtocolHandler', s.link] : ['open', s.link]
  const run = await $.process.run(argv)
  if (run.exitCode !== 0) $.ui.toast(`Не удалось открыть: ${run.stderr.trim()}`)
  // Замер: сколько занял запуск ссылки
  const t1 = await $.clock.now()
  void logEvent($, `click ${s.id.slice(6, 14)} open=${t1 - t0}ms exit=${run.exitCode}`)
  if (s.state === 'waiting') await markRead($, [s.id])
}

// «Прочитано»: диалог уходит в «Завершены», пока в нём снова не появится активность
async function markRead($: Dollar, ids: string[]): Promise<void> {
  const now = await $.clock.now()
  const marks: Record<string, number> = {}
  for (const id of ids) {
    const s = raw.find(r => r.sessionId === id)
    marks[id] = Math.max(s ? activityOf(s) : 0, now)
    sawStop.delete(id)
  }
  await markSeen($, marks)
  await classify($)
}

// Что делает кнопка панели — по её имени; false — имя не наше
async function pressKey($: Dollar, key: string): Promise<boolean> {
  if (key.startsWith('tune.')) return pressTune($, key)
  const v = await read($, view)
  const byId = (prefix: string) => v.sessions.find(x => `${prefix}${x.id}` === key)
  let isKnown = true
  if (key.startsWith('open-') || key.startsWith('go-')) {
    const s = byId('open-') ?? byId('go-')
    if (s) await openSession($, s)
  } else if (key.startsWith('read-')) {
    const s = byId('read-')
    if (s) await markRead($, [s.id])
  } else if (key === 'clear-waiting') {
    await markRead($, v.sessions.filter(x => x.state === 'waiting').map(x => x.id))
  } else if (key === 'toggle-hide') {
    await toggleHideDone($)
  } else if (key === 'toggle-done') {
    await update($, showAllDone, x => !x)
  } else if (key === 'refresh') {
    await refresh($, true)
  } else if (key === 'close') {
    await $.ui.close({ id: PANE })
  } else isKnown = false
  return isKnown
}

// Раскрытый список — свой у каждого диалога и при старте свёрнут: общим он раскрывался сам в каждом новом
async function toggleList($: Dollar): Promise<void> {
  await update($, listOpen, x => !x)
}

// «Завершены»: показать или скрыть их в полосе; показ заодно раскрывает список
async function toggleBandDone($: Dollar): Promise<void> {
  const next = !(await read($, bandShowDone))
  await update($, bandShowDone, () => next)
  if (next && !(await read($, listOpen))) await update($, listOpen, () => true)
}

// Любая кнопка мода — панели или полосы — по имени; false — имя не наше
async function press($: Dollar, key: string): Promise<boolean> {
  if (key === BAND_SELF) return true
  if (key === BAND_LIST) await toggleList($)
  else if (key === BAND_DONE) await toggleBandDone($)
  else if (key === BAND_MORE) {
    if (!(await $.ui.panes()).some(p => p.id === PANE)) await togglePane($, 'band-more')
  }
  else if (key === BAND_PANE) await togglePane($, 'band')
  else return pressKey($, key)
  return true
}

type BtnOpts = { dimColor?: boolean; role?: 'dismiss' }

// Тон раздела: карточки слегка подкрашены его цветом, бейдж числа — тем же цветом погуще.
// Полупрозрачные заливки ложатся и на светлую, и на тёмную тему. Общие для панели и полосы
type Tone = { card: string; hover: string; badge: string; ink: string; dot: string | null }
const TONE: Record<'live' | 'waiting' | 'done', Tone> = {
  live: { card: '#22c55e14', hover: '#22c55e29', badge: '#22c55e26', ink: '#15803d', dot: '#22c55e' },
  waiting: { card: '#eab3081f', hover: '#eab30838', badge: '#eab30833', ink: '#a16207', dot: '#eab308' },
  done: { card: '#8080800d', hover: '#8080801f', badge: '#8080801f', ink: '#71717a', dot: null },
}
const toneOf = (state: HubState): Tone => (state === 'waiting' ? TONE.waiting : state === 'done' ? TONE.done : TONE.live)

// Кнопка между перерисовками — тот же объект, пока подпись та же (см. buttons). scope — поверхность и место
// children — подпись из Text (бейдж числа, тусклая часть); label тогда только называет кнопку и входит в подпись кэша
function cachedBtn(
  $: Dollar,
  Button: any,
  scope: string,
  key: string,
  label: string,
  opts: BtnOpts = {},
  children?: RenderChildren,
): any {
  const id = `${scope}|${key}`
  const sig = JSON.stringify([label, opts, children === undefined])
  const hit = buttons.get(id)
  if (hit && hit.sig === sig) return hit.el
  // Обработчик — запасной: нажатия разбирает хук ui.press по имени, и устаревать тут нечему
  const el =
    children === undefined ? (
      <Button key={key} plain {...opts} onPress={() => void press($, key)}>
        {label}
      </Button>
    ) : (
      <Button key={key} plain label={label} {...opts} onPress={() => void press($, key)}>
        {children}
      </Button>
    )
  buttons.set(id, { sig, el })
  return el
}

async function toggleHideDone($: Dollar): Promise<void> {
  const next = !(await read($, hideDone))
  await update($, hideDone, () => next)
  await writeFlag($, { hideDone: next })
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    // Терминал, VS Code: прежние версии писали числа в строку статуса — убрать их
    $.ui.status(undefined)
    if (e.surface !== 'desktop' && !(await isInApp($))) return next(e)
    await $.command.register({
      name: 'sessions',
      description: 'Открыть или закрыть панель диалогов во всех диалогах',
    })
    // Новый диалог — полоса свёрнута, завершённые скрыты
    await update($, listOpen, () => false)
    await update($, bandShowDone, () => false)
    await activate($, 'start')
    void $.clock.after(5_000, () => void loadTune($))
    void $.clock.after(3_000, () => void markSelfSeenIfShown($))
    return next(e)
  })

  // Приложение показывает диалог (в том числе старый, давно запущенный) — сверить панель сразу
  on('session.attach', async ($, e, next) => {
    if (e.surface !== 'desktop') return next(e)
    void activate($, `attach-${e.surface}`).then(() => paneUp && schedule($, 1))
    void loadTune($)
    return next(e)
  })

  on('command.run', { command: 'sessions' }, async $ => {
    const isOpen = await togglePane($, 'command')
    return {
      text: isOpen
          ? 'Панель диалогов открыта во всех диалогах.'
          : 'Панель диалогов закрыта во всех диалогах. Открыть снова: /sessions или кнопкой над полем ввода',
    }
  })

  on('ui.close', async ($, e, next) => {
    if (e.id === PANE) {
      await setPaneOpen($, false)
      // Полоса над полем ввода продолжает показывать числа — опрос ей нужен
      if (!bandLive || e.origin.kind === 'unload') stopPolling()
      if (e.origin.kind !== 'unload' && !isSyncClosing && isReady) {
        await writeFlag($, { isOpen: false })
        void logEvent($, `close (${e.origin.kind})`)
      }
    }
    return next(e)
  })

  // Этот диалог list_sessions не возвращает: его статус — из собственных ходов (у сабагентов turn.start нет)
  on('turn.start', async ($, e, next) => {
    if (!isReady) void activate($, 'turn')
    if (!(await read($, self)).isRunning) await setSelf($, { isRunning: true, startedAt: await $.clock.now() })
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (!e.agentId) {
      await setSelf($, { isRunning: false, startedAt: null })
      void markSelfSeenIfShown($)
    }
    return next(e)
  })

  // Кнопки панели — по имени (key), а не по адресу обработчика: каждая перерисовка выдаёт кнопкам новые
  // адреса, и нажатие по адресу из прошлой отрисовки терялось («ui_press not handled»). Имя кнопки
  // от перерисовки не меняется, поэтому нажатие разбирается здесь и до обработчика не доходит
  on('ui.press', { plugin: 'session-hub' }, async ($, e, next) => {
    const isOurs = e.requestId === PANE || e.component === 'AbovePrompt'
    if (!isOurs || !(await press($, e.element))) return next(e)
    void logEvent($, `press ${e.element.slice(0, 22)}`)
    return { element: e.element }
  })

  // Полоса над полем ввода — в стиле боковой панели. Свёрнута — одна строка; раскрывает её левая кнопка
  // с числами, рядом — «Завершены». Плашки — сеткой по ширине полосы, скруглены рамкой цвета фона:
  //  ▸ Диалоги  В работе 2  Ждут ответа 1  Завершены 7                                    панель
  //  ╭ ● Рефакторинг модов · mods   10 мин ✓ ╮ ╭ ● Анализ платформы · platform-ai  12 мин ╮
  // Нажатия — только кнопками: невидимый слой live.tsx в 2.1.309 падал и выводил ошибку поверх полосы.
  // Десктоп рисует у кнопки только подпись (label), детей-Text отбрасывает — поэтому числа, точки и проект
  // стоят рядом с кнопкой отдельным текстом, а не внутри неё
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    // Без инструментов сессий (терминал, VS Code) чисел нет — полосу не рисуем
    const isOn = await read($, bandOn)
    if (!bandLive && e.surface === 'desktop' && !isBandKicked) {
      isBandKicked = true
      void activate($, 'band')
    }
    // Полоса только во вкладке Code приложения; после перезагрузки мода isOn уже включён, а опрос
    // перезапускает активация выше
    if (e.props.hasSurvey || !isOn || e.surface !== 'desktop') return next(e)
    // Терминал не смешивает полупрозрачные цвета: там без заливок и рамок, только цвет текста
    const isDesktop = e.surface === 'desktop'
    const { Box, Text, Button } = $.ui.resolve(e)
    const v = await read($, view)
    const me = await read($, self)
    const isPaneOpen = await read($, paneOpen)
    const isListOpen = await read($, listOpen)
    const isDoneShown = await read($, bandShowDone)
    const running = v.sessions
      .filter(s => s.state === 'running' || s.state === 'background')
      .sort((a, b) => (a.startedAt ?? 0) - (b.startedAt ?? 0) || a.id.localeCompare(b.id))
    const waiting = v.sessions.filter(s => s.state === 'waiting')
    const done = v.sessions.filter(s => s.state === 'done')
    const runningCount = running.length + (me.isRunning ? 1 : 0)
    const DONE_DOT = '#a1a1aa'
    const scope = `${e.surface}|band`
    const btn = (key: string, label: string, opts: BtnOpts = {}, children?: RenderChildren) =>
      cachedBtn($, Button, scope, key, label, opts, children)

    const badge = (n: number, tone: Tone) => {
      const t = n > 0 ? tone : TONE.done
      return isDesktop ? (
        <Text backgroundColor={t.badge} color={t.ink} bold>
          {` ${n} `}
        </Text>
      ) : (
        <Text color={n > 0 ? (t.dot ?? undefined) : undefined} dimColor={n === 0} bold>
          {String(n)}
        </Text>
      )
    }

    // Сетка: столько колонок, сколько влезает плашек не уже MIN_PILL
    const MIN_PILL = 36
    const GAP = 1
    const avail = Math.max(MIN_PILL, (e.props.bodyColumns || 80) - 2)
    const per = Math.max(1, Math.floor((avail + GAP) / (MIN_PILL + GAP)))
    const pillW = Math.floor((avail - GAP * (per - 1)) / per)
    const fit = (text: string, room: number) => (text.length <= room ? text : `${text.slice(0, Math.max(1, room - 1))}…`)

    // Плашка: точка, название, проект, время; у ждущих «✓» — прочитано. Нажатие на название открывает диалог.
    // Кнопка не умеет обрезать подпись сама — режем заранее под ширину плашки, иначе время уходит на новую строку
    // Колонки плашки фиксированной ширины: время прижато вправо, место под «✓» есть у всех — время
    // не ездит от плашки к плашке. Самый длинный текст времени — «23 ч 59 м» (workedFor) и «59 мин» (ago)
    const TIME_W = 9
    // «✓» — нативная кнопка со своими полями: в 3 символа она обрезалась
    const READ_W = 4
    const now = await $.clock.now()
    const pill = (s: HubSession | null) => {
      const state: HubState = s ? s.state : 'running'
      const tone = toneOf(state)
      const isWaiting = state === 'waiting'
      const isDone = state === 'done'
      const title = s ? s.title : me.title
      const project = s ? [s.project, state === 'background' ? 'субагенты' : ''].filter(Boolean).join(' · ') : 'этот диалог'
      const when = s ? s.when : me.startedAt ? workedFor(now - me.startedAt) : ''
      const key = s ? s.id : 'self'
      // рамка 2, поля 2, точка 2, колонки времени и «✓»; поля нативной кнопки десктопа съедает запас колонок
      const room = Math.max(8, pillW - (isDesktop ? 7 : 4) - TIME_W - READ_W)
      const shownTitle = fit(title, room)
      const projectRoom = room - shownTitle.length - 3
      const shownProject = projectRoom >= 6 ? ` · ${fit(project, projectRoom)}` : ''
      return (
        <Box
          key={`pill-${key}`}
          width={pillW}
          flexDirection="row"
          paddingX={1}
          overflow="hidden"
          {...(isDesktop
            ? {
                backgroundColor: tone.card,
                borderStyle: 'round' as const,
                borderColor: tone.card,
                ...(s ? { hover: { backgroundColor: tone.hover, borderColor: tone.hover } } : {}),
              }
            : {})}
        >
          {/* Десктоп рисует у кнопки только подпись: точка и проект — отдельным текстом рядом.
              Название этого диалога — тоже кнопка (без действия): у кнопки свои поля, и без неё
              название стояло бы левее, чем у соседних плашек */}
          <Box flexShrink={0}>
            <Text color={tone.dot ?? DONE_DOT}>● </Text>
          </Box>
          <Box flexShrink={0}>{btn(s ? `open-${s.id}` : BAND_SELF, shownTitle)}</Box>
          <Box flexGrow={1} flexShrink={1} minWidth={0}>
            <Text dimColor wrap="truncate-end">
              {shownProject}
            </Text>
          </Box>
          <Box width={TIME_W} flexShrink={0} justifyContent="flex-end">
            <Text {...(isDone ? { dimColor: true } : { color: tone.ink })}>{when}</Text>
          </Box>
          <Box width={READ_W} flexShrink={0} justifyContent="center">
            {s && isWaiting ? btn(`read-${s.id}`, '✓', { dimColor: true }) : null}
          </Box>
        </Box>
      )
    }

    const doneShown = done.slice(0, BAND_DONE_MAX)
    const isEmpty = waiting.length === 0 && runningCount === 0 && !(isDoneShown && done.length > 0)
    const isDoneOn = isDoneShown && isListOpen

    return (
      <Box flexDirection="column">
        <Box flexDirection="row" justifyContent="space-between" paddingX={1}>
          <Box flexDirection="row" flexShrink={1} minWidth={0} gap={2}>
            {btn(BAND_LIST, `${isListOpen ? '▾' : '▸'} Диалоги`)}
            {v.hasData ? (
              <Box flexDirection="row" flexShrink={1} minWidth={0} gap={2}>
                <Text wrap="truncate-end">
                  {'В работе '}
                  {badge(runningCount, TONE.live)}
                </Text>
                <Text wrap="truncate-end">
                  {'Ждут ответа '}
                  {badge(waiting.length, TONE.waiting)}
                </Text>
                <Box flexDirection="row" flexShrink={0}>
                  {btn(BAND_DONE, isDoneOn ? 'Скрыть завершённые' : 'Завершены', { dimColor: !isDoneOn })}
                  <Text> </Text>
                  {badge(done.length, TONE.done)}
                </Box>
              </Box>
            ) : (
              <Text dimColor>загружаю…</Text>
            )}
          </Box>
          <Box flexDirection="row" flexShrink={0} marginLeft={2}>
            {btn(BAND_PANE, isPaneOpen ? 'закрыть панель' : 'панель', { dimColor: true })}
          </Box>
        </Box>
        {isListOpen && v.hasData && (
          <Box flexDirection="row" flexWrap="wrap" columnGap={GAP} rowGap={isDesktop ? 1 : 0} paddingX={1} marginTop={isDesktop ? 1 : 0}>
            {waiting.map(s => pill(s))}
            {me.isRunning && pill(null)}
            {running.map(s => pill(s))}
            {isDoneShown && doneShown.map(s => pill(s))}
            {isEmpty && <Text dimColor>никто не работает и не ждёт ответа</Text>}
          </Box>
        )}
        {isListOpen && isDoneShown && done.length > BAND_DONE_MAX && (
          <Box paddingX={1}>{btn(BAND_MORE, `ещё ${done.length - BAND_DONE_MAX} — в боковой панели`, { dimColor: true })}</Box>
        )}
        {isListOpen && v.error && (
          <Box paddingX={1}>
            <Text color="red" wrap="truncate-end">
              Не удалось получить список: {v.error}
            </Text>
          </Box>
        )}
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const btn = (key: string, label: string, opts: BtnOpts = {}) => cachedBtn($, Button, e.surface, key, label, opts)
    const v = await read($, view)
    const me: HubSelf = await read($, self)
    const showAll = await read($, showAllDone)
    const isHidden = await read($, hideDone)
    const tuneView = await readTune($)

    // Работающие — по времени старта: время активности у них меняется каждую секунду, и сортировка по нему
    // переставляла строки туда-обратно
    const running = v.sessions
      .filter(s => s.state === 'running' || s.state === 'background')
      .sort((a, b) => (a.startedAt ?? 0) - (b.startedAt ?? 0) || a.id.localeCompare(b.id))
    const pending = v.sessions.filter(s => s.state === 'waiting')
    const done = v.sessions.filter(s => s.state === 'done')
    const doneShown = showAll ? done : done.slice(0, DONE_PREVIEW)
    const runningCount = running.length + (me.isRunning ? 1 : 0)

    const INSET = 1
    const isDesktop = e.surface === 'desktop'
    // Ширина текста внутри карточки: отступ панели, рамка и поля карточки с обеих сторон
    const inner = Math.max(12, (e.props.bodyColumns || 40) - INSET * 4 - 2)
    // Кнопка не умеет обрезать подпись сама — режем заранее, иначе длинное название вылезает за карточку
    const fit = (text: string, room: number) => (text.length <= room ? text : `${text.slice(0, Math.max(1, room - 1))}…`)


    // Карточка: скругление даёт рамка round того же цвета, что и фон, — её не видно, а углы круглые.
    // В терминале фон не нужен: только подсветка при наведении
    const cardFrame = (tone: Tone, isInteractive: boolean) =>
      isDesktop
        ? {
            backgroundColor: tone.card,
            borderStyle: 'round',
            borderColor: tone.card,
            ...(isInteractive ? { hover: { backgroundColor: tone.hover, borderColor: tone.hover } } : {}),
          }
        : isInteractive
          ? { hover: { backgroundColor: '#3a3a3a' } }
          : {}

    const dotOf = (tone: Tone) => (tone.dot ? <Text color={tone.dot}>● </Text> : null)

    //  ╭────────────────────────────────────╮
    //  │ Анализ структуры платформы         │
    //  │ ● platform-ai · субагенты   12 мин │
    //  ╰────────────────────────────────────╯
    const row = (s: HubSession) => {
      const tone = toneOf(s.state)
      const isLive = s.state === 'running' || s.state === 'background'
      const sub = [s.project, s.state === 'background' ? 'субагенты' : '', s.state === 'waiting' ? s.when : '']
        .filter(Boolean)
        .join(' · ')
      return (
        <Box
          key={`row-${s.id}`}
          flexDirection="column"
          paddingX={INSET}
          marginX={INSET}
          marginBottom={1}
          overflow="hidden"
          {...cardFrame(tone, true)}
        >
          {/* Название и строка проекта — обычные кнопки: клик по ним открывает диалог */}
          {s.state === 'waiting'
            ? cachedBtn($, Button, e.surface, `open-${s.id}`, fit(s.title, inner), {}, <Text bold>{fit(s.title, inner)}</Text>)
            : btn(`open-${s.id}`, fit(s.title, inner))}
          <Box flexDirection="row">
            {dotOf(tone)}
            {/* minWidth 0: без него гибкий блок не сжимается уже своего текста и выталкивает время за край */}
            <Box flexGrow={1} flexShrink={1} minWidth={0}>
              <Text dimColor wrap="truncate-end">
                {sub}
              </Text>
            </Box>
            {isLive && (
              <Box flexShrink={0} marginLeft={1}>
                <Text color={tone.ink}>{s.when}</Text>
              </Box>
            )}
            {s.state === 'done' && (
              <Box flexShrink={0} marginLeft={1}>
                <Text dimColor>{s.when}</Text>
              </Box>
            )}
            {s.state === 'waiting' && (
              <Box flexShrink={0} marginLeft={1}>
                {btn(`read-${s.id}`, '✓ прочитано', { dimColor: true })}
              </Box>
            )}
          </Box>
        </Box>
      )
    }

    const selfRow = (
      <Box key="row-self" flexDirection="column" paddingX={INSET} marginX={INSET} marginBottom={1} {...cardFrame(TONE.live, false)}>
        <Text wrap="truncate-end">{me.title}</Text>
        <Box flexDirection="row">
          {dotOf(TONE.live)}
          <Text dimColor wrap="truncate-end">
            этот диалог
          </Text>
        </Box>
      </Box>
    )

    // Заголовок раздела: название, число в бейдже цвета раздела, тихое действие справа
    const header = (key: string, title: string, count: number, tone: Tone, action?: RenderChildren) => (
      <Box key={`head-${key}`} flexDirection="row" justifyContent="space-between" paddingX={INSET * 2} marginBottom={1}>
        <Text wrap="truncate-end">
          <Text bold>{title} </Text>
          {isDesktop ? (
            <Text backgroundColor={tone.badge} color={tone.ink} bold>
              {` ${count} `}
            </Text>
          ) : (
            <Text dimColor>{count}</Text>
          )}
        </Text>
        {action}
      </Box>
    )

    const empty = (text: string) => (
      <Box paddingX={INSET * 2} marginBottom={1}>
        <Text dimColor>{text}</Text>
      </Box>
    )

    const note = !v.hasData ? 'загружаю…' : v.isStale ? 'обновляю…' : null

    return (
      <Box flexDirection="column">
        {v.error && (
          <Box paddingX={INSET * 2} marginBottom={1}>
            <Text color="red" wrap="truncate-end">
              Не удалось получить список: {v.error}
            </Text>
          </Box>
        )}

        {/* Советы раз в неделю — сверху: внизу, под длинным списком завершённых, их не видно */}
        {tuneSection(tuneView, { Box, Text, Button }, { isDesktop, width: inner, inset: INSET })}

        {header('running', 'В работе', runningCount, TONE.live)}
        {runningCount === 0 && empty('ничего не выполняется')}
        {me.isRunning && selfRow}
        {running.map(row)}

        {header(
          'waiting',
          'Ждут ответа',
          pending.length,
          TONE.waiting,
          pending.length > 1 ? (
            btn('clear-waiting', 'все прочитаны', { dimColor: true })
          ) : undefined,
        )}
        {pending.length === 0 && empty('никто не ждёт')}
        {pending.map(row)}

        {header(
          'done',
          'Завершены',
          done.length,
          TONE.done,
          done.length > 0 ? (
            btn('toggle-hide', isHidden ? 'показать' : 'скрыть', { dimColor: true })
          ) : undefined,
        )}
        {!isHidden && doneShown.map(row)}
        {!isHidden && done.length > DONE_PREVIEW && (
          <Box paddingX={INSET * 2} marginBottom={1}>
            {btn('toggle-done', showAll ? 'свернуть' : `ещё ${done.length - DONE_PREVIEW}`, { dimColor: true })}
          </Box>
        )}

        <Box flexDirection="row" justifyContent="space-between" paddingX={INSET * 2}>
          <Box flexDirection="row" gap={2}>
            {btn('refresh', 'обновить', { dimColor: true })}
            {note && <Text dimColor>{note}</Text>}
          </Box>
          <Box flexDirection="row" gap={2}>
            <Text dimColor>v{VERSION}</Text>
            {btn('close', 'закрыть', { dimColor: true, role: 'dismiss' })}
          </Box>
        </Box>
      </Box>
    )
  })
}
