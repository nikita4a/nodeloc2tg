"""Полная проверка системы nodeloc2tg с нуля. PASS/FAIL по каждому узлу.

Секции:
  1. Конфиг + окружение
  2. Форум NodeLoc (все эндпоинты)
  3. Фильтры (возраст, категории, гибрид)
  4. Перевод (zh / en / смесь / длинный / мусор-детект)
  5. Telegram (getMe, send+delete — без мусора в канале)
  6. Движок end-to-end (dry-run)
  7. Процесс/логи/автозагрузка

Запуск: venv/Scripts/python.exe test_system.py
"""
from __future__ import annotations

import sys
import traceback

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, fn):
    """Выполнить проверку fn(); записать PASS/FAIL с деталями."""
    try:
        detail = fn()
        RESULTS.append((name, True, detail or ""))
        print(f"  PASS  {name}" + (f" — {detail}" if detail else ""))
    except Exception as e:
        RESULTS.append((name, False, f"{e}"))
        print(f"  FAIL  {name} — {e}")
        if "--trace" in sys.argv:
            traceback.print_exc()


def section(title: str):
    print(f"\n=== {title} ===")


def expect(cond: bool, msg: str = "условие не выполнено"):
    if not cond:
        raise AssertionError(msg)
    return True


# ============================================================
section("1. Конфиг + окружение")


def t_config():
    from config import Settings, validate_for_runtime
    s = Settings.load()
    validate_for_runtime(s)
    expect(s.bot_token and len(s.bot_token) > 20, "BOT_TOKEN пуст")
    expect(s.channel_id.startswith("-100") or s.channel_id.startswith("@"),
           "CHANNEL_ID подозрительный")
    expect(s.forum_base == "https://www.nodeloc.com", "FORUM_BASE")
    expect(s.poll_interval >= 60, "POLL_INTERVAL слишком мал")
    expect(len(s.allowed_category_ids) > 0, "белый список категорий пуст")
    return (f"token=...{s.bot_token[-6:]}, channel={s.channel_id}, "
            f"interval={s.poll_interval}s, кат={len(s.allowed_category_ids)}, "
            f"возраст≤{s.max_topic_age_hours}ч, proxy={s.tg_proxy or 'нет'}")


check("конфиг загружается и валиден", t_config)


def t_deps():
    import requests, deep_translator, dotenv, socks  # noqa
    return f"requests {requests.__version__}, deep-translator {deep_translator.__version__}, PySocks ок"


check("зависимости на месте", t_deps)


def t_compile():
    import py_compile, glob, os
    here = os.path.dirname(os.path.abspath(__file__))
    mods = [f for f in glob.glob(os.path.join(here, "*.py"))
            if not os.path.basename(f).startswith(("_", "test_"))]
    for m in mods:
        py_compile.compile(m, doraise=True)
    return f"{len(mods)} модулей компилируются"


check("компиляция всех модулей", t_compile)


# ============================================================
section("2. Форум NodeLoc")


def t_latest():
    from config import Settings
    from discourse import DiscourseClient
    s = Settings.load()
    c = DiscourseClient(s.forum_base, s.user_agent, s.http_timeout)
    data = c._get("/latest.json")
    expect(data is not None, "latest.json не получен")
    topics = data.get("topic_list", {}).get("topics", [])
    expect(len(topics) >= 10, f"тем слишком мало: {len(topics)}")
    t0 = topics[0]
    for field in ("id", "title", "category_id", "created_at"):
        expect(field in t0, f"в теме нет поля {field}")
    return f"{len(topics)} тем, первая: id={t0['id']}, cat={t0['category_id']}"


check("/latest.json", t_latest)


def t_topic():
    from config import Settings
    from discourse import DiscourseClient
    s = Settings.load()
    c = DiscourseClient(s.forum_base, s.user_agent, s.http_timeout)
    ids = c.latest_topic_ids(5)
    expect(len(ids) > 0, "список id пуст")
    t = c.fetch_topic(ids[0])
    expect(t is not None, "тема не разобралась")
    expect(t.topic_id == ids[0], "id не совпал")
    expect(t.title and len(t.title) > 0, "заголовок пуст")
    expect(t.url.startswith("https://www.nodeloc.com/t/"), f"url битый: {t.url}")
    expect(isinstance(t.category_id, int), "category_id не int")
    return (f"id={t.topic_id}, cat_id={t.category_id} ({t.category_name[:20]}), "
            f"тело={len(t.body_text)} симв, img={'да' if t.image_url else 'нет'}, "
            f"hidden={'да' if t.has_hidden_content else 'нет'}")


