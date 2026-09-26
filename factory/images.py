"""Картинки: none | source (фото товара из источника) | gemini (Nano Banana, платно ~$0,03/шт) | openai."""
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


GEMINI_IMAGE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def generate_gemini(prompt: str, api_key: str, out_dir: str, model: str = "gemini-3.1-flash-lite-image",
                    aspect: str = "4:3") -> tuple[str | None, str | None]:
    """Возвращает (путь к картинке, None) или (None, текст ошибки). Ошибка не ломает публикацию."""
    if not api_key or not prompt:
        return None, "нет ключа или промпта"
    try:
        r = httpx.post(
            GEMINI_IMAGE_URL.format(model=model), params={"key": api_key},
            json={"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                  "generationConfig": {"responseModalities": ["IMAGE"],
                                       "imageConfig": {"aspectRatio": aspect}}},
            timeout=180,
        )
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:300]}"
        parts = (r.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        img = next((p.get("inlineData") or p.get("inline_data") for p in parts
                    if p.get("inlineData") or p.get("inline_data")), None)
        if not img:
            return None, f"в ответе нет картинки: {str(r.json())[:300]}"
    except Exception as e:  # noqa: BLE001
        return None, str(e)[:300]
    ext = ".png" if "png" in img.get("mimeType", img.get("mime_type", "png")) else ".jpg"
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{uuid.uuid4().hex}{ext}")
    with open(path, "wb") as f:
        f.write(base64.b64decode(img["data"]))
    return path, None


def add_watermark(path: str, text: str) -> str:
    """Водяной знак: адрес канала в правом нижнем углу, полупрозрачная плашка. Ошибка — картинка без знака."""
    if not path or not text:
        return path
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.open(path).convert("RGBA")
        w, h = img.size
        size = max(18, int(min(w, h) * 0.035))
        font = ImageFont.load_default(size=size)
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        x0, y0, x1, y1 = d.textbbox((0, 0), text, font=font)
        tw, th = x1 - x0, y1 - y0
        pad, margin = int(size * 0.5), int(size * 0.8)
        bx1, by1 = w - margin, h - margin
        bx0, by0 = bx1 - tw - 2 * pad, by1 - th - 2 * pad
        d.rounded_rectangle((bx0, by0, bx1, by1), radius=pad, fill=(0, 0, 0, 110))
        d.text((bx0 + pad - x0, by0 + pad - y0), text, font=font, fill=(255, 255, 255, 235))
        out = os.path.splitext(path)[0] + "_wm.png"
        Image.alpha_composite(img, layer).convert("RGB").save(out, "PNG")
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("водяной знак не поставлен: %s", e)
        return path
