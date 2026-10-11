#!/bin/zsh
cd -- "${0:A:h}" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv || exit 1
  .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt || exit 1
fi
echo "Запущен мониторинг Tonnel. Окно нужно оставить открытым. Ctrl+C — остановить."
.venv/bin/python tonnel_watch.py --send
read '?Нажми Enter, чтобы закрыть окно.'
