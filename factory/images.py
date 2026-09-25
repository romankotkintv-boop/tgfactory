"""Картинки: none | source (фото товара из источника) | openai (генерация)."""
import base64
import logging
import os
import uuid

import httpx

log = logging.getLogger("images")


def generate_openai(prompt: str, api_key: str, out_dir: str, model: str = "gpt-image-1-mini") -> str | None:
    """Возвращает путь к PNG или None. Модель задаётся в .env (OPENAI_IMAGE_MODEL) — сверить с прайсом OpenAI."""
    if not api_key or not prompt:
        return None
    try:
        r = httpx.post(
            "https://api.openai.com/v1/images/generations",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "prompt": prompt, "size": "1024x1024", "n": 1},
            timeout=180,
        )
        r.raise_for_status()
        b64 = r.json()["data"][0]["b64_json"]
    except Exception as e:  # noqa: BLE001
        log.warning("Картинка не создана: %s", e)
        return None
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{uuid.uuid4().hex}.png")
    with open(path, "wb") as f:
        f.write(base64.b64decode(b64))
    return path
