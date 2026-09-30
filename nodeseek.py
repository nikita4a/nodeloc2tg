"""Клиент к NodeSeek (nodeseek.com) — через cloudscraper (обход Cloudflare).

Официального API нет; парсим HTML главной (список свежих тем) и страницы
поста (контент/время/картинки). Прокси обязателен — с RU напрямую резется,
а системный env-прокси может быть мёртвым (вычищаем).

Формат Topic совместим с discourse.Topic (те же поля) — engine работает
с обоими источниками одинаково.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

BASE = "https://www.nodeseek.com"

# ---------------------------------------------------------------------------
# Тематические фильтры NodeSeek. Без них канал заливает шаблонный поток
# сетевых тестов VPS («TCP-тест», «обратный маршрут», 测评留档 — авторы
# гоняют их десятками в день). Режим белого списка: постим только темы
# с техно/халява-ключевиками; чёрные паттерны бьют первыми.
# ---------------------------------------------------------------------------
import re as _ns_re
NS_BLACKLIST = _ns_re.compile(
    r"""(?:
        测评|留档|回程|路由|TCP|NQ|家宽|IPLC|专线|9929|4837
        |PING|ping站|延迟|测速|speedtest|去程
        |netframework|路由器测
    )""", _ns_re.IGNORECASE | _ns_re.VERBOSE)
NS_WHITELIST = _ns_re.compile(
    r"""(?:
        AI|GPT|Claude|Gemini|DeepSeek|Kimi|Qwen|Llama|OpenAI|Anthropic
        |免费|白嫖|羊毛|优惠|折扣|抽奖|赠送|送|注册送|低价|甩卖
        |域名|domain|SSL|GitHub|开源|脚本|教程|Docker|API
        |ChatGPT|Copilot|Office|M365|微软|Google|谷歌
        |虚拟卡|信用卡|支付|账号|合租|车
    )""", _ns_re.IGNORECASE | _ns_re.VERBOSE)


def ns_title_allowed(title: str) -> bool:
    """True если заголовок NodeSeek-темы проходит тематический фильтр."""
    if not title:
        return False
    if NS_BLACKLIST.search(title):
        return False
    return bool(NS_WHITELIST.search(title))


@dataclass(frozen=True)
class NSTopic:
    topic_id: int
    title: str
    url: str
    category_name: str
    category_id: int
    author: str
    created_at: str          # ISO (пусто если не нашли — возраст не фильтруем)
    body_html: str
    body_text: str
    image_url: str = ""
    image_urls: list = None
    has_hidden_content: bool = False

    def __post_init__(self):
        if self.image_urls is None:
            object.__setattr__(self, "image_urls",
                               [self.image_url] if self.image_url else [])


class NodeSeekClient:
    def __init__(self, proxy: str = "", timeout: int = 40,
                 min_delay_ms: int = 800):
        # вычистить чужие системные прокси — cloudscraper внутри плодит
        # запросы мимо явных proxies, env-мусор (мёртвый 10809) их ломает
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                  "http_proxy", "https_proxy", "all_proxy"):
            os.environ.pop(k, None)
        # curl_cffi имитирует TLS-фингерпринт реального Chrome — проходит
        # жёсткий CF-челлендж NodeSeek, который cloudscraper больше не берёт
        from curl_cffi import requests as creq
        self._creq = creq
        self.s = creq.Session(impersonate="chrome124")
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})
        self.timeout = timeout
        self._min_delay = min_delay_ms / 1000.0
        self._last_req = 0.0

    # ---------- низкий уровень ----------
    def _get(self, url: str) -> str | None:
        for attempt in range(1, 4):
            elapsed = time.monotonic() - self._last_req
            if elapsed < self._min_delay:
                time.sleep(self._min_delay - elapsed)
            try:
                self._last_req = time.monotonic()
                r = self.s.get(url, timeout=self.timeout)
                if r.status_code == 200:
                    return r.text
                log.warning("NodeSeek %s → HTTP %s (попытка %d)",
                            url[:60], r.status_code, attempt)
                if r.status_code in (403, 503) and attempt < 3:
                    time.sleep(5 * attempt)
                    continue
                return None
            except Exception as e:
                log.warning("NodeSeek сеть: %s (%s) попытка %d",
                            url[:60], e.__class__.__name__, attempt)
                if attempt < 3:
                    time.sleep(5 * attempt)
        return None

    # ---------- список свежих тем с главной ----------
    def latest(self, limit: int = 30) -> list[int]:
        """topic_id свежих тем с главной, прошедших тематический фильтр.

        Шаблонные сетевые тесты VPS (TCP-тест/回程/NQ...) отсеиваются ещё
        здесь — до запроса страниц постов.
        """
        html = self._get(f"{BASE}/")
        if not html:
            return []
        pairs = re.findall(
            r'class="post-title"><a href="/post-(\d+)[^"]*"[^>]*>([^<]+)</a>',
            html)
        seen, out, skipped = set(), [], 0
        for pid, title in pairs:
            if pid in seen:
                continue
            seen.add(pid)
            if not ns_title_allowed(title):
                skipped += 1
                continue
            out.append(int(pid))
            if len(out) >= limit:
                break
        if skipped:
            log.debug("NodeSeek: отсеяно фильтром %d шаблонных тем", skipped)
        return out

    # ---------- один пост ----------
    def fetch(self, topic_id: int) -> NSTopic | None:
        html = self._get(f"{BASE}/post-{topic_id}-1")
        if not html:
            return None
        try:
            title_m = re.search(
                r'<h1[^>]*class="post-title[^"]*"[^>]*>(.*?)</h1>', html,
                re.DOTALL)
            if not title_m:
                # запасной: <title>
                title_m = re.search(r"<title>([^<]+)</title>", html)
            title = _strip_tags(title_m.group(1)) if title_m else ""

            # время первого поста (первый datetime в документе)
            created = ""
            dt = re.search(r'datetime="([^"]+)"', html)
            if dt:
                created = dt.group(1)

            # автор: ссылка /space/N у шапки поста
            author = ""
            au = re.search(r'class="info-author".*?<a[^>]*>([^<]+)</a>', html,
                           re.DOTALL)
            if au:
                author = au.group(1).strip()

            # контент первого поста
            body_html = ""
            cm = re.search(r'class="post-content[^"]*"[^>]*>(.*?)</div>\s*(?:<div class="post-(?:info|reply)|$)',
                           html, re.DOTALL)
            if not cm:
                cm = re.search(r'class="post-content[^"]*"[^>]*>(.*?)</div>',
                               html, re.DOTALL)
            if cm:
                body_html = cm.group(1)

            # чистка HTML — переиспользуем чистильщик NodeLoc-парсера
            from discourse import _html_to_text, _image_urls, \
                _detect_hidden_content
            body_text = _html_to_text(body_html)
            imgs = _image_urls(body_html, BASE, limit=5)
            # скипаем стикеры/аватары/иконки (в _image_urls фильтр по классам,
            # у NodeSeek стикеры имеют class="sticker" — добавим руками)
            imgs = [u for u in imgs
                    if "/sticker/" not in u and "/avatar/" not in u
                    and "/static/image/" not in u]

            return NSTopic(
                topic_id=topic_id,
                title=title.strip(),
                url=f"{BASE}/post-{topic_id}-1",
                category_name="",
                category_id=0,
                author=author,
                created_at=created,
                body_html=body_html,
                body_text=body_text,
                image_url=imgs[0] if imgs else "",
                image_urls=imgs,
                has_hidden_content=_detect_hidden_content(body_html),
            )
        except Exception as e:
            log.exception("NodeSeek: ошибка разбора поста %d: %s", topic_id, e)
            return None


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()