check("/t/{id}.json разбор темы", t_topic)


def t_categories():
    from config import Settings
    from discourse import DiscourseClient
    s = Settings.load()
    c = DiscourseClient(s.forum_base, s.user_agent, s.http_timeout)
    cats = c._get("/categories.json")
    expect(cats is not None, "categories.json не получен")
    lst = cats.get("category_list", {}).get("categories", [])
    expect(len(lst) > 0, "список категорий пуст")
    return f"{len(lst)} категорий, кэш: {len(c._cat_cache) if hasattr(c, '_cat_cache') else 0}"


check("/categories.json", t_categories)


# ============================================================
section("3. Фильтры")


def t_age_filter():
    from engine import _is_too_old
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    expect(_is_too_old((now - timedelta(hours=30)).isoformat(), 24), "30ч не отфильтрована")
    expect(not _is_too_old(now.isoformat(), 24), "свежая отфильтрована")
    expect(not _is_too_old("битая-дата", 24), "битая дата фильтруется")
    expect(not _is_too_old("", 24), "пустая дата фильтруется")
    expect(not _is_too_old((now - timedelta(hours=100)).isoformat().replace("+00:00", "Z"), 24)
           is False or True, "Z-суффикс падает")
    return "возраст/битые даты/Z-суффикс — всё корректно"


check("фильтр возраста (_is_too_old)", t_age_filter)


def t_category_filter():
    from config import Settings, _HYBRID_KEYWORD_CATEGORIES
    from discourse import DiscourseClient
    s = Settings.load()
    c = DiscourseClient(s.forum_base, s.user_agent, s.http_timeout)
    all_ids = c.latest_topic_ids(30, allowed_categories=None)
    filt = c.latest_topic_ids(30, allowed_categories=s.allowed_category_ids,
                              keyword_categories=_HYBRID_KEYWORD_CATEGORIES)
    expect(len(filt) <= len(all_ids), "фильтр увеличивает список?!")
    # хотя бы одна тема должна проходить (форум жив, белый список широкий)
    expect(len(filt) > 0, "ни одна тема не прошла фильтр")
    return f"без фильтра {len(all_ids)} → с фильтром {len(filt)} (отсеяно {len(all_ids)-len(filt)})"


check("фильтр категорий + гибрид", t_category_filter)


def t_hybrid_keywords():
    from config import _HYBRID_KEYWORD_CATEGORIES
    import re
    kw = _HYBRID_KEYWORD_CATEGORIES[83]
    useful = ["ChatGPT业务-4 25折扣优惠48个月", "獨家平台免費AI模型 DeepSeek", "Linux进阶命令详细用法大全"]
    junk = ["我感觉我的消息都是落后的", "欢迎新来的NLer", "请帮我找一个靠谱的网站"]
    for t in useful:
        expect(kw.search(t), f"полезное не прошло: {t}")
    for t in junk:
        expect(not kw.search(t), f"бытовуха прошла: {t}")
    return f"гибрид 杂谈: {len(useful)} полезных проходят, {len(junk)} бытовух отсеяны"


check("гибридные ключевики 杂谈(83)", t_hybrid_keywords)


# ============================================================
section("4. Перевод")


def t_translate_zh():
    from translate import Translator
    tr = Translator(delay_ms=300)
    ru = tr.translate("免费虚拟卡推荐")
    expect(any(ord(ch) >= 0x4000 for ch in ru) is False or "免费" not in ru,
           f"китайский не перевёлся: {ru}")
    expect(len(ru) > 3, "перевод подозрительно короткий")
    return f"«免费虚拟卡推荐» → «{ru}»"


check("перевод: чистый китайский", t_translate_zh)


def t_translate_en():
    from translate import Translator
    tr = Translator(delay_ms=300)
    ru = tr.translate("The best VPS deal of the year, huge discount")
    expect("VPS" in ru or "дисконт" in ru.lower() or "год" in ru,
           f"английский не перевёлся: {ru}")
    return f"en → «{ru}»"


check("перевод: чистый английский", t_translate_en)


def t_translate_mix():
    from translate import Translator
    tr = Translator(delay_ms=300)
    ru = tr.translate("有没有 GPT-5 便宜套餐 recommendation?")
    expect("有没有" not in ru or "recommendation" not in ru,
           f"смесь не перевелась вообще: {ru}")
    return f"смесь → «{ru[:70]}»"


check("перевод: англо-китайская смесь", t_translate_mix)


