"""Веб-источники из ai-radar (порт): linux.do, v2ex, 52pojie, lowendtalk,
reddit, huggingface, openai community, lowendspirit, hackernews.

CF-защищённые форумы читаются через r.jina.ai (markdown-прокси), фидовые —
напрямую. Каждый источник = WebListSource с интерфейсом latest()/fetch()
движка. topic_id = стабильный md5-хеш ссылки на тред.
"""
from __future__ import annotations

import hashlib
import html as ihtml
import logging
import re
import time

from cfsource import CFTopic

log = logging.getLogger(__name__)

JINA_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
UA = JINA_UA

# (имя, URL листинга, паттерн ссылки на тред, способ)
WEB_SOURCES = [
    ("linuxdo-welfare", "https://linux.do/c/welfare/36",
     r"https://linux\.do/t/topic/(\d+)", "jina"),
    ("linuxdo-lottery", "https://linux.do/c/welfare/36/l/lottery",
     r"https://linux\.do/t/topic/(\d+)", "jina"),
    ("linuxdo-resource", "https://linux.do/c/resource/14",
     r"https://linux\.do/t/topic/(\d+)", "jina"),
    ("linuxdo-news", "https://linux.do/c/news/34",
     r"https://linux\.do/t/topic/(\d+)", "jina"),
    ("linuxdo-dev", "https://linux.do/c/develop/4",
     r"https://linux\.do/t/topic/(\d+)", "jina"),
    ("linuxdo-gossip", "https://linux.do/c/gossip/11",
     r"https://linux\.do/t/topic/(\d+)", "jina"),
    ("linuxdo-square", "https://linux.do/c/square/110",
     r"https://linux\.do/t/topic/(\d+)", "jina"),
    ("v2ex-free", "https://www.v2ex.com/go/free",
     r"https://www\.v2ex\.com/t/(\d+)", "jina"),
    ("v2ex-ai", "https://www.v2ex.com/go/ai",
     r"https://www\.v2ex\.com/t/(\d+)", "jina"),
    ("v2ex-deals", "https://www.v2ex.com/go/deals",
     r"https://www\.v2ex\.com/t/(\d+)", "jina"),
    ("v2ex-cloud", "https://www.v2ex.com/go/cloud",
     r"https://www\.v2ex\.com/t/(\d+)", "jina"),
    ("lowendtalk-offers", "https://lowendtalk.com/categories/offers",
     r"https://lowendtalk\.com/discussion/(\d+)/[a-z0-9-]+", "rss"),
    ("lowendtalk-hosting", "https://lowendtalk.com/categories/shared-hosting-offers",
     r"https://lowendtalk\.com/discussion/(\d+)/[a-z0-9-]+", "rss"),
    ("52pojie-soft", "https://www.52pojie.cn/forum-16-1.html",
     r"https://www\.52pojie\.cn/thread-(\d+)-1-1\.html", "jina"),
    ("52pojie-tools", "https://www.52pojie.cn/forum-41-1.html",
     r"https://www\.52pojie\.cn/thread-(\d+)-1-1\.html", "jina"),
    ("52pojie-re", "https://www.52pojie.cn/forum-4-1.html",
     r"https://www\.52pojie\.cn/thread-(\d+)-1-1\.html", "jina"),
    ("reddit-localllama", "https://www.reddit.com/r/LocalLLaMA/.rss",
     r"/r/LocalLLaMA/comments/([a-z0-9]+)", "rss_body"),
    ("reddit-openai", "https://www.reddit.com/r/OpenAI/.rss",
     r"/r/OpenAI/comments/([a-z0-9]+)", "rss_body"),
    ("reddit-chatgpt", "https://www.reddit.com/r/ChatGPT/.rss",
     r"/r/ChatGPT/comments/([a-z0-9]+)", "rss_body"),
    ("reddit-sdinfo", "https://www.reddit.com/r/StableDiffusionInfo/.rss",
     r"/r/StableDiffusionInfo/comments/([a-z0-9]+)", "rss_body"),
    ("huggingface-discuss", "https://discuss.huggingface.co/latest",
     r"https://discuss\.huggingface\.co/t/([a-z0-9-]+/\d+)", "jina"),
    ("openai-community", "https://community.openai.com/latest",
     r"https://community\.openai\.com/t/([a-z0-9-]+/\d+)", "jina"),
    ("lowendspirit-offers", "https://lowendspirit.com/categories/offers",
     r"https://lowendspirit\.com/discussion/(\d+)/[a-z0-9-]+", "rss"),
    ("hackernews-ai", "https://hnrss.org/newest?q=AI&points=3&count=15",
     r"news\.ycombinator\.com/item\?id=(\d+)", "rss_body"),
    # hackforums (даркнет/cracking-маркет: раздачи Spotify, SMS-активации,
    # криптеры) — jina-рендер; lowendbox — VPS-халява-офферы (WordPress feed)
    ("hackforums", "https://hackforums.net/", r"showthread\.php\?tid=(\d+)", "jina"),
    ("lowendbox", "https://lowendbox.com/feed/", r"lowendbox\.com/(?:blog/)?([a-z0-9-]{8,})", "rss_url"),
    # серверы/хомлаб/VPS: XenForo-фид; Linux/хардware: Discourse; underground
    ("servethehome", "https://forums.servethehome.com/index.php?forums/-/index.rss",
     r"threads/(\d+)", "rss_url"),
    ("level1techs", "https://forum.level1techs.com/latest.rss",
     r"/t/[^/]+/(\d+)", "rss_url"),
    ("0x00sec", "https://0x00sec.org/rss/",
     r"/t/[^/]+/(\d+)", "rss_url"),
]

