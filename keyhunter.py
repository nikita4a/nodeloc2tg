"""Хантер API-ключей в постах (порт из ai-radar).

Сканирует заголовок+тело темы на ключи 13 провайдеров, отсекает
плейсхолдеры/примеры, рендерит отдельный TG-пост с ключами в <code>
(тап-копирование). Дедуп по значению ключа.
"""
from __future__ import annotations

import html as ihtml
import re

KEY_PATTERNS = [
    ("ELEVENLABS", re.compile(r'\bsk_[a-f0-9]{32,64}\b')),
    ("TELEGRAM",   re.compile(r'(?<![\d:])\b\d{8,10}:[A-Za-z0-9_\-]{30,45}\b')),
    ("GOOGLE",     re.compile(r'\bAIza[0-9A-Za-z_\-]{35}\b')),
    ("REPLICATE",  re.compile(r'\br8_[A-Za-z0-9]{30,}\b')),
    ("ANTHROPIC",  re.compile(r'\bsk-ant-[A-Za-z0-9_\-]{40,}\b')),
    ("OPENROUTER", re.compile(r'\bsk-or-v1-[a-f0-9]{64}\b')),
    ("HUGGINGFACE", re.compile(r'\bhf_[A-Za-z0-9]{30,}\b')),
    ("CAPSOLVER",  re.compile(r'\bCAP-[A-F0-9]{32}\b')),
    ("ZAI",        re.compile(r'\b[a-f0-9]{32}\.[A-Za-z0-9]{16}\b')),
    ("KIMI",       re.compile(r'\bsk-[A-Za-z0-9]{40,56}\b')),
    ("OPENAI",     re.compile(r'\bsk-(?:proj-[A-Za-z0-9_\-]{20,}|'
                              r'[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20,})')),
    ("DASHSCOPE",  re.compile(r'\bsk-[a-f0-9]{32}\b')),
    ("SK-OTHER",   re.compile(r'\bsk-[A-Za-z0-9_\-]{20,}\b')),
]
# 32-hex без префикса: только если рядом явно назван провайдер
KEY_CTX_PATTERNS = [
    ("TWOCAPTCHA",  re.compile(r'\b[a-f0-9]{32}\b'),
     ("2captcha", "twocaptcha", "rucaptcha")),
    ("ANTICAPTCHA", re.compile(r'\b[a-f0-9]{32}\b'),
     ("anti-captcha", "anticaptcha", "anti captcha")),
]
KEY_FAKE = re.compile(
    r'(?i)(your[_-]?|example|placeholder|sample|dummy|changeme|'
    r'test[_-]?key|secret|xxx{2,}|abc{2,}|(?:1234){2,})')
KEY_MIN_LEN = 20


def _looks_fake(k: str) -> bool:
    if KEY_FAKE.search(k):
        return True
    body = k.split(':')[-1]
    tail = re.split(r'[_\-]', body)[-1]
    if len(tail) >= 16:
        body = tail
    if body.isdigit():
        return True
    if len(set(body)) < 8:
        return True
    if re.search(r'(.)\1{3,}', body):
        return True
    return False


def scan_keys(text: str) -> dict[str, set[str]]:
    """Ключи провайдеров в тексте: {PROVIDER: {key, ...}}.

    Ключ достаётся первому (самому специфичному) совпавшему шаблону.
    DEEPSEEK/DASHSCOPE разводятся по контексту слова в тексте.
    """
    found: dict[str, set[str]] = {}
    text = text or ""
    low = text.lower()
    claimed: set[str] = set()
    for prov, pat in KEY_PATTERNS:
        for k in pat.findall(text):
            k = k.strip()
            if k in claimed or len(k) < KEY_MIN_LEN or _looks_fake(k):
                continue
            prov2 = "DEEPSEEK" if (prov == "DASHSCOPE" and "deepseek" in low) else prov
            found.setdefault(prov2, set()).add(k)
            claimed.add(k)
    for prov, pat, ctx in KEY_CTX_PATTERNS:
        if any(c in low for c in ctx):
            for k in pat.findall(text):
                if k not in claimed and len(k) >= KEY_MIN_LEN and not _looks_fake(k):
                    found.setdefault(prov, set()).add(k)
                    claimed.add(k)
    return found


def render_keys_post(provider: str, keys, label: str, url: str = "",
                     note: str = "") -> str:
    keys = list(keys)[:30]
    lines = [f"🔑 <b>{provider}</b> — {len(keys)} шт.{note}", ""]
    lines += ["<code>" + ihtml.escape(k) + "</code>" for k in keys]
    tail = "\n\n" + "─" * 12 + "\n🔗 " + ihtml.escape(label[:120])
    if url:
        tail += f' · <a href="{ihtml.escape(url, quote=True)}">источник</a>'
    return "\n".join(lines)[:3600] + tail
