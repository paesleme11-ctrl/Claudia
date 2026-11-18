"""Agente de promoções da Shopee (Afiliados) -> Telegram.
Custo: R$0 (API oficial de afiliados + GitHub Actions + bot do Telegram).
Uso local:  python agent.py --dry-run   (só mostra, não envia nem grava histórico)
"""
import argparse, hashlib, html, json, math, os, time
from datetime import datetime, timezone
from pathlib import Path

import requests

ENDPOINT = "https://open-api.affiliate.shopee.com.br/graphql"
BASE = Path(__file__).parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
STATE_FILE = BASE / "postados.json"

# Se a Shopee mudar o schema e der erro de campo, ajuste esta lista.
FIELDS = ("itemId productName commissionRate price sales ratingStar "
          "priceDiscountRate imageUrl shopName productLink offerLink")


def gql(query: str) -> dict:
    app_id, secret = os.environ["SHOPEE_APP_ID"], os.environ["SHOPEE_SECRET"]
    payload = json.dumps({"query": query}, separators=(",", ":"))
    ts = str(int(time.time()))
    sig = hashlib.sha256((app_id + ts + payload + secret).encode()).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"SHA256 Credential={app_id}, Timestamp={ts}, Signature={sig}",
    }
    r = requests.post(ENDPOINT, data=payload.encode(), headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(f"Erro da API Shopee: {data['errors']}")
    return data["data"]


def num(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def search(keyword: str) -> list[dict]:
    q = (f'{{ productOfferV2(keyword: {json.dumps(keyword)}, sortType: {CFG["sort_type"]}, '
         f'page: 1, limit: {CFG["limit_per_keyword"]}) {{ nodes {{ {FIELDS} }} }} }}')
    return gql(q)["productOfferV2"]["nodes"] or []


def short_link(url: str) -> str | None:
    sub = json.dumps(CFG.get("sub_ids", ["agente"]))
    q = (f'mutation {{ generateShortLink(input: {{originUrl: {json.dumps(url)}, '
         f'subIds: {sub}}}) {{ shortLink }} }}')
    try:
        return gql(q)["generateShortLink"]["shortLink"]
    except Exception as e:  # cai para o offerLink
        print("  (short link falhou, usando offerLink):", e)
        return None


def passes(p: dict) -> bool:
    f = CFG["filters"]
    return (num(p.get("priceDiscountRate")) >= f["min_discount"]
            and num(p.get("ratingStar")) >= f["min_rating"]
            and num(p.get("sales")) >= f["min_sales"]
            and num(p.get("commissionRate")) >= f["min_commission_rate"]
            and 0 < num(p.get("price")) <= f["max_price"])


def score(p: dict) -> float:
    return ((1 + num(p["priceDiscountRate"]) / 100)
            * (num(p["commissionRate"]) * 100)
            * math.log10(num(p["sales"]) + 10)
            * (num(p["ratingStar"]) / 5))


def brl(v: float) -> str:
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def mil(n: float) -> str:
    return f"{n/1000:.1f} mil".replace(".", ",") if n >= 1000 else str(int(n))


def build_message(p: dict, link: str) -> str:
    price, d = num(p["price"]), num(p["priceDiscountRate"])
    name = html.escape(p["productName"][:90])
    linha_preco = f"💸 <b>{brl(price)}</b>"
    if d > 0:
        linha_preco = f"💸 De <s>{brl(price / (1 - d / 100))}</s> por <b>{brl(price)}</b> (-{int(d)}%)"
    return (f"🔥 <b>{name}</b>\n\n{linha_preco}\n"
            f"⭐ {num(p['ratingStar']):.1f}  |  🛒 {mil(num(p['sales']))} vendidos\n\n"
            f"👉 {link}")


def send(p: dict, text: str) -> None:
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    api = f"https://api.telegram.org/bot{token}"
    if p.get("imageUrl"):
        r = requests.post(f"{api}/sendPhoto", timeout=30, data={
            "chat_id": chat, "photo": p["imageUrl"], "caption": text, "parse_mode": "HTML"})
        if r.ok:
            return
    r = requests.post(f"{api}/sendMessage", timeout=30, data={
        "chat_id": chat, "text": text, "parse_mode": "HTML"})
    r.raise_for_status()


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    dry = ap.parse_args().dry_run

    now = time.time()
    state = {k: v for k, v in load_state().items() if now - v < 30 * 86400}
    cooldown = CFG["repeat_after_days"] * 86400

    found: dict[str, dict] = {}
    for kw in CFG["keywords"]:
        try:
            for p in search(kw):
                found[str(p["itemId"])] = p
        except Exception as e:
            print(f"Falha na busca '{kw}': {e}")
        time.sleep(1)  # respeita o limite de requisições

    ok = [p for k, p in found.items() if passes(p) and now - state.get(k, 0) > cooldown]
    ok.sort(key=score, reverse=True)
    print(f"{len(found)} produtos encontrados, {len(ok)} aprovados nos filtros.")

    for p in ok[: CFG["top_n"]]:
        link = short_link(p["offerLink"] or p["productLink"]) or p["offerLink"] or p["productLink"]
        text = build_message(p, link)
        if dry or not os.environ.get("TELEGRAM_TOKEN"):
            print("\n" + "-" * 40 + "\n" + text)
        else:
            send(p, text)
            time.sleep(2)
        if not dry:
            state[str(p["itemId"])] = now

    if not dry:
        STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
