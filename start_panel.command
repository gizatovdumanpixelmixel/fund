#!/bin/bash
# Запуск панели Фонда двойным кликом.
# Файл должен лежать в одной папке с app.py и pipeline.py.

cd "$(dirname "$0")" || exit 1

if ! python3 -c "import streamlit" 2>/dev/null; then
  echo "Первый запуск: устанавливаю Streamlit (один раз, около минуты)..."
  python3 -m pip install streamlit || python3 -m pip install --user streamlit || {
    echo "Не удалось установить Streamlit. Пришлите этот текст Claude."
    read -r -p "Нажмите Enter, чтобы закрыть окно."
    exit 1
  }
fi

echo "Панель запускается. Страница откроется в браузере: http://localhost:8501"
echo "Чтобы выключить панель, закройте это окно."

(sleep 3 && open "http://localhost:8501") &
python3 -m streamlit run app.py \
  --server.headless true \
  --server.address localhost \
  --server.port 8501 \
  --browser.gatherUsageStats false
