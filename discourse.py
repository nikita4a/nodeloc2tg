"""Клиент к форуму NodeLoc (Discourse).

Discourse отдаёт JSON без авторизации, по простым GET с Accept: application/json.
Браузер/JS-рендер НЕ нужен.

Эндпоинты:
  /latest.json        — список последних тем
  /t/{topic_id}.json  — полная тема: заголовок, тело первого поста, автор, ссылка
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable

import requests

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Topic:
    topic_id: int
    title: str          # оригинальный (китайский)
    url: str            # https://www.nodeloc.com/t/{slug}/{id}
    category_name: str  # напр. "AI", "羊毛党"
    category_id: int    # ID категории NodeLoc (для фильтрации)
    author: str         # username автора
    created_at: str     # ISO
    body_html: str      # HTML первого поста (cooked в Discourse)
    body_text: str      # текст без тегов, для перевода
    image_url: str = ""  # абсолютный URL первой картинки темы (или "")
    image_urls: list = None  # все содержательные картинки (до 5), для альбома

    def __post_init__(self):
        if self.image_urls is None:
            self.image_urls = ([self.image_url] if self.image_url else [])
    has_hidden_content: bool = False  # есть ли reply-to-view блок (请回复后查看)


class DiscourseClient:
    def __init__(self, base_url: str, user_agent: str, timeout: int = 20,
                 min_delay_ms: int = 350):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self._min_delay = min_delay_ms / 1000.0
        self._last_req = 0.0
        self.session = requests.Session()
        # trust_env=False: не подхватывать HTTP(S)_PROXY из окружения —
        # NodeLoc доступен напрямую, прокси нужен только переводчику/TG.
        self.session.trust_env = False
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "application/json",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )

    # ---------- низкоуровневый GET с лёгким retry ----------
    def _get(self, path: str, *, retries: int = 3) -> dict | None:
        url = f"{self.base}{path}"
        last_err: Exception | None = None
        for attempt in range(1, retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=self.timeout)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (429, 502, 503, 504):
                    wait = 2 ** attempt
                    log.warning(
                        "HTTP %s на %s, жду %ds (попытка %d/%d)",
                        resp.status_code, path, wait, attempt, retries,
                    )
                    time.sleep(wait)
                    continue
                # 404 / другие — не ретраим
                log.warning("HTTP %s на %s: %s", resp.status_code, path, resp.text[:200])
                return None
            except requests.RequestException as e:
                last_err = e
                wait = 2 ** attempt
                log.warning("Сеть: %s на %s, жду %ds (попытка %d/%d)",
                            e.__class__.__name__, path, wait, attempt, retries)
                time.sleep(wait)
        log.error("Не удалось получить %s после %d попыток: %s", path, retries, last_err)
        return None

    def _throttle(self) -> None:
        """Минимальная задержка между запросами к форуму — защита от 429."""
        elapsed = time.monotonic() - self._last_req
        if elapsed < self._min_delay:
            time.sleep(self._min_delay - elapsed)
        self._last_req = time.monotonic()

    # ---------- список последних тем ----------
    def latest_topic_ids(self, limit: int,
                         allowed_categories: frozenset[int] | None = None,
                         keyword_categories: dict[int, "re.Pattern"] | None = None,
                         filtered_out: list | None = None,
                         ) -> list[int]:
        """Возвращает topic_id последних тем, самые свежие первыми.

        Двухуровневый фильтр категорий (срабатывает ДО запроса /t/{id}.json,
        экономя трафик и запросы перевода):
          1. allowed_categories: множество ID, пропускаемых безусловно.
             Если None/пусто — фильтр категорий выключен.
          2. keyword_categories: {cat_id: compiled_regex}. Тема такой категории
             проходит ТОЛЬКО если в заголовке есть совпадение с regex.
             Гибрид для offtopic-разделов (杂谈), где техно и бытовуха вперемешку.

        filtered_out: если список передан — в него складываются отсеянные темы
        {reason, cat, title} для журнала часового отчёта.
        """
        data = self._get("/latest.json")
        if not data:
            return []
        topics = data.get("topic_list", {}).get("topics", [])
        # Discourse отдаёт уже отсортированным по последней активности.
        out: list[int] = []
        for t in topics:
            if "id" not in t:
                continue
            cat = t.get("category_id")
            title = t.get("title", "") or ""
            # разрешающие правила
            in_allowed = (not allowed_categories) or (cat in allowed_categories)
            # гибридные: даже если не в allowed, могут пройти по ключевику
            kw_rule = keyword_categories.get(cat) if keyword_categories else None
            if kw_rule:
                if kw_rule.search(title):
                    in_allowed = True
            if not in_allowed:
                if filtered_out is not None and len(filtered_out) < 40:
                    filtered_out.append({
                        "reason": "категория",
                        "cat": cat,
                        "title": title,
                    })
                continue
            out.append(t["id"])
            if len(out) >= limit:
                break
        return out

    # ---------- одна тема ----------
    def fetch_topic(self, topic_id: int) -> Topic | None:
        data = self._get(f"/t/{topic_id}.json")
        if not data:
            return None
        try:
            title = (data.get("title") or "").strip()
            category_id = data.get("category_id")
            category_name = self._category_name(data, category_id)
            slug = data.get("slug") or "topic"
            url = f"{self.base}/t/{slug}/{topic_id}"

            # Первый пост = пост-открытие темы
            post_stream = data.get("post_stream", {})
            posts = post_stream.get("posts", [])
            if not posts:
                log.info("Тема %d без постов — пропускаю", topic_id)
                return None
            first = posts[0]
            body_html = first.get("cooked") or ""
            author = (first.get("username") or data.get("details", {})
                      .get("created_by", {}).get("username") or "unknown")
            created_at = data.get("created_at") or first.get("created_at") or ""
            body_text = _html_to_text(body_html)
            image_urls = _image_urls(body_html, self.base, limit=5)
            image_url = image_urls[0] if image_urls else ""
            # Детект скрытого контента: Discourse ставит плейсхолдер-спаны
            # классов permission-reply-placeholder / reply-to-see / скрытый
            # spoiler под ответ. Проверяем по HTML ДО чистки, т.к. потом
            # текст плейсхолдера вырезается бойлерплейт-фильтром.
            has_hidden = _detect_hidden_content(body_html)

            return Topic(
                topic_id=topic_id,
                title=title,
                url=url,
                category_name=category_name,
                category_id=category_id if isinstance(category_id, int) else 0,
                author=author,
                created_at=created_at,
                body_html=body_html,
                body_text=body_text,
                image_url=image_url,
                image_urls=image_urls,
                has_hidden_content=has_hidden,
            )
        except Exception as e:
            log.exception("Ошибка разбора темы %d: %s", topic_id, e)
            return None

    def fetch_many(self, topic_ids: Iterable[int]) -> list[Topic]:
        out: list[Topic] = []
        for tid in topic_ids:
            t = self.fetch_topic(tid)
            if t:
                out.append(t)
        return out

    # ---------- вспомогательное ----------
    def _category_name(self, topic_json: dict, category_id: int | None) -> str:
        # В /t/{id}.json часто есть categories внутри (нет) — но в /latest.json есть.
        # Делаем fallback: подгрузим список категорий один раз и закэшируем.
        if category_id is None:
            return ""
        if not hasattr(self, "_cat_cache"):
            self._cat_cache: dict[int, str] = {}
        if category_id in self._cat_cache:
            return self._cat_cache[category_id]
        # /categories.json — общий список
        cats = self._get("/categories.json")
        if cats:
            for cat in cats.get("category_list", {}).get("categories", []):
                self._cat_cache[cat["id"]] = cat.get("name", "")
        return self._cat_cache.get(category_id, "")


# ---------------------- утилиты ----------------------

def _html_to_text(html: str) -> str:
    """Грубый, но достаточный конвертер HTML→текст для перевода.

    Discourse 'cooked' содержит aside-onebox (превью ссылок),
    картинки, цитаты. Убираем теги, склеиваем пробелы, чистим типовой мусор.
    """
    import re
    if not html:
        return ""
    # Onebox-вставки (превью ссылок): выносим URL отдельной строкой —
    # автор вставил ссылку как контент, терять её нельзя. Заголовок/описание
    # превью выкидываем (дублируют текст). URL берём из data-onebox-src,
    # запасной — первый <a href> внутри.
    def _onebox_repl(m):
        block = m.group(0)
        url = ""
        src = re.search(r'data-onebox-src="([^"]+)"', block)
        if src:
            url = src.group(1)
        else:
            href = re.search(r'<a[^>]+href="([^"]+)"', block)
            if href:
                url = href.group(1)
        return f"\n🔗 {url}\n" if url else " "
    html = re.sub(
        r"<aside\b[^>]*\bonebox\b[^>]*>.*?</aside>",
        _onebox_repl, html, flags=re.DOTALL | re.IGNORECASE)
    # прочие aside (цитаты и пр.) — выкидываем целиком: в первом посте
    # цитат обычно нет, а если есть — это чужой текст
    html = re.sub(r"<aside\b[^>]*>.*?</aside>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    # <div class="meta"> внутри lightbox — имя файла + размер («学生认证3 1920×1115 186 KB»)
    html = re.sub(r"<div\b[^>]*class=[\"'][^\"']*\bmeta\b[\"'][^>]*>.*?</div>",
                  " ", html, flags=re.DOTALL | re.IGNORECASE)
    # удаляем <img ...> ЦЕЛИКОМ — alt не вставляем: на NodeLoc это мусор
    # (имена скриншотов, «学生认证3» и т.п.), картинки уходят в пост как фото
    html = re.sub(r"<img\b[^>]*>", " ", html, flags=re.IGNORECASE)
    # скрипты/стили — долой
    html = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ",
                  html, flags=re.DOTALL | re.IGNORECASE)
    # <br> → перенос
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    # блочные теги → перенос
    html = re.sub(r"</(p|div|li|h[1-6]|tr)>", "\n", html, flags=re.IGNORECASE)
    # удаляем остальные теги
    text = re.sub(r"<[^>]+>", "", html)
    # сущности
    text = (text
            .replace("&nbsp;", " ")
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
            .replace("&hellip;", "…"))
    # Типовой мусор Discourse:
    #  - метаданные картинок: "<хеш>×<число> NN КБ"  (напр. "06d6463a...×288 7,94 КБ")
    #  - чистые хеши файлов: 32-значные hex
    text = re.sub(r"\b[0-9a-fA-F]{32}\b", " ", text)
    text = re.sub(r"\b[0-9a-fA-F]{16,}×\d+(?:\s*[,.\d]+\s*[КкMм]?[Бб])?", " ", text)
    text = re.sub(r"\b\d{3,4}×\d{3,4}(?:\s*[,.\d]+\s*[КкMм]?[Бб])?", " ", text)
    text = re.sub(r"\b\d+(?:[.,]\d+)?\s*[КкKkMmГг]?[БбBb]\b", " ", text)  # размеры «7,94 КБ»
    # размеры картинок: «2433×1385», «1262433×1385», с любым разделителем ×/x/х
    text = re.sub(r"\d+\s*[×xхХ]\s*\d+", " ", text)
    # alt-плейсхолдеры со склейкой: «[Snapzy_...]Snapzy_... » целиком в мусор
    text = re.sub(r"\[?\s*(?:Snapzy|Screenshot|Screen|image|img|IMG)[\w\-. ]*\]?", " ",
                  text, flags=re.IGNORECASE)
    # пустые alt-плейсхолдеры от картинок: "[ ]" или "[]" в одиночку
    text = re.sub(r"(?m)^\s*\[\s*\]\s*$", " ", text)
    text = re.sub(r"\[\s*\]", " ", text)
    # маски скрытого контента и бойлерплейт Discourse — вырезаем,
    # иначе они засоряют пост. Многоязычные варианты (zh/en/ru после перевода
    # могут попасть сюда в оригинале — чистим и по-китайски, и по-русски).
    text = _strip_boilerplate(text)

    # склеиваем множественные пробелы/переносы
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # тролль-посты: «哦齁齁齁...» (смех×200), повторы одной фразы.
    # Если разнообразие символов ничтожно — контента нет, тело выбрасываем.
    if len(text) > 60:
        # тролль-детект только для CJK-текста: латиница при 6% уникальных
        # символов — норма для английского, а не признак «ха-ха×200»
        cjk = sum(1 for ch in text if chr(0x4e00) <= ch <= chr(0x9fff))
        if cjk > len(text) * 0.2:
            uniqueness = len(set(text)) / len(text)
            if uniqueness < 0.06:
                return ""
    # пустые строки в начале/конце абзацев
    return text.strip()


# Шаблоны бойлерплейта Discourse. Регулярки компилируем один раз.
import re as _re
_BOILERPLATE = [
    # плейсхолдер скрытого контента (reply-to-see) — все языки + переведённый вид.
    # Берём агрессивно: от ключевой фразы ДО конца строки.
    _re.compile(r"请\s*回\s*复[^\n]*", _re.IGNORECASE),
    _re.compile(r"(?s)该内容[^\n]*", _re.IGNORECASE),
    _re.compile(r"本帖[^\n]*隐藏[^\n]*", _re.IGNORECASE),
    _re.compile(r"隐藏内容[^\n]*", _re.IGNORECASE),
    _re.compile(r"reply[\s\-]*to[\s\-]*(view|see|reveal)[^\n]*", _re.IGNORECASE),
    _re.compile(r"please[^\n]*(reply|log\s*in|sign\s*in)[^\n]*", _re.IGNORECASE),
    _re.compile(r"(?i)скрыт[а-яё]*[^\n]*(содержимое|контент|часть)[^\n]*"),
    _re.compile(r"(?i)ответьте[^\n]*"),
    _re.compile(r"(?i)пожалуйста[^\n]*(ответ|просмотр|войдите|войти)[^\n]*"),
    _re.compile(r"(?i)содержимое\s+скрыто[^\n]*"),
    _re.compile(r"(?i)войдите,?\s+чтобы[^\n]*"),
    # обрезки после truncation: висячее «Please» / «Пожалуйста» в конце
    _re.compile(r"(?im)^\s*(please|пожалуйста)\s*$"),
    # плейсхолдер trust-level гейта: всё тело = эта служебная фраза
    _re.compile(r"this topic requires[^\n]*trust level[^\n]*", _re.IGNORECASE),
    _re.compile(r"(?i)please\s+increase\s+your\s+trust\s+level[^\n]*"),
    _re.compile(r"(?i)требуется\s+уровень\s+доверия[^\n]*"),
    # призывы к лайку/ответу/подписке — типовой хвост тем NodeLoc
    _re.compile(r"(?i)(如果|若)[^\n]{0,20}(喜欢|觉得好|觉得不错|觉得可以)[^\n]{0,50}(点赞|回复|评论|支持|评分|表情|留言)[^\n]*"),
    _re.compile(r"(?i)点赞[^\n]{0,30}(支持|回复|感谢|表情)[^\n]*"),
    _re.compile(r"(?i)не\s+забудь[^\n]*"),
    _re.compile(r"(?i)(спасибо|благодар)[^\n]{0,30}(лайк|оценк|поддержк)[^\n]*"),
    _re.compile(r"(?i)(если\s+(вам|понрав|нрав)|постав[^\n]{0,10}лайк)[^\n]*"),
    _re.compile(r"(?i)оставь[^\n]{0,20}(смайл|эмодзи|коммент)[^\n]*"),
    # «адрес/ссылка ниже:» висячие подписи без адреса — все варианты написания.
    # 链接/连结/网址/地址 + возможный префикс (优惠/购买/注册/...) и хвост «如下/:»
    _re.compile(r"[*＊*·•\-]?\s*[\u4e00-\u9fff]{0,6}(地址|链接|连结|网址|網址|鏈接|連結)\s*(如下|在下面|在下方|如下所示)?\s*[:：]?\s*$",
                _re.IGNORECASE | _re.MULTILINE),
    _re.compile(r"(?im)^\s*(адрес|ссылка|url|link)\s*(ниже|таков|следующий)?\s*[:：]?\s*$"),
    _re.compile(r"(?im)^\s*[*＊]?\s*(скидочн\w*|акционн\w*)?\s*(ссылка|адрес|url)[^\n:]{0,20}[:：]?\s*$"),
    # подписи разделов «скриншот/картинка» — одиночные строки-метки
    _re.compile(r"(?im)^\s*(截图|图片|附图|配图|效果图)\s*[:：]?\s*$"),
    _re.compile(r"(?im)^\s*(скриншот|изображение|картинк[аи])\s*[:：]?\s*$"),
    # «если не знаешь что писать, можешь:»
    _re.compile(r"(?i)(如果|若)\s*(不知道|不知)[^\n]{0,30}(回复|发|写|留言)[^\n]*"),
]


def _strip_boilerplate(text: str) -> str:
    """Удалить бойлерплейт Discourse построчно + отшлифовать пустые строки."""
    if not text:
        return text
    for pat in _BOILERPLATE:
        text = pat.sub("\n", text)  # заменяем на перенос, а не пробел — сохраняем абзацы
    # висячие пунктуационные ошмётки
    text = _re.sub(r"(?m)^[ \t]*[*＊·•\-—–:：]+\s*$", " ", text)
    return text


def _detect_hidden_content(html: str) -> bool:
    """True если в cooked есть блок скрытого контента (reply-to-view / login-to-view).

    Discourse рендерит такие блоки как спаны со специфичными классами:
      <span class='permission-reply-placeholder'>请回复后查看内容。</span>
      <span class='permission-login-placeholder'>...
      <div class="spoiler">...</div>            (старый spoiler-плагин)
    Плюс страхуемся текстовым патчем — на случай если разметка изменится.
    """
    import re
    if not html:
        return False
    # по классу — самый надёжный сигнал
    if re.search(r"class=['\"][^'\"]*\b(permission-reply-placeholder"
                 r"|permission-login-placeholder"
                 r"|reply-to-see|hidden-content|spoiler)\b", html, flags=re.IGNORECASE):
        return True
    # текстовый запасной: точные формулировки NodeLoc
    txt_patterns = (
        "请回复后查看", "请回复以查看", "回复可见", "登录后查看",
        "reply to view", "reply to see", "login to view", "login to see",
        # гейт по trust level (тело = служебная фраза вместо контента)
        "requires new user trust level", "trust level to read",
        "increase your trust level", "upgrade your trust level",
    )
    low = html.lower()
    for p in txt_patterns:
        if p.lower() in low:
            return True
    return False


def _image_urls(html: str, forum_base: str, limit: int = 5) -> list:
    """Вернуть абсолютные URL содержательных картинок из cooked (до limit).

    Логика:
      - пропускаем смайлики/иконки (class содержит emoji/icon/avatar/emoji-image)
      - пропускаем tiny (width/height < 50)
      - Discourse-аплоады отдаются как '/uploads/...' → делаем абсолютным
      - внешние 'https://...' оставляем как есть
      - SVG-иконки не берём
      - дубликаты (превью+оригинал одной картинки) схлопываем
    """
    import re
    if not html:
        return []
    base_host = re.match(r"(https?://[^/]+)", forum_base)
    base_host = base_host.group(1) if base_host else forum_base

    out: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"<img\b[^>]*>", html, flags=re.IGNORECASE):
        tag = m.group(0)
        cls = (re.search(r'\bclass="([^"]*)"', tag) or [None, ""])[1].lower()
        if any(skip in cls for skip in ("emoji", "avatar", "icon", "site-icon")):
            continue
        src_m = re.search(r'\bsrc="([^"]+)"', tag, flags=re.IGNORECASE)
        if not src_m:
            continue
        src = src_m.group(1).strip()
        if not src or src.startswith("data:"):
            continue
        if src.lower().endswith(".svg"):
            continue
        # размеры
        w = re.search(r'\bwidth="(\d+)"', tag)
        h = re.search(r'\bheight="(\d+)"', tag)
        if w and h and (int(w.group(1)) < 50 or int(h.group(1)) < 50):
            continue
        # абсолютный
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = base_host + src
        elif src.startswith("http://") or src.startswith("https://"):
            pass
        else:
            src = base_host + "/" + src
        # дедуп: Discourse любит рядом optimized/original одного изображения
        key = src.split("/optimized/")[0] + src.split("/original/")[-1] \
            if ("/optimized/" in src or "/original/" in src) else src
        if key in seen:
            continue
        seen.add(key)
        out.append(src)
        if len(out) >= limit:
            break
    return out


def _first_image_url(html: str, forum_base: str) -> str:
    """Совместимость: первая содержательная картинка или ''."""
    urls = _image_urls(html, forum_base, limit=1)
    return urls[0] if urls else ""
