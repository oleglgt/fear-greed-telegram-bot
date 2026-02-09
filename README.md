# Telegram bot: CNN Fear & Greed Index

Простой бот на Python, который по команде `/fg` отправляет текущее значение Fear & Greed Index из CNN.

## 1) Установка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2) Токен бота

Создайте бота через `@BotFather` и возьмите токен.

```bash
export TELEGRAM_BOT_TOKEN="ваш_токен"
```

## 3) Запуск

```bash
python bot.py
```

Или двойным кликом по файлу `start.command` в Finder (macOS).

## 4) Команды в Telegram

- `/start` — справка
- `/fg` — текущий Fear & Greed Index
