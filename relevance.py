"""Двухуровневый фильтр релевантности постов для канала.

Whitelist по ключевикам пропускает слишком много мусора: любое нытьё
«GPT сломался», личные вопросы и болтовня содержат слово GPT/AI и проходят.

Уровень 1 — regex-эвристика со скорингом: работает ВСЕГДА (без сети/ключа).
  - STRONG_GOOD: однозначная ценность (халява/скидка/релиз/раздача/продажа)
  - NOISE: однозначный мусор (жалобы/поломки/нытьё/вопросы-болтовня)
  - WEAK_GOOD: тематические маркеры (VPS/API/домен) — ценность НЕ доказывают
  Правила: noise без strong → ОТКЛОН; strong без noise → ПУБЛИК;
  конфликт/неуверенность → None (отдаём LLM).

Уровень 2 — LLM-классификатор (logfare, напрямую без прокси): судит по сути
только когда эвристика не уверена. Fail-open: сбой/401/таймаут → публикуем
(лучше лишний пост, чем потеря контента).
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_SYSTEM = """Ты модератор Telegram-канала о технологиях. Канал публикует ТОЛЬКО:
- AI/техно-НОВОСТИ (релизы, обновления, анонсы моделей/сервисов)
- халяву: бесплатные раздачи, ключи, подписки, бонусы
- абузы: способы обхода лимитов/регионов/верификации, лазейки
- розыгрыши и раздачи (VPS, аккаунты, домены)
- скидки и акции на сервисы/VPS/подписки/eSIM
- поставщиков: обзоры и предложения VPS/прокси/CDN/eSIM/хостинга/доменов
- полезные инструменты, open-source проекты, скрипты, гайды

НЕ публиковать (отклонять):
- жалобы «у меня сломалось/не работает/забанят ли»
- личные вопросы и просьбы о помощи без общей ценности
- болтовню, обсуждения чужих проблем, «кто что думает»
- нытьё про лимиты/цены/списания средств
- оффтоп, пустые темы

