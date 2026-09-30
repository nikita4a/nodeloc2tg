# nodeloc2tg — бот: китайские техно-форумы → перевёл → Telegram

Парсит 5 китайских форумов, переводит на русский через LLM,
фильтрует мусор и постит в телеграм-канал. Работает автономно,
переживает падения сети/прокси, дедуп на SQLite.

## Что делает

- **5 источников:**
  - NodeLoc (Discourse JSON API) — VPS/AI/скидки/раздачи
  - NodeSeek (официальный RSS) — торговля аккаунтами/VPS
  - V2EX (JSON API + TLS-имитация Chrome) — программисты/фаервол
  - LinuxDo (через TG-зеркало t.me/s/LinuxDoNew) — AI/софт/байки
  - Naixi 奶昔论坛 (Discuz RSS) — SIM/eSIM/KYC зарубежных операторов
- **Перевод:** LLM (OpenAI-совместимый API) → Bing → Google → MyMemory,
  гвардия качества (кириллица/иероглифы), словарь китайского сленга
  (白嫖=халява, 车位=слот подписки, 机场=VPN, ...)
- **Фильтр мусора (relevance.py):** двухуровневый — regex-эвристика
  (мгновенно, без ключей) + LLM-классификатор (semantics). Отсекает
  нытьё/жалобы/вопросы-болтовню, пропускает новости/халяву/скидки.
- **Медиа:** фото (CDN-загрузка с ретраями), альбомы (sendMediaGroup),
  видео (sendVideo) из постов тем.
- **Защита от зависаний:** жёсткие таймауты на все запросы, замок
  одиночного инстанса (.bot.lock), вечное ожидание сети при старте,
  авто-восстановление в цикле. Темы никогда не теряются (SQLite дедуп).

## Установка

```bash
python -m venv venv
# Windows:
venv\Scripts\pip install -r requirements.txt
# Linux/macOS:
venv/bin/pip install -r requirements.txt

cp .env.example .env   # и заполни (см. ниже)
python main.py         # или: venv\Scripts\pythonw.exe main.py (фоном, Windows)
```

## Настройка (.env)

| Переменная | Что |
|---|---|
| `BOT_TOKEN` | токен от @BotFather |
| `CHANNEL_ID` | @username канала или числовой ID (-100...) |
| `TG_PROXY` | SOCKS5/HTTP прокси до api.telegram.org (если заблокирован) |
| `LLM_API_BASE` | OpenAI-совместимый API (например https://api.glm.ai/v1) |
| `LLM_API_KEY` | ключ к нему |
| `LLM_MODEL` / `LLM_FALLBACK_MODEL` | модели для перевода (быстрая + запасная) |
| `RELEVANCE_FILTER` | 1 = LLM-фильтр мусора включён |
| `SOURCES` | nodeloc,nodeseek,v2ex,linuxdo,naixi |
| `POLL_INTERVAL` | 300 (секунды) |
| `MAX_TOPIC_AGE_HOURS` | 24 — старше не постим |

Полный список — в `.env.example`.

## Как работает цикл

```
main.py (цикл 5 мин)
 ├─ проверка сети/прокси (self-heal, ожидание если лежит)
 ├─ NodeLoc: latest.json → фильтр возраст/категории/длина
 ├─ NodeSeek: rss.nodeseek.com → дедуп по topic_id
 ├─ V2EX: /api/topics/latest.json (curl_cffi, TLS Chrome)
 ├─ LinuxDo: t.me/s/LinuxDoNew (веб-превью, без API)
 ├─ Naixi: forum.php?mod=rss (Discuz RSS)
 │    ↓ каждый кандидат:
 │   translate.py → LLM → Bing → Google → MyMemory → гвардия
 │   relevance.py → regex + LLM → постить/отклонить
 │   formatter.py → HTML + свёрнутая цитата + footer
 │   telegram_client.py → send_message/photo/media_group/video
 └─ storage.py (SQLite) → mark posted/skipped/error
```

## Структура

```
main.py            — вход, цикл, автолекарь сети
config.py          — Settings из .env
engine.py          — конвейер источников
storage.py         — SQLite дедуп/история
discourse.py       — NodeLoc клиент (JSON API)
nodeseek_rss.py    — NodeSeek через RSS
cfsource.py        — V2EX через curl_cffi (TLS-имитация)
linuxdo_mirror.py  — LinuxDo через TG-зеркало
naixi.py           — Naixi (Discuz RSS)
nodeseek.py        — (legacy) NodeSeek через cloudscraper — сайт закрыл CF
translate.py       — каскад переводчиков + словарь сленга
relevance.py       — фильтр мусора (regex + LLM)
formatter.py       — HTML-шаблоны постов
telegram_client.py — TG Bot API (фото/альбомы/видео, ретраи)
watch100.py        — (опционально) счётчик постов + сторож
run_once.py        — одиночный прогон (тест/force)
test_system.py     — системные проверки
```

## Грабли, которые уже закопаны

- **Cloudflare:** NodeSeek/www и LinuxDo закрыты JS-челленджем — лечится
  RSS и TG-зеркалом. V2EX проходит через TLS-имитацию (curl_cffi).
- **Прокси в РФ:** api.telegram.org заблокирован — нужен прокси в TG_PROXY.
- **translators (Bing):** не имеет таймаута — все вызовы в потоке с лимитом.
- **Двойной инстанс:** venv-обёртки Windows плодят два процесса — замок
  .bot.lock (msvcrt) отсекает второй.
- **curl_cffi + VERBOSE regex:** пробелы в паттернах нужно `\s+`.

## Лицензия

MIT. Сделано для личных каналов-агрегаторов.