JUNK_TITLE = re.compile(
    r"^(Today|Yesterday|Вчера|Сегодня|\d{1,2}:\d{2}|\d{1,2}/\d{1,2}|"
    r"Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|"
    r"Пн|Вт|Ср|Чт|Пт|Сб|Вс|Image \d|!\[|Get your|See \d)", re.I)
DATE_TAIL = re.compile(
    r"\s*((Yesterday|Today|Monday|Tuesday|Wednesday|Thursday|Friday|"
    r"Saturday|Sunday)\s+at.*|(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|"
    r"Nov|Dec)[a-z]*\s+\d{1,2},\s*\d{4}.*|\d{1,2}:\d{2}\s*(AM|PM).*)$",
    re.I)
PIN_TITLE = re.compile(
    r"(版规|总版规|违规怎么办|清理未活跃会员|破解入门|逆向入门|论坛精华集|"
    r"权威发布|优秀会员名单|入门教学|禁止发布任何可能存在商业侵权|关于.*类别)", re.I)

REDDIT_LAST = [0.0]

# ротация: за 5-мин цикл опрашиваем только окно из N веб-источников —
# меньше 429 (reddit/jina), быстрее цикл, полнота за 2-3 цикла
_WEB_PER_CYCLE = 16


def clean_title(t: str) -> str:
    t = re.sub(r"\s{2,}", " ", t).strip()
    for _ in range(3):
        t2 = DATE_TAIL.sub("", t).strip()
        if t2 == t:
            break
        t = t2
    return t


def _feed_txt(s: str) -> str:
    return re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S).strip()


def _strip_tags(s: str) -> str:
    return ihtml.unescape(re.sub(r"<[^>]+>", " ", s)).strip()


def _tid(link: str) -> int:
    return int(hashlib.md5(link.encode("utf-8")).hexdigest()[:10], 16)


