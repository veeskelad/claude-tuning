#!/usr/bin/env bash
# Строка состояния Claude Code: <модель>  context:X%  5h:[полоса] X% время  7d:[полоса] X% время
# - Без текущей папки: проекты переключаются, и шум только мешает.
# - context — сколько занято окно контекста: растёт к 100 %, потом сжатие; по нему видно, когда пора /clear.
# - 5-часовое и недельное окно лимитов с полосой, процентом использования и временем до сброса
#   (показываются, когда Claude Code их передаёт; на тарифах без окон строка короче).
# Нужен jq. Подключение — ключ statusLine в ~/.claude/settings.json:
#   "statusLine": {"type": "command", "command": "bash \"$HOME/.claude/statusline.sh\""}

input=$(cat)

if ! command -v jq >/dev/null 2>&1; then
  printf 'нужен jq\n'
  exit 0
fi

RESET="\033[0m"

# 24-битные цвета
C_MODEL="\033[38;2;180;180;180m"   # светло-серый
C_CTX="\033[38;2;255;167;38m"      # янтарный
C_5H="\033[38;2;240;152;164m"      # коралловый
C_7D="\033[38;2;129;199;132m"      # мягкий зелёный
C_BAR_FILL="\033[38;2;200;200;200m"
C_BAR_EMPTY="\033[38;2;70;70;70m"

SEP="  "

# --- Данные ---
model=$(jq -r '.model.display_name // "?"' <<<"$input" 2>/dev/null)
[ -z "$model" ] && model="?"

ctx_used=$(jq -r '.context_window.used_percentage // empty' <<<"$input" 2>/dev/null)

# Лимиты: пробуем разные имена полей (формат Claude Code менялся).
five_used=$(jq -r '
  .rate_limits.five_hour.used_percentage //
  .rate_limits["5h"].used_percentage //
  .rate_limits.five_hour.percent_used //
  empty' <<<"$input" 2>/dev/null)
five_reset=$(jq -r '
  .rate_limits.five_hour.resets_at //
  .rate_limits["5h"].resets_at //
  empty' <<<"$input" 2>/dev/null)

week_used=$(jq -r '
  .rate_limits.seven_day.used_percentage //
  .rate_limits.weekly.used_percentage //
  .rate_limits["7d"].used_percentage //
  .rate_limits.seven_day.percent_used //
  empty' <<<"$input" 2>/dev/null)
week_reset=$(jq -r '
  .rate_limits.seven_day.resets_at //
  .rate_limits.weekly.resets_at //
  .rate_limits["7d"].resets_at //
  empty' <<<"$input" 2>/dev/null)

# --- Помощники ---
# Целая часть процента; всё, что не число, — 0.
to_pct() {
  local v=${1%.*}
  [[ "$v" =~ ^[0-9]+$ ]] || v=0
  printf '%s' "$v"
}

# Полоса фиксированной ширины. Аргументы: процент, ширина
draw_bar() {
  local pct=$1 width=$2
  [ "$pct" -gt 100 ] && pct=100
  local filled=$(( pct * width / 100 ))
  local empty=$(( width - filled ))
  local bar="${C_BAR_FILL}" i
  for ((i=0; i<filled; i++)); do bar="${bar}█"; done
  bar="${bar}${C_BAR_EMPTY}"
  for ((i=0; i<empty; i++)); do bar="${bar}░"; done
  printf '%s' "${bar}${RESET}"
}

# «Время до сброса» в виде 1h31m / 6d14h / 12m.
# Аргумент — Unix epoch (целое) или ISO 8601 (на случай, если формат сменится).
format_until() {
  local target=$1
  [ -z "$target" ] && { printf -- '—'; return; }
  local target_epoch
  if [[ "$target" =~ ^[0-9]+$ ]]; then
    target_epoch=$target
  else
    # macOS: date -j -f; Linux: date -d.
    target_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "${target%%.*}Z" "+%s" 2>/dev/null) || \
    target_epoch=$(date -d "$target" "+%s" 2>/dev/null) || target_epoch=0
  fi
  local secs days hours mins
  secs=$(( target_epoch - $(date +%s) ))
  if [ "$secs" -le 0 ]; then printf '0m'; return; fi
  days=$(( secs / 86400 ))
  hours=$(( (secs % 86400) / 3600 ))
  mins=$(( (secs % 3600) / 60 ))
  if [ "$days" -gt 0 ]; then
    printf '%dd%dh' "$days" "$hours"
  elif [ "$hours" -gt 0 ]; then
    printf '%dh%dm' "$hours" "$mins"
  else
    printf '%dm' "$mins"
  fi
}

# --- Вывод ---
out="${C_MODEL}${model}${RESET}"
out="${out}${SEP}${C_CTX}context:$(to_pct "$ctx_used")%${RESET}"

if [ -n "$five_reset" ]; then
  p=$(to_pct "$five_used")
  out="${out}${SEP}${C_5H}5h:${RESET}$(draw_bar "$p" 10)${C_5H} ${p}% $(format_until "$five_reset")${RESET}"
fi

if [ -n "$week_reset" ]; then
  p=$(to_pct "$week_used")
  out="${out}${SEP}${C_7D}7d:${RESET}$(draw_bar "$p" 10)${C_7D} ${p}% $(format_until "$week_reset")${RESET}"
fi

printf "%b\n" "$out"