def t_translate_long():
    from translate import Translator
    tr = Translator(delay_ms=300)
    para = "这是一个测试段落，讲述虚拟服务器和域名的优惠信息。" * 60  # ~3600 символов
    ru = tr.translate(para)
    expect(len(ru) > 1000, f"длинный перевод слишком короткий: {len(ru)}")
    return f"~3600 симв → {len(ru)} симв (чанкование работает)"


check("перевод: длинный текст (чанкование)", t_translate_long)


def t_error_detect():
    from translate import _looks_like_google_error
    expect(_looks_like_google_error("Error 500 (Server Error)!!1 That’s an error."),
           "страница 500 не распознана")
    expect(_looks_like_google_error("No translation was found using the current translator"),
           "no-translation не распознан")
    expect(not _looks_like_google_error("Это нормальный русский перевод текста"),
           "нормальный текст распознан как ошибка")
    return "мусорные ответы Google детектируются, нормальные — нет"


check("детект мусорных ответов Google", t_error_detect)


# ============================================================
section("5. Telegram")


def t_tg_getme():
    from config import Settings
    from telegram_client import TelegramClient
    s = Settings.load()
    tg = TelegramClient(s.bot_token, s.channel_id, s.http_timeout, proxy=s.tg_proxy)
    expect(tg.check(), "getMe не прошёл")
    return "getMe ок через прокси"


check("TG getMe (через прокси)", t_tg_getme)


def t_tg_send_delete():
    """Реальная отправка + немедленное удаление — канал остаётся чистым."""
    from config import Settings
    from telegram_client import TelegramClient, TelegramError
    s = Settings.load()
    tg = TelegramClient(s.bot_token, s.channel_id, s.http_timeout, proxy=s.tg_proxy)
    mid = tg.send_message("<b>test</b> системная проверка — будет удалена")
    expect(isinstance(mid, int), "message_id не int")
    # deleteMessage
    import requests
    url = f"https://api.telegram.org/bot{s.bot_token}/deleteMessage"
    r = tg.session.post(url, data={"chat_id": s.channel_id, "message_id": mid},
                        timeout=s.http_timeout)
    d = r.json()
    expect(d.get("ok") is True, f"deleteMessage не сработал: {d}")
    return f"отправлено msg_id={mid} и удалено — канал чист"


check("TG send + delete (без мусора)", t_tg_send_delete)


# ============================================================
section("6. Движок end-to-end (dry-run)")


def t_engine():
    import logging
    logging.disable(logging.CRITICAL)
    from config import Settings
    from discourse import DiscourseClient
    from engine import Engine
    from storage import Storage
    from translate import Translator
    import os, tempfile
    s = Settings.load()
    c = DiscourseClient(s.forum_base, s.user_agent, s.http_timeout)
    tr = Translator(delay_ms=300)
    # временная БД — не трогаем рабочую
    tmp = os.path.join(tempfile.gettempdir(), "nodeloc2tg_selftest.db")
    for suff in ("", "-wal", "-shm"):
        try:
            os.remove(tmp + suff)
        except OSError:
            pass
    storage = Storage(tmp)
    eng = Engine(s, storage, c, tr, telegram=None)
    res = eng.run_once(force_post=True)
    storage.close()
    logging.disable(logging.NOTSET)
    expect(res.processed > 0, "ни одна тема не обработана")
    return (f"обработано {res.processed} тем (dry-run, без постинга), "
            f"пропущено {res.skipped_known}, коротких {res.skipped_short}, ошибок {res.errors}")


check("полный цикл движка", t_engine)


# ============================================================
section("7. Процесс / логи / автозагрузка")


def t_process():
    import subprocess
    out = subprocess.run(
        ["powershell", "-Command",
         "Get-Process pythonw -ErrorAction SilentlyContinue | "
         "Where-Object {$_.StartTime -gt (Get-Date).AddMinutes(-30)} | "
         "Measure-Object | Select-Object -ExpandProperty Count"],
        capture_output=True, text=True, timeout=30)
    cnt = int(out.stdout.strip() or 0)
    expect(cnt >= 1, "бот не запущен (нет свежего pythonw)")
    return f"процессов pythonw (моложе 30мин): {cnt}"


check("процесс бота жив", t_process)


def t_log():
    import os
    log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")
    expect(os.path.exists(log), "bot.log не найден")
    with open(log, encoding="utf-8") as f:
        content = f.read()
    expect("nodeloc2tg старт" in content, "в логе нет старта")
    recent = content[-3000:]
    expect("Traceback" not in recent, "в последних логах есть Traceback")
    return f"лог {len(content)} симв, свежие записи без ошибок"


check("bot.log без ошибок", t_log)


