# Постоянный запуск на Ubuntu VPS

Рекомендуемая минимальная конфигурация: Ubuntu 24.04, 1 vCPU, 1 ГБ RAM, 10–15 ГБ диска и публичный IPv4 в европейском дата-центре.

Проект должен находиться в `/opt/telegram-gift-watch`. Файл `secrets.json` передаётся на сервер отдельно, не добавляется в Git и должен иметь права `0600`.

После загрузки проекта выполните от `root`:

```sh
cd /opt/telegram-gift-watch
sh deploy/install.sh
```

Полезные команды:

```sh
systemctl status gift-watch
journalctl -u gift-watch -f
systemctl restart gift-watch
systemctl stop gift-watch
```

Сервис запускается вместе с сервером и перезапускается через 30 секунд после аварийного завершения. После установки отдельно проверьте доступ сервера к Tonnel и отправку тестового сообщения через Telegram Bot API.
