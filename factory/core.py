"""Конвейер: сбор → генерация → (одобрение) → публикация по слотам."""
import json
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yaml

from . import images, llm as llm_mod, sources
from .db import DB
from .telegram import Bot

log = logging.getLogger("core")
SLOT_WINDOW_MIN = 90        # если сервер лежал дольше — пропущенный слот не догоняем
MAX_GENERATE_PER_RUN = 3    # ограничение расходов на один запуск
MAX_FAILS_PER_RUN = 2       # после стольких неудачных ответов канал ждёт следующего запуска


def load_config(path="config/channels.yaml"):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)["channels"]


class Factory:
    def __init__(self, config_path="config/channels.yaml", env=None):
        self.env = env if env is not None else os.environ
        self.cfg = load_config(config_path)
        self.db = DB(self.env.get("DB_PATH", "factory.db"))
        self.dry = self.env.get("DRY_RUN", "1") == "1"
        self.admin = self.env.get("ADMIN_CHAT_ID")
        self.llm_down = False   # модель перегружена — в этом запуске больше не дёргаем
        self.pause = float(self.env.get("GEN_PAUSE_SEC", "5"))
        provider = self.env.get("LLM_PROVIDER", "gemini" if self.env.get("GEMINI_API_KEY") else "anthropic")
        if provider == "gemini":
            self.llm = llm_mod.LLM(self.env.get("GEMINI_API_KEY"), self.env.get("GEMINI_MODEL", ""),
                                   provider="gemini")
        else:
            self.llm = llm_mod.LLM(self.env.get("ANTHROPIC_API_KEY"), self.env.get("CLAUDE_MODEL", ""),
                                   self.env.get("CLAUDE_MODEL_CHEAP"))
        if not self.llm.mock and not self.llm.model:
            raise SystemExit("Задай GEMINI_MODEL / CLAUDE_MODEL (точный id модели из Google AI Studio / console.anthropic.com)")

    # ---------- 0. Стартовые посты из seed/*.json ----------
    def import_seed(self, seed_dir="seed"):
        """Грузит готовые посты в очередь один раз (повторный запуск ничего не дублирует)."""
        import hashlib
        if self.dry:  # стартовые посты бережём для боевого режима (в dry-run кнопки одобрения не придут)
            log.info("DRY_RUN=1: стартовые посты не загружаю")
            return 0
        total = 0
        for key, c in self.channels().items():
            path = os.path.join(seed_dir, f"{key}.json")
            if not os.path.exists(path):
                continue
            raw = open(path, encoding="utf-8").read()
            mark = f"{key}:seed:" + hashlib.sha1(raw.encode()).hexdigest()[:12]
            if self.db.get(mark):
                continue
            status = "pending_approval" if c.get("approval") else "queued"
            for post in json.loads(raw):
                pid = self.db.add_post(key, None, post["text"].strip(), status)
                if status == "pending_approval":
                    msg = self.bot(c).ask_approval(self.admin or "ADMIN", pid, post["text"])
                    self.db.set_post(pid, approval_msg_id=msg.get("message_id"))
                total += 1
            self.db.put(mark, 1)
        return total

    def channels(self):
        return {k: c for k, c in self.cfg.items() if c.get("enabled")}

    def bot(self, c):
        return Bot(self.env.get(c["bot_token_env"]), self.dry)

    def chat(self, c):
        cid = self.env.get(c["channel_id_env"])
        if not cid and not self.dry:
            raise SystemExit(f"Нет {c['channel_id_env']} в .env")
        return cid or f"DRY_{c['channel_id_env']}"

    # ---------- 1. Сбор ----------
    def collect(self, key, c):
        mix = c.get("mix", {})
        n = 0
        if "rss_digest" in mix:
            n += sources.collect_rss(self.db, key, c)
        if "evergreen" in mix:
            n += sources.collect_evergreen(self.db, key, c)
        if "shopee_offers" in mix:
            n += sources.collect_shopee(self.db, key, c, self.env.get("SHOPEE_APP_ID"),
                                        self.env.get("SHOPEE_SECRET"))
        return n

    # ---------- 2. Генерация ----------
    def _pick_item(self, key, c):
        mix = c.get("mix", {})
        kinds = list(mix)
        first = random.choices(kinds, weights=[mix[k] for k in kinds])[0]
        kinds.remove(first)
        kinds.insert(0, first)  # сначала тип по весам mix, остальные — запасные
        for kind in kinds:
            item = self.db.next_unused_item(key, kind)
            if item:
                return kind, item
        return None, None

    def _finalize(self, kind, item, c, text):
        payload = json.loads(item["payload"] or "{}")
        if kind == "rss_digest" and item["url"] and item["url"] not in text:
            text += f'\n\n<a href="{item["url"]}">Источник</a>'
        if kind == "shopee_offers":
            link = payload.get("offerLink") or item["url"]
            text += f'\n\n👉 <a href="{link}">Ver oferta na Shopee</a>\n{c.get("disclosure", "#publi")}'
        return text

    def generate(self, key, c):
        made = fails = 0
        system = open(c["prompt"], encoding="utf-8").read()
        while self.db.ready_count(key) < c.get("queue_target", 5) and made < MAX_GENERATE_PER_RUN:
            if self.llm_down or fails >= MAX_FAILS_PER_RUN:
                break
            kind, item = self._pick_item(key, c)
            if not item:
                log.info("%s: нет сырья для генерации", key)
                break
            self.db.mark_item_used(item["id"])
            if not self.llm.mock and self.pause:
                time.sleep(self.pause)   # бесплатный тариф: не больше ~12 запросов в минуту
            try:
                data = llm_mod.parse_json(self.llm.complete(system, llm_mod.build_user_prompt(kind, item, c)))
            except llm_mod.LLMUnavailable as e:
                self.db.unmark_item(item["id"])   # сырьё вернём в очередь
                self.llm_down = True
                log.warning("%s: модель недоступна (%s) — продолжу в следующий запуск", key, e)
                break
            except Exception as e:  # noqa: BLE001
                fails += 1
                log.warning("%s: генерация не удалась: %s", key, e)
                continue
            text = self._finalize(kind, item, c, data["text"].strip())
            reason = llm_mod.is_rejected_by_rules(text, c)
            if reason:
                log.info("%s: пост отклонён автопроверкой: %s", key, reason)
                continue
            img_cfg = c.get("image", {})
            image_path = image_url = None
            if img_cfg.get("provider") == "openai":
                prompt = f'{data.get("image_prompt","")}. {img_cfg.get("style","")}'.strip(". ")
                image_path = images.generate_openai(prompt, self.env.get("OPENAI_API_KEY"), "media",
                                                    self.env.get("OPENAI_IMAGE_MODEL", "gpt-image-1-mini"))
            elif img_cfg.get("provider") == "source":
                image_url = json.loads(item["payload"] or "{}").get("imageUrl")
            status = "pending_approval" if c.get("approval") else "queued"
            pid = self.db.add_post(key, item["id"], text, status, image_url, image_path)
            if status == "pending_approval":
                msg = self.bot(c).ask_approval(self.admin or "ADMIN", pid, text, image_path, image_url)
                self.db.set_post(pid, approval_msg_id=msg.get("message_id"))
            made += 1
        return made

    # ---------- 3. Одобрение ----------
    def approvals(self, key, c):
        if not c.get("approval"):
            return
        bot = self.bot(c)
        off_key = f"{key}:updates_offset"
        decisions, offset = bot.poll_decisions(int(self.db.get(off_key, 0)))
        self.db.put(off_key, offset)
        for pid, d in decisions:
            p = self.db.get_post(pid)
            if p and p["channel"] == key and p["status"] == "pending_approval":
                self.db.set_post(pid, status="approved" if d == "ok" else "rejected")
        timeout = timedelta(minutes=c.get("approval_timeout_min", 240))
        for p in self.db.posts(key, "pending_approval"):
            if datetime.now(timezone.utc) - datetime.fromisoformat(p["created_at"]) > timeout:
                self.db.set_post(p["id"], status="rejected", error="нет ответа — истёк срок одобрения")

    # ---------- 4. Публикация ----------
    def due_slot(self, c, now_utc=None):
        tz = ZoneInfo(c.get("timezone", "UTC"))
        now = (now_utc or datetime.now(timezone.utc)).astimezone(tz)
        day = now.date().isoformat()
        for s in c.get("slots", []):
            hh, mm = map(int, s.split(":"))
            slot_t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if slot_t <= now < slot_t + timedelta(minutes=SLOT_WINDOW_MIN):
                return day, s
        return None, None

    def publish(self, key, c, now_utc=None):
        day, slot = self.due_slot(c, now_utc)
        if not slot or self.db.slot_used(key, day, slot):
            return None
        ready = self.db.posts(key, "approved") + self.db.posts(key, "queued")
        self.db.use_slot(key, day, slot)
        if not ready:
            self.bot(c).notify(self.admin, f"⚠️ {c['title']}: слот {slot} пропущен — нет готовых постов")
            return None
        p = ready[0]
        try:
            msg = self.bot(c).send_post(self.chat(c), p["text"], p["image_path"], p["image_url"])
            self.db.set_post(p["id"], status="published", published_at=datetime.now(timezone.utc).isoformat(),
                             tg_message_id=msg.get("message_id"))
            return p["id"]
        except Exception as e:  # noqa: BLE001
            self.db.set_post(p["id"], status="failed", error=str(e))
            self.bot(c).notify(self.admin, f"❌ {c['title']}: ошибка публикации поста {p['id']}: {e}")
            return None

    # ---------- Полный цикл (cron каждые 10 минут) ----------
    def run(self, collect_every_min=60):
        chans = list(self.channels().items())
        random.shuffle(chans)   # чтобы при лимитах модели не голодал всегда последний канал
        for key, c in chans:
            try:
                last = self.db.get(f"{key}:last_collect")
                if not last or datetime.now(timezone.utc) - datetime.fromisoformat(last) > timedelta(minutes=collect_every_min):
                    self.collect(key, c)
                    self.db.put(f"{key}:last_collect", datetime.now(timezone.utc).isoformat())
                self.approvals(key, c)
                self.generate(key, c)
                self.publish(key, c)
            except Exception as e:  # noqa: BLE001
                log.exception("%s: сбой цикла", key)
                self.bot(c).notify(self.admin, f"❌ {c.get('title', key)}: сбой цикла: {e}")

    def status(self):
        rows = []
        for key in self.cfg:
            counts = {s: len(self.db.posts(key, s)) for s in
                      ("queued", "pending_approval", "approved", "published", "rejected", "failed")}
            rows.append((key, counts))
        return rows