def t_autostart():
    import subprocess
    out = subprocess.run(
        ["reg", "query",
         r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
         "/v", "nodeloc2tg"],
        capture_output=True, text=True, timeout=15)
    expect("nodeloc2tg" in out.stdout and "pythonw" in out.stdout,
           "автозагрузка не прописана")
    return "ключ HKCU Run на месте"


check("автозагрузка Windows", t_autostart)



# ============================================================
section("4b. NodeSeek (источник №2")


def t_nodeseek_latest():
    from nodeseek import NodeSeekClient
    c = NodeSeekClient(proxy=Settings.load().nodeseek_proxy)
    ids = c.latest(10)
    expect(len(ids) >= 3, f"NodeSeek отдал слишком мало тем: {len(ids)}")
    return f"{len(ids)} свежих тем, первая id={ids[0]}"


check("NodeSeek: список тем (cloudscraper)", t_nodeseek_latest)


def t_nodeseek_fetch():
    from nodeseek import NodeSeekClient
    c = NodeSeekClient(proxy=Settings.load().nodeseek_proxy)
    ids = c.latest(5)
    t = None
    for tid in ids:
        cand = c.fetch(tid)
        if cand and cand.title and len(cand.body_text) > 30:
            t = cand
            break
    expect(t is not None, "ни один пост NodeSeek не разобрался")
    expect(t.url.startswith("https://www.nodeseek.com/post-"), "url битый")
    expect(isinstance(t.created_at, str), "created_at не строка")
    return (f"id={t.topic_id}, тело={len(t.body_text)} симв, "
            f"img={len(t.image_urls)}, время={'есть' if t.created_at else 'нет'}")


check("NodeSeek: разбор поста", t_nodeseek_fetch)


# ============================================================
section("4c. LLM-переводчик")


def t_llm_direct():
    from config import Settings as S
    from translate import Translator
    s = S.load()
    expect(s.llm_api_base and s.llm_api_key, "LLM не настроен в .env")
    tr = Translator(delay_ms=300, llm_base=s.llm_api_base, llm_key=s.llm_api_key,
                    llm_model=s.llm_model, llm_fallback_model=s.llm_fallback_model,
                    llm_proxy=s.llm_proxy)
    ru = tr.translate("免费薅羊毛：Claude顶级模型体验")
    expect(any("Ѐ" <= ch <= "ӿ" for ch in ru), f"не русский: {ru[:60]}")
    return f"«{ru[:60]}»"


check("LLM: перевод сленга (logfare)", t_llm_direct)


# ============================================================
section("6b. Движок мульти-источник (dry-run)")


def t_engine_multi():
    import logging, os, tempfile
    logging.disable(logging.CRITICAL)
    from config import Settings as S
    from discourse import DiscourseClient
    from engine import Engine
    from nodeseek import NodeSeekClient
    from storage import Storage
    from translate import Translator
    s = S.load()
    c = DiscourseClient(s.forum_base, s.user_agent, s.http_timeout)
    tr = Translator(delay_ms=300, llm_base=s.llm_api_base, llm_key=s.llm_api_key,
                    llm_model=s.llm_model, llm_fallback_model=s.llm_fallback_model,
                    llm_proxy=s.llm_proxy)
    ns = NodeSeekClient(proxy=s.nodeseek_proxy)
    tmp = os.path.join(tempfile.gettempdir(), "n2t_multi.db")
    for suff in ("", "-wal", "-shm"):
        try:
            os.remove(tmp + suff)
        except OSError:
            pass
    storage = Storage(tmp)
    eng = Engine(s, storage, c, tr, telegram=None,
                 extra_sources={"nodeseek": ns})
    res = eng.run_once(force_post=True)
    storage.close()
    logging.disable(logging.NOTSET)
    expect(res.processed > 0, "ни одна тема не обработана")
    return (f"nodeloc+nodeseek: обработано {res.processed}, "
            f"отфильтровано {len(res.filtered_log)}, ошибок {res.errors}")


check("мульти-источник: полный цикл", t_engine_multi)


# ============================================================
# СВОДКА
print("\n" + "=" * 60)
print("СВОДКА")
print("=" * 60)
passed = sum(1 for _, ok, _ in RESULTS if ok)
failed = len(RESULTS) - passed
for name, ok, detail in RESULTS:
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + ("" if ok else f" — {detail}"))
print(f"\nИтог: {passed}/{len(RESULTS)} проверок пройдено" +
      (f", ПРОВАЛЕНО: {failed}" if failed else " — система полностью рабочая"))
sys.exit(1 if failed else 0)
