import json
import os
from datetime import datetime, timezone

import pytest

from factory import llm, sources
from factory.core import Factory
from factory.db import DB
from factory.telegram import visible_len

ROOT = os.path.dirname(os.path.dirname(__file__))

RSS_XML = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Малый бизнес и маркетплейсы: новые правила рекламы</title><link>https://ex.com/a</link>
<description>Маркетплейс меняет правила продвижения для малого бизнеса.</description></item>
<item><title>Курс биткоина</title><link>https://ex.com/b</link><description>крипта</description></item>
</channel></rss>""".encode()


@pytest.fixture
def fac(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    env = {"DB_PATH": str(tmp_path / "t.db"), "DRY_RUN": "1", "ALLOW_MOCK_POSTS": "1"}
    monkeypatch.setenv("ALLOW_MOCK_POSTS", "1")
    f = Factory(env=env)
    for c in f.cfg.values():
        c["_outbox"] = str(tmp_path)
    return f


def test_rss_keyword_filter(monkeypatch):
    class R:
        content = RSS_XML
        def raise_for_status(self): pass
    monkeypatch.setattr(sources.httpx, "get", lambda *a, **k: R())
    items = sources.fetch_rss("https://ex.com/rss", ["маркетплейс", "малый бизнес"])
    assert [i["url"] for i in items] == ["https://ex.com/a"]


def test_dedup(tmp_path):
    db = DB(str(tmp_path / "d.db"))
    assert db.add_item("c", "rss_digest", "u1") is True
    assert db.add_item("c", "rss_digest", "u1") is False


def test_fake_discount_blocked_by_history(tmp_path):
    db = DB(str(tmp_path / "d.db"))
    for d in range(1, 11):
        db.record_price("42", 100.0, day=f"2099-01-{d:02d}")  # в будущем, чтобы попасть в окно
    db.conn.execute("UPDATE prices SET day=date('now','-' || (rowid) || ' day')")
    rules = {"min_discount_pct": 20, "min_rating": 4.5, "min_sales": 10, "max_price_brl": 300,
             "history_days": 30, "min_real_drop": 0.10}
    offer = {"itemId": 42, "priceMin": 98, "priceDiscountRate": 40, "ratingStar": 4.8, "sales": 100}
    ok, why = sources.offer_passes(db, offer, rules)
    assert not ok and "историей" in why          # «скидка 40%», но цена почти как обычно
    offer["priceMin"] = 80
    assert sources.offer_passes(db, offer, rules)[0]


def test_offer_filters(tmp_path):
    db = DB(str(tmp_path / "d.db"))
    rules = {"min_discount_pct": 20, "min_rating": 4.5, "min_sales": 50, "max_price_brl": 300}
    base = {"itemId": 1, "priceMin": 50, "priceDiscountRate": 30, "ratingStar": 4.9, "sales": 500}
    assert sources.offer_passes(db, base, rules)[0]
    assert not sources.offer_passes(db, {**base, "ratingStar": 4.0}, rules)[0]
    assert not sources.offer_passes(db, {**base, "priceMin": 500}, rules)[0]


def test_shopee_signature_is_deterministic():
    h = sources.shopee_headers("app", "sec", '{"q":1}', ts=1700000000)
    assert "Credential=app" in h["Authorization"] and "Timestamp=1700000000" in h["Authorization"]
    assert h == sources.shopee_headers("app", "sec", '{"q":1}', ts=1700000000)


def test_parse_json_with_fence():
    assert llm.parse_json('```json\n{"text": "hi", "image_prompt": ""}\n```')["text"] == "hi"


def test_visible_len_ignores_tags():
    assert visible_len('<b>ab</b> <a href="x">c</a>') == 4


def test_pipeline_evergreen_approval_and_publish(fac, tmp_path, monkeypatch):
    """UZ: тема → пост на одобрение → одобрен → публикация в слот."""
    key, c = "uz_ielts", fac.cfg["uz_ielts"]
    monkeypatch.setattr(fac.llm, "complete", lambda s, u, **k: json.dumps(
        {"text": "<b>Kun so'zi</b>\n" + "Resilient — chidamli. Example: She is resilient. " * 4, "image_prompt": ""}))
    assert fac.collect(key, c) == len(c["evergreen_topics"])
    assert fac.generate(key, c) == 3                       # лимит на запуск
    pend = fac.db.posts(key, "pending_approval")
    assert len(pend) == 3
    fac.db.set_post(pend[0]["id"], status="approved")      # имитация кнопки ✅
    now = datetime(2026, 9, 26, 3, 35, tzinfo=timezone.utc)  # 08:35 в Ташкенте → слот 08:30
    assert fac.publish(key, c, now) == pend[0]["id"]
    assert fac.publish(key, c, now) is None                # слот уже использован


def test_pipeline_shopee_post_has_link_and_publi(fac, monkeypatch):
    key, c = "br_deals", fac.cfg["br_deals"]
    offer = {"itemId": 7, "productName": "Organizador de gaveta", "priceMin": 39.9, "priceDiscountRate": 35,
             "imageUrl": "https://img/x.jpg", "offerLink": "https://s.shopee.com.br/abc", "ratingStar": 4.8,
             "sales": 1200, "shopName": "Loja"}
    fac.db.add_item(key, "shopee_offers", "7", offer["productName"], offer["offerLink"], json.dumps(offer))
    captured = {}
    def fake(system, user, **k):
        captured["user"] = user
        return json.dumps({"text": "🏠 <b>Organizador de gaveta</b>\nDe R$ 61,38 por R$ 39,90 (−35%)\n⭐ 4,8 · 1,2 mil vendidos. Deixa a gaveta em ordem.", "image_prompt": ""})
    monkeypatch.setattr(fac.llm, "complete", fake)
    assert fac.generate(key, c) == 1
    p = fac.db.posts(key, "queued")[0]
    assert "https://s.shopee.com.br/abc" in p["text"] and "#publi" in p["text"]
    assert p["image_url"] == "https://img/x.jpg"
    assert '"preco_antigo_brl_calculado": 61.38' in captured["user"]


def test_rss_post_gets_source_link(fac, monkeypatch):
    key, c = "ru_marketing", fac.cfg["ru_marketing"]
    c["mix"] = {"rss_digest": 1.0}
    c["image"] = {"provider": "none"}
    fac.db.add_item(key, "rss_digest", "https://ex.com/a", "Новость", "https://ex.com/a", json.dumps({"summary": "s"}))
    monkeypatch.setattr(fac.llm, "complete", lambda s, u, **k: json.dumps(
        {"text": "<b>Новость</b>\n" + "Факт из источника для малого бизнеса. " * 5, "image_prompt": ""}))
    fac.generate(key, c)
    assert '<a href="https://ex.com/a">Источник</a>' in fac.db.posts(key, "queued")[0]["text"]


def test_short_text_rejected():
    assert llm.is_rejected_by_rules("<b>кратко</b>", {}) == "слишком короткий текст"


def test_approval_timeout(fac):
    key, c = "uz_ielts", fac.cfg["uz_ielts"]
    pid = fac.db.add_post(key, None, "x" * 200, "pending_approval")
    fac.db.set_post(pid, created_at="2000-01-01T00:00:00+00:00")
    fac.approvals(key, c)
    assert fac.db.get_post(pid)["status"] == "rejected"


def test_gemini_text_parsing():
    resp = {"candidates": [{"content": {"parts": [{"text": '{"text": "ok"'}, {"text": ', "image_prompt": ""}'}]}}]}
    assert llm.parse_json(llm.gemini_text(resp))["text"] == "ok"
    with pytest.raises(ValueError):
        llm.gemini_text({"candidates": []})


def test_import_seed_once(fac):
    assert fac.import_seed("seed") == 0              # в dry-run не грузим
    fac.dry = False
    fac.bot = lambda c: type("B", (), {"ask_approval": lambda *a, **k: {"message_id": 1}})()
    n = fac.import_seed("seed")
    assert n >= 40                                   # 15 RU + 15 UZ + 12 BR
    assert fac.import_seed("seed") == 0              # повторно не дублирует
    assert len(fac.db.posts("uz_ielts", "pending_approval")) == 15   # УЗ — через одобрение
    assert len(fac.db.posts("ru_marketing", "queued")) == 15


def test_seed_files_valid():
    import re
    for name in ("ru_marketing", "uz_ielts", "br_deals"):
        posts = json.load(open(os.path.join(ROOT, "seed", f"{name}.json"), encoding="utf-8"))
        for p in posts:
            assert visible_len(p["text"]) <= 1024 or name != "br_deals"
            assert set(re.findall(r"</?(\w+)", p["text"])) <= {"b", "i"}
