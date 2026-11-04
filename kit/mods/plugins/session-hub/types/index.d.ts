export type HubState = 'running' | 'waiting' | 'done'

/** Строка списка: только то, что рисуется, — снимок меняется редко, полоса перерисовывается редко */
export type HubRow = {
  id: string
  title: string
  project: string
  link: string | null
  state: HubState
  /** lastActivityAt, мс */
  at: number
  /** с какого момента работает (мс); у не работающих null */
  runningSince: number | null
}

export type HubSelf = { title: string; isRunning: boolean; since: number | null }

declare module 'claude-code' {
  interface PluginState {
    'session-hub': {
      rows: Shaped<HubRow[]>
      self: HubSelf
      error: string
      isListOpen: boolean
      isDoneShown: boolean
      isPaneOpen: boolean
    }
  }
}
