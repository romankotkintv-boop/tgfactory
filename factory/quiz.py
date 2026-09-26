"""Регулярные викторины (Telegram quiz-опросы): модель придумывает вопрос, бот публикует sendPoll type=quiz."""
import json
import logging
import re

from . import llm as llm_mod

log = logging.getLogger(__name__)

Q_MAX, OPT_MAX, EXPL_MAX = 300, 100, 200   # лимиты Telegram

SYSTEM = """Ты придумываешь одну викторину для Telegram-канала. Ответ — строго JSON:
{"question": "...", "options": ["...", "...", "...", "..."], "correct": 0, "explanation": "...", "topic": "..."}
Правила: question ≤ 250 символов; 3–4 варианта, каждый ≤ 90 символов, только один верный; correct — индекс верного
(0-based), верный ответ ставь в СЛУЧАЙНУЮ позицию; explanation ≤ 180 символов — почему верно, коротко и полезно;
topic — 2–4 слова о теме. Без HTML, без эмодзи в вариантах. Никакой политики, религии, трагедий."""


def validate(d: dict) -> dict:
    q = str(d.get("question", "")).strip()
    opts = [str(o).strip() for o in d.get("options", []) if str(o).strip()]
    c = d.get("correct")
    ex = str(d.get("explanation", "")).strip()
    if not q or len(q) > Q_MAX:
        raise ValueError("вопрос пустой или длиннее 300")
    if not 2 <= len(opts) <= 10 or any(len(o) > OPT_MAX for o in opts) or len(set(opts)) != len(opts):
        raise ValueError("варианты: 2–10 штук, ≤100 символов, без повторов")
    if not isinstance(c, int) or not 0 <= c < len(opts):
        raise ValueError("неверный индекс правильного ответа")
    return {"question": q, "options": opts, "correct": c, "explanation": ex[:EXPL_MAX],
            "topic": str(d.get("topic", ""))[:60]}


def make(llm, qcfg: dict, recent: list[str]) -> dict:
    if llm.mock:
        return validate({"question": "Test quiz: 2+2=?", "options": ["3", "4", "5"], "correct": 1,
                         "explanation": "Проверка без API", "topic": "test"})
    user = (f"{qcfg.get('brief', '')}\n\nНедавние темы (не повторяй): {', '.join(recent[-30:]) or 'нет'}")
    raw = llm.complete(SYSTEM, user, max_tokens=600)
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError(f"модель вернула не JSON: {raw[:150]}")
    return validate(json.loads(m.group(0)))


def recent_topics(db, key) -> list[str]:
    try:
        return json.loads(db.get(f"{key}:quiz_topics", "[]"))
    except ValueError:
        return []


def remember(db, key, q: dict):
    t = recent_topics(db, key)
    t.append(q["topic"] or q["question"][:40])
    db.put(f"{key}:quiz_topics", json.dumps(t[-40:], ensure_ascii=False))


__all__ = ["make", "validate", "remember", "recent_topics", "llm_mod"]
