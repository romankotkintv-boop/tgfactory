"""Источники: RSS-ленты, вечнозелёные темы, офферы Shopee Affiliate Open API."""
import hashlib
import json
import logging
import time

import feedparser
import httpx

log = logging.getLogger("sources")
UA = {"User-Agent": "Mozilla/5.0 (compatible; tgfactory/1.0)"}


# ---------- RSS ----------
def matches(text: str, keywords) -> bool:
    t = (text or "").lower()
    return any(k.lower() in t for k in keywords) if keywords else True


def fetch_rss(url: str, keywords=None, timeout=20):
    """Возвращает список {uid,title,url,summary}. Ошибка одной ленты не валит остальные."""
    try:
        r = httpx.get(url, headers=UA, timeout=timeout, follow_redirects=True)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception as e:  # noqa: BLE001
        log.warning("RSS %s: %s", url, e)
        return []
    out = []
    for e in feed.entries[:40]:
        title = e.get("title", "").strip()
        link = e.get("link", "").strip()
        summary = e.get("summary", "")
        if not link or not matches(f"{title} {summary}", keywords):
            continue
        out.append({"uid": link, "title": title, "url": link, "summary": summary[:1500]})
    return out


def collect_rss(db, channel_key, cfg):
    new = 0
    for url in cfg.get("rss", []):
        for it in fetch_rss(url, cfg.get("keywords")):
            payload = json.dumps({"summary": it["summary"], "feed": url}, ensure_ascii=False)
            new += db.add_item(channel_key, "rss_digest", it["uid"], it["title"], it["url"], payload)
    log.info("%s: RSS новых материалов: %s", channel_key, new)
    return new


# ---------- Вечнозелёные темы ----------
def collect_evergreen(db, channel_key, cfg):
    """Темы крутятся по кругу: когда все использованы — добавляются заново с новым циклом."""
    topics = cfg.get("evergreen_topics", [])
    if not topics:
        return 0
    cycle = int(db.get(f"{channel_key}:evergreen_cycle", 0))
    if db.count_items(channel_key, "evergreen", used=False) == 0 and db.count_items(channel_key, "evergreen") > 0:
        cycle += 1
        db.put(f"{channel_key}:evergreen_cycle", cycle)
    new = 0
    for t in topics:
        uid = f"c{cycle}:" + hashlib.sha1(t.encode()).hexdigest()[:12]
        new += db.add_item(channel_key, "evergreen", uid, t, "", json.dumps({"cycle": cycle}))
    return new


# ---------- Shopee Affiliate Open API ----------
# Эндпоинт и схема подписи: open-api.affiliate.shopee.com.br, SHA256(AppId+Timestamp+Payload+Secret)
# (по неофициальному SDK github.com/gregojoao/shopee-affiliate). Имена полей запроса
# ПРОВЕРИТЬ в официальном explorer: https://open-api.affiliate.shopee.com.br/explorer/v2
SHOPEE_URL = "https://open-api.affiliate.shopee.com.br/graphql"
SHOPEE_QUERY = """
query($keyword:String,$page:Int,$limit:Int){
  productOfferV2(keyword:$keyword, page:$page, limit:$limit, sortType:2){
    nodes{ itemId productName priceMin priceMax priceDiscountRate imageUrl
           offerLink commissionRate ratingStar sales shopName }
  }
}"""


def shopee_headers(app_id: str, secret: str, payload: str, ts: int | None = None) -> dict:
    ts = ts or int(time.time())
    sig = hashlib.sha256(f"{app_id}{ts}{payload}{secret}".encode()).hexdigest()
    return {
        "Content-Type": "application/json",
        "Authorization": f"SHA256 Credential={app_id}, Timestamp={ts}, Signature={sig}",
    }


def shopee_search(app_id, secret, keyword, limit=20):
    body = json.dumps({"query": SHOPEE_QUERY, "variables": {"keyword": keyword, "page": 1, "limit": limit}})
    r = httpx.post(SHOPEE_URL, content=body, headers=shopee_headers(app_id, secret, body), timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(f"Shopee API: {data['errors']}")
    return data["data"]["productOfferV2"]["nodes"]


def offer_passes(db, offer: dict, rules: dict) -> tuple[bool, str]:
    """Фильтр качества + защита от фейковой скидки по собственной истории цен."""
    price = float(offer.get("priceMin") or 0)
    disc = float(offer.get("priceDiscountRate") or 0)
    if price <= 0:
        return False, "нет цены"
    if price > rules.get("max_price_brl", 1e9):
        return False, "дорого"
    if disc < rules.get("min_discount_pct", 0):
        return False, "маленькая скидка"
    if float(offer.get("ratingStar") or 0) < rules.get("min_rating", 0):
        return False, "низкий рейтинг"
    if int(offer.get("sales") or 0) < rules.get("min_sales", 0):
        return False, "мало продаж"
    median, n = db.price_median(str(offer["itemId"]), rules.get("history_days", 30))
    if n >= 7:  # история есть — проверяем реальное снижение
        if price > median * (1 - rules.get("min_real_drop", 0.1)):
            return False, f"скидка не подтверждается историей (медиана {median:.2f})"
    elif rules.get("require_history"):
        return False, "нет истории цен"
    return True, "ok"


def collect_shopee(db, channel_key, cfg, app_id, secret):
    rules = cfg.get("shopee", {})
    if not (app_id and secret):
        log.warning("%s: нет SHOPEE_APP_ID/SHOPEE_SECRET — пропускаю сбор офферов", channel_key)
        return 0
    new = 0
    for kw in rules.get("keywords", []):
        try:
            offers = shopee_search(app_id, secret, kw)
        except Exception as e:  # noqa: BLE001
            log.warning("Shopee '%s': %s", kw, e)
            continue
        for o in offers:
            uid = str(o["itemId"])
            ok, why = offer_passes(db, o, rules)
            db.record_price(uid, float(o.get("priceMin") or 0))  # копим историю для всех
            if ok:
                new += db.add_item(channel_key, "shopee_offers", uid, o.get("productName", ""),
                                   o.get("offerLink", ""), json.dumps(o, ensure_ascii=False))
            else:
                log.debug("skip %s: %s", uid, why)
    log.info("%s: Shopee новых офферов: %s", channel_key, new)
    return new
