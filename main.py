"""Точка входа 24/7: бесконечный цикл poll → translate → post → sleep.

Запуск:
    python main.py

Под systemd см. nodeloc2tg.service.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path

from config import Settings, validate_for_runtime
from discourse import DiscourseClient
from engine import Engine
from storage import Storage
from telegram_client import TelegramClient
from translate import Translator


def setup_logging(level: str) -> None:
    """Логи: в stdout (интерактивный запуск) И в bot.log (фоновый pythonw)."""
    fmt = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
        datefmt=datefmt,
    )
    log_path = Path(__file__).resolve().parent / "bot.log"
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(getattr(logging, level.upper(), logging.INFO))
    fh.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    logging.getLogger().addHandler(fh)
    # urllib3/connect pool спамит в DEBUG — приглушим
    logging.getLogger("urllib3").setLevel(logging.WARNING)


_stop = False


def _handle_sig(signum, _frame):
    global _stop
    logging.getLogger(__name__).info("Получен сигнал %s — завершаемся после цикла",
                                     signum)
    _stop = True


def _acquire_single_instance_lock():
    """Замок одиночного инстанса (Windows): venv-pythonw на этой машине
    почему-то плодит два процесса на один запуск — оба исполняют main.py,
    дерутся за БД и постят гонки. Держим файловый lock; второй инстанс
    молча выходит. Захват msvcrt.locking — байт 1 файла .bot.lock."""
    import msvcrt
    lock_path = Path(__file__).resolve().parent / ".bot.lock"
    fh = open(lock_path, "a+")
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return None  # заблокировано другим инстансом
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh  # НЕ закрывать — жизнь замка = жизнь процесса


def main() -> int:
    lock = _acquire_single_instance_lock()
    if lock is None:
        # второй инстанс — тихо выходим, не мешаем первому
        sys.exit(0)
    settings = Settings.load()
    setup_logging(settings.log_level)
    log = logging.getLogger("main")

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    log.info("=== nodeloc2tg старт ===")
    log.info("форум: %s | интервал: %ds | latest_limit: %d | backfill: %d",
             settings.forum_base, settings.poll_interval,
             settings.latest_limit, settings.initial_backfill)

    # ----- автолекарь туннеля -----
    # TG с RU-IP заблокирован, бот живёт только пока поднят Mullvad-туннель.
    # Туннель регулярно отваливается (роутер/провайдер рвёт сессию) — поэтому
    # перед каждым циклом дешёвая проверка TG, при недоступности — mullvad
    # connect + ожидание до 90с. Это единственная точка отказа системы.
    MULLVAD = os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "Mullvad VPN", "resources", "mullvad.exe")

    def tunnel_ok() -> bool:
        """Проверка доступности TG-пути: getMe через настроенный прокси.
        Прямой TCP из РФ заблокирован всегда — проверять его бесполезно.

        ВАЖНО: getMe через мёртвый SOCKS (порт открыт, forwarding нет)
        виснет в рукопожатии БЕЗ таймаута (PySocks legacy) — поэтому
        жёсткий лимит через поток. shutdown(wait=False): зависший поток
        бросаем, иначе весь бот стоит вечно (случай 16.09)."""
        if not tg:
            return True
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as _TE
        ex = ThreadPoolExecutor(max_workers=1)
        try:
            fut = ex.submit(tg.check)
            try:
                return bool(fut.result(timeout=30))
            except _TE:
                log.warning("tunnel_ok: getMe завис >30с — считаю мёртвым")
                return False
            except Exception:
                return False
        finally:
            ex.shutdown(wait=False)

    def heal_tunnel() -> bool:
        """Ждать восстановления сети (xray-прокси поднимает пользователь,
        Mullvad больше не используем — подписка кончилась)."""
        log.warning("TG недоступен — жду восстановления сети/прокси")
        for i in range(1, 16):  # до 90с
            time.sleep(6)
            if tunnel_ok():
                log.info("Сеть восстановлена (ожидание %dс)", i * 6)
                return True
        log.error("Сеть не ожила за 90с — пробуем в следующем цикле")
        return False

    # Валидация — ТОЛЬКО если реально постим. Для dry-run не нужна.
    tg: TelegramClient | None = None
    try:
        validate_for_runtime(settings)
        tg = TelegramClient(settings.bot_token, settings.channel_id,
                            settings.http_timeout, proxy=settings.tg_proxy)
        if not tg.check():
            # сеть/прокси могли не успеть подняться при старте системы.
            # НЕ выходим (нечему перезапускать бота) — ждём вечно, раз в
            # минуту, пока TG не ответит. Держим lock — второй инстанс
            # при попытке старта молча выйдет.
            log.warning("TG недоступен при старте — жду восстановления сети")
            while not _stop:
                if heal_tunnel():
                    break
                log.warning("сети всё ещё нет — следующая попытка через 60с")
                time.sleep(60)
            if not tg.check():
                log.error("TG так и не ожил — выход")
                return 2
    except RuntimeError as e:
        log.warning("%s — работаю в режиме dry-run (без постинга в TG)", e)
        tg = None

    storage = Storage()
    discourse = DiscourseClient(settings.forum_base, settings.user_agent,
                                settings.http_timeout)
    translator = Translator(delay_ms=settings.translate_delay_ms,
                            proxy=settings.translate_proxy,
                            llm_base=settings.llm_api_base,
                            llm_key=settings.llm_api_key,
                            llm_model=settings.llm_model,
                            llm_fallback_model=settings.llm_fallback_model,
                            llm_proxy=settings.llm_proxy)
    # мульти-источники: NodeSeek и др. (SOURCES=nodeloc,nodeseek в .env)
    extra_sources = {}
    _src = settings.sources.split(",")
    if "nodeseek" in _src:
        # www.nodeseek.com закрыт CF JS-челленджем (браузерный вызов) —
        # идём через официальный RSS rss.nodeseek.com (открыт, 20 тем)
        from nodeseek_rss import NodeSeekRSS
        extra_sources["nodeseek"] = NodeSeekRSS(
            proxy=settings.nodeseek_proxy, timeout=40)
    if "linuxdo" in _src:
        # linux.do закрыт CF JS-челленджем + логином — идём через публичное
        # TG-зеркало t.me/s/LinuxDoNew (структурированные посты)
        from linuxdo_mirror import LinuxDoMirror
        extra_sources["linuxdo"] = LinuxDoMirror(proxy=settings.nodeseek_proxy)
    if "naixi" in _src:
        # 奶昔论坛 (Discuz, SIM/eSIM/KYC-тематика) — открытый RSS
        from naixi import NaixiRSS
        extra_sources["naixi"] = NaixiRSS(proxy=settings.nodeseek_proxy)
    # threat-intel/leak-мониторы через публичное t.me/s-превью
    if "cybermonitum" in _src:
        from tg_channel import TGChannelSource
        extra_sources["cybermonitum"] = TGChannelSource(
            "CyberMonitum", proxy=settings.nodeseek_proxy)
    if "cvedetector" in _src:
        from tg_channel import TGChannelSource
        extra_sources["cvedetector"] = TGChannelSource(
            "CVEDetector", proxy=settings.nodeseek_proxy,
            severity_min=7.0)
    if "v2ex" in _src:
        from cfsource import CFSource, ldo_title_allowed

        class V2EX(CFSource):
            # без фильтра V2EX тащит общие обсуждения (музыка/экономика) —
            # пропускаем только техно/халява-темы, как у NodeSeek
            def latest(self, limit=30):
                ids = super().latest(limit * 3)
                out = []
                for tid in ids:
                    t = self.fetch(tid)
                    if t and ldo_title_allowed(t.title):
                        out.append(tid)
                    if len(out) >= limit:
                        break
                return out

        extra_sources["v2ex"] = V2EX(
            "v2ex", "https://www.v2ex.com", kind="v2ex",
            proxy=settings.nodeseek_proxy)
    # LLM-классификатор релевантности (прямой доступ к logfare, без прокси)
    relevance = None
    if settings.relevance_enabled and settings.llm_api_key:
        from relevance import RelevanceFilter
        relevance = RelevanceFilter(settings.llm_api_base, settings.llm_api_key,
                                    settings.relevance_model, proxy="")
        log.info("Фильтр релевантности: модель %s", settings.relevance_model)
    engine = Engine(settings, storage, discourse, translator, tg,
                    extra_sources=extra_sources, relevance=relevance)

    # ----- статистика для heartbeat-статуса -----
    import time as _t
    if not storage.get_meta("boot_ts"):
        storage.set_meta("boot_ts", str(int(_t.time())))

    hb = {
        "cycles": 0, "processed": 0, "posted": 0, "errors": 0,
        "last_title": "", "last_posted_ts": 0.0,
        "last_sent_ts": 0.0,  # когда отправляли последний heartbeat
        "posted_log": [],     # [{title,cat,translated,time}] — за период
        "filtered_log": [],   # [{reason,cat,title}] — за период
        "dupes": 0,
    }
    hb["last_sent_ts"] = float(storage.get_meta("hb_sent_ts", "0") or 0)

    def send_heartbeat() -> None:
        """Часовой отчёт в канал: стабильность, опубликованные посты
        (с категорией и отметкой перевода), что отфильтровано и почему.
        Старый отчёт удаляется — в канале всегда один активный."""
        if tg is None:
            return
        try:
            from datetime import datetime
            from html import escape
            boot = int(storage.get_meta("boot_ts", str(int(_t.time()))))
            uptime_h = (_t.time() - boot) / 3600
            up = (f"{int(uptime_h)}ч {int(uptime_h % 1 * 60)}м"
                  if uptime_h >= 1 else f"{int(uptime_h * 60)}м")

            # --- сводка стабильности ---
            ok = hb["errors"] == 0
            head = ("✅ Стабильно" if ok else f"⚠️ Ошибок: {hb['errors']}")
            lines = [
                "<b>🤖 Часовой отчёт nodeloc2tg</b>",
                "",
                f"{head} | Аптайм {up} | Циклов {hb['cycles']}",
            ]

            # --- опубликованные посты ---
            posted = hb["posted_log"]
            n_ru = sum(1 for p in posted if p.get("translated"))
            lines.append(f"📰 Опубликовано: {len(posted)}")
            for p in posted[:10]:
                mark = "🇷🇺" if p.get("translated") else "⚠️без перевода"
                tt = datetime.fromtimestamp(p.get("time", 0)).strftime("%H:%M")
                lines.append(f"  • {tt} [{escape(str(p.get('cat',''))[:14])}] "
                             f"{mark} {escape(p['title'][:55])}")
            if not posted:
                lines.append("  (новых тем по фильтрам не было)")
            elif n_ru < len(posted):
                lines.append(f"  ⚠️ Перевод: {n_ru}/{len(posted)} на русском — проверить!")

            # --- что отфильтровано и почему ---
            flt = hb["filtered_log"]
            if flt:
                by_reason: dict = {}
                for f in flt:
                    by_reason.setdefault(f["reason"], []).append(f)
                reasons = ", ".join(f"{r}: {len(v)}" for r, v in by_reason.items())
                lines.append(f"🚫 Отфильтровано: {len(flt)} ({reasons})")
                for f in flt[:5]:
                    lines.append(f"  • [{f['reason']}] {escape(f['title'][:50])}")
                if len(flt) > 5:
                    lines.append(f"  … и ещё {len(flt)-5}")

            if hb["dupes"]:
                lines.append(f"♻️ Дублей отклонено: {hb['dupes']}")

            text = "\n".join(lines)

            # старый отчёт — удалить (один активный в канале)
            old_mid = storage.get_meta("hb_msg_id", "")
            if old_mid:
                tg.delete_message(int(old_mid))
            mid = tg.send_message(text, disable_preview=True)
            storage.set_meta("hb_msg_id", str(mid))
            storage.set_meta("hb_sent_ts", str(int(_t.time())))
            # счётчики периода обнуляем
            hb.update(cycles=0, processed=0, posted=0, errors=0,
                      last_title="", last_posted_ts=0.0,
                      posted_log=[], filtered_log=[], dupes=0)
            hb["last_sent_ts"] = _t.time()
            log.info("Отчёт отправлен → msg_id=%d", mid)
        except Exception:
            log.exception("Отчёт не ушёл — продолжаю работу")

    # первый heartbeat сразу после старта (если период задан)
    if settings.heartbeat_hours > 0 and tg is not None:
        send_heartbeat()

    while not _stop:
        try:
            if not tunnel_ok():
                if not heal_tunnel():
                    # туннель не поднялся — коротко спим и пробуем снова,
                    # темы НЕ помечаем ошибкой (потери не будет)
                    time.sleep(60)
                    continue
            res = engine.run_once()
            hb["cycles"] += 1
            hb["processed"] += res.processed
            hb["posted"] += res.posted
            hb["errors"] += res.errors
            hb["dupes"] += res.skipped_known
            if res.last_post_title:
                hb["last_title"] = res.last_post_title
                hb["last_posted_ts"] = _t.time()
            # журналы для часового отчёта
            now = _t.time()
            for p in res.posted_log:
                p["time"] = now
                hb["posted_log"].append(p)
            for f in res.filtered_log:
                hb["filtered_log"].append(f)
            # не даём журналам расти бесконечно между отчётами
            if len(hb["posted_log"]) > 30:
                hb["posted_log"] = hb["posted_log"][-30:]
            if len(hb["filtered_log"]) > 40:
                hb["filtered_log"] = hb["filtered_log"][-40:]
            log.info("Проход: обработано=%d опубликовано=%d пропущено(изв.)=%d "
                     "коротких=%d ошибок=%d",
                     res.processed, res.posted, res.skipped_known,
                     res.skipped_short, res.errors)
        except Exception:
            hb["errors"] += 1
            log.exception("Непредвиденная ошибка в цикле — продолжаю")

        # heartbeat по расписанию
        if (settings.heartbeat_hours > 0 and tg is not None
                and (_t.time() - hb["last_sent_ts"]) >= settings.heartbeat_hours * 3600):
            send_heartbeat()

        # спим, но отзывчиво к сигналу — дробим интервал
        slept = 0
        while slept < settings.poll_interval and not _stop:
            time.sleep(1)
            slept += 1

    storage.close()
    log.info("=== nodeloc2tg остановлен ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
