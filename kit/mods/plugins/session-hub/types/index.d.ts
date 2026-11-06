/** Где диалог стоит в панели */
export type HubState = 'running' | 'background' | 'waiting' | 'done'

/** Строка панели: только то, что видно, — поэтому снимок меняется (и панель перерисовывается) редко */
export type HubSession = {
  id: string
  title: string
  project: string
  link: string | null
  state: HubState
  /** «10 мин назад»; у работающих пусто */
  when: string
  /** Когда начал работать (мс эпохи) — секундомер карточки; у остальных null */
  startedAt: number | null
}

/** Снимок списка: пишется одним значением и только когда изменился — одна перерисовка на изменение */
export type HubView = {
  sessions: HubSession[]
  error: string | null
  /** Показан снимок из кэша прошлых диалогов, свежий ещё не пришёл */
  isStale: boolean
  /** Хоть один снимок есть — иначе «Загружаю…» */
  hasData: boolean
}

/** Этот диалог: list_sessions его не возвращает, поэтому статус — из его собственных ходов */
export type HubSelf = {
  title: string
  isRunning: boolean
  /** Начало текущего хода этого диалога */
  startedAt: number | null
}

/** Совет раздела «Подстройка» (tuning-suggest.py) */
export type TuneItem = { id: string; title: string; what: string; why: string; prompt: string }
/** Советы панели и когда собраны (мс) */
export type TuneView = { items: TuneItem[]; at: number }

declare module 'claude-code' {
  interface PluginState {
    'session-hub': {
      view: HubView
      self: HubSelf
      showAllDone: boolean
      /** Завершённые скрыты — общий выбор всех диалогов */
      hideDone: boolean
      /** Панель открыта в этом диалоге — подпись кнопки на полосе над полем ввода */
      paneOpen: boolean
      /** Список диалогов раскрыт в полосе над полем ввода — свой у каждого диалога, при старте свёрнут */
      listOpen: boolean
      /** Полоса над полем ввода включена: диалог в приложении Claude */
      bandOn: boolean
      /** Завершённые показаны в полосе — свой у каждого диалога */
      bandShowDone: boolean
      /** Раздел «Подстройка»: советы tuning-suggest.py */
      tune: TuneView
    }
  }
}
