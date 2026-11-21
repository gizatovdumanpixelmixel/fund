#!/usr/bin/env bash
# Сохраняет данные системы (портфель, сделки, журнал, уроки, отчёты) обратно в репозиторий.
set -e
git config user.name "fund-bot"
git config user.email "fund-bot@users.noreply.github.com"
for path in portfolio.json results.jsonl decisions.jsonl lessons.jsonl rejected_reviews.jsonl \
            usage.jsonl control.json telegram_state.json briefings logs; do
  [ -e "$path" ] && git add -A -- "$path"
done
if git diff --cached --quiet; then
  echo "Изменений нет"
  exit 0
fi
git commit -q -m "$1"
for attempt in 1 2 3; do
  if git pull --rebase -q && git push -q; then
    echo "Данные сохранены"
    exit 0
  fi
  sleep 5
done
echo "Не удалось сохранить данные"
exit 1