Ответь строго YES (публиковать) или NO (отклонить), без пояснений."""

# ---------- Уровень 1: regex-скоринг ----------

# Однозначная ЦЕННОСТЬ: халява/скидки/релизы/раздачи/продажи/гайды.
_STRONG_GOOD = re.compile(
    r"""(?:
        免费|白嫖|薅|羊毛|福利|赠送|抽奖|[0-9]折|折扣|减价|降价|特价|促销
        |发布|上线|推出|官宣|开源|注册送|送[0-9一二两几]
        |出[：:]|收[：:]|拼车|发车
        |优惠码|邀请码|促销码|返现|返利
        |giveaway|discount|\bfree\b|release|announc
        |bulletproof|dmca|абуз|abuse[- ](?:free|friendly|tolerant|ignored)
    )""", re.IGNORECASE | re.VERBOSE)

_STRONG_GOOD_RU = re.compile(
    r"""(?:бесплатн|халяв|розыгрыш|разыграю|разда[юётм]|скидк|акци[ия]
        |промокод|подар[ки]?|бонус|релиз|анонс|выпуск|выпустил|запуск
        |представ|open.?source|открыт\w*\s+исходн|отдам|продам|прода[юж]
        |куплю|поделюсь|выложил|опубликовал)""",
    re.IGNORECASE | re.VERBOSE)

# Однозначный МУСОР: жалобы/поломки/нытьё/болтовня/личные вопросы.
_NOISE = re.compile(
    r"""(?:
        坏了|崩了|挂了|炸了|废了|凉了|死了|不行了|完蛋|寄了
        |用不了|不能用|无法|打不开|进不去|连不上|登不上|失败
        |被封|封号|封了|降智|缩水|减配|限流|限速|卡顿
        |扣费|扣款|乱扣|多扣|退款|退钱|被骗|跑路|倒闭|暴雷
        |怎么办|咋办|咋回事|怎么回事|什么情况|啥情况|为什么|为啥
        |有没有人|求教|求助|请教|帮忙看|帮看|怎么看
        |吐槽|抱怨|恶心|离谱|无语|醉了|麻了|裂开|难受|气死
        |水一下|水贴|随便聊|无聊|日记|打卡|早安|晚安|心情
    )""", re.IGNORECASE | re.VERBOSE)

_NOISE_RU = re.compile(
    r"""(?:сломал|не\s+работает|перестал|глюч|висит|тормоз|отвалил|упал[ао]?\b
        |забан|заблокир|блокирую|сгорел|пропал|исчез|не\s+грузит
        |не\s+открыва|не\s+запуска|не\s+могу|не\s+получается|не\s+заходит|не\s+пускает
        |не\s+уда[ёе]тся|не\s+выходит
        |списа|удержал|обману|мошенн|скам|развод|проблем
        |размышл|усыхан|нытьё|жалоб|бесит|раздраж|устал|надоел
        |настроен|кто.нибудь|подскажите|посоветуй|помогите
        |как\s+мне|что\s+делать|есть\s+ли\s+смысл|стоит\s+ли|правда\s+ли
        |что\s+происходит|почему|зачем|вопрос[:：])""",
    re.IGNORECASE | re.VERBOSE)

# Вопрос-болтовня: «...吗?» / «...?» без strong-маркера ценности.
_QUESTION = re.compile(r"吗[?？]?\s*$|[?？]\s*$")


def _heuristic_verdict(title_orig: str, title_ru: str) -> bool | None:
    """Быстрый вердикт: True/False или None (не уверен → LLM)."""
    joined = " ".join(t for t in (title_orig, title_ru) if t)

    strong = bool(_STRONG_GOOD.search(joined)) or bool(_STRONG_GOOD_RU.search(joined))
    noise = bool(_NOISE.search(joined)) or bool(_NOISE_RU.search(joined))

    # вопрос без ценности — болтовня («封号严重吗，有必要上车吗»)
    if not strong and _QUESTION.search(joined.strip()):
        noise = True

    if noise and not strong:
        return False
    if strong and not noise:
        return True
    if strong and noise:
        return None  # конфликт («раздача после бана») — решает LLM
    return None  # ни маркеров ценности, ни мусора — решает LLM


class RelevanceFilter:
    def __init__(self, base: str, key: str, model: str,
                 proxy: str = "", timeout: int = 25):
        import requests
        self._base = base.rstrip("/")
        self._key = key
        self._model = model
        self._timeout = timeout
        self._session = requests.Session()
        self._session.trust_env = False  # logfare из РФ доступен напрямую
        if proxy:
            self._session.proxies.update({"http": proxy, "https": proxy})
        self._cache: dict[str, bool] = {}
        self._llm_dead = False  # после 401 не дёргаем LLM до рестарта

    def relevant(self, title_orig: str, title_ru: str,
                 body_snippet: str = "") -> bool:
        """True — публиковать."""
        if not title_orig and not title_ru:
            return False
        key = (title_orig or title_ru)[:120]
        if key in self._cache:
            return self._cache[key]

        # Уровень 1: эвристика (всегда, мгновенно)
        verdict = _heuristic_verdict(title_orig, title_ru)

        # Уровень 2: LLM (когда эвристика не уверена и ключ живой)
        if verdict is None and not self._llm_dead:
            verdict = self._ask(title_orig, title_ru, body_snippet)

        if verdict is None:
            verdict = True  # fail-open

        self._cache[key] = verdict
        if len(self._cache) > 500:
            self._cache.clear()
            self._cache[key] = verdict
        return verdict

    def _ask(self, title_orig: str, title_ru: str, snippet: str) -> bool | None:
        """LLM-вердикт. None = сбой (fail-open), 401 помечает ключ мёртвым."""
        import json
        parts = []
        if title_orig:
            parts.append(f"Оригинал: {title_orig[:300]}")
        if title_ru:
            parts.append(f"Перевод: {title_ru[:300]}")
        if snippet:
            parts.append(f"Начало текста: {snippet[:400]}")
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": "\n".join(parts)},
            ],
            "max_tokens": 10,
            "temperature": 0.0,
        }
        try:
            resp = self._session.post(
                f"{self._base}/chat/completions",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=self._timeout,
                headers={"Authorization": f"Bearer {self._key}",
                         "Content-Type": "application/json"})
            if resp.status_code == 401:
                log.warning("relevance: ключ LLM мёртв (401) — только эвристика")
                self._llm_dead = True
                return None
            if resp.status_code != 200:
                return None
            data = resp.json()
            content = ((data.get("choices") or [{}])[0]
                       .get("message", {}).get("content") or "").strip().upper()
            if not content:
                return None
            if re.search(r"\bNO\b|\bНЕТ\b", content):
                return False
            if re.search(r"\bYES\b|\bДА\b", content):
                return True
            return None
        except Exception:
            return None
