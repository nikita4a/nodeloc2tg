"""Универсальный источник за Cloudflare: LinuxDo (Discourse) и V2EX (JSON API).

Оба форума закрыты CF-щитом: напрямую/с RU-IP — 403. Проходят через
cloudscraper + прокси (socks5h://127.0.0.1:1080 — v2rayN/Mullvad юзера).

Интерфейс совместим с NodeSeekClient: latest() + fetch() → Topic-подобный
объект, engine подключает их одинаково.
"""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger(__name__)


class CFTopic:
    """Тема в формате, совместимом с engine._process_topic."""
    __slots__ = ("topic_id", "title", "url", "category_name", "category_id",
                 "author", "created_at", "body_html", "body_text",
                 "image_url", "image_urls", "has_hidden_content", "tg_url",
                 "video_url")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k, "" if k != "topic_id" and k != "category_id" else 0))
        if not self.image_urls:
            self.image_urls = [self.image_url] if self.image_url else []


class CFSource:
    """Один CF-закрытый форум.

    kind='discourse' — /latest.json + /t/{id}.json (LinuxDo и любые Discourse)
    kind='v2ex'      — /api/topics/latest.json + /api/topics/show.json (V2EX)
    """

    def __init__(self, name: str, base: str, kind: str = "discourse",
                 proxy: str = "", timeout: int = 40, min_delay_ms: int = 800):
        self.name = name
        self.base = base.rstrip("/")
        self.kind = kind
        self.timeout = timeout
        self._min_delay_ms = min_delay_ms
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                  "http_proxy", "https_proxy", "all_proxy"):
            os.environ.pop(k, None)
        # curl_cffi: TLS-фингерпринт Chrome проходит жёсткий CF-челлендж
        from curl_cffi import requests as creq
        self.s = creq.Session(impersonate="chrome124")
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})
        self._last = 0.0
        import time as _t
        self._time = _t

    def _get(self, url: str, want_json: bool = True):
        import time
        for attempt in range(1, 3):
            elapsed = time.monotonic() - self._last
            if elapsed < self._min_delay_ms / 1000:
                time.sleep(self._min_delay_ms / 1000 - elapsed)
            try:
                self._last = time.monotonic()
                r = self.s.get(url, timeout=self.timeout,
                               headers={"Accept": "application/json"})
                if r.status_code == 200:
                    return r.json() if want_json else r.text
                log.warning("%s %s → HTTP %s (попытка %d)",
                            self.name, url[:60], r.status_code, attempt)
            except Exception as e:
                log.warning("%s сеть: %s (%s)", self.name, url[:50],
                            e.__class__.__name__)
            if attempt < 2:
                time.sleep(4 * attempt)
        return None

    # ---------- список тем ----------
    def latest(self, limit: int = 30) -> list[int]:
        if self.kind == "discourse":
            data = self._get(f"{self.base}/latest.json")
            if not data:
                return []
            topics = data.get("topic_list", {}).get("topics", [])
            return [t["id"] for t in topics if "id" in t][:limit]
        # v2ex
        data = self._get(f"{self.base}/api/topics/latest.json")
        if not isinstance(data, list):
            return []
        return [t["id"] for t in data if isinstance(t, dict) and "id" in t][:limit]

    # ---------- одна тема ----------
    def fetch(self, topic_id: int):
        from discourse import _html_to_text, _image_urls, _detect_hidden_content
        if self.kind == "discourse":
            data = self._get(f"{self.base}/t/{topic_id}.json")
            if not data:
                return None
            try:
                posts = data.get("post_stream", {}).get("posts", [])
                if not posts:
                    return None
                first = posts[0]
                body_html = first.get("cooked") or ""
                slug = data.get("slug") or "topic"
                return CFTopic(
                    topic_id=topic_id,
                    title=(data.get("title") or "").strip(),
                    url=f"{self.base}/t/{slug}/{topic_id}",
                    author=first.get("username") or "",
                    created_at=data.get("created_at") or "",
                    body_html=body_html,
                    body_text=_html_to_text(body_html),
                    image_urls=_image_urls(body_html, self.base, limit=5),
                    has_hidden_content=_detect_hidden_content(body_html),
                )
            except Exception as e:
                log.exception("%s: разбор темы %d: %s", self.name, topic_id, e)
                return None
        # v2ex
        data = self._get(f"{self.base}/api/topics/show.json?id={topic_id}")
        if isinstance(data, list):
            data = data[0] if data else None  # v2ex оборачивает ответ в массив
        if not isinstance(data, dict) or not data:
            return None
        try:
            body_html = data.get("content", "") or ""
            created = data.get("created") or ""
            if isinstance(created, (int, float)):
                import datetime
                created = datetime.datetime.fromtimestamp(
                    created, datetime.timezone.utc).isoformat()
            member = data.get("member") or {}
            return CFTopic(
                topic_id=topic_id,
                title=(data.get("title") or "").strip(),
                url=f"{self.base}/t/{topic_id}",
                author=member.get("username") or "",
                created_at=str(created),
                body_html=body_html,
                body_text=_html_to_text(body_html),
                image_urls=_image_urls(body_html, self.base, limit=5),
                has_hidden_content=False,
            )
        except Exception as e:
            log.exception("%s: разбор v2ex темы %d: %s", self.name, topic_id, e)
            return None


# Тематический фильтр LinuxDo: тот же принцип что у NodeSeek —
# белый список техно/халява ключевиков, чёрной список шаблонов.
_LDO_BLACK = re.compile(r"水贴|无聊|日记|打卡|周末|晚安|早安", re.IGNORECASE)
_LDO_WHITE = re.compile(
    r"""(?:
        AI|GPT|Claude|Gemini|DeepSeek|Kimi|Qwen|Llama|OpenAI|Anthropic
        |免费|白嫖|羊毛|优惠|折扣|抽奖|赠送|送
        |域名|domain|SSL|GitHub|开源|脚本|教程|Docker|API
        |ChatGPT|Copilot|Office|M365|Linux|VPS|服务器|NAS
        |虚拟卡|信用卡|支付|合租|开发|编程|Python|Rust
    )""", re.IGNORECASE | re.VERBOSE)


def ldo_title_allowed(title: str) -> bool:
    if not title:
        return False
    if _LDO_BLACK.search(title):
        return False
    return bool(_LDO_WHITE.search(title))
