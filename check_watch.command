#!/bin/zsh
cd -- "${0:A:h}" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv || exit 1
  .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt || exit 1
fi
echo "Один цикл без отправки сообщений:"
.venv/bin/python tonnel_watch.py --once
read '?Нажми Enter, чтобы закрыть окно.'
