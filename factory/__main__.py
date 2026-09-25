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
    ap.add_argument("cmd", choices=["run", "collect", "generate", "status", "publish-now", "import-seed"])
    ap.add_argument("channel", nargs="?")
    a = ap.parse_args(argv)
    f = Factory()
    chans = f.channels()
    if a.channel:
        chans = {a.channel: chans[a.channel]}
    if a.cmd == "import-seed":
        print("загружено стартовых постов:", f.import_seed())
    elif a.cmd == "run":
        f.run()
    elif a.cmd == "collect":
        for k, c in chans.items():
            print(k, "новых:", f.collect(k, c))
    elif a.cmd == "generate":
        for k, c in chans.items():
            print(k, "создано постов:", f.generate(k, c))
    elif a.cmd == "publish-now":  # ручная проверка: публикует следующий готовый пост вне расписания
        for k, c in chans.items():
            f.approvals(k, c)  # сначала забираем нажатые кнопки ✅/❌
        for k, c in chans.items():
            ready = f.db.posts(k, "approved") + f.db.posts(k, "queued")
            if not ready:
                print(k, "нет готовых постов")
                continue
            p = ready[0]
            msg = f.bot(c).send_post(f.chat(c), p["text"], p["image_path"], p["image_url"])
            f.db.set_post(p["id"], status="published", tg_message_id=msg.get("message_id"))
            print(k, "опубликован пост", p["id"])
    elif a.cmd == "status":
        for k, counts in f.status():
            print(k, counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
