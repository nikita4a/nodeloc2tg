"""Ядро логики: связка парсер → перевод → форматер → TG + хранилище.

Используется и в main.py (24/7 цикл) и в run_once.py (одиночный прогон),
чтобы поведение было идентичным.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from config import Settings, _HYBRID_KEYWORD_CATEGORIES
from discourse import DiscourseClient, Topic
from formatter import build_caption, build_message
from storage import Storage
from translate import Translator

log = logging.getLogger(__name__)


@dataclass
class StepResult:
    processed: int = 0
    posted: int = 0
    skipped_known: int = 0
    skipped_short: int = 0
    skipped_irrelevant: int = 0   # LLM-классификатор: нытьё/оффтоп
    errors: int = 0
    last_post_title: str = ""  # заголовок последнего успешно опубликованного поста
    # Журнал для часового отчёта (лимит 15):
    posted_log: list = None       # [{time,title,cat}]
    filtered_log: list = None     # [{reason,title}]
    dupes_log: list = None        # [title] — уже запостили раньше

    def __post_init__(self):
        if self.posted_log is None:
            self.posted_log = []
        if self.filtered_log is None:
            self.filtered_log = []
        if self.dupes_log is None:
            self.dupes_log = []


class Engine:
    def __init__(self, settings: Settings, storage: Storage,
                 discourse: DiscourseClient, translator: Translator,
                 telegram=None, extra_sources: dict = None,
                 relevance=None):
        self.s = settings
        self.storage = storage
        self.discourse = discourse
        self.translator = translator
        self.telegram = telegram  # None → режим «без постинга» (dry-run/тест)
        # дополнительные источники: {'nodeseek': NodeSeekClient}
        self.extra_sources = extra_sources or {}
        # LLM-классификатор релевантности (None → выключен)
        self.relevance = relevance

    # ---------- один проход ----------
    def run_once(self, *, force_post: bool = False) -> StepResult:
        """force_post=True игнорирует initial_backfill (для run_once --force)."""
        res = StepResult()
        try:
            self._run_nodeloc(res, force_post)
        except Exception:
            log.exception("NodeLoc: сбой прохода — продолжаем другие источники")
            res.errors += 1
        for name, src in self.extra_sources.items():
            try:
                self._run_simple_source(name, src, res, force_post)
            except Exception:
                log.exception("Источник %s: сбой прохода — продолжаем", name)
                res.errors += 1
        return res

    def _run_nodeloc(self, res: StepResult, force_post: bool) -> None:
        cat_filtered: list = []
        ids = self.discourse.latest_topic_ids(
            self.s.latest_limit,
            allowed_categories=self.s.allowed_category_ids or None,
            # гибридные категории (杂谈=83) проходят только по ключевику в заголовке
            keyword_categories=_HYBRID_KEYWORD_CATEGORIES,
            filtered_out=cat_filtered,
        )
        for f in cat_filtered[:15]:
            res.filtered_log.append(f)
        if not ids:
            log.warning("NodeLoc: не получили список тем — пропускаем")
            return

        fresh_ids = [tid for tid in ids if not self.storage.is_known(tid)]
        if not force_post:
            seeded = self._maybe_seed_backlog(ids)
            if seeded:
                fresh_ids = [tid for tid in ids if not self.storage.is_known(tid)]
        if not fresh_ids:
            log.debug("NodeLoc: свежих тем нет")
            return

        topics = self.discourse.fetch_many(reversed(fresh_ids))
        log.info("NodeLoc: обрабатываем %d свежих тем", len(topics))
        for t in topics:
            res.processed += 1
            try:
                self._process_topic(t, res, source="nodeloc")
            except Exception as e:
                res.errors += 1
                log.exception("Ошибка обработки темы %d: %s", t.topic_id, e)
                self.storage.mark(t.topic_id, "error", title_orig=t.title,
                                  source="nodeloc")

    def _run_simple_source(self, name: str, src, res: StepResult,
                           force_post: bool) -> None:
        """Источник без категорий (NodeSeek и т.п.): только возраст + дедуп."""
        ids = src.latest(self.s.latest_limit)
        if not ids:
            log.warning("%s: не получили список тем", name)
            return
        fresh = [tid for tid in ids if not self.storage.is_known(tid, source=name)]
        if not fresh:
            log.debug("%s: свежих тем нет", name)
            return
        # Первый старт этого источника: пометить текущие как известные,
        # постить только новое (аналог backfill-seed NodeLoc)
        first_seen = self.storage.get_meta(f"{name}_seeded", "")
        if not first_seen and not force_post:
            for tid in ids:
                self.storage.mark(tid, "skipped", title_orig="<seed>",
                                  source=name)
            self.storage.set_meta(f"{name}_seeded", "1")
            log.info("%s: первый старт — помечено %d тем как известных",
                     name, len(ids))
            return
        log.info("%s: обрабатываем %d свежих тем", name, len(fresh))
        for tid in reversed(fresh):
            t = src.fetch(tid)
            if t is None:
                res.errors += 1
                continue
            # шаблонные темы-заглушки: крошечное тело без картинок — не постим.
            # min_body у источника может быть ниже (зеркало LinuxDo: посты-заголовки)
            min_body = getattr(src, "min_body", 150)
            if len(t.body_text) < min_body and not t.image_urls:
                res.skipped_short += 1
                self.storage.mark(tid, "skipped", title_orig=t.title,
                                  source=name)
                log.debug("%s: тема %d — тело-огрызок, пропуск", name, tid)
                continue
            res.processed += 1
            try:
                self._process_topic(t, res, source=name)
            except Exception as e:
                res.errors += 1
                log.exception("%s: ошибка обработки темы %d: %s",
                              name, tid, e)
                self.storage.mark(tid, "error", title_orig="",
                                  source=name)

    def _process_topic(self, t: Topic, res: StepResult,
                       source: str = "nodeloc") -> None:
        # фильтр по возрасту темы: NodeLoc бампает темы в /latest при любом
        # ответе, поэтому старая тема может всплывать в топе. Постим только
        # действительно свежие — по created_at, не по позиции в списке.
        if self.s.max_topic_age_hours > 0 and _is_too_old(t.created_at,
                                                          self.s.max_topic_age_hours):
            res.skipped_known += 1  # переиспользуем счётчик «пропущено»
            self.storage.mark(t.topic_id, "skipped", title_orig=t.title, source=source)
            if len(res.filtered_log) < 15:
                res.filtered_log.append({
                    "reason": "старая", "cat": t.category_id, "title": t.title,
                })
            log.debug("Тема %d старая (created_at=%s) — пропуск", t.topic_id, t.created_at)
            return

        # фильтр по длине
        if len(t.body_text) < self.s.min_body_length and not t.title:
            res.skipped_short += 1
            self.storage.mark(t.topic_id, "skipped", title_orig=t.title, source=source)
            log.debug("Тема %d слишком короткая — пропуск", t.topic_id)
            return

        # заголовок-URL вместо текста (автор вставил ссылку) — в ленте это
        # мусор: пост без заголовка ценности не несёт
        if t.title and re.fullmatch(r"https?://\S+", t.title.strip()):
            res.skipped_irrelevant += 1
            self.storage.mark(t.topic_id, "skipped", title_orig=t.title,
                              source=source)
            log.info("Тема %d: заголовок — голый URL, пропуск", t.topic_id)
            return

        title_ru = self.translator.translate(t.title) if t.title else ""
        body_ru = (self.translator.translate_summary(t.body_text)
                   if t.body_text else "")

        # LLM-фильтр релевантности: нытьё/вопросы/болтовня не публикуются.
        # Fail-open: сбой LLM → тема проходит (не теряем контент).
        if self.relevance is not None:
            ok = self.relevance.relevant(t.title, title_ru, t.body_text[:400])
            if not ok:
                res.skipped_irrelevant += 1
                self.storage.mark(t.topic_id, "skipped", title_orig=t.title,
                                  source=source, title_ru=title_ru)
                if len(res.filtered_log) < 15:
                    res.filtered_log.append({
                        "reason": "нерелевант", "cat": t.category_id,
                        "title": (title_ru or t.title),
                    })
                log.info("Тема %d отклонена фильтром (%s)", t.topic_id,
                         (title_ru or t.title)[:60])
                return

        msg = build_message(t, title_ru, body_ru, self.s.max_body_chars)
        # хантер API-ключей: ключи из оригинального текста — в <code>
        keys_html = _keys_section(t)
        if keys_html:
            msg = f"{msg}\n\n{keys_html}"

        # dry-run: никуда не постим, но в БД НЕ отмечаем — чтобы при
        # реальном старте тема всё-таки ушла.
        if self.telegram is None:
            log.info("[dry-run] Тема %d: %s (img=%s)",
                     t.topic_id, (title_ru or t.title)[:60],
                     "да" if t.image_url else "нет")
            print("=" * 60)
            if t.image_url:
                print("[ФОТО]", t.image_url)
            print(msg)
            print("=" * 60)
            return

        # Видео (зеркало LinuxDo): качаем и шлём sendVideo с подписью
        video_url = getattr(t, "video_url", "")
        if video_url and self.telegram is not None:
            caption = build_caption(t, title_ru, body_ru, self.s.max_caption_chars)
            try:
                mid = self.telegram.send_video(video_url, caption)
                res.posted += 1
                res.last_post_title = (title_ru or t.title).strip()
                if len(res.posted_log) < 15:
                    res.posted_log.append({
                        "title": res.last_post_title,
                        "cat": getattr(t, "category_name", "") or t.category_id,
                        "translated": _has_cyrillic(res.last_post_title),
                    })
                self.storage.mark(t.topic_id, "posted", source=source,
                                  title_orig=t.title, title_ru=title_ru)
                log.info("Опубликовано (видео) тема %d → msg_id=%d (%s)",
                         t.topic_id, mid, (title_ru or t.title)[:60])
                return
            except Exception as e:
                log.warning("send_video упал для темы %d (%s) — fallback",
                            t.topic_id, e)

        # Несколько картинок → альбом (фото + подпись на первом) ;
        # одна → фото с подписью; ни одной → текст. На любой проблеме с фото
        # откатываемся к текстовому сообщению (тема должна уйти в любом случае).
        if len(t.image_urls) >= 2:
            caption = build_caption(t, title_ru, body_ru, self.s.max_caption_chars)
            try:
                mid = self.telegram.send_media_group(t.image_urls, caption)
                res.posted += 1
                res.last_post_title = (title_ru or t.title).strip()
                if len(res.posted_log) < 15:
                    res.posted_log.append({
                        "title": res.last_post_title,
                        "cat": t.category_name or t.category_id,
                        "translated": _has_cyrillic(res.last_post_title),
                    })
                self.storage.mark(t.topic_id, "posted", source=source,
                                  title_orig=t.title, title_ru=title_ru)
                log.info("Опубликовано (альбом %d фото) тема %d → msg_id=%d (%s)",
                         len(t.image_urls), t.topic_id, mid,
                         (title_ru or t.title)[:60])
                return
            except Exception as e:
                log.warning("send_media_group упал для темы %d (%s) — fallback",
                            t.topic_id, e)

        if t.image_url:
            caption = build_caption(t, title_ru, body_ru, self.s.max_caption_chars)
            try:
                log.info("Тема %d: постим фото %s", t.topic_id, t.image_url[:80])
                mid = self.telegram.send_photo(t.image_url, caption)
                res.posted += 1
                res.last_post_title = (title_ru or t.title).strip()
                if len(res.posted_log) < 15:
                    res.posted_log.append({
                        "title": res.last_post_title,
                        "cat": t.category_name or t.category_id,
                        "translated": _has_cyrillic(res.last_post_title),
                    })
                self.storage.mark(t.topic_id, "posted", source=source,
                                  title_orig=t.title, title_ru=title_ru)
                log.info("Опубликовано (фото) тема %d → msg_id=%d (%s)",
                         t.topic_id, mid, (title_ru or t.title)[:60])
                return
            except Exception as e:
                log.warning("send_photo упал для темы %d (%s) — fallback на текст",
                            t.topic_id, e)

        # сюда попадаем только если фото не найдено или не отправилось —
        # фиксируем почему (ловля редких случаев «пост без фото»)
        if t.image_urls and not t.image_url:
            log.warning("Тема %d: image_urls=%d но image_url пуст — баг!",
                        t.topic_id, len(t.image_urls))
        elif not t.image_urls:
            log.info("Тема %d: без фото (картинок в теме не найдено)", t.topic_id)
        try:
            mid = self.telegram.send_message(msg)
            res.posted += 1
            res.last_post_title = (title_ru or t.title).strip()
            if len(res.posted_log) < 15:
                res.posted_log.append({
                    "title": res.last_post_title,
                    "cat": t.category_name or t.category_id,
                    "translated": _has_cyrillic(res.last_post_title),
                })
            self.storage.mark(t.topic_id, "posted", source=source,
                              title_orig=t.title, title_ru=title_ru)
            log.info("Опубликовано тема %d → msg_id=%d (%s)",
                     t.topic_id, mid, (title_ru or t.title)[:60])
        except Exception as e:
            res.errors += 1
            self.storage.mark(t.topic_id, "error", title_orig=t.title, source=source)
            log.error("Не удалось запостить тему %d: %s", t.topic_id, e)

    # ---------- backfill ----------
    def _maybe_seed_backlog(self, ids: list[int]) -> int:
        """Если БД пуста — помечаем все свежие как известные,
        кроме initial_backfill последних (самых свежих).
        INITIAL_BACKFILL=0 → не постим ничего из истории.
        Возвращает сколько тем помечено (0 если БД уже не пуста).
        """
        if self.storage.count() > 0:
            return 0
        n_backfill = self.s.initial_backfill
        if n_backfill < 0:
            n_backfill = 0
        # ids уже отсортирован свежие-сверху. Хотим оставить свежими
        # только n_backfill штук, остальные пометить как skipped.
        to_skip = ids[n_backfill:] if n_backfill < len(ids) else []
        for tid in to_skip:
            self.storage.mark(tid, "skipped", title_orig="<backfill-seed>")
        log.info("Первый старт: помечено %d старых тем как уже известных "
                 "(initial_backfill=%d)", len(to_skip), n_backfill)
        return len(to_skip)


def _keys_section(t) -> str:
    """Хантер ключей: <code>-строки с найденными API-ключами ('' если нет)."""
    from html import escape
    from keyhunter import scan_keys
    found = scan_keys((t.title or "") + "\n" + (t.body_text or ""))
    if not found:
        return ""
    lines = []
    for prov, ks in list(found.items())[:6]:
        for k in sorted(ks)[:10]:
            lines.append(f"🔑 {prov}: <code>{escape(k)}</code>")
    return "\n".join(lines)


def _has_cyrillic(text: str) -> bool:
    """True если в тексте есть кириллица (заголовок перевёлся на русский)."""
    return any("а" <= ch.lower() <= "я" or ch in "ёЁ" for ch in (text or ""))


def _is_too_old(created_at: str, max_age_hours: int) -> bool:
    """True если тема создана раньше, чем max_age_hours назад.

    created_at приходит от Discourse в виде '2026-07-23T16:19:19.000Z'.
    При невозможности распарсить (пусто/битый формат) возвращаем False —
    лучше запостить, чем пропустить из-за сбоя парсинга даты.
    """
    if not created_at:
        return False
    try:
        # нормализуем 'Z' → '+00:00' для fromisoformat (Python <3.11 не ест 'Z')
        iso = created_at.strip().replace("Z", "+00:00")
        created = datetime.fromisoformat(iso)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        age_hours = (now - created).total_seconds() / 3600.0
        return age_hours > max_age_hours
    except (ValueError, TypeError) as e:
        log.debug("Не удалось распарсить created_at=%r: %s — не фильтруем",
                  created_at, e)
        return False
