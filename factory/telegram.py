"""Telegram Bot API: публикация, одобрение кнопками, dry-run в папку outbox/."""
import html
import logging
import os
import re
import time

import httpx

log = logging.getLogger("telegram")
CAPTION_LIMIT = 1024  # лимит подписи к фото (core.telegram.org)
TEXT_LIMIT = 4096


def visible_len(html_text: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", html_text)))


class Bot:
    def __init__(self, token: str | None, dry_run: bool, outbox="outbox"):
        self.token = token
        self.dry_run = dry_run or not token
        self.outbox = outbox

    def _call(self, method, data=None, files=None, tries=4):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        for _ in range(tries):
            r = httpx.post(url, data=data, files=files, timeout=60)
            j = r.json()
            if j.get("ok"):
                return j["result"]
            if j.get("error_code") == 429:  # лимит — ждём, сколько сказал Telegram
                wait = j.get("parameters", {}).get("retry_after", 5)
                log.warning("429, жду %ss", wait)
                time.sleep(wait + 1)
                continue
            raise RuntimeError(f"{method}: {j.get('description')}")
        raise RuntimeError(f"{method}: превышено число попыток")

    def _dry(self, chat_id, text, image=None, tag="post"):
        os.makedirs(self.outbox, exist_ok=True)
        path = os.path.join(self.outbox, f"{int(time.time()*1000)}_{tag}_{str(chat_id).strip('@')}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"<!-- chat: {chat_id} image: {image} -->\n{text}\n")
        log.info("[DRY-RUN] %s", path)
        return {"message_id": 0}

    def send_post(self, chat_id, text, image_path=None, image_url=None, reply_markup=None):
        """Фото + подпись, если влезает в 1024; иначе текстом с превью ссылки."""
        if self.dry_run:
            return self._dry(chat_id, text, image_path or image_url)
        base = {"chat_id": chat_id, "parse_mode": "HTML"}
        if reply_markup:
            base["reply_markup"] = reply_markup
        if (image_path or image_url) and visible_len(text) <= CAPTION_LIMIT:
            if image_path:
                with open(image_path, "rb") as f:
                    return self._call("sendPhoto", {**base, "caption": text}, files={"photo": f})
            return self._call("sendPhoto", {**base, "caption": text, "photo": image_url})
        if visible_len(text) > TEXT_LIMIT:
            raise ValueError("текст длиннее 4096 символов")
        if image_path or image_url:   # текст длиннее подписи: сначала картинка, следом текст
            photo = {"chat_id": chat_id}
            if image_path:
                with open(image_path, "rb") as f:
                    self._call("sendPhoto", photo, files={"photo": f})
            else:
                self._call("sendPhoto", {**photo, "photo": image_url})
        return self._call("sendMessage", {**base, "text": text})

    # --- одобрение ---
    def ask_approval(self, admin_chat, post_id, text, image_path=None, image_url=None):
        kb = ('{"inline_keyboard":[[{"text":"✅ Публиковать","callback_data":"ok:%d"},'
              '{"text":"❌ Нет","callback_data":"no:%d"}]]}' % (post_id, post_id))
        if self.dry_run:
            return self._dry(admin_chat, text, image_path or image_url, tag=f"approve{post_id}")
        return self.send_post(admin_chat, text, image_path, image_url, reply_markup=kb)

    def poll_decisions(self, offset: int):
        """Возвращает (решения [(post_id, 'ok'|'no')], новый offset)."""
        if self.dry_run:
            return [], offset
        updates = self._call("getUpdates", {"offset": offset, "timeout": 0,
                                            "allowed_updates": '["callback_query"]'})
        decisions = []
        for u in updates:
            offset = max(offset, u["update_id"] + 1)
            cq = u.get("callback_query")
            if not cq:
                continue
            m = re.fullmatch(r"(ok|no):(\d+)", cq.get("data", ""))
            if m:
                decisions.append((int(m.group(2)), m.group(1)))
                try:
                    self._call("answerCallbackQuery", {"callback_query_id": cq["id"],
                                                       "text": "Принято" if m.group(1) == "ok" else "Отклонено"})
                except Exception:  # noqa: BLE001
                    pass
        return decisions, offset

    def notify(self, admin_chat, text):
        if self.dry_run or not admin_chat:
            log.info("[NOTIFY] %s", text)
            return
        try:
            self._call("sendMessage", {"chat_id": admin_chat, "text": text[:4000]})
        except Exception as e:  # noqa: BLE001
            log.error("не смог уведомить админа: %s", e)
