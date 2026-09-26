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
                    aspect: str = "4:3", ref_path: str | None = None) -> tuple[str | None, str | None]:
    """Возвращает (путь к картинке, None) или (None, текст ошибки). Ошибка не ломает публикацию.
    ref_path — картинка-образец (маскот): модель рисует того же персонажа."""
    if not api_key or not prompt:
        return None, "нет ключа или промпта"
    parts_in = []
    if ref_path and os.path.exists(ref_path):
        with open(ref_path, "rb") as f:
            mime = "image/png" if ref_path.lower().endswith(".png") else "image/jpeg"
            parts_in.append({"inlineData": {"mimeType": mime, "data": base64.b64encode(f.read()).decode()}})
    parts_in.append({"text": prompt})
    try:
        r = httpx.post(
            GEMINI_IMAGE_URL.format(model=model), params={"key": api_key},
            json={"contents": [{"role": "user", "parts": parts_in}],
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


def letterbox_bounds(path: str):
    """Модель иногда рисует вертикальный постер по центру и заливает бока (размытием или однотонно).
    Ищем такие полосы: резкий вертикальный шов слева и справа + почти пустые края.
    Возвращает (x_left, x_right) границ полезной картинки или None."""
    try:
        import numpy as np
        from PIL import Image
        a = np.asarray(Image.open(path).convert("L"), dtype=float)
    except Exception:  # noqa: BLE001
        return None
    h, w = a.shape
    e = np.abs(np.diff(a, axis=1))
    gx = e.mean(axis=0)
    med = float(np.median(gx)) + 1e-6
    lo, hi = int(w * .12), int(w * .42)
    lo2, hi2 = int(w * .58), int(w * .88)
    xl = lo + int(gx[lo:hi].argmax())
    xr = lo2 + int(gx[lo2:hi2].argmax())
    seam = min(gx[xl], gx[xr]) / med
    side = (e[:, :int(w * .2)].mean() + e[:, int(w * .8):].mean()) / 2
    ctr = e[:, int(w * .3):int(w * .7)].mean() + 1e-6
    symmetric = abs(xl - (w - xr)) < w * .05   # постер ровно по центру
    if seam > 10 and side / ctr < 0.5 and symmetric:
        return xl + 2, xr - 1
    return None


def crop_to(path: str, x_left: int, x_right: int) -> str:
    from PIL import Image
    im = Image.open(path)
    im.crop((x_left, 0, x_right, im.size[1])).save(path)
    return path


CRITIC_PROMPT = """You are a strict art director of a popular Telegram channel. Rate this post image for how
eye-catching and NON-boring it is in a fast-scrolling feed (1 = dull stock/cliche, 10 = stops the scroll).
Penalize: generic stock scenes (people at laptops/desks, charts, handshakes, meetings), cliche icons (lightbulbs,
rockets, gears, targets, puzzle pieces, arrows), empty or flat composition, muddy colors, blurred/letterboxed sides,
anything unpleasant or creepy, garbled text. Reward: one bold unexpected idea, strong emotion or action, dramatic
light, rich contrast, clear link to the post topic.
Post (for context): {post}
Answer strictly JSON: {{"score": <1-10>, "fix": "<one short instruction how to make it more striking>"}}"""


def rate_image(path: str, post: str, api_key: str, model: str) -> tuple[int | None, str]:
    """Оценка «цепляет / скучно» мультимодальной моделью. Ошибка — (None, '') и картинку не трогаем."""
    import json as _json
    import re as _re
    if not (api_key and model and path and os.path.exists(path)):
        return None, ""
    try:
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode()
        mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
        r = httpx.post(GEMINI_IMAGE_URL.format(model=model), params={"key": api_key}, timeout=90,
                       json={"contents": [{"role": "user", "parts": [
                           {"inlineData": {"mimeType": mime, "data": data}},
                           {"text": CRITIC_PROMPT.format(post=post[:600])}]}],
                             "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 2048}})
        if r.status_code != 200:
            return None, ""
        parts = (r.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        raw = "".join(p.get("text", "") for p in parts)
        m = _re.search(r"\{.*\}", raw, _re.S)
        d = _json.loads(m.group(0)) if m else {}
        return int(d.get("score")), str(d.get("fix", ""))[:300]
    except Exception:  # noqa: BLE001
        return None, ""
