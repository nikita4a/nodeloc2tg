"""Конфигурация nodeloc2tg.

Все настройки читаются из переменных окружения (.env).
Дефолты подобраны под NodeLoc.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

try:
    # python-dotenv подгружает .env, если он есть.
    # override=True: .env проекта имеет приоритет над системными переменными —
    # иначе, напр., чужой мёртвый TG_PROXY из системы (10808) перекрывает
    # рабочий прокси из нашего .env.
    from dotenv import load_dotenv

    load_dotenv(override=True)
except Exception:
    # dotenv может быть не установлен при импорте config без venv — ок
    pass

# v2rayN/xray-клиенты прописывают в пользовательское окружение Windows
# HTTP_PROXY/HTTPS_PROXY на свой порт (10809). Когда клиент выключен —
# порт мёртв, и ВСЕ запросы (переводчики, картинки) падают ProxyError.
# Если наш .env не задаёт прокси явно — вычищаем чужие, бот ходит напрямую.
_env_tg = os.environ.get("TG_PROXY", "").strip()
_env_tr = os.environ.get("TRANSLATE_PROXY", "").strip()
if not _env_tg and not _env_tr:
    for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
               "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(_k, None)


def _get(name: str, default: str) -> str:
    val = os.environ.get(name)
    return val if val not in (None, "") else default


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_category_ids(name: str, default: frozenset[int]) -> frozenset[int]:
    """Парсит список ID категорий из переменной окружения.

    Формат значения: числа через запятую/пробел, напр. '5,7,31' или '5 7 31'.
    Пустое значение → вернуть default. 'all'/'*' → пустой frozenset (выкл.).
    """
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    raw = raw.strip().lower()
    if raw in ("all", "*", "0", "none"):
        return frozenset()  # фильтр выключен — пропускать все категории
    ids: set[int] = set()
    for token in raw.replace(",", " ").split():
        token = token.strip()
        if not token:
            continue
        try:
            ids.add(int(token))
        except ValueError:
            continue
    return frozenset(ids) if ids else default


# Белый список категорий NodeLoc по умолчанию.
# Подобран под ТЗ: AI, технологии, интернет-услуги (VPS/хостинг/домены/email),
# абузы/скидки/сделки, виртуальные карты. Бытовуха, спорт, медиа — исключены.
#
# Структура (родитель → подкатегории), ID из site.json NodeLoc:
#   5  互联网服务 (Интернет-услуги) — берём целиком как родителя
#      + ключевые подкатегории: 27 VPS, 11 评测, 25 建站, 58 域名社,
#        60 应用, 98 Email, 116 ai大模型信息差, 91 Ubuntu, 119 HTTP代理
#   7  科技与创作 (Технологии) — целиком как родитель
#      + 31 AI, 30 编程开发, 67 开源, 46 虚拟币, 111 工具控, 108 单片机,
#        88 GitHub仓库项目分享, 122 游戏开发, 110 RustFS
#   6  数码与硬件 → 44 SIM/ESIM, 45 虚拟卡·信用卡, 34 电脑与外设
#   79 商业与金融 → 10 优惠情报, 43 羊毛党, 14 拼车, 13 交易与交换
#   26 活动与互动 → 12 抽奖 (розыгрыши — популярно у аудитории)
#   21 公告 (объявления форума)
#
# «杂谈»(83, offtopic внутри 5) и «AskNodeLoc»(54) намеренно НЕ включены —
# там много жизненных вопросов. Если захочешь — добавь их в .env.
_DEFAULT_ALLOWED_CATEGORIES = frozenset({
    # интернет-услуги: родитель + техно-подкатегории
    5, 27, 11, 25, 58, 60, 98, 116, 91, 119, 65, 102, 121, 127,
    # доменные сервисы-сообщества
    96, 129,
    # M365 Copilot (Microsoft 365 — релизы Gateway и т.п.)
    134,
    # технологии: родитель + подкатегории
    7, 31, 30, 67, 46, 111, 108, 88, 122, 110, 112, 101,
    # железо: SIM/карты/ПК
    6, 44, 45, 34,
    # финансы-скидки/сделки
    79, 10, 43, 14, 13,
    # розыгрыши
    26, 12,
    # объявления
    21,
    # 杂谈 (83) — общий чат NodeLoc. ВАЛЁТ ВМЕСТЕ техно и бытовуху,
    # поэтому НЕ в безусловном списке: проходит только при наличии
    # тематического ключевика в заголовке (см. _HYBRID_KEYWORD_CATEGORIES
    # и engine._passes_category_filter). Так пропустим скидки ChatGPT /
    # AI-модели / Linux-гайды, но отсеем «жизненные» вопросы.
})


# Категории, пропускаемые ГИБРИДНО: тема проходит, только если в заголовке
# есть тематический ключевик. Применяется к offtopic-разделам, где намешано
# всё подряд (напр. 杂谈 = 83).
# Карта: {category_id: compiled_regex ключевиков}
import re as _re
_HYBRID_KEYWORD_CATEGORIES = {
    83: _re.compile(
        r"""(?:
            GPT|gpt|Claude|claude|Gemini|DeepSeek|ChatGPT|chatgpt|
            VPS|vps|服务器|主机|hosting|host|cloud|CDN|cdn|
            域名|domain|SSL|ssl|DNS|dns|IPv6|ipv6|
            优惠|折扣|免费|白嫖|羊毛|赠送|注册|register|
            Linux|linux|Ubuntu|Docker|docker|Python|python|代码|编程|
            Obsidian|Notion|API|api|VPN|vpn|代理|梯子|节点|
            GitHub|github|开源|软件|工具|教程|NVMe|SSD|
            虚拟卡|信用卡|套餐|订阅|subscribe|
            建站|WordPress|博客|blog|
            模型|大模型|token|Token|
            AI模型|AI绘画|AI工具|AI教程|AI平台|大AI|
            gptpuls|gptplus|gpt-plus|gpt_plus
        )""",
        _re.VERBOSE | _re.IGNORECASE,
    ),
}


@dataclass(frozen=True)
class Settings:
    # Telegram
    bot_token: str
    channel_id: str
    tg_proxy: str  # напр. socks5h://127.0.0.1:1080 (Mullvad) — нужен если TG API недоступен напрямую

    # Источник
    forum_base: str
    sources: str  # включённые источники через запятую: nodeloc,nodeseek
    web_sources: str   # радарные веб-источники: all | none | список имён
    forum_urls: str    # произвольные форумы по URL (авто-детект движка)
    nodeseek_proxy: str

    # Поведение
    poll_interval: int
    latest_limit: int
    initial_backfill: int
    min_body_length: int
    max_topic_age_hours: int

    # LLM-фильтр релевантности: отклоняет нытьё/вопросы/болтовню,
    # оставляет новости/халяву/абузы/раздачи/скидки/поставщиков
    relevance_enabled: bool
    relevance_model: str
    allowed_category_ids: frozenset[int]
    http_timeout: int
    user_agent: str

    # Перевод
    translate_delay_ms: int
    translate_proxy: str  # напр. socks5h://127.0.0.1:1080 — обход rate-limit Google по IP
    llm_api_base: str     # OpenAI-совместимый endpoint (пусто = LLM-перевод выключен)
    llm_api_key: str
    llm_model: str
    llm_fallback_model: str
    llm_proxy: str        # отдельный прокси для LLM API (logfare блокируется с RU напрямую)
    max_body_chars: int
    max_caption_chars: int

    # Логи
    log_level: str

    # Статус-уведомления в канал (heartbeat)
    heartbeat_hours: float  # период статус-поста; 0 = выключено
    tg_channels: str  # произвольные TG-каналы: @chan1,@chan2

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            bot_token=_get("BOT_TOKEN", ""),
            channel_id=_get("CHANNEL_ID", ""),
            tg_proxy=_get("TG_PROXY", ""),
            forum_base=_get("FORUM_BASE", "https://www.nodeloc.com").rstrip("/"),
            sources=_get("SOURCES", "nodeloc"),
            web_sources=_get("WEBSOURCES", "all"),
            forum_urls=_get("FORUM_URLS", ""),
            nodeseek_proxy=_get("NODESEEK_PROXY", ""),
            poll_interval=_get_int("POLL_INTERVAL", 300),
            latest_limit=_get_int("LATEST_LIMIT", 30),
            initial_backfill=_get_int("INITIAL_BACKFILL", 0),
            min_body_length=_get_int("MIN_BODY_LENGTH", 80),
            relevance_enabled=_get("RELEVANCE_FILTER", "1") not in ("0", "false", "False", ""),
            relevance_model=_get("RELEVANCE_MODEL", "glm-5"),
            max_topic_age_hours=_get_int("MAX_TOPIC_AGE_HOURS", 24),
            allowed_category_ids=_get_category_ids(
                "ALLOWED_CATEGORY_IDS", _DEFAULT_ALLOWED_CATEGORIES
            ),
            http_timeout=_get_int("HTTP_TIMEOUT", 20),
            user_agent=_get("USER_AGENT", "Mozilla/5.0 (compatible; nodeloc2tg/1.0)"),
            translate_delay_ms=_get_int("TRANSLATE_DELAY_MS", 600),
            translate_proxy=_get("TRANSLATE_PROXY", ""),
            llm_api_base=_get("LLM_API_BASE", "").rstrip("/"),
            llm_api_key=_get("LLM_API_KEY", ""),
            llm_model=_get("LLM_MODEL", "deepseek-v4-flash-0731"),
            llm_fallback_model=_get("LLM_FALLBACK_MODEL", "glm-5.2"),
            llm_proxy=_get("LLM_PROXY", ""),
            max_body_chars=_get_int("MAX_BODY_CHARS", 1500),
            max_caption_chars=_get_int("MAX_CAPTION_CHARS", 900),
            log_level=_get("LOG_LEVEL", "INFO"),
            heartbeat_hours=_get_float("HEARTBEAT_HOURS", 6.0),
            tg_channels=_get("TG_CHANNELS", ""),
        )


# Проверка критичных значений — вызывается явно из main/run_once,
# чтобы импорт config сам по себе не падал (удобно для тестов).
def validate_for_runtime(s: Settings) -> None:
    missing = []
    if not s.bot_token or "ExampleToken" in s.bot_token:
        missing.append("BOT_TOKEN")
    if not s.channel_id or s.channel_id.startswith("@your_channel"):
        missing.append("CHANNEL_ID")
    if missing:
        raise RuntimeError(
            f"Не заполнены обязательные переменные: {', '.join(missing)}. "
            f"См. .env.example"
        )
