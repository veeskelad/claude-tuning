/** idle — ничего; offer — показан вопрос; writing — Claude пишет документ; error — перенос не удался */
export type Phase = 'idle' | 'offer' | 'writing' | 'error'

declare module 'claude-code' {
  interface PluginState {
    handoff: {
      phase: Phase
      /** С какого процента контекста снова предложить перенос */
      offerAt: number
      percent: number
      /** Текст ошибки для полосы */
      note: string
      /** Документ передачи и когда его заказали (мс эпохи) — переживают перезагрузку мода */
      doc: string
      askedAt: number
    }
  }
}
