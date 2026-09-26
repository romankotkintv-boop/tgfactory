"""Конвейер: сбор → генерация → (одобрение) → публикация по слотам."""
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yaml

from . import quiz as quiz_mod
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
        if "trending" in mix:
            n += sources.collect_trending(self.db, key, c)
        if "shopee_offers" in mix:
            n += sources.collect_shopee(self.db, key, c, self.env.get("SHOPEE_APP_ID"),
                                        self.env.get("SHOPEE_SECRET"))
        return n

    # ---------- 2. Генерация ----------
    def _pick_item(self, key, c, only=None):
        mix = c.get("mix", {})
        kinds = list(mix)
        first = random.choices(kinds, weights=[mix[k] for k in kinds])[0]
        kinds.remove(first)
        kinds.insert(0, first)  # сначала тип по весам mix, остальные — запасные
        if only:
            kinds = [only] if only in mix else []
        max_age = c.get("trending", {}).get("max_age_hours", 36) * 3600
        for kind in kinds:
            while True:
                item = self.db.next_unused_item(key, kind)
                if not item:
                    break
                if kind == "trending":   # хайп протухает: старше max_age_hours не берём
                    ts = json.loads(item["payload"] or "{}").get("published_ts") or 0
                    if ts and time.time() - ts > max_age:
                        self.db.mark_item_used(item["id"])
                        continue
                return kind, item
        return None, None

    def _finalize(self, kind, item, c, text):
        payload = json.loads(item["payload"] or "{}")
        if kind in ("rss_digest", "trending") and item["url"] and "trends.google" not in item["url"] \
                and item["url"] not in text:
            text += f'\n\n<a href="{item["url"]}">Источник</a>'
        if kind == "shopee_offers":
            link = payload.get("offerLink") or item["url"]
            text += f'\n\n👉 <a href="{link}">Ver oferta na Shopee</a>\n{c.get("disclosure", "#publi")}'
        return text

    def generate(self, key, c):
        made = fails = 0
        system = open(c["prompt"], encoding="utf-8").read()
        # Хайп не ждёт очереди: если готового поста по свежей горячей теме нет — делаем один сразу
        want_hot = "trending" in c.get("mix", {}) and not self.hot_posts(key, c)
        while (want_hot or self.db.ready_count(key) < c.get("queue_target", 5)) and made < MAX_GENERATE_PER_RUN:
            if self.llm_down or fails >= MAX_FAILS_PER_RUN:
                break
            kind, item = self._pick_item(key, c, only="trending" if want_hot else None)
            if want_hot and not item:
                want_hot = False
                continue
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
            if kind == "trending":
                want_hot = False
        return made

    def hot_posts(self, key, c):
        """Свежие посты по горячим темам; протухшие (старше max_age_hours) снимаем с публикации."""
        max_age = timedelta(hours=c.get("trending", {}).get("max_age_hours", 36))
        fresh = []
        for p in self.db.hot_posts(key):
            if datetime.now(timezone.utc) - datetime.fromisoformat(p["created_at"]) > max_age:
                self.db.set_post(p["id"], status="rejected", error="горячая тема устарела")
            else:
                fresh.append(p)
        return fresh

    def ready_posts(self, key, c):
        """Порядок публикации: свежий хайп → одобренные → очередь."""
        hot = self.hot_posts(key, c)
        ids = {p["id"] for p in hot}
        rest = [p for p in self.db.posts(key, "approved") + self.db.posts(key, "queued") if p["id"] not in ids]
        return hot + rest

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

    # ---------- 4. Картинка к посту (в момент публикации: платим только за вышедшие посты) ----------
    def ensure_image(self, key, c, p):
        """Возвращает (image_path, image_url). Нет картинки — публикуем текстом, админу одно уведомление в день."""
        if p["image_path"] or p["image_url"]:
            return p["image_path"], p["image_url"]
        img = c.get("image", {})
        if "gemini" not in (img.get("provider"), img.get("fallback")) or self.dry:
            return None, None   # в тестовом режиме не тратим деньги на картинки
        plain = re.sub(r"<[^>]+>", "", p["text"])[:700]
        if img.get("text") == "english":   # латиница у модели выходит чисто — даём короткий английский заголовок
            rule = ("Put exactly ONE short English headline (max 4 words: the key English word or phrase of the post) "
                    "into the image in large bold clean sans-serif letters, spelled exactly correctly. "
                    "No other text, no Uzbek, no Russian, no numbers")
        else:
            rule = "do not put any text, letters or numbers into the image"
        ref = img.get("mascot_ref") if img.get("mascot_ref") and os.path.exists(img["mascot_ref"]) else None
        mascot = f"\n\n{img['mascot']}" if ref and img.get("mascot") else ""
        prompt = (f"{img.get('style', '')}{mascot}\n\nThe image illustrates this Telegram post ({rule}):\n{plain}")
        model = self.env.get("GEMINI_IMAGE_MODEL") or "gemini-3.1-flash-lite-image"
        path, err = images.generate_gemini(prompt, self.env.get("GEMINI_API_KEY"), "media", model,
                                           img.get("aspect", "4:3"), ref_path=ref)
        if err and ref:   # с образцом не вышло — рисуем без маскота, пост не должен остаться без картинки
            log.warning("%s: картинка с маскотом не вышла (%s), пробую без него", key, err)
            prompt = (f"{img.get('style', '')}\n\nThe image illustrates this Telegram post ({rule}):\n{plain}")
            path, err = images.generate_gemini(prompt, self.env.get("GEMINI_API_KEY"), "media", model,
                                               img.get("aspect", "4:3"))
        if path:
            mark = img.get("watermark") or self.env.get(c["channel_id_env"], "")
            path = images.add_watermark(path, mark if str(mark).startswith("@") else "")
        if err:
            log.warning("%s: картинка не создана: %s", key, err)
            flag = f"img_fail:{datetime.now(timezone.utc).date().isoformat()}"
            if not self.db.get(flag):
                self.db.put(flag, 1)
                self.bot(c).notify(self.admin, f"⚠️ Картинки не создаются (пост ушёл без картинки): {err[:300]}")
        return path, None

    # ---------- 5. Публикация ----------
    def due_slot(self, c, now_utc=None, slots=None):
        tz = ZoneInfo(c.get("timezone", "UTC"))
        now = (now_utc or datetime.now(timezone.utc)).astimezone(tz)
        day = now.date().isoformat()
        for s in (c.get("slots", []) if slots is None else slots):
            hh, mm = map(int, s.split(":"))
            slot_t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if slot_t <= now < slot_t + timedelta(minutes=SLOT_WINDOW_MIN):
                return day, s
        return None, None

    def publish(self, key, c, now_utc=None):
        day, slot = self.due_slot(c, now_utc)
        if not slot or self.db.slot_used(key, day, slot):
            return None
        ready = self.ready_posts(key, c)
        self.db.use_slot(key, day, slot)
        if not ready:
            self.bot(c).notify(self.admin, f"⚠️ {c['title']}: слот {slot} пропущен — нет готовых постов")
            return None
        p = ready[0]
        try:
            image_path, image_url = self.ensure_image(key, c, p)
            msg = self.bot(c).send_post(self.chat(c), p["text"], image_path, image_url)
            self.db.set_post(p["id"], status="published", published_at=datetime.now(timezone.utc).isoformat(),
                             tg_message_id=msg.get("message_id"))
            return p["id"]
        except Exception as e:  # noqa: BLE001
            self.db.set_post(p["id"], status="failed", error=str(e))
            self.bot(c).notify(self.admin, f"❌ {c['title']}: ошибка публикации поста {p['id']}: {e}")
            return None

    # ---------- 6. Викторины (регулярный интерактив) ----------
    def publish_quiz(self, key, c, now_utc=None, force=False):
        qcfg = c.get("quiz")
        if not qcfg:
            return None
        if force:
            day, slot = None, None
        else:
            day, slot = self.due_slot(c, now_utc, qcfg.get("slots", []))
            if not slot or self.db.slot_used(key, day, "quiz " + slot):
                return None
        if self.llm_down:
            return None
        last_err = None
        for _ in range(2):   # модель иногда нарушает лимиты — вторая попытка
            try:
                q = quiz_mod.make(self.llm, qcfg, quiz_mod.recent_topics(self.db, key))
                break
            except llm_mod.LLMUnavailable as e:
                self.llm_down = True
                log.warning("%s: викторина отложена, модель недоступна: %s", key, e)
                return None   # слот не занимаем — попробуем в следующий запуск (окно 90 мин)
            except Exception as e:  # noqa: BLE001
                last_err = e
        else:
            if slot:
                self.db.use_slot(key, day, "quiz " + slot)
            self.bot(c).notify(self.admin, f"⚠️ {c['title']}: викторина не собралась: {last_err}")
            return None
        if slot:
            self.db.use_slot(key, day, "quiz " + slot)
        try:
            intro = qcfg.get("intro")
            if intro and not self.dry:
                self.bot(c).send_post(self.chat(c), intro)
            msg = self.bot(c).send_quiz(self.chat(c), q["question"], q["options"], q["correct"], q["explanation"])
            quiz_mod.remember(self.db, key, q)
            return msg.get("message_id")
        except Exception as e:  # noqa: BLE001
            self.bot(c).notify(self.admin, f"❌ {c['title']}: ошибка публикации викторины: {e}")
            return None

    # ---------- Полный цикл (cron каждые 10 минут) ----------
    def reconcile(self):
        """already_published в channels.yaml: посты, которые уже вышли, но память о них потерялась.
        Совпадение по куску текста — такие посты помечаем опубликованными, чтобы не было дублей."""
        n = 0
        for key, c in self.channels().items():
            for frag in c.get("already_published", []):
                for st in ("queued", "approved", "pending_approval"):
                    for p in self.db.posts(key, st):
                        if frag in p["text"]:
                            self.db.set_post(p["id"], status="published", error="уже был опубликован ранее")
                            n += 1
        return n

    def run(self, collect_every_min=60):
        self.reconcile()
        chans = list(self.channels().items())
        random.shuffle(chans)   # чтобы при лимитах модели не голодал всегда последний канал
        for key, c in chans:
            try:
                last = self.db.get(f"{key}:last_collect")
                if not last or datetime.now(timezone.utc) - datetime.fromisoformat(last) > timedelta(minutes=collect_every_min):
                    self.collect(key, c)
                    self.db.put(f"{key}:last_collect", datetime.now(timezone.utc).isoformat())
                elif "trending" in c.get("mix", {}):   # хайп проверяем каждый запуск — он быстро меняется
                    sources.collect_trending(self.db, key, c)
                self.approvals(key, c)
                self.generate(key, c)
                self.publish(key, c)
                self.publish_quiz(key, c)
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
