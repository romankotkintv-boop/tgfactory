"""Генерация текста через Claude API (Messages API). Без ключа — режим-заглушка для тестов."""
import json
import logging
import os
import re
import time

import httpx

log = logging.getLogger("llm")
API_URL = "https://api.anthropic.com/v1/messages"


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
RETRY_STATUSES = {429, 500, 502, 503, 504, 529}


class LLMUnavailable(Exception):
    """Модель перегружена / исчерпан лимит. Сырьё не тратим, пробуем в следующий запуск."""


def _wait_seconds(r, attempt):
    try:
        return min(float(r.headers.get("retry-after", "")), 30)
    except ValueError:
        return 8 * (attempt + 1)


class LLM:
    """provider: anthropic (платно) | gemini (есть бесплатный тариф, лимиты — в Google AI Studio)."""

    def __init__(self, api_key: str | None, model: str, cheap_model: str | None = None,
                 provider: str = "anthropic"):
        self.api_key = api_key
        self.model = model
        self.cheap_model = cheap_model or model
        self.provider = provider

    @property
    def mock(self):
        return not self.api_key

    def complete(self, system: str, user: str, max_tokens=1200, cheap=False) -> str:
        if self.mock:
            body = ("Заглушка: здесь будет текст от Claude API. " * 4).strip()
            return json.dumps({"text": f"<b>[ЧЕРНОВИК БЕЗ API]</b>\n{user[:300]}\n\n{body}",
                               "image_prompt": "test"}, ensure_ascii=False)
        if self.provider == "gemini":
            return self._gemini(system, user, max_tokens)
        r = httpx.post(
            API_URL,
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": self.cheap_model if cheap else self.model, "max_tokens": max_tokens,
                  "system": system, "messages": [{"role": "user", "content": user}]},
            timeout=120,
        )
        if r.status_code in RETRY_STATUSES:
            raise LLMUnavailable(f"Claude API: HTTP {r.status_code}")
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"])

    def _gemini(self, system, user, max_tokens, sleep=time.sleep):
        """GEMINI_MODEL может быть списком через запятую: при перегрузке берём следующую модель."""
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"maxOutputTokens": max_tokens * 2,
                                     "responseMimeType": "application/json"}}
        last = None
        for model in [m.strip() for m in self.model.split(",") if m.strip()]:
            for attempt in range(2):
                r = httpx.post(GEMINI_URL.format(model=model), params={"key": self.api_key},
                               json=body, timeout=120)
                if r.status_code in RETRY_STATUSES:
                    last = f"{model}: HTTP {r.status_code}"
                    log.warning("Gemini %s, жду и пробую ещё раз", last)
                    if attempt == 0:
                        sleep(_wait_seconds(r, attempt))
                    continue
                r.raise_for_status()
                return gemini_text(r.json())
        raise LLMUnavailable(last or "Gemini недоступна")


def gemini_text(resp: dict) -> str:
    cands = resp.get("candidates") or []
    if not cands:
        raise ValueError(f"Gemini: пустой ответ {str(resp)[:200]}")
    return "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []))


def parse_json(raw: str) -> dict:
    """Достаёт JSON даже если модель обернула его в ```json ... ```."""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError(f"LLM вернула не JSON: {raw[:200]}")
    data = json.loads(m.group(0))
    if not data.get("text"):
        raise ValueError("В ответе LLM нет поля text")
    return data


def build_user_prompt(kind: str, item, cfg: dict) -> str:
    payload = json.loads(item["payload"] or "{}")
    if kind == "rss_digest":
        return (f"Тип поста: rss_digest.\nЗаголовок источника: {item['title']}\n"
                f"URL: {item['url']}\nКраткое содержание источника:\n{payload.get('summary','')}\n\n"
                "Напиши пост строго по фактам источника.")
    if kind == "evergreen":
        return f"Тип поста: evergreen.\nТема: {item['title']}\nНапиши пост."
    if kind == "shopee_offers":
        price = float(payload.get("priceMin") or 0)
        disc = float(payload.get("priceDiscountRate") or 0)
        old = round(price / (1 - disc / 100), 2) if 0 < disc < 100 else None
        facts = {
            "produto": payload.get("productName"),
            "preco_atual_brl": price,
            "preco_antigo_brl_calculado": old,
            "desconto_pct": disc or None,
            "nota": payload.get("ratingStar"),
            "vendidos": payload.get("sales"),
            "loja": payload.get("shopName"),
        }
        return "Dados do produto (use só isto):\n" + json.dumps(facts, ensure_ascii=False, indent=1)
    raise ValueError(f"неизвестный тип {kind}")


def is_rejected_by_rules(text: str, cfg: dict) -> str | None:
    """Автопроверка перед очередью. Возвращает причину отказа или None."""
    stop = ["как языковая модель", "as an ai", "sun'iy intellekt sifatida", "como uma ia",
            "[черновик без api]"]
    low = text.lower()
    for s in stop:
        if s in low and not os.getenv("ALLOW_MOCK_POSTS"):
            return f"стоп-фраза: {s}"
    if len(re.sub(r"<[^>]+>", "", text)) < 120:
        return "слишком короткий текст"
    return None
