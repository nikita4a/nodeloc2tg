"""Универсальный источник: любой форум по URL с авто-детектом движка.

Поддержка: Discourse (/latest.json), XenForo (Atom /forums/-/index.rss),
Discuz (forum.php?mod=rss), phpBB (feed.php), generic RSS/Atom.
Интерфейс совместим с engine: latest(limit) + fetch(tid) → CFTopic.

topic_id для RSS-источников = стабильный md5-хеш ссылки (обрезан до int),
для Discourse — реальный id темы. Storage изолирует по имени источника
u:<host>, коллизий нет.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from cfsource import CFTopic
from discourse import _html_to_text

log = logging.getLogger(__name__)


def _tid_from_link(link: str) -> int:
    return int(hashlib.md5(link.encode("utf-8")).hexdigest()[:10], 16)


class UniversalForumSource:
    min_body = 40

    def __init__(self, url: str, proxy: str = "", timeout: int = 30):
        self.base = url.rstrip("/")
        host = urlparse(self.base).hostname or "forum"
        self.name = f"u:{host}"
        self.timeout = timeout
        self._kind: str | None = None
        self._cache: dict[int, dict] = {}
        from curl_cffi import requests as creq
        self.s = creq.Session(impersonate="chrome124")
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})

    # ---------- детект движка ----------
    def _detect(self) -> str:
        if self._kind:
            return self._kind
        probes = [
            ("discourse", f"{self.base}/latest.json"),
            ("xenforo", f"{self.base}/forums/-/index.rss"),
            ("discuz", f"{self.base}/forum.php?mod=rss"),
            ("phpbb", f"{self.base}/feed.php?mode=topics"),
            ("rss", f"{self.base}/rss"),
        ]
        for kind, url in probes:
            try:
                r = self.s.get(url, timeout=self.timeout)
                if r.status_code != 200:
                    continue
                body = r.text[:200000]
                if kind == "discourse" and '"topic_list"' in body:
                    self._kind = kind
                    return kind
                if kind in ("xenforo", "rss") and ("<entry" in body or "<item" in body):
                    self._feed_url = url
                    self._kind = "atom" if "<entry" in body else "rss"
                    return self._kind
                if kind == "discuz" and "<item" in body:
                    self._feed_url = url
                    self._kind = "rss"
                    return "rss"
                if kind == "phpbb" and ("<entry" in body or "<item" in body):
                    self._feed_url = url
                    self._kind = "atom" if "<entry" in body else "rss"
                    return self._kind
            except Exception:
                continue
        # последний шанс: RSS-ссылка из <head> главной
        try:
            r = self.s.get(self.base, timeout=self.timeout)
            m = re.search(
                r'<link[^>]+type="application/(?:rss|atom)\+xml"[^>]+href="([^"]+)"',
                r.text)
            if m:
                href = m.group(1)
                self._feed_url = href if href.startswith("http") else self.base + href
                self._kind = "atom" if "atom" in href else "rss"
                return self._kind
        except Exception:
            pass
        self._kind = "none"
        return self._kind

    # ---------- RSS/Atom ----------
    def _feed_items(self, limit: int) -> list[dict]:
        kind = self._detect()
        url = getattr(self, "_feed_url", None)
        if not url:
            if kind == "rss":
                url = self.base + "/forum.php?mod=rss"
            else:
                return []
        try:
            r = self.s.get(url, timeout=self.timeout)
            if r.status_code != 200:
                return []
            xml = r.text
        except Exception as e:
            log.warning("%s: сеть %s", self.name, e.__class__.__name__)
            return []
        out = []
        if "<entry" in xml:  # Atom
            for item in re.findall(r"<entry>(.*?)</entry>", xml, re.DOTALL)[:limit]:
                def tag(name: str) -> str:
                    m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", item, re.DOTALL)
                    return m.group(1).strip() if m else ""
                link_m = re.search(r'<link[^>]*href="([^"]+)"', item)
                link = link_m.group(1) if link_m else tag("id")
                title = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", tag("title"), flags=re.DOTALL)
                content = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1",
                                 tag("content") or tag("summary"), flags=re.DOTALL)
                author = tag("name")
                cat = ""
                m_c = re.search(r'<category[^>]*term="([^"]+)"', item)
                if m_c:
                    cat = m_c.group(1)
                created = ""
                pd = tag("published") or tag("updated")
                if pd:
                    try:
                        created = datetime.fromisoformat(
                            pd.replace("Z", "+00:00")).isoformat()
                    except ValueError:
                        pass
                if link:
                    out.append({"tid": _tid_from_link(link), "title": title,
                                "url": link, "body_html": content, "author": author,
                                "cat": cat, "created": created})
        else:  # RSS 2.0
            for item in re.findall(r"<item>(.*?)</item>", xml, re.DOTALL)[:limit]:
                def tag(name: str) -> str:
                    m = re.search(
                        rf"<{name}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>",
                        item, re.DOTALL)
                    return m.group(1).strip() if m else ""
                link = tag("link") or tag("guid")
                title = tag("title")
                content = tag("description") or tag("content:encoded")
                created = ""
                pd = tag("pubDate")
                if pd:
                    try:
                        created = parsedate_to_datetime(pd).astimezone(
                            timezone.utc).isoformat()
                    except Exception:
                        pass
                if link:
                    out.append({"tid": _tid_from_link(link), "title": title,
                                "url": link, "body_html": content,
                                "author": tag("dc:creator") or tag("author"),
                                "cat": tag("category"), "created": created})
        for p in out:
            self._cache[p["tid"]] = p
        return out

    # ---------- Discourse ----------
    def _discourse_items(self, limit: int) -> list[dict]:
        try:
            r = self.s.get(f"{self.base}/latest.json?limit={min(limit, 50)}",
                           timeout=self.timeout)
            if r.status_code != 200:
                return []
            topics = r.json().get("topic_list", {}).get("topics", [])[:limit]
        except Exception:
            return []
        out = []
        for t in topics:
            tid = t.get("id")
            if not tid:
                continue
            slug = t.get("slug") or "topic"
            out.append({"tid": tid,
                        "title": t.get("title") or "",
                        "url": f"{self.base}/t/{slug}/{tid}",
                        "body_html": "",  # тело подтянем в fetch() по /t/{id}.json
                        "author": "",
                        "cat": str(t.get("category_id") or ""),
                        "created": t.get("created_at") or ""})
        for p in out:
            self._cache[p["tid"]] = p
        return out

    def _discourse_topic(self, tid: int) -> dict | None:
        try:
            r = self.s.get(f"{self.base}/t/{tid}.json", timeout=self.timeout)
            if r.status_code != 200:
                return None
            d = r.json()
            posts = d.get("post_stream", {}).get("posts", [])
            cooked = posts[0].get("cooked") if posts else ""
            return {"tid": tid, "title": d.get("title") or "",
                    "url": f"{self.base}/t/{d.get('slug') or 'topic'}/{tid}",
                    "body_html": cooked or "",
                    "author": (posts[0].get("username") if posts else ""),
                    "cat": str(d.get("category_id") or ""),
                    "created": d.get("created_at") or ""}
        except Exception:
            return None

    # ---------- jina-фоллбек (CF-гейт/неопознанный движок) ----------
    _JINA_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

    def _jina_get(self, url: str) -> str:
        # ВАЖНО: без явного UA jina отдаёт 403 при chrome-имперсонации curl_cffi
        hdr = {"User-Agent": self._JINA_UA}
        try:
            r = self.s.get("https://r.jina.ai/" + url, timeout=45, headers=hdr)
            if r.status_code == 429:
                import time as _t
                _t.sleep(4)
                r = self.s.get("https://r.jina.ai/" + url, timeout=45, headers=hdr)
            return r.text if r.status_code == 200 else ""
        except Exception:
            return ""

    def _jina_items(self, limit: int) -> list[dict]:
        """Листинг форума через r.jina.ai: markdown-ссылки на треды."""
        host = urlparse(self.base).hostname or ""
        md = self._jina_get(self.base)
        if len(md) < 300:
            return []
        from websources import JUNK_TITLE, PIN_TITLE, clean_title
        out, seen = [], set()
        for m in re.finditer(r"\[([^\]\[]{8,150})\]\((https?://[^\s)\"]+)[^)]*\)", md):
            title, link = m.group(1).strip(), m.group(2).strip()
            if JUNK_TITLE.match(title) or PIN_TITLE.search(title):
                continue
            lhost = urlparse(link).hostname or ""
            path = urlparse(link).path
            if lhost != host or not path or path in ("/",):
                continue
            # тред-подобный путь: содержит /t/, thread-, viewtopic, topic, число
            if not re.search(r"/t/|thread-|viewtopic|topic|/\d", path):
                continue
            if title in seen:
                continue
            seen.add(title)
            out.append({"tid": _tid_from_link(link), "title": title,
                        "url": link, "body_html": "", "author": "",
                        "cat": "", "created": ""})
        for p in out[:limit]:
            self._cache[p["tid"]] = p
        return out[:limit]

    def _md_to_text(self, md: str) -> str:
        md = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md)
        md = re.sub(r"(?m)^(Title|URL Source|Published Time|Markdown Content):.*$", "", md)
        md = re.sub(r"(?m)^\[Image \d+\].*$", "", md)
        return re.sub(r"<[^>]+>", " ", md).strip()

    # ---------- интерфейс источника ----------
    def latest(self, limit: int = 30) -> list[int]:
        kind = self._detect()
        if kind == "discourse":
            items = self._discourse_items(limit)
        elif kind in ("rss", "atom"):
            items = self._feed_items(limit)
        else:
            items = self._jina_items(limit)  # CF-гейт: рендерим через jina
            if items:
                self._kind = "jina"
        return [p["tid"] for p in items]

    def fetch(self, tid: int):
        p = self._cache.get(tid)
        if p is None:
            if self._detect() == "discourse":
                p = self._discourse_topic(tid)
            elif self._kind == "jina":
                self._jina_items(50)
                p = self._cache.get(tid)
            else:
                self._feed_items(50)
                p = self._cache.get(tid)
        if p is None:
            return None
        body_text = _html_to_text(p.get("body_html") or "") or (p.get("title") or "")
        if self._kind == "jina" and len(body_text) < 200:
            body_text = self._md_to_text(self._jina_get(p["url"]))[:6000]
        return CFTopic(
            topic_id=tid,
            title=p["title"],
            url=p["url"],
            category_name=p.get("cat", ""),
            author=p.get("author", ""),
            created_at=p.get("created", ""),
            body_html=p.get("body_html", ""),
            body_text=body_text,
            has_hidden_content=False,
        )
