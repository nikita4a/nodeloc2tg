"""Источник: NodeSeek через официальный RSS (rss.nodeseek.com).

Сайт www.nodeseek.com закрыт Cloudflare JS-челленджем (curl_cffi больше
не проходит — нужен реальный браузер). Но RSS-фид открыт и отдаёт всё:
title/description(тело)/link/guid(topic_id)/category/author/pubDate.

Схема та же что у naixi.py (Discuz RSS) — свой модуль, потому что
структура фида другая (guid = id, category = trade/tech/…).
"""
from __future__ import annotations

import logging
import re
from email.utils import parsedate_to_datetime

from cfsource import CFTopic

log = logging.getLogger(__name__)

RSS_URL = "https://rss.nodeseek.com/"
BASE = "https://www.nodeseek.com"


class NodeSeekRSS:
    # короткие темы-вопросы тут норма — порог снижаем как у naixi
    min_body = 60

    def __init__(self, proxy: str = "", timeout: int = 30):
        from curl_cffi import requests as creq
        self.s = creq.Session(impersonate="chrome124")
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})
        self.timeout = timeout
        self._cache: dict[int, dict] = {}

    def _fetch_feed(self) -> list[dict]:
        try:
            r = self.s.get(RSS_URL, timeout=self.timeout)
            if r.status_code != 200:
                log.warning("NodeSeek RSS: HTTP %s", r.status_code)
                return []
            xml = r.text
        except Exception as e:
            log.warning("NodeSeek RSS сеть: %s", e.__class__.__name__)
            return []
        out = []
        for item in re.findall(r"<item>(.*?)</item>", xml, re.DOTALL):
            def tag(name: str) -> str:
                m = re.search(rf"<{name}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>",
                              item, re.DOTALL)
                return m.group(1).strip() if m else ""
            link = tag("link")
            tid = tag("guid") or ""
            if not tid.isdigit():
                m_id = re.search(r"post-(\d+)", link)
                tid = m_id.group(1) if m_id else ""
            if not tid:
                continue
            created = ""
            pd = tag("pubDate")
            if pd:
                try:
                    created = parsedate_to_datetime(pd).isoformat()
                except Exception:
                    pass
            out.append({
                "topic_id": int(tid),
                "title": tag("title"),
                "url": link or f"{BASE}/post-{tid}-1",
                "body_html": tag("description"),
                "category": tag("category"),
                "author": tag("dc:creator"),
                "created_at": created,
            })
        for p in out:
            self._cache[p["topic_id"]] = p
        return out

    def latest(self, limit: int = 20) -> list[int]:
        posts = self._fetch_feed()
        return [p["topic_id"] for p in posts[:limit]]

    def fetch(self, topic_id: int):
        p = self._cache.get(topic_id)
        if p is None:
            self._fetch_feed()
            p = self._cache.get(topic_id)
        if p is None:
            return None
        from discourse import _html_to_text, _image_urls
        body_html = p["body_html"]
        body_text = _html_to_text(body_html)
        imgs = _image_urls(body_html, BASE, limit=5)
        return CFTopic(
            topic_id=topic_id,
            title=p["title"],
            url=p["url"],
            category_name=p["category"],
            author=p["author"],
            created_at=p["created_at"],
            body_html=body_html,
            body_text=body_text,
            image_url=imgs[0] if imgs else "",
            image_urls=imgs,
            has_hidden_content=False,
        )
