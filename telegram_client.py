"""Тонкий клиент Telegram Bot API (sendMessage в канал).

Зависимости только requests — никаких python-telegram-bot, чтобы держать
сборку минимальной и предсказуемой на VPS.

Метод sendMessage поддерживает parse_mode=HTML. Текст сообщения формирует
formatter.py и отвечает за экранирование.
"""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(Exception):
    pass


class TelegramClient:
    def __init__(self, bot_token: str, channel_id: str, timeout: int = 20,
                 proxy: str = ""):
        if not bot_token:
            raise ValueError("bot_token пустой")
        self.bot_token = bot_token
        self.channel_id = channel_id
        # мульти-таргет: '7448683285,@NODELOCRUS' — постим во все
        self.targets = [t.strip() for t in channel_id.split(',') if t.strip()]
        self.timeout = timeout
        self.session = requests.Session()
        # trust_env=False: игнорировать системные прокси-переменные (HTTP_PROXY и
        # пр.) — используем ТОЛЬКО явно заданный proxy, иначе системный мусор
        # конфликтует с нашим SOCKS и получается недетерминированный таймаут.
        self.session.trust_env = False
        # Прокси нужен там, где api.telegram.org недоступен напрямую (напр. RU
        # хостинговые сети). socks5h — DNS тоже через прокси (важно, иначе
        # локальный DNS может резолвить в недостижимый/фальшивый адрес).
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
            log.info("TG API через прокси: %s", proxy)

    # ---------- публичное ----------
    def _send_message_chat(self, chat: str, html_text: str, *, disable_preview: bool = True) -> int:
        """Отправить HTML-сообщение в канал. Возвращает message_id.

        Raises TelegramError если TG отклонил окончательно.
        """
        params = {
            "chat_id": chat,
            "text": html_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }
        # лимит обычного сообщения — 4096.
        if len(html_text) > 4096:
            params["text"] = html_text[:4090] + "\n…"

        for attempt in range(1, 4):
            try:
                resp = self.session.post(
                    API_BASE.format(token=self.bot_token, method="sendMessage"),
                    data=params,
                    timeout=self.timeout,
                )
                data = resp.json()
                if resp.status_code == 200 and data.get("ok"):
                    return data["result"]["message_id"]

                desc = data.get("description", "")
                # 429 — rate limit от TG: ждём retry_after и пробуем снова
                if resp.status_code == 429:
                    retry_after = data.get("parameters", {}).get("retry_after", 5)
                    log.warning("TG 429: ждём %.1fs", retry_after)
                    time.sleep(float(retry_after) + 1)
                    continue
                # 400 — баг в тексте/форматировании; не ретраим, кидаем наверх
                raise TelegramError(f"TG {resp.status_code}: {desc}")
            except requests.RequestException as e:
                wait = 2 ** attempt
                log.warning("Сеть TG: %s, жду %ds (попытка %d/3)",
                            e.__class__.__name__, wait, attempt)
                time.sleep(wait)
        raise TelegramError("sendMessage: превышены 3 попытки")

    def _send_photo_chat(self, chat: str, image_url: str, caption: str) -> int:
        """Отправить фото с HTML-подписью в канал.

        Скачивает картинку по URL и шлёт как multipart (надёжнее, чем
        передавать URL напрямую — TG сам иногда не тянет внешние картинки).
        caption обрезается до 1024 (жёсткий лимит TG для подписи).
        Raises TelegramError при фатальной ошибке.
        """
        cap = caption
        if len(cap) > 1024:
            cap = cap[:1020] + "…"

        # скачиваем картинку
        img_bytes = self._download_image(image_url)
        if img_bytes is None:
            raise TelegramError(f"не удалось скачать картинку: {image_url}")

        for attempt in range(1, 4):
            try:
                files = {"photo": ("image.jpg", img_bytes, "image/jpeg")}
                data = {
                    "chat_id": chat,
                    "caption": cap,
                    "parse_mode": "HTML",
                }
                resp = self.session.post(
                    API_BASE.format(token=self.bot_token, method="sendPhoto"),
                    data=data, files=files,
                    timeout=self.timeout,
                )
                payload = resp.json()
                if resp.status_code == 200 and payload.get("ok"):
                    return payload["result"]["message_id"]
                desc = payload.get("description", "")
                if resp.status_code == 429:
                    retry_after = payload.get("parameters", {}).get("retry_after", 5)
                    log.warning("TG 429 (sendPhoto): ждём %.1fs", retry_after)
                    time.sleep(float(retry_after) + 1)
                    continue
                raise TelegramError(f"TG {resp.status_code}: {desc}")
            except requests.RequestException as e:
                wait = 2 ** attempt
                log.warning("Сеть TG (sendPhoto): %s, жду %ds (попытка %d/3)",
                            e.__class__.__name__, wait, attempt)
                time.sleep(wait)
        raise TelegramError("sendPhoto: превышены 3 попытки")

    def _send_media_group_chat(self, chat: str, image_urls: list, caption: str = "") -> int:
        """Отправить альбом фото (2-10). Возвращает message_id первого.

        caption ставится на первое фото (TG показывает его под альбомом).
        Качинки скачиваем и шлём multipart — надёжнее, чем URL-режим.
        """
        import json as _json
        if not image_urls:
            raise TelegramError("send_media_group: пустой список")
        photos: list[tuple[str, bytes]] = []
        for url in image_urls[:10]:
            data = self._download_image(url)
            if data:
                photos.append((url, data))
        if not photos:
            raise TelegramError("ни одну картинку не удалось скачать")

        media = []
        files = {}
        for i, (url, data) in enumerate(photos):
            attach = f"photo{i}" if i else "photo"
            entry = {"type": "photo", "media": f"attach://{attach}"}
            if i == 0 and caption:
                entry["caption"] = caption[:1020]
                entry["parse_mode"] = "HTML"
            media.append(entry)
            files[attach] = (f"img{i}.jpg", data, "image/jpeg")

        for attempt in range(1, 4):
            try:
                resp = self.session.post(
                    API_BASE.format(token=self.bot_token, method="sendMediaGroup"),
                    data={"chat_id": chat, "media": _json.dumps(media)},
                    files=files,
                    timeout=self.timeout,
                )
                payload = resp.json()
                if resp.status_code == 200 and payload.get("ok"):
                    return payload["result"][0]["message_id"]
                desc = payload.get("description", "")
                if resp.status_code == 429:
                    retry_after = payload.get("parameters", {}).get("retry_after", 5)
                    log.warning("TG 429 (mediaGroup): ждём %.1fs", retry_after)
                    time.sleep(float(retry_after) + 1)
                    continue
                raise TelegramError(f"TG {resp.status_code}: {desc}")
            except requests.RequestException as e:
                wait = 2 ** attempt
                log.warning("Сеть TG (mediaGroup): %s, жду %ds", e.__class__.__name__, wait)
                time.sleep(wait)
        raise TelegramError("sendMediaGroup: превышены 3 попытки")

    def _send_video_chat(self, chat: str, video_url: str, caption: str) -> int:
        """Отправить видео с HTML-подписью (скачиваем и шлём multipart)."""
        cap = caption[:1020] + "…" if len(caption) > 1024 else caption
        data = self._download_image(video_url)
        if data is None:
            raise TelegramError(f"не удалось скачать видео: {video_url}")
        if len(data) > 50 * 1024 * 1024:
            raise TelegramError("видео >50MB — лимит Bot API")
        for attempt in range(1, 4):
            try:
                files = {"video": ("video.mp4", data, "video/mp4")}
                payload = {"chat_id": chat, "caption": cap,
                           "parse_mode": "HTML"}
                resp = self.session.post(
                    API_BASE.format(token=self.bot_token, method="sendVideo"),
                    data=payload, files=files, timeout=max(self.timeout, 120))
                js = resp.json()
                if resp.status_code == 200 and js.get("ok"):
                    return js["result"]["message_id"]
                desc = js.get("description", "")
                if resp.status_code == 429:
                    time.sleep(float(js.get("parameters", {}).get("retry_after", 5)) + 1)
                    continue
                raise TelegramError(f"TG {resp.status_code}: {desc}")
            except requests.RequestException as e:
                time.sleep(2 ** attempt)
                if attempt == 3:
                    raise TelegramError(f"sendVideo сеть: {e}")
        raise TelegramError("sendVideo: превышены попытки")

    # ---------- бродкаст во все таргеты ----------
    def _broadcast(self, method_name: str, *args, **kwargs) -> int:
        """Отправить во все таргеты; вернуть message_id первого."""
        mids = []
        for chat in self.targets:
            try:
                mids.append(getattr(self, method_name)(chat, *args, **kwargs))
            except TelegramError as e:
                log.warning("TG %s → %s: %s", method_name, chat, e)
        if not mids:
            raise TelegramError(f"{method_name}: все таргеты провалились")
        return mids[0]

    def send_message(self, html_text: str, *, disable_preview: bool = True) -> int:
        return self._broadcast("_send_message_chat", html_text,
                               disable_preview=disable_preview)

    def send_photo(self, image_url: str, caption: str) -> int:
        return self._broadcast("_send_photo_chat", image_url, caption)

    def send_media_group(self, image_urls: list, caption: str = "") -> int:
        return self._broadcast("_send_media_group_chat", image_urls, caption)

    def send_video(self, video_url: str, caption: str) -> int:
        return self._broadcast("_send_video_chat", video_url, caption)

    def _download_image(self, url: str) -> bytes | None:
        """Скачать картинку/видео. Возвращает bytes или None.

        КАЧЕСТВЕННО через self.session — в ней настроен прокси: CDN Telegram
        (telesco.pe) и часть зарубежных хостингов из РФ напрямую недоступны.
        """
        # Telegram CDN (telesco.pe) периодически отдаёт 500 на валидные URL —
        # обязателен ретрай, иначе посты теряют фото (случай msg 6374)
        for attempt in range(1, 4):
            try:
                resp = self.session.get(url, timeout=self.timeout)
                if resp.status_code == 200:
                    data = resp.content
                    if len(data) > 10 * 1024 * 1024:
                        log.warning("Картинка слишком большая (%d байт) — пропускаю",
                                    len(data))
                        return None
                    return data
                if resp.status_code >= 500 and attempt < 3:
                    log.warning("Картинка → HTTP %s, ретрай %d/3",
                                resp.status_code, attempt)
                    time.sleep(2 * attempt)
                    continue
                log.warning("Картинка %s → HTTP %s", url[:80], resp.status_code)
                return None
            except requests.RequestException as e:
                if attempt < 3:
                    log.warning("Сеть картинки: %s, ретрай %d/3",
                                e.__class__.__name__, attempt)
                    time.sleep(2 * attempt)
                    continue
                log.warning("Не удалось скачать картинку %s: %s", url[:80], e)
                return None
        return None

    def delete_message(self, message_id: int) -> bool:
        """Удалить своё сообщение (для замены статус-постов)."""
        try:
            resp = self.session.post(
                API_BASE.format(token=self.bot_token, method="deleteMessage"),
                data={"chat_id": self.channel_id, "message_id": message_id},
                timeout=self.timeout,
            )
            return resp.json().get("ok", False)
        except Exception as e:
            log.warning("deleteMessage(%s) упал: %s", message_id, e)
            return False

    def check(self) -> bool:
        """getMe — проверить что токен валиден. True/False."""
        try:
            resp = self.session.get(
                API_BASE.format(token=self.bot_token, method="getMe"),
                timeout=self.timeout,
            )
            data = resp.json()
            if data.get("ok"):
                log.info("TG-бот: @%s (%s)",
                         data["result"].get("username"), data["result"].get("first_name"))
                return True
            log.error("TG getMe: %s", data.get("description"))
            return False
        except Exception as e:
            log.error("TG getMe упал: %s", e)
            return False
