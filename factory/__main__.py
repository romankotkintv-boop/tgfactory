"""CLI: python -m factory run | collect | generate | publish-now <канал> | status"""
import argparse
import logging
import os
import sys

from dotenv import load_dotenv

from .core import Factory


def main(argv=None):
    load_dotenv()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="factory")
    ap.add_argument("cmd", choices=["run", "collect", "generate", "status", "publish-now", "import-seed", "recover-items", "logos"])
    ap.add_argument("channel", nargs="?")
    a = ap.parse_args(argv)
    f = Factory()
    chans = f.channels()
    if a.channel:
        chans = {a.channel: chans[a.channel]}
    if a.cmd == "import-seed":
        print("загружено стартовых постов:", f.import_seed())
    elif a.cmd == "recover-items":  # после сбоев модели: вернуть сырьё без постов в очередь
        print("возвращено в очередь:", f.db.recover_orphan_items(a.channel))
    elif a.cmd == "logos":  # варианты логотипов каналов → папка logos/ (коммитится в репозиторий)
        from . import images
        os.makedirs("logos", exist_ok=True)
        for k, c in f.cfg.items():
            if a.channel and k != a.channel:
                continue
            for i, prompt in enumerate(c.get("logo_prompts", []), 1):
                path, err = images.generate_gemini(prompt, f.env.get("GEMINI_API_KEY"), "logos",
                                                   f.env.get("GEMINI_IMAGE_MODEL") or "gemini-3.1-flash-lite-image", "1:1")
                if path:
                    dst = os.path.join("logos", f"{k}_{i}{os.path.splitext(path)[1]}")
                    os.replace(path, dst)
                    print(k, "логотип:", dst)
                else:
                    print(k, "логотип не создан:", err)
    elif a.cmd == "run":
        f.run()
    elif a.cmd == "collect":
        for k, c in chans.items():
            print(k, "новых:", f.collect(k, c))
    elif a.cmd == "generate":
        for k, c in chans.items():
            print(k, "создано постов:", f.generate(k, c))
    elif a.cmd == "publish-now":  # ручная проверка: публикует следующий готовый пост вне расписания
        f.reconcile()
        for k, c in chans.items():
            f.approvals(k, c)  # сначала забираем нажатые кнопки ✅/❌
        for k, c in chans.items():
            ready = f.ready_posts(k, c)
            if not ready:
                print(k, "нет готовых постов")
                continue
            p = ready[0]
            try:
                image_path, image_url = f.ensure_image(k, c, p)
                msg = f.bot(c).send_post(f.chat(c), p["text"], image_path, image_url)
                f.db.set_post(p["id"], status="published", tg_message_id=msg.get("message_id"))
                print(k, "опубликован пост", p["id"])
            except Exception as e:  # noqa: BLE001  — не повторяем: пост мог дойти, дубль хуже пропуска
                f.db.set_post(p["id"], status="failed", error=str(e)[:500])
                f.bot(c).notify(f.admin, f"❌ {c['title']}: ошибка публикации поста {p['id']}: {e}")
                print(k, "ошибка публикации", p["id"], e)
    elif a.cmd == "status":
        for k, counts in f.status():
            print(k, counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