class WebListSource:
    # тред-фечи через jina дорогие — берём немного нового за цикл
    min_body = 40
    _seq = 0  # порядковый номер для round-robin ротации

    def __init__(self, name: str, list_url: str, pattern: str,
                 how: str = "jina", proxy: str = "", timeout: int = 45):
        self.name = name
        self.list_url = list_url
        self.pattern = pattern
        self.how = how
        self.timeout = timeout
        self._cache: dict[int, dict] = {}
        self._seq = WebListSource._seq
        WebListSource._seq += 1
        from curl_cffi import requests as creq
        self.s = creq.Session(impersonate="chrome124")
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})

    # ---------- транспорт ----------
    def _jina_get(self, url: str) -> str:
        for attempt in (1, 2):
            try:
                r = self.s.get("https://r.jina.ai/" + url, timeout=self.timeout,
                               headers={"User-Agent": JINA_UA})
                if r.status_code == 200:
                    return r.text
                if r.status_code == 429 and attempt == 1:
                    time.sleep(4)
                    continue
                return ""
            except Exception:
                if attempt == 1:
                    time.sleep(2)
                    continue
                return ""
        return ""

    def _http_get(self, url: str) -> str:
        try:
            r = self.s.get(url, timeout=self.timeout)
            return r.text if r.status_code == 200 else ""
        except Exception:
            return ""

    # ---------- парсеры ----------
    def _parse_topics(self, md: str) -> list[dict]:
        items, seen = [], set()
        for m in re.finditer(r"\[([^\]]{8,150})\]\((https?://[^\s)\"]+)[^)]*\)", md):
            title, link = m.group(1).strip(), m.group(2).strip()
            idm = re.search(self.pattern, link)
            if not idm or idm.group(1) in seen:
                continue
            seen.add(idm.group(1))
            if JUNK_TITLE.match(title) or PIN_TITLE.search(title):
                continue
            title = clean_title(title)
            if len(title) < 6:
                continue
            items.append({"title": title, "link": link, "body": ""})
        return items

    def _parse_feed(self, raw: str, with_body: bool = False) -> list[dict]:
        items, seen = [], set()
        blocks = re.findall(r"<item>(.*?)</item>", raw, re.S)
        blocks += re.findall(r"<entry>(.*?)</entry>", raw, re.S)
        bodies = {}
        if with_body:
            for pair in re.findall(r"<entry>(.*?)</entry>|<item>(.*?)</item>",
                                   raw, re.S):
                block = pair[0] or pair[1]
                lm = (re.search(r'<link[^>]*href="([^"]+)"', block)
                      or re.search(r"<link>(.*?)</link>", block, re.S))
                cm = (re.search(r'<content type="html">(.*?)</content>', block, re.S)
                      or re.search(r"<description>(.*?)</description>", block, re.S))
                if lm and cm:
                    bodies[_feed_txt(lm.group(1)).split("#")[0]] = _feed_txt(cm.group(1))
        for b in blocks:
            tm = re.search(r"<title>(.*?)</title>", b, re.S)
            lm = (re.search(r'<link[^>]*href="([^"]+)"', b)
                  or re.search(r"<link>(.*?)</link>", b, re.S))
            if not tm or not lm:
                continue
            title, link = _feed_txt(tm.group(1)), _feed_txt(lm.group(1))
            idm = re.search(self.pattern, link)
            if not idm or idm.group(1) in seen:
                continue
            seen.add(idm.group(1))
            if JUNK_TITLE.match(title):
                continue
            title = clean_title(title)
            if len(title) < 6:
                continue
            items.append({"title": title, "link": link,
                          "body": bodies.get(link.split("#")[0], "")})
        return items

    # ---------- интерфейс источника ----------
    def latest(self, limit: int = 30) -> list[int] | None:
        # round-robin по эпохе (5-мин слот). seq*7 (взаимно простой с total)
        # разбрасывает СОСЕДНИЕ источники (reddit-сабы идут подряд) по окнам:
        # s=20..23 → 20,3,10,17 — максимум 1-2 реддита в окне из 10.
        # None = «не мой слот» — engine пропускает молча (не [] с варнингом).
        total = WebListSource._seq
        if total > _WEB_PER_CYCLE:
            epoch = int(time.time() // 300)
            if (self._seq * 7 + epoch) % total >= _WEB_PER_CYCLE:
                return None
        if "reddit.com" in self.list_url:  # анти-429
            wait = 25 - (time.time() - REDDIT_LAST[0])
            if wait > 0:
                time.sleep(min(wait, 20))
            REDDIT_LAST[0] = time.time()
        topics = None
        if self.how == "jina":
            raw = self._jina_get(self.list_url)
            if len(raw) > 300:
                topics = self._parse_topics(raw)
        elif self.how == "rss":
            raw = self._http_get(self.list_url.rstrip("/") + "/feed.rss")
            if len(raw) > 300:
                topics = self._parse_feed(raw)
        elif self.how == "rss_url":
            # фид по прямому URL (WordPress /feed/ и т.п.)
            raw = self._http_get(self.list_url)
            if len(raw) > 300:
                topics = self._parse_feed(raw)
        elif self.how == "rss_body":
            raw = self._http_get(self.list_url)
            if len(raw) < 300:
                time.sleep(5)
                raw = self._http_get(self.list_url)
            if len(raw) < 300 and "www.reddit.com" in self.list_url:
                # 429 с www — пробуем old.reddit (другой лимит-бакет)
                raw = self._http_get(
                    self.list_url.replace("www.reddit.com", "old.reddit.com"))
            if len(raw) > 300:
                topics = self._parse_feed(raw, with_body=True)
        if topics is None:
            log.warning("%s: листинг недоступен", self.name)
            return []
        for it in topics[:limit]:
            self._cache[_tid(it["link"])] = it
        return [_tid(it["link"]) for it in topics[:limit]]

    def fetch(self, tid: int):
        it = self._cache.get(tid)
        if it is None:
            return None
        body = it.get("body") or ""
        if len(body) < 200 and self.how in ("jina", "rss", "rss_url"):
            body = self._jina_get(it["link"])[:6000]
        elif len(body) < 200 and self.how == "rss":
            body = self._jina_get(it["link"])[:6000]
        # markdown/HTML -> текст
        md = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", body)
        # jina-бойлерплейт: шапка Title/URL Source/Published Time
        md = re.sub(r"(?m)^(Title|URL Source|Published Time|Markdown Content):.*$", "", md)
        body_txt = _strip_tags(md).strip()
        return CFTopic(
            topic_id=tid,
            title=it["title"],
            url=it["link"],
            author=self.name,
            body_html="",
            body_text=body_txt,
            has_hidden_content=False,
        )
