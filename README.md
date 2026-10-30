# Boxing Life — Telegram-бот

## Запуск на Render
1. Отзовите старый токен в @BotFather (`/revoke`) и получите новый.
2. Залейте эти файлы в GitHub-репозиторий (бот.py, render.yaml, requirements.txt, .gitignore).
3. Render → New → Blueprint → выберите репозиторий.
4. В Environment задайте:
   - `BOT_TOKEN` — новый токен
   - `CREATOR_TELEGRAM_ID` — ваш Telegram ID (узнать у @userinfobot)
5. Deploy. Проверка: `https://<ваш-сервис>.onrender.com/health` должен ответить текстом.

## Важно про бесплатный план
- Free web-сервис засыпает через 15 минут без HTTP-запросов, и бот перестаёт отвечать.
  Заведите бесплатный монитор (UptimeRobot / cron-job.org), который каждые 5 минут
  открывает `/health`.
- Файловая система бесплатного плана временная: база SQLite стирается при деплое/перезапуске.
  Для постоянного хранения — платный план + диск (см. комментарии в render.yaml).

## Локальный запуск
```
pip install -r requirements.txt
BOT_TOKEN=... CREATOR_TELEGRAM_ID=... python bot.py
```

## Админ-команды (только для CREATOR_TELEGRAM_ID)
`админ панель`, `/give <id> <сумма>`, `/ban <id>`, `/unban <id>`
