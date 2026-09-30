"""Источник LinuxDo через TG-канал-зеркало t.me/s/LinuxDoNew.

Сам linux.do закрыт Cloudflare JS-челленджем + логином, но публичный
TG-канал транслирует его посты в структурированном виде:

    标题: {заголовок}
    作者: #{автор}
    板块: #{категория}
    编号: {topic_id}
    帖子: https://linux.do/t/topic/{id}
    时间: {YYYY-MM-DD HH:MM:SS}
    摘要: {краткое содержание, может быть пустым}

Парсим веб-превью канала (t.me/s/...), переводим заголовок+摘要 нашим
конвейером, постим со ссылкой на оригинал linux.do.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta

from cfsource import CFTopic, ldo_title_allowed

log = logging.getLogger(__name__)

CHANNEL_URL = "https://t.me/s/LinuxDoNew"
CST = timezone(timedelta(hours=8))  # linux.do время — Китай


class LinuxDoMirror:
    # зеркало отдаёт короткие 摘要 (или пустые) — порог длины тела не применяем
    min_body = 0

    def __init__(self, proxy: str = "", timeout: int = 30):
        from curl_cffi import requests as creq
        self._creq = creq
        self.s = creq.Session(impersonate="chrome124")
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})
        self.timeout = timeout
        self._cache: dict[int, dict] = {}

    # ---------- парсинг страницы канала ----------
    def _parse_page(self, html: str) -> list[dict]:
        out = []
        # режем по data-post — так TG-id сообщения гарантированно связан
        # с его текстом (порядок div-ов может расходиться при сервисных постах)
        chunks = html.split('data-post="LinuxDoNew/')[1:]
        texts = []
        self._chunks = []
        for ch in chunks:
            tg_id = ch.split('"', 1)[0]
            m = re.search(
                r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                ch, re.DOTALL)
            texts.append((tg_id, m.group(1) if m else ""))
            self._chunks.append(ch)
        # медиа привязаны к чанкам сообщений — собираем параллельно
        for idx, (tg_id, raw) in enumerate(texts):
            if not raw:
                continue
            t = re.sub(r"<br\s*/?>", "\n", raw)
            t = re.sub(r"<[^>]+>", "", t)
            t = t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            m_id = re.search(r"编号:\s*(\d+)", t)
            m_title = re.search(r"标题:\s*(.+)", t)
            if not m_id or not m_title:
                continue
            m_url = re.search(r"帖子:\s*(https://linux\.do/t/topic/\d+)", t)
            m_author = re.search(r"作者:\s*#?(\S+)", t)
            m_cat = re.search(r"板块:\s*#?(\S+)", t)
            m_time = re.search(r"时间:\s*([\d\-]+ [\d:]+)", t)
            m_sum = re.search(r"摘要:\s*(.*)", t, re.DOTALL)
            summary = (m_sum.group(1).strip() if m_sum else "")
            created = ""
            if m_time:
                try:
                    dt = datetime.strptime(m_time.group(1), "%Y-%m-%d %H:%M:%S")
                    created = dt.replace(tzinfo=CST).isoformat()
                except ValueError:
                    pass
            # фото: background-image в photo_wrap (CDN telesco.pe, публичные)
            chunk = self._chunks[idx] if idx < len(self._chunks) else ""
            photos = re.findall(r"background-image:url\('([^']+)'\)", chunk)
            # видео: <video src> (встречается реже)
            vid = re.search(r'<video[^>]+src="([^"]+)"', chunk)
            out.append({
                "tg_id": tg_id,
                "photos": photos[:5],
                "video": vid.group(1) if vid else "",
                "topic_id": int(m_id.group(1)),
                "title": m_title.group(1).strip(),
                "url": m_url.group(1) if m_url else f"https://linux.do/t/topic/{m_id.group(1)}",
                "author": m_author.group(1).strip() if m_author else "",
                "category": m_cat.group(1).strip() if m_cat else "",
                "created_at": created,
                "summary": summary,
            })
        return out

    def _fetch_page(self) -> list[dict]:
        try:
            r = self.s.get(CHANNEL_URL, timeout=self.timeout)
            if r.status_code != 200:
                log.warning("LinuxDo-зеркало: HTTP %s", r.status_code)
                return []
            posts = self._parse_page(r.text)
            for p in posts:
                self._cache[p["topic_id"]] = p
            return posts
        except Exception as e:
            log.warning("LinuxDo-зеркало сеть: %s", e.__class__.__name__)
            return []

    # ---------- интерфейс источника ----------
    def latest(self, limit: int = 30) -> list[int]:
        """Свежие topic_id, прошедшие тематический фильтр."""
        posts = self._fetch_page()
        out = []
        for p in posts:
            title = p["title"]
            # 板块 搞七捻三 = флудилка: только если заголовок технарский
            if not ldo_title_allowed(title):
                continue
            out.append(p["topic_id"])
            if len(out) >= limit:
                break
        return out

    def fetch(self, topic_id: int):
        p = self._cache.get(topic_id)
        if p is None:
            self._fetch_page()
            p = self._cache.get(topic_id)
        if p is None:
            return None
        return CFTopic(
            topic_id=topic_id,
            title=p["title"],
            url=p["url"],
            category_name=p["category"],
            author=p["author"],
            created_at=p["created_at"],
            body_html="",
            body_text=p["summary"],
            image_url=(p.get("photos") or [""])[0],
            image_urls=p.get("photos", []),
            has_hidden_content=False,
            tg_url=f"https://t.me/LinuxDoNew/{p.get('tg_id', '')}" if p.get("tg_id") else "",
            video_url=p.get("video", ""),
        )
