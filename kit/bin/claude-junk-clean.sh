#!/usr/bin/env bash
# Ежедневная уборка того, что Claude Code сам не чистит (запуск по расписанию — kit/launchd/claude-junk-clean.plist.template).
# Транскрипты и debug Claude Code удаляет сам (cleanupPeriodDays) — их не трогаем.
# Правила и сроки — rules/file-hygiene.md.
#
#   claude-junk-clean.sh              убрать
#   claude-junk-clean.sh --dry-run    только напечатать, что было бы убрано
#
# Трогает только файлы внутри ~/.claude (или $CLAUDE_HOME) по узким маскам; за его пределы не выходит.
set -uo pipefail

C="${CLAUDE_HOME:-$HOME/.claude}"
DRY=false; [ "${1:-}" = "--dry-run" ] && DRY=true
LOG_DIR="${CLAUDE_JUNK_CLEAN_LOG_DIR:-$HOME/.local/state/claude-junk-clean}"; mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/clean.log"
# Журнал не растёт бесконечно: больше 1 МБ — оставить последние 2000 строк
[ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 1048576 ] && tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }
# Удаляет файлы из списка (NUL-разделённый stdin), печатает количество
del_files() { local n=0 f; while IFS= read -r -d '' f; do n=$((n+1)); $DRY || rm -f -- "$f"; done; echo "$n"; }
# Удаляет пустые каталоги (список — NUL-разделённый stdin, глубокие первыми), печатает количество
del_empty_dirs() { local n=0 d; while IFS= read -r -d '' d; do n=$((n+1)); $DRY || rmdir -- "$d" 2>/dev/null; done; echo "$n"; }

[ -d "$C" ] || { log "нет папки $C, нечего убирать"; exit 0; }

# 1. Планы: старше 14 дней — в plans/archive, архив старше 90 дней — удалить
P="$C/plans"; A="$P/archive"
if [ -d "$P" ]; then
  $DRY || mkdir -p "$A"
  n=0
  while IFS= read -r -d '' f; do n=$((n+1)); $DRY || mv -- "$f" "$A/"; done \
    < <(find "$P" -maxdepth 1 -type f -name '*.md' ! -name 'INDEX.md' -mtime +14 -print0)
  log "plans: в архив $n"
  log "plans/archive: удалено $(find "$A" -type f -mtime +90 -print0 2>/dev/null | del_files)"
fi

# 2. Пустые служебные каталоги
log "session-env: пустых каталогов $(find "$C/session-env" -mindepth 1 -maxdepth 1 -type d -empty -mtime +1 -print0 2>/dev/null | del_empty_dirs)"
log "projects (временные cwd): пустых каталогов $(find "$C/projects" -mindepth 1 -depth -type d -empty \( -path "$C/projects/-private-*" -o -path "$C/projects/-tmp*" \) -mtime +1 -print0 2>/dev/null | del_empty_dirs)"

# 3. Ручные бэкапы конфигов живут 7 дней. ctime, а не mtime: cp -p сохраняет старое mtime,
#    и свежая копия выглядела бы старой
log "бэкапы *.bak* старше 7 дней: $(find "$C" "$C/rules" "$C/hooks" "$C/bin" -maxdepth 1 -type f \( -name '*.bak' -o -name '*.bak[-.]*' \) -ctime +7 -print0 2>/dev/null | del_files)"
log "транскрипты-копии *.jsonl.bak*/*.dup* старше 7 дней: $(find "$C/projects" -maxdepth 2 -type f \( -name '*.jsonl.bak*' -o -name '*.jsonl.dup*' \) -ctime +7 -print0 2>/dev/null | del_files)"

# 4. История правок файлов старше 30 дней (как у транскриптов)
log "file-history старше 30 дней: $(find "$C/file-history" -type f -mtime +30 -print0 2>/dev/null | del_files)"
$DRY || find "$C/file-history" -mindepth 1 -depth -type d -empty -exec rmdir {} + 2>/dev/null

# 5. Обломки установки плагинов
n=0
while IFS= read -r -d '' d; do n=$((n+1)); $DRY || rm -rf -- "$d"; done \
  < <(find "$C/plugins/cache" -mindepth 1 -maxdepth 1 -type d -name 'temp_git_*' -mtime +1 -print0 2>/dev/null)
log "plugins/cache temp_git_*: $n"

$DRY && log "dry-run, ничего не удалено"
exit 0
