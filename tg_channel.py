"""Универсальный источник: TG-канал через публичное веб-превью t.me/s/<name>.

Для каналов threat-intel/leak-мониторов (CyberMonitum, CVEDetector и т.п.),
у которых включено веб-превью. Первый пост-строка = заголовок, остальной
текст = тело (переводится/саммарируется конвейером как любой форумный пост).

topic_id = id сообщения TG (уникален в рамках канала; storage изолирует
источники по имени, коллизий с форумными id нет).
"""
from __future__ import annotations

import logging
import re
import html as ihtml
from datetime import datetime, timezone

from cfsource import CFTopic

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"


class TGChannelSource:
    # посты каналов бывают короткими (одна строка CVE) — не режем по длине
    min_body = 0

    def __init__(self, channel: str, proxy: str = "", timeout: int = 30,
                 severity_min: float = 0.0):
        self.channel = channel
        self.url = f"https://t.me/s/{channel}"
        self.timeout = timeout
        self._cache: dict[int, dict] = {}
        # CVE-фида: постить только >= severity_min (7.0 = High/Critical),
        # иначе канал льёт ~1 CVE/мин и топит ЛС
        self.severity_min = severity_min
        from curl_cffi import requests as creq
        self.s = creq.Session(impersonate="chrome124")
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})

    # ---------- парсинг ----------
    def _parse_page(self, html: str) -> list[dict]:
        out = []
        chunks = html.split(f'data-post="{self.channel}/')[1:]
        for ch in chunks:
            tg_id = ch.split('"', 1)[0]
            if not tg_id.isdigit():
                continue
            m = re.search(
                r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                ch, re.DOTALL)
            raw = m.group(1) if m else ""
            if not raw.strip():
                continue
            t = re.sub(r"<br\s*/?>", "\n", raw)
            t = re.sub(r"<[^>]+>", "", t)
            t = ihtml.unescape(t).strip()
            if not t:
                continue
            # дата сообщения: <time datetime="2026-09-30T18:22:41+00:00">
            created = ""
            m_dt = re.search(r'datetime="([\d\-T:+]+)"', ch)
            if m_dt:
                try:
                    created = datetime.fromisoformat(m_dt.group(1)).isoformat()
                except ValueError:
                    pass
            # медиа этого сообщения
            photos = re.findall(r"background-image:url\('([^']+)'\)", ch)
            vid = re.search(r'<video[^>]+src="([^"]+)"', ch)
            # заголовок = первая непустая строка (обрезана до 120), тело = остальное
            lines = [ln.strip() for ln in t.split("\n") if ln.strip()]
            title = lines[0][:120] if lines else ""
            body = "\n".join(lines[1:]) if len(lines) > 1 else title

            # JSON-посты (CVE-фид): заголовок/тело из полей Title/Content
            if t.lstrip().startswith("{"):
                m_t = re.search(r'"Title"\s*:\s*"([^"]+)"', t)
                if m_t:
                    title = m_t.group(1)[:200]
                m_c = re.search(
                    r'"(?:Content|Summary|Description)"\s*:\s*"([^"]+)"', t)
                body = (m_c.group(1) if m_c else t).strip()
            sev = 99.0
            m_sev = re.search(r"Severity:\s*(\d+(?:\.\d+)?)", t)
            if m_sev:
                sev = float(m_sev.group(1))
            out.append({
                "severity": sev,
                "tg_id": int(tg_id),
                "title": title,
                "body": body,
                "created_at": created,
                "photos": photos[:5],
                "video": vid.group(1) if vid else "",
            })
        return out

    def _fetch_page(self) -> list[dict]:
        try:
            r = self.s.get(self.url, timeout=self.timeout)
            if r.status_code != 200:
                log.warning("%s: HTTP %s", self.channel, r.status_code)
                return []
            posts = self._parse_page(r.text)
            for p in posts:
                self._cache[p["tg_id"]] = p
            return posts
        except Exception as e:
            log.warning("%s сеть: %s", self.channel, e.__class__.__name__)
            return []

    # ---------- интерфейс источника ----------
    def latest(self, limit: int = 30) -> list[int]:
        posts = self._fetch_page()[:limit]
        return [p["tg_id"] for p in posts
                if not self.severity_min
                or p.get("severity", 99.0) >= self.severity_min]

    def fetch(self, tg_id: int):
        p = self._cache.get(tg_id)
        if p is None:
            self._fetch_page()
            p = self._cache.get(tg_id)
        if p is None:
            return None
        return CFTopic(
            topic_id=tg_id,
            title=p["title"],
            url=f"https://t.me/{self.channel}/{tg_id}",
            author=self.channel,
            created_at=p["created_at"],
            body_html="",
            body_text=p["body"],
            image_url=(p.get("photos") or [""])[0],
            image_urls=p.get("photos", []),
            has_hidden_content=False,
            tg_url=f"https://t.me/{self.channel}/{tg_id}",
            video_url=p.get("video", ""),
        )
