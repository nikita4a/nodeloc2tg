"""Сборка HTML-сообщений для Telegram — минималистичный стиль.

Telegram parse_mode=HTML: <b><i><a><code><pre><s><u>.
Любые спецсимволы в пользовательском тексте ДОЛЖНЫ быть экранированы,
иначе TG вернёт 400 "can't parse entities".

Поэтому: перевод → экранирование → обрамление тегами.

Стиль постов (минимализм):
    Заголовок (жирный, можно в 2 строки если длинный)
    <пустая строка>
    Тело перевода — чистыми абзацами
    <пустая строка>
    nodeloc.com · @author · Открыть ↗

Без эмодзи-шума, без дублирующего китайского заголовка, без разделителей.
"""
from __future__ import annotations

from html import escape

from discourse import Topic


def _short_title(ru: str, orig: str) -> str:
    """Один чистый заголовок. Если перевода нет — берём оригинал."""
    ru = (ru or "").strip()
    return ru if ru else (orig or "").strip()


def _author_handle(author: str) -> str:
    """Имя автора как простой текст.

    ВАЖНО: НЕ добавляем '@' — Telegram автолинкует любой '@username' как
    упоминание (t.me/...), даже в HTML. NodeLoc-юзернейм — это НЕ Telegram-
    аккаунт, поэтому показываем его простым текстом без префикса.
    """
    a = (author or "").strip()
    if not a:
        return ""
    # на всякий случай отрежем '@', если Discourse вернул с префиксом
    return a.lstrip("@")


def _hidden_notice(topic: Topic) -> str:
    """Строка-уведомление: часть контента скрыта за reply-to-view на форуме.

    Курсивом, отдельной строкой — чтобы визуально выделялось, но не кричало.
    Текст нейтральный: зовёт открыть оригинал, где читатель сам ответит
    и увидит скрытое (бот ответы не пишет).
    """
    if not getattr(topic, "has_hidden_content", False):
        return ""
    return ("<i>🔒 Часть содержимого скрыта автором (открывается после "
            "ответа на форуме) — см. оригинал.</i>")


# Промо проекта: отдельная строка под цитатой, завёрнутая в tg-spoiler —
# текст скрыт «шторкой» и раскрывается по тапу (formatting: hidden).
PROMO_HTML = ('<span class="tg-spoiler"><b>⚡️ '
              '<a href="https://t.me/iishogatewaybot">@iishogatewaybot'
              ' — бесплатные нейросети</a></b></span>')


def _quote(body: str) -> str:
    """Сворачиваемая цитата с телом поста (пустое тело → None, см. callers)."""
    return f"<blockquote expandable>{body}</blockquote>" if body else ""


def _footer(topic: Topic) -> str:
    """Компактная подпись: домен · @author · Открыть ↗
    Домен берём из URL темы — работает для любого источника."""
    from urllib.parse import urlparse
    url = escape(topic.url)
    domain = urlparse(topic.url).netloc.replace("www.", "") or "nodeloc.com"
    author = _author_handle(topic.author)
    parts = [domain]
    if author:
        parts.append(escape(author))
    parts.append(f'<a href="{url}">Открыть ↗</a>')
    tg_url = getattr(topic, "tg_url", "")
    if tg_url:
        parts.append(f'<a href="{escape(tg_url)}">в канале ↗</a>')
    return " · ".join(parts)


def build_message(topic: Topic, title_ru: str, body_ru: str,
                  max_body_chars: int = 1500) -> str:
    """Текстовый пост (без фото). ≤4096.

    Заголовок жирным, тело — в сворачиваемой цитате (blockquote expandable):
    длинный текст схлопнут, читатель разворачивает по тапу.
    """
    title = escape(_short_title(title_ru, topic.title))
    # теги цитаты съедают часть лимита — ужимаем тело
    QUOTE_OVERHEAD = (len("<blockquote expandable></blockquote>")
                      + len(PROMO_HTML) + 4)
    body = _clean_body(body_ru, max(200, max_body_chars - QUOTE_OVERHEAD))

    parts: list[str] = []
    if title:
        parts.append(f"<b>{title}</b>")
    parts.append(_quote(body))
    parts.append(PROMO_HTML)
    notice = _hidden_notice(topic)
    if notice:
        parts.append(notice)
    parts.append(_footer(topic))

    text = "\n\n".join(p for p in parts if p)
    # жёсткий лимит обычного сообщения
    if len(text) > 4096:
        text = _truncate_with_footer(text, _footer(topic), 4096)
    return text.strip()


def build_caption(topic: Topic, title_ru: str, body_ru: str,
                  max_caption_chars: int = 900) -> str:
    """Подпись к фото/альбому. ≤1024 (жёсткий лимит TG для caption).

    Заголовок жирным, тело — в сворачиваемой цитате.
    """
    TG_CAPTION_HARD = 1024
    cap = min(max_caption_chars, TG_CAPTION_HARD)

    title = escape(_short_title(title_ru, topic.title))
    footer = _footer(topic)
    notice = _hidden_notice(topic)
    QUOTE_OVERHEAD = (len("<blockquote expandable></blockquote>")
                      + len(PROMO_HTML) + 4)
    # бюджет на тело (учитываем notice и теги цитаты)
    overhead = (len(title) + 2 + len(footer) + 4 + QUOTE_OVERHEAD
                + (len(notice) + 2 if notice else 0))
    budget = cap - overhead
    body = _clean_body(body_ru, budget) if budget > 80 else ""

    parts: list[str] = []
    if title:
        parts.append(f"<b>{title}</b>")
    parts.append(_quote(body))
    parts.append(PROMO_HTML)
    if notice:
        parts.append(notice)
    parts.append(footer)

    text = "\n\n".join(p for p in parts if p)
    if len(text) > TG_CAPTION_HARD:
        text = _truncate_with_footer(text, footer, TG_CAPTION_HARD)
    return text.strip()


# ---------- внутреннее ----------

def _clean_body(body_ru: str, max_chars: int) -> str:
    """Тело перевода: экранировать, убрать пустые строки в краях,
    обрезать по абзацной границе до max_chars."""
    if not body_ru:
        return ""
    body = body_ru.strip()
    if not body:
        return ""
    if max_chars and len(body) > max_chars:
        # режем по последнему переносу в пределах лимита — без рваных предложений
        chunk = body[:max_chars]
        cut = chunk.rfind("\n")
        body = (chunk[:cut] if cut > max_chars // 2 else chunk).rstrip() + "…"
    # убираем строки, состоящие только из пробелов (становятся «пустыми»),
    # затем склеиваем 3+ переносов в два (визуальный абзац).
    import re
    body = re.sub(r"(?m)^[ \t]+$", "", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    body = body.strip()
    body = escape(body)
    return body


def _truncate_with_footer(text: str, footer: str, hard_limit: int) -> str:
    """Если текст длиннее hard_limit — отрезаем тело, оставляя footer целым."""
    avail = hard_limit - len(footer) - 6  # «…\n\n» запас
    if avail < 40:
        return footer  # места почти нет — только подпись
    # текст = заголовок\n\nТЕЛО\n\nFOOTER → режем ТЕЛО
    cut = text[:avail].rstrip()
    # убираем возможный рваный хвост после переноса
    if cut.endswith("\n"):
        cut = cut.rstrip() + "…"
    else:
        cut = cut + "…"
    return f"{cut}\n\n{footer}"
