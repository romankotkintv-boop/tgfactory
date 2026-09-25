# Контент-завод для Telegram-каналов

Один код ведёт три канала: 🇷🇺 маркетинг для малого бизнеса, 🇺🇿 IELTS/английский, 🇧🇷 скидки «дом и быт» (Shopee).
Цикл каждые 10 минут: сбор → генерация (Claude API) → одобрение кнопкой (если включено) → публикация по расписанию.

## Что где
| Файл | Зачем |
|---|---|
| `config/channels.yaml` | каналы, расписание, источники, фильтры — меняется без кода |
| `prompts/*.md` | стиль и правила каждого канала |
| `.env` | ключи и токены (создаётся из `.env.example`, chmod 600) |
| `factory/` | код: `sources` сбор, `llm` текст, `images` картинки, `telegram` публикация, `core` конвейер |
| `tests/` | тесты: `python -m pytest -q` |

## Установка на новый VPS (Ubuntu)
```bash
scp tgfactory.zip root@SERVER:/root/ && ssh root@SERVER
apt-get install -y unzip && unzip tgfactory.zip && cd tgfactory
sudo bash install.sh
nano /opt/tgfactory/.env        # заполнить ключи
```
Старт в `DRY_RUN=1`: посты пишутся в `/opt/tgfactory/outbox/`, в Telegram ничего не уходит.

## Боевой запуск — по шагам
1. Создать 3 бота в @BotFather, добавить каждого админом своего канала с правом публикации.
2. Написать каждому боту `/start` со своего аккаунта (иначе бот не сможет прислать пост на одобрение).
3. Узнать свой chat_id (@userinfobot) → `ADMIN_CHAT_ID`.
4. Заполнить `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` (точный id из console.anthropic.com).
5. Проверка: `sudo -u tgfactory bash -c "cd /opt/tgfactory && .venv/bin/python -m factory generate"` → смотреть `outbox/`.
6. Сначала `TG_CHANNEL_*` = закрытый тестовый канал, `DRY_RUN=0`, неделя наблюдения.
7. Переключить на боевые каналы.

## Команды
```bash
python -m factory status              # сколько постов в очереди / опубликовано
python -m factory collect [канал]     # собрать сырьё
python -m factory generate [канал]    # сделать посты (до 3 за запуск)
python -m factory publish-now канал   # опубликовать следующий пост вне расписания
python -m factory run                 # полный цикл (его запускает cron)
```

## Определение готовности (DoD)
- [ ] `pytest` — все тесты зелёные.
- [ ] 3 дня в тестовом канале без ошибок в `factory.log` и без уведомлений ❌.
- [ ] Каждый канал публикует во все свои слоты.
- [ ] UZ: носитель языка вычитал 10 постов, правки внесены в `prompts/uz_ielts.md`.
- [ ] BR: в каждом посте есть партнёрская ссылка и `#publi`.
- [ ] Бэкап базы появляется в `backups/` каждую ночь.

## Известные ограничения (честно)
- **Shopee:** имена полей GraphQL взяты из неофициального SDK — сверить в официальном explorer
  (https://open-api.affiliate.shopee.com.br/explorer/v2) после получения ключей. Если поле называется иначе — поправить `SHOPEE_QUERY` в `factory/sources.py`.
- **Цена «было»** в BR-постах вычисляется из процента скидки Shopee. Проверка на фейковую скидку работает после ~7 дней накопления истории цен.
- **Mercado Livre и Amazon** — только вручную (нет API для партнёрских ссылок / нужен порог продаж).
- **Статистика просмотров** не собирается: Bot API её не отдаёт. Смотреть в TGStat или в самом канале.
- **RSS**: ленты проверены 25.09.2026; из тестовой среды часть сайтов была недоступна по сетевой политике — проверить `collect` на сервере.
- **Отбор тем** сейчас — фильтр по ключевым словам (дёшево). ИИ-скоринг можно добавить позже.
- Не храни `.env` и токены на старом Hetzner после инцидента — только на новом сервере.

## Бесплатный режим: GitHub Actions + Gemini (без VPS и без оплаты API)
Файл `.github/workflows/factory.yml` запускает завод каждые 30 минут на серверах GitHub.
Приватный репозиторий на бесплатном тарифе: 2000 минут Actions в месяц; завод тратит ~1 мин за запуск (~1440 мин/мес).
1. Создать приватный репозиторий на github.com → загрузить содержимое архива (кнопка «Add file → Upload files», перетащить папки).
2. Settings → Secrets and variables → Actions:
   - Secrets: `GEMINI_API_KEY`, `ADMIN_CHAT_ID`, `TG_BOT_RU`, `TG_CHANNEL_RU`, `TG_BOT_UZ`, `TG_CHANNEL_UZ`, `TG_BOT_BR`, `TG_CHANNEL_BR` (позже `SHOPEE_APP_ID`, `SHOPEE_SECRET`).
   - Variables: `GEMINI_MODEL` (id модели из Google AI Studio), `DRY_RUN` = `1` на первый прогон, потом `0`.
3. Actions → tgfactory → Run workflow (поле publish_now = `all`) — первые посты уйдут сразу.
Стартовые посты (`seed/`) загружаются в очередь один раз; дальше завод генерирует новые через Gemini.
Лимиты бесплатного Gemini смотреть в Google AI Studio (Rate limits) — я их не подтвердил.
