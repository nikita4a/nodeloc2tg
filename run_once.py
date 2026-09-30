"""Одиночный прогон без бесконечного цикла.

Удобно:
  - проверить что парсинг+перевод работают (dry-run, печатает в консоль)
  - запускать по cron вместо main.py
  - принудительно запостить накопившееся

Флаги:
  --dry-run   не постить в TG, только напечатать собранное сообщение
  --force     игнорировать initial_backfill (полезно после первого старта)
  --no-tg     синоним --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys

from config import Settings
from discourse import DiscourseClient
from engine import Engine
from storage import Storage
from telegram_client import TelegramClient
from translate import Translator


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Не постить в TG, только напечатать сообщения")
    ap.add_argument("--no-tg", action="store_true",
                    help="То же что --dry-run")
    ap.add_argument("--force", action="store_true",
                    help="Игнорировать initial_backfill (обработать даже старое)")
    args = ap.parse_args()

    settings = Settings.load()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("run_once")

    dry = args.dry_run or args.no_tg

    tg = None
    if not dry and settings.bot_token and "ExampleToken" not in settings.bot_token:
        tg = TelegramClient(settings.bot_token, settings.channel_id,
                            settings.http_timeout, proxy=settings.tg_proxy)
        if not tg.check():
            log.error("TG-бот недоступен, переключаюсь в dry-run")
            tg = None
            dry = True

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
    engine = Engine(settings, storage, discourse, translator, tg)

    res = engine.run_once(force_post=args.force)
    log.info("Готово: обработано=%d опубликовано=%d пропущено=%d коротких=%d ошибок=%d",
             res.processed, res.posted, res.skipped_known,
             res.skipped_short, res.errors)
    storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
