#!/bin/zsh
cd -- "${0:A:h}" || exit 1
python3 connect_bot.py
read '?Нажми Enter, чтобы закрыть окно.'
