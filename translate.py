"""Перевод zh→ru через Google Translate (бесплатный эндпоинт) через deep-translator.

Стратегия устойчивости:
  1. deep-translator GoogleTranslator — основной путь.
  2. На TooManyRequests / ConnectionError — экспоненциальный backoff.
  3. Если упало окончательно — возвращаем исходный текст (лучше китайский,
     чем тишина в канале; пост всё равно уходит, в логах видно).
  4. Длинные тексты бьём на чанки ≤ 4500 символов (лимит google free ~5000).
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

# лимит одного запроса к бесплатному гуглу — осторожно, около 5000 символов
_CHUNK = 4500

# ---------------------------------------------------------------------------
# Словарь китайского форумного сленга NodeLoc. Google/MyMemory переводят его
# буквально и получается чушь («яйца ошейника», «парковка Office365»,
# «узел аэропорта»). Заменяем на АНГЛИЙСКИЕ эквиваленты ДО перевода —
# переводчик доведёт их до русского сам. Важно: НЕ подставлять русский сразу:
# MyMemory не переваривает смесь кит+рус (оставляет непереведённым),
# а кит+англ переводит стабильно. Длинные фразы — раньше коротких.
# ---------------------------------------------------------------------------
_SLANG_REPLACEMENTS: list[tuple[str, str]] = [
    # торговые префиксы
    ("出：", "Selling: "), ("出:", "Selling: "), ("出售", "for sale"),
    ("收：", "Buying: "), ("收:", "Buying: "),
    # те же префиксы в заголовочных скобках: 【出】…【收】…
    ("【出】", "[Selling] "), ("【收】", "[Buying] "),
    # 家人 в торговых темах = слот в семейной подписке (не «член семьи»)
    ("家人", "family plan member"),
    # подписочные группы (拼车 = «карпулинг» семейных подписок)
    ("家庭组车位", "family group subscription slot"),
    ("车位", "subscription slot"),
    ("家庭组", "family group"),
    ("拼车", "shared subscription"),
    ("上车", "join the subscription"),
    ("车头", "subscription group organizer"),
    ("踢下车", "removal from subscription"),
    # халява/бонусы
    ("薅羊毛", "grab freebies"), ("羊毛", "freebie deals"),
    ("白嫖", "freeload"), ("领鸡蛋", "claim free bonus"),
    ("鸡蛋", "bonus"), ("快来领", "come and get"),
    # сетевой сленг
    ("机场节点", "VPN service node"), ("机场", "VPN service"),
    ("梯子", "VPN"), ("小鸡", "cheap VPS"),
    ("翻墙", "bypass censorship"),
    # мемы/названия (哈吉米 — форумный мем для Gemini, авторы пишут «哈吉米（Gemini）»)
    ("哈吉米", "Gemini"),
    # 牛来 — сленг для LLM Niulai/NewLM (иначе «корова приходит»)
    ("牛来", "Niulai"),
    # розыгрыши (抽 многозначно: «тянуть/рисовать» — переводчик промахивается)
    ("抽一个", "giveaway of one"), ("抽几个", "giveaway of a few"),
    ("抽奖", "lucky draw"), ("送几台", "giveaway of a few units"),
    ("送一台", "giveaway one unit"), ("注册送", "signup bonus"),
    # автоматизация вместо юзера (挂 = «висеть/фармиться в фоне»)
    ("代挂", "auto-farming"), ("挂机", "idle farming"),
]

# Регексные правила: числительные сроки. В NodeLoc «N个月/天/年» после
# названия продукта = длительность подписки, а переводчик выдаёт «через N
# месяцев». Переводим в формат «N-month», который ложится в «N-месячный».
import re as _slang_re
_SLANG_REGEX = [
    (_slang_re.compile(r"(\d+)\s*个月"), r"\1-month "),
    (_slang_re.compile(r"(\d+)\s*年"), r"\1-year "),
    (_slang_re.compile(r"(\d+)\s*天"), r"\1-day "),
]


def _apply_slang_dict(text: str, for_weak: bool = False) -> str:
    """Заменить сленговые выражения ДО машинного перевода.

    Замены обрамляем пробелами: китайский пишут без пробелов, и без этого
    английская вставка склеивается с соседом («Office365»+«family group» →
    «Office365familygroup» — одно непереводимое слово).

    for_weak=True — дополнительно применяем регексы числительных сроков
    (N-month и пр.): они нужны ТОЛЬКО слабому MyMemory; сильные движки
    (Bing/Google) сами разбирают «4个月» по контексту («GPT4 месяца не
    проходит» ≠ «подписка на 4 месяца»), а регекс им только мешает.
    """
    if not text:
        return text
    import re
    for zh, en in _SLANG_REPLACEMENTS:
        if zh in text:
            text = text.replace(zh, f" {en} ")
    if for_weak:
        for pattern, repl in _SLANG_REGEX:
            text = pattern.sub(repl, text)
    # нормализуем пробелы, но не трогаем переносы
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" +([,.!?;:，。！？；：])", r"\1", text)
    # «! ! !» / «？ ！» → «!!!» / «?!» — Bing/MyMemory растаскивают знаки
    text = re.sub(r"([!?！？])\s+(?=[!?！？])", r"\1", text)
    # скобки без висячих пробелов и без склейки с соседним словом:
    # «[ bonus ]Qwen» → «[bonus] Qwen»
    text = re.sub(r"\[\s+", "[", text)
    text = re.sub(r"\s+\]", "]", text)
    text = re.sub(r"\](?=[A-Za-z0-9А-Яа-я])", "] ", text)
    return text


class Translator:
    def __init__(self, delay_ms: int = 600, proxy: str = "",
                 llm_base: str = "", llm_key: str = "",
                 llm_model: str = "", llm_fallback_model: str = "",
                 llm_proxy: str = ""):
        self.delay_ms = delay_ms
        self._last_call_ts = 0.0
        # Прокси для Google/MyMemory: оба бьют по IP-лимитам. Прокси меняет
        # IP (напр. через Mullvad SOCKS) → свежие лимиты. deep-translator
        # умеет proxies= только в GoogleTranslator — MyMemory идёт по env.
        self._proxies = ({"http": proxy, "https": proxy} if proxy else None)
        if proxy:
            import os
            os.environ["HTTP_PROXY"] = proxy
            os.environ["HTTPS_PROXY"] = proxy
            log.info("Перевод через прокси: %s", proxy)
        # LLM-перевод (OpenAI-совместимый endpoint): заметно лучше машинных
        # движков на сленге и датах. Отдельный прокси — endpoint'ы вроде
        # logfare.ai блокируются с RU напрямую, в отличие от Bing/TG.
        self._llm_base = llm_base
        self._llm_key = llm_key
        self._llm_model = llm_model
        self._llm_fallback = llm_fallback_model
        self._llm_session = None
        if llm_base and llm_key:
            import requests as _rq
            self._llm_session = _rq.Session()
            self._llm_session.trust_env = False
            self._llm_session.headers.update({
                "Authorization": f"Bearer {llm_key}",
                "Content-Type": "application/json",
            })
            if llm_proxy:
                self._llm_session.proxies.update(
                    {"http": llm_proxy, "https": llm_proxy})
            log.info("LLM-перевод: %s (%s, fallback %s)%s",
                     llm_base, llm_model, llm_fallback_model,
                     f" через {llm_proxy}" if llm_proxy else "")
        try:
            from deep_translator import GoogleTranslator
            # библиотечный класс переиспользуем; source/target задаём на вызов
            self._GoogleTranslator = GoogleTranslator
        except Exception as e:  # pragma: no cover
            raise RuntimeError(
                "deep-translator не установлен. Выполни: pip install -r requirements.txt"
            ) from e

    # ---------- публичное ----------
    def translate(self, text: str, source: str = "auto", target: str = "ru") -> str:
        """Перевести text → ru. На фатальной ошибке вернуть оригинал.

        source='auto' (умолчание): Google сам определяет язык. На NodeLoc темы
        бывают чисто китайские, английские и смешанные — жёсткий 'zh-CN'
        ломается на англо-китайском тексте («No translation was found»).

        Цепочка: Google (основной) → MyMemory (запасной) → оригинал.
        """
        if not text or not text.strip():
            return text
        # 1) LLM — лучшее качество (сленг, даты, естественность)
        if self._llm_session is not None:
            try:
                res = _strip_translation_echo(_collapse_loops(self._llm_translate(text)))
                _validate_ru(res, text)
                return res
            except Exception as e:
                log.warning("LLM не смог (%s) → пробую Bing", str(e)[:120])
        # 2) Bing → 3) Google → 4) MyMemory
        text_strong = _apply_slang_dict(text, for_weak=False)
        try:
            res = _strip_translation_echo(_collapse_loops(self._bing_translate(text_strong, target)))
            _validate_ru(res, text)
            return res
        except Exception as e:
            log.warning("Bing не смог (%s) → пробую Google", e)
        try:
            parts = self._translate_chunks(text_strong, source, target)
            res = _strip_translation_echo(_collapse_loops("".join(parts)))
            _validate_ru(res, text)
            return res
        except Exception as e:
            log.warning("Google не смог (%s) → пробую MyMemory", e)
        try:
            res = self._mymemory_translate(_apply_slang_dict(text, for_weak=True), target)
            res = _strip_translation_echo(_collapse_loops(res))
            _validate_ru(res, text)
            return res
        except Exception as e:
            log.warning("MyMemory тоже не смог (%s) — оставляю оригинал", e)
            return text

    _LLM_SYSTEM_PROMPT = (
        "Ты — переводчик постов китайского техно-форума NodeLoc "
        "(темы: VPS/хостинг, AI-модели, подписки, скидки, домены, виртуальные карты). "
        "Переводи китайский и английский текст в естественный русский язык, не дословно. "
        "Форумный сленг: 支付宝口令=код оплаты Alipay; 家人/车位/家庭组=слот в семейной "
        "подписке; 拼车=совместная подписка; 上车=присоединиться к подписке; 出/【出】=продаю; "
        "收/【收】=куплю; 白嫖/薅羊毛=халява; 羊毛=халявные акции; 鸡蛋/领鸡蛋=бонусы; "
        "机场=VPN-сервис; 梯子=VPN; 小鸡=дешёвый VPS; 牛来=Niulai (LLM); 哈吉米=Gemini; "
        "抽=розыгрыш; 挂机/代挂=авто-фарминг. Даты: 28年=2028 год, N个月 в контексте "
        "подписок = «на N месяцев». Сохраняй emoji, ссылки, цифры и форматирование. "
        "Отвечай ТОЛЬКО переводом, без комментариев."
    )

    def _llm_translate(self, text: str, timeout: int = 60) -> str:
        """Перевод через OpenAI-совместимый LLM (качество выше машинных движков).

        Чанкуем до 2500 символов (больше — в один запрос не имеет смысла,
        посты и так обрезаны). Пробуем основную модель, при 5xx/пустом
        ответе — запасную. Reasoning-модели: берём message.content,
        на нехватку токенов отвечаем большим max_tokens.
        """
        import json as _json
        out = []
        for piece in _iter_soft_chunks(text, 2500):
            self._throttle()
            last_err = None
            for model in (self._llm_model, self._llm_fallback):
                if not model:
                    continue
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": self._LLM_SYSTEM_PROMPT},
                        {"role": "user", "content": piece},
                    ],
                    "max_tokens": 4000,
                    "temperature": 0.2,
                }
                try:
                    resp = self._llm_session.post(
                        f"{self._llm_base}/chat/completions",
                        data=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        timeout=timeout)
                    data = resp.json()
                    if resp.status_code != 200 or "choices" not in data:
                        raise RuntimeError(f"http {resp.status_code}: "
                                           f"{str(data)[:100]}")
                    content = (data["choices"][0].get("message", {})
                               .get("content") or "").strip()
                    if not content:
                        # reasoning-модель потратила лимит на размышления
                        fr = data["choices"][0].get("finish_reason", "")
                        raise RuntimeError(f"пустой content (finish={fr})")
                    out.append(content)
                    break
                except Exception as e:
                    last_err = e
                    continue
            else:
                raise RuntimeError(f"LLM: обе модели не смогли: {last_err}")
        return "\n".join(out)

    def _bing_translate(self, text: str, target: str) -> str:
        """Первичный движок: Bing (Microsoft) через библиотеку translators.

        Качество zh→ru заметно выше MyMemory, автоопределение языка надёжное.
        Чанкуем до 3000 симв — с запасом под лимиты endpoint'а.
        Если в результате остались иероглифы — перевод частичный (Bing
        споткнулся на смеси языков) → исключение, цепочка идёт дальше.
        """
        import translators as ts
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as _TE
        out = []
        for piece in _iter_soft_chunks(text, 3000):
            self._throttle()
            # translators не имеет своего таймаута и может висеть вечно
            # на полуоткрытом соединении. ВАЖНО: with-block БЛОКИРУЕТСЯ на
            # выходе (shutdown(wait=True)) пока висит поток — поэтому никаких
            # with: shutdown(wait=False), повисший поток остаётся в фоне
            # (единичная утечка лучше зависшего бота), бот идёт дальше.
            _ex = ThreadPoolExecutor(max_workers=1)
            try:
                fut = _ex.submit(ts.translate_text, piece,
                                 translator="bing", from_language="auto",
                                 to_language=target)
                try:
                    res = str(fut.result(timeout=35))
                except _TE:
                    raise RuntimeError("bing: таймаут 35с (поток брошен)")
            finally:
                _ex.shutdown(wait=False)
            if not res or _looks_like_google_error(res):
                raise RuntimeError(f"bing вернул мусор: {res[:60]}")
            if any("\u4e00" <= ch <= "\u9fff" for ch in res):
                raise RuntimeError(f"bing перевёл частично (остались иероглифы): {res[:60]}")
            out.append(res)
        return "".join(out)

    def _mymemory_translate(self, text: str, target: str) -> str:
        """Запасной переводчик MyMemory (free, ~1000 слов/день с IP).

        ВАЖНО: MyMemory требует коды языков СО СТРАНОЙ ('ru-RU', 'en-GB',
        'zh-CN') — короткие 'ru'/'en' он отвергает. Лимит запроса ~500 симв —
        режем на чанки. Пробуем zh-CN (основной язык форума), затем en-GB
        (для англоязычных тем).
        """
        from deep_translator import MyMemoryTranslator
        target_full = "ru-RU" if target == "ru" else target
        out = []
        for piece in _iter_soft_chunks(text, 450):
            self._throttle()
            translated = None
            # порядок source по факту: если в куске нет иероглифов (после
            # словаря сленга текст мог стать чисто английским) — en-GB первым
            has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in piece)
            sources = ("zh-CN", "en-GB") if has_cjk else ("en-GB", "zh-CN")
            for src in sources:
                try:
                    tr = MyMemoryTranslator(source=src, target=target_full)
                    res = tr.translate(piece)
                    if res and not _looks_like_google_error(res):
                        # проверка что реально перевёл: если вернул вход
                        # на другом языке — попробовать следующий source
                        if res.strip() != piece.strip():
                            translated = res
                            break
                except Exception:
                    continue
            if translated is None:
                raise RuntimeError("mymemory failed on chunk")
            out.append(translated)
        return "".join(out)

    # ---------- внутреннее ----------
    def _throttle(self) -> None:
        elapsed = (time.monotonic() - self._last_call_ts) * 1000
        if elapsed < self.delay_ms:
            time.sleep((self.delay_ms - elapsed) / 1000.0)
        self._last_call_ts = time.monotonic()

    def _translate_chunks(self, text: str, source: str, target: str) -> list[str]:
        if len(text) <= _CHUNK:
            return [self._one(text, source, target)]
        # режем по границам абзацев/предложений, чтобы не разорвать посередине слова
        out: list[str] = []
        for piece in _iter_soft_chunks(text, _CHUNK):
            out.append(self._one(piece, source, target))
        return out

    def _one(self, chunk: str, source: str, target: str, attempts: int = 5) -> str:
        """Один запрос к гуглу с backoff на 429/сеть/мусорные ответы."""
        tr = self._GoogleTranslator(source=source, target=target,
                                    proxies=self._proxies)
        last = None
        for attempt in range(1, attempts + 1):
            self._throttle()
            try:
                result = tr.translate(chunk) or ""
                # Google free иногда возвращает СТРАНИЦУ ОШИБКИ как результат
                # вместо исключения. Детектим сигнатуры и считаем ошибкой.
                if _looks_like_google_error(result):
                    raise RuntimeError(f"google вернул мусор: {result[:80]}")
                return result
            except Exception as e:
                last = e
                msg = str(e).lower()
                # 429/500/сеть/мусор — ретраим с ростом задержки
                transient = any(s in msg for s in (
                    "429", "too many", "timed out", "connection", "reset",
                    "unavailable", "500", "server error", "мусор",
                    "no translation was found",
                ))
                wait = (2 ** attempt) + 1
                if transient and attempt < attempts:
                    log.debug("translate retry %d/%d after %s (%.1fs)",
                              attempt, attempts, e.__class__.__name__, wait)
                    time.sleep(wait)
                    continue
                # либо финальная попытка, либо непонятная ошибка — наверх
                raise
        raise RuntimeError(f"translate failed: {last}")


def _looks_like_google_error(text: str) -> bool:
    """True если 'перевод' на самом деле страница ошибки/заглушка Google."""
    if not text:
        return False
    low = text.lower()
    signatures = (
        "that's an error", "that’s an error", "error 500",
        "server error", "please try again later", "that's all we know",
        "no translation was found", "translation api error",
    )
    return any(sig in low for sig in signatures)


def _validate_ru(result: str, original: str) -> None:
    """Гвардия качества: для ru-таргета доля кириллицы в результате должна
    превосходить долю иероглифов. Иначе движок вернул сырой/частичный
    перевод («GPT Pro 20x，老订阅群...») — кидаем исключение, каскад идёт
    дальше. Исходник без CJK не проверяем (там иероглифов не будет)."""
    if not result or not any("一" <= ch <= "鿿" for ch in original):
        return
    n = len(result) or 1
    cyr = sum(1 for ch in result if "а" <= ch.lower() <= "я" or ch in "ёЁ")
    cjk = sum(1 for ch in result if "一" <= ch <= "鿿")
    if cjk / n > 0.2 and cjk >= cyr:
        raise RuntimeError(f"перевод сырой: кит={cjk} кир={cyr} из {n}")


def _strip_translation_echo(text: str) -> str:
    """Убрать «эхо»: движки иногда возвращают «оригинал → перевод» одной
    строкой. Если до стрелки иероглифы, а после — кириллица, левую часть
    отрезаем (это исходник, а не перевод)."""
    import re
    if "→" not in text:
        return text
    left, _, right = text.partition("→")
    has_cjk_left = any("\u4e00" <= ch <= "\u9fff" for ch in left)
    has_cyr_right = any("а" <= ch.lower() <= "я" or ch in "ёЁ" for ch in right)
    if has_cjk_left and has_cyr_right and len(right.strip()) > 5:
        return right.strip()
    return text


def _collapse_loops(text: str) -> str:
    """Схлопнуть зацикленный перевод (MyMemory любит повторять сегмент ×20).

    Делим на сегменты по [。；;.!?\n]; если один сегмент ≥8 символов
    встречается ≥3 раз и занимает >50% текста — оставляем первые 2 вхождения,
    остальное выбрасываем.
    """
    import re
    if not text or len(text) < 40:
        return text
    segs = [s.strip() for s in re.split(r"[。；;.!？?\n]+", text) if s.strip()]
    if len(segs) < 4:
        return text
    from collections import Counter
    cnt = Counter(segs)
    seg, n = cnt.most_common(1)[0]
    if len(seg) >= 8 and n >= 3 and n * len(seg) > len(text) * 0.5:
        # оставить первые 2 вхождения
        kept, seen = [], 0
        for s in segs:
            if s == seg:
                seen += 1
                if seen > 2:
                    continue
            kept.append(s)
        return " ".join(kept)
    return text


def _iter_soft_chunks(text: str, limit: int):
    """Yield кусков ≤ limit, стараясь резать по '\\n' затем по '. '."""
    if len(text) <= limit:
        yield text
        return
    buf = ""
    # сначала по абзацам
    for para in text.split("\n"):
        if len(buf) + len(para) + 1 <= limit:
            buf += ("\n" if buf else "") + para
            continue
        if buf:
            yield buf
            buf = ""
        if len(para) <= limit:
            buf = para
            continue
        # абзац длиннее лимита — режем по предложениям
        for sent in _split_sentences(para, limit):
            if len(buf) + len(sent) + 1 <= limit:
                buf += (" " if buf else "") + sent
            else:
                if buf:
                    yield buf
                buf = sent
    if buf:
        yield buf


def _split_sentences(text: str, limit: int):
    """Режет длинный абзац на части ≤ limit по '. ', при необходимости по символам."""
    out: list[str] = []
    # китайские предложения часто кончаются на 。！？ — учтём
    import re
    parts = re.split(r"(?<=[\.\!\?。！？])\s+", text)
    buf = ""
    for p in parts:
        if len(buf) + len(p) + 1 <= limit:
            buf += (" " if buf else "") + p
        else:
            if buf:
                out.append(buf)
            buf = ""
            if len(p) <= limit:
                buf = p
            else:
                # жёсткий разрез
                for i in range(0, len(p), limit):
                    out.append(p[i:i + limit])
    if buf:
        out.append(buf)
    return out
