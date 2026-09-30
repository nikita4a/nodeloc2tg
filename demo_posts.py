"""Демо: живые темы → перевод → форматтер → печать постов как их пошлёт бот.

Запуск: PYTHONPATH=venv/Lib/site-packages py -3.11 demo_posts.py [n]
Без LLM-ключей перевод идёт через fallback-цепочку (Bing/Google).
"""
from __future__ import annotations
import sys

from config import Settings
from discourse import DiscourseClient
from nodeseek_rss import NodeSeekRSS
from linuxdo_mirror import LinuxDoMirror
from translate import Translator
from formatter import build_message


N = int(sys.argv[1]) if len(sys.argv) > 1 else 2

s = Settings.load()
tr = Translator(delay_ms=s.translate_delay_ms, proxy=s.translate_proxy)


def show(tag: str, t, title_ru: str, body_ru: str):
    print("=" * 70)
    print("[%s] topic_id=%s  %s" % (tag, t.topic_id, t.url))
    print("-" * 70)
    print(build_message(t, title_ru, body_ru, s.max_body_chars)[:2600])
    print()


def grab(doit, tag):
    try:
        doit(tag)
    except Exception as e:
        print("%s: %s: %s" % (tag, e.__class__.__name__, e))


def run_nodeloc(tag):
    dc = DiscourseClient(s.forum_base, s.user_agent, s.http_timeout)
    ids = dc.latest_topic_ids(30)[-N:]
    for t in dc.fetch_many(ids):
        show(tag, t, tr.translate(t.title), tr.translate(t.body_text[:800]))


def run_nodeseek(tag):
    ns = NodeSeekRSS(proxy=s.nodeseek_proxy)
    for tid in ns.latest(30)[-N:]:
        t = ns.fetch(tid)
        if t:
            show(tag, t, tr.translate(t.title), tr.translate(t.body_text[:800]))


def run_linuxdo(tag):
    ld = LinuxDoMirror(proxy=s.nodeseek_proxy)
    for tid in ld.latest(30)[-N:]:
        t = ld.fetch(tid)
        if t:
            show(tag, t, tr.translate(t.title), tr.translate(t.body_text[:800]))


grab(run_nodeloc, "nodeloc")
grab(run_nodeseek, "nodeseek")
grab(run_linuxdo, "linuxdo")
