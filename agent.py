"""Agente de promoções Shopee Afiliados -> Telegram.

Teste local: python agent.py --dry-run
O dry-run não envia mensagens nem grava o histórico.
"""
import argparse
import hashlib
import html
import json
import math
import os
import re
import time
from pathlib import Path

import requests

ENDPOINT = "https://open-api.affiliate.shopee.com.br/graphql"
BASE = Path(__file__).parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
STATE_FILE = BASE / "postados.json"

FIELDS = (
    "itemId productName commissionRate price sales ratingStar "
    "priceDiscountRate imageUrl shopName productLink offerLink"
)


def gql(query: str) -> dict:
    """Executa GraphQL com tentativas limitadas para erros transitórios."""
    app_id = os.environ["SHOPEE_APP_ID"]
    secret = os.environ["SHOPEE_SECRET"]
    payload = json.dumps({"query": query}, separators=(",", ":"))
    last_error = None

    for attempt in range(3):
        ts = str(int(time.time()))
        sig = hashlib.sha256((app_id + ts + payload + secret).encode()).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "Authorization": (
                f"SHA256 Credential={app_id}, Timestamp={ts}, Signature={sig}"
            ),
        }
        try:
            response = requests.post(
                ENDPOINT, data=payload.encode(), headers=headers, timeout=30
            )
            if response.status_code == 429 or response.status_code >= 500:
                response.raise_for_status()
            response.raise_for_status()
            data = response.json()
            if data.get("errors"):
                message = str(data["errors"])
                # O erro 10000/System Error pode ser transitório. Outros erros
                # GraphQL (ex.: schema/campo inválido) não são repetidos.
                if "10000" in message or "System Error" in message:
                    raise RuntimeError(f"Erro transitório da API Shopee: {message}")
                raise RuntimeError(f"Erro da API Shopee: {message}")
            return data["data"]
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            last_error = exc
            transient = (
                isinstance(exc, requests.RequestException)
                or "transitório" in str(exc).lower()
                or "system error" in str(exc).lower()
                or "10000" in str(exc)
            )
            if not transient or attempt == 2:
                break
            wait = 2 ** (attempt + 1)
            print(f"  Tentativa {attempt + 1}/3 falhou; nova tentativa em {wait}s.")
            time.sleep(wait)

    raise RuntimeError(str(last_error))


def num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def search(keyword: str) -> list[dict]:
    query = (
        f'{{ productOfferV2(keyword: {json.dumps(keyword)}, '
        f'sortType: {CFG["sort_type"]}, page: 1, '
        f'limit: {CFG["limit_per_keyword"]}) {{ nodes {{ {FIELDS} }} }} }}'
    )
    result = gql(query)
    return result["productOfferV2"]["nodes"] or []


def short_link(url: str) -> str | None:
    if not url:
        return None
    sub_ids = json.dumps(CFG.get("sub_ids", ["agente"]))
    query = (
        "mutation { generateShortLink(input: {"
        f"originUrl: {json.dumps(url)}, subIds: {sub_ids}"
        "}) { shortLink } }"
    )
    try:
        return gql(query)["generateShortLink"]["shortLink"]
    except Exception as exc:
        print(f"  Não foi possível gerar short link oficial: {exc}")
        return None


def passes(product: dict) -> bool:
    filters = CFG["filters"]
    return (
        filters["min_discount"] <= num(product.get("priceDiscountRate")) <= filters["max_discount"]
        and num(product.get("ratingStar")) >= filters["min_rating"]
        and num(product.get("sales")) >= filters["min_sales"]
        and num(product.get("commissionRate")) >= filters["min_commission_rate"]
        and 0 < num(product.get("price")) <= filters["max_price"]
        and bool(product.get("productName"))
        and bool(product.get("offerLink") or product.get("productLink"))
    )


def score(product: dict) -> float:
    """Pontuação de qualidade (0-100), com componentes limitados."""
    discount = min(max(num(product.get("priceDiscountRate")), 0), 70) / 70
    rating = min(max((num(product.get("ratingStar")) - 4.0) / 1.0, 0), 1)
    sales = min(math.log10(max(num(product.get("sales")), 0) + 1) / 5, 1)
    commission = min(max(num(product.get("commissionRate")), 0) / 0.20, 1)
    weights = CFG.get("score_weights", {
        "discount": 35, "rating": 25, "sales": 20, "commission": 20
    })
    return (
        discount * weights["discount"]
        + rating * weights["rating"]
        + sales * weights["sales"]
        + commission * weights["commission"]
    )


def category_for(product: dict) -> str:
    """Infere uma categoria ampla pelo nome; não depende de campo inexistente na API."""
    name = str(product.get("productName", "")).lower()
    categories = CFG.get("category_keywords", {})
    for category, terms in categories.items():
        if any(term.lower() in name for term in terms):
            return category
    return "outros"


def normalized_words(name: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", str(name).lower())
    return {word for word in words if len(word) > 2}


def too_similar(product: dict, selected: list[dict]) -> bool:
    words = normalized_words(product.get("productName", ""))
    if not words:
        return False
    for previous in selected:
        other = normalized_words(previous.get("productName", ""))
        union = words | other
        similarity = len(words & other) / len(union) if union else 0
        if similarity >= CFG.get("similarity_threshold", 0.72):
            return True
    return False


def choose_products(products: list[dict], limit: int) -> list[dict]:
    """Escolhe por score, evitando excesso de produtos da mesma categoria."""
    ranked = sorted(products, key=score, reverse=True)
    selected = []
    category_counts = {}
    max_per_category = CFG.get("max_per_category", 2)

    for product in ranked:
        category = category_for(product)
        if category_counts.get(category, 0) >= max_per_category:
            continue
        if too_similar(product, selected):
            continue
        selected.append(product)
        category_counts[category] = category_counts.get(category, 0) + 1
        if len(selected) >= limit:
            break

    # Se a diversidade impedir completar a quantidade, aproveita os próximos
    # melhores sem repetir nomes muito semelhantes.
    if len(selected) < limit:
        selected_ids = {str(p.get("itemId")) for p in selected}
        for product in ranked:
            if str(product.get("itemId")) in selected_ids:
                continue
            if too_similar(product, selected):
                continue
            selected.append(product)
            selected_ids.add(str(product.get("itemId")))
            if len(selected) >= limit:
                break
    return selected


def brl(value: float) -> str:
    return "R$ " + f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def mil(value: float) -> str:
    return f"{value/1000:.1f} mil".replace(".", ",") if value >= 1000 else str(int(value))


def headline_for(product: dict) -> str:
    """Cria uma chamada curta baseada no produto, evitando um título único."""
    name = str(product.get("productName", "")).lower()
    category = category_for(product)

    specific = [
        (("protetor solar", "protetor facial"), [
            "☀️ Proteção para a pele com preço de achadinho!",
            "🧴 Um aliado para sua rotina de skincare!",
            "✨ Achado de cuidado facial para aproveitar!"
        ]),
        (("sérum", "serum"), [
            "💧 Um cuidado especial para sua pele!",
            "✨ Sérum queridinho para conhecer!",
            "🧖‍♀️ Seu próximo passo no skincare pode estar aqui!"
        ]),
        (("base líquida", "base matte", "base"), [
            "💄 Uma base para deixar na nécessaire!",
            "✨ Achadinho de make que vale conferir!",
            "💋 Mais um item para renovar sua maquiagem!"
        ]),
        (("batom", "lip tint", "gloss"), [
            "💋 Um toque de cor para sua make!",
            "💄 Achadinho perfeito para a nécessaire!",
            "✨ Um mimo para quem ama maquiagem!"
        ]),
        (("shampoo", "condicionador", "máscara capilar"), [
            "🫧 Um cuidado a mais para seus cabelos!",
            "💆‍♀️ Achado para sua rotina capilar!",
            "✨ Seu cabelo merece esse carinho!"
        ]),
        (("escova alisadora", "chapinha", "secador"), [
            "💇‍♀️ Mais praticidade na hora de arrumar o cabelo!",
            "🔥 Achadinho para facilitar sua rotina!",
            "✨ Um aliado para seus dias de beleza!"
        ]),
        (("hidratante corporal", "creme para mãos"), [
            "🧴 Um carinho extra para sua rotina de autocuidado!",
            "💗 Achado para cuidar da pele sem complicação!",
            "✨ Seu momento de autocuidado começa aqui!"
        ]),
        (("esfoliante",), [
            "🛁 Um cuidado especial para sua rotina!",
            "💗 Achadinho para seu momento de autocuidado!",
            "✨ Mais um mimo para cuidar de você!"
        ]),
    ]

    templates = None
    for terms, options in specific:
        if any(term in name for term in terms):
            templates = options
            break

    if templates is None:
        by_category = {
            "skincare": [
                "✨ Um achado para sua rotina de cuidados com a pele!",
                "💗 Mais um mimo para seu skincare!",
                "🧴 Cuidado com a pele com preço para aproveitar!"
            ],
            "maquiagem": [
                "💄 Achadinho para quem ama maquiagem!",
                "✨ Um item novo para sua nécessaire!",
                "💋 Sua próxima descoberta de make!"
            ],
            "cabelo": [
                "💇‍♀️ Um achado para cuidar dos cabelos!",
                "✨ Mais praticidade na sua rotina capilar!",
                "🫧 Um mimo para seus cuidados com o cabelo!"
            ],
            "corpo": [
                "🛁 Um achado para seu autocuidado!",
                "💗 Um carinho extra na sua rotina!",
                "✨ Cuide de você e aproveite esse achadinho!"
            ],
        }
        templates = by_category.get(category, [
            "💗 Olha esse achadinho que encontrei!",
            "✨ Uma oferta que vale conferir!",
            "🛍️ Mais uma descoberta para aproveitar!"
        ])

    identity = str(product.get("itemId") or product.get("productName") or "")
    index = int(hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8], 16) % len(templates)
    return templates[index]


def build_message(product: dict, link: str) -> str:
    price = num(product.get("price"))
    discount = num(product.get("priceDiscountRate"))
    name = html.escape(str(product.get("productName", "Achadinho"))[:100])
    lines = [
        f"<b>{html.escape(headline_for(product))}</b>",
        "",
        f"✨ {name}",
        "",
    ]
    if 0 < discount < 100:
        old_price = price / (1 - discount / 100)
        lines.append(
            f"💸 De <s>{brl(old_price)}</s> por <b>{brl(price)}</b>"
        )
        lines.append(f"🏷️ Desconto de: <b>{discount:.0f}% OFF</b>")
    else:
        lines.append(f"💸 <b>{brl(price)}</b>")

    rating = num(product.get("ratingStar"))
    sales = num(product.get("sales"))
    if rating:
        lines.append(f"⭐ {rating:.1f}/5")
    if sales:
        lines.append(f"🛒 {mil(sales)} vendidos")

    lines.extend([
        "",
        "Encontrei esse achadinho e vim compartilhar com você! 🥰",
        "",
        "🛍️ COMPRE AQUI:",
        html.escape(link),
    ])
    return "\n".join(lines)


def send(product: dict, message: str) -> None:
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID não configurado.")

    api = f"https://api.telegram.org/bot{token}"
    if product.get("imageUrl"):
        try:
            response = requests.post(
                f"{api}/sendPhoto",
                timeout=30,
                data={
                    "chat_id": chat_id,
                    "photo": product["imageUrl"],
                    "caption": message,
                    "parse_mode": "HTML",
                },
            )
            result = response.json()
            if response.ok and result.get("ok"):
                return
            print("  Envio com imagem falhou; tentando mensagem de texto.")
        except (requests.RequestException, ValueError):
            print("  Imagem indisponível; tentando mensagem de texto.")

    response = requests.post(
        f"{api}/sendMessage",
        timeout=30,
        data={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram recusou a mensagem: {result}")


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("Aviso: histórico inválido/indisponível; iniciando histórico vazio.")
        return {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    dry_run = parser.parse_args().dry_run

    now = time.time()
    state = {
        key: value for key, value in load_state().items()
        if isinstance(value, (int, float)) and now - value < 30 * 86400
    }
    cooldown = CFG["repeat_after_days"] * 86400

    found = {}
    successful_searches = 0
    for keyword in CFG["keywords"]:
        try:
            products = search(keyword)
            successful_searches += 1
            for product in products:
                item_id = str(product.get("itemId", ""))
                if not item_id:
                    continue
                if item_id not in found:
                    product["_matched_keywords"] = [keyword]
                    found[item_id] = product
                elif keyword not in found[item_id]["_matched_keywords"]:
                    found[item_id]["_matched_keywords"].append(keyword)
        except Exception as exc:
            print(f"Falha na busca '{keyword}': {exc}")
        time.sleep(CFG.get("request_pause_seconds", 1))

    eligible = [
        product for item_id, product in found.items()
        if passes(product) and now - state.get(item_id, 0) > cooldown
    ]
    selected = choose_products(eligible, CFG["top_n"])
    print(
        f"{len(found)} produtos encontrados; {len(eligible)} aprovados; "
        f"{len(selected)} selecionados; "
        f"{successful_searches}/{len(CFG['keywords'])} buscas concluídas."
    )

    if not selected:
        print("Nenhuma oferta elegível nesta rodada.")
    for product in selected:
        original_url = product.get("offerLink") or product.get("productLink")
        affiliate_url = short_link(original_url)
        if affiliate_url:
            link = affiliate_url
            print(f"Link curto gerado pela API de afiliados para item {product['itemId']}.")
        else:
            link = original_url
            print(
                f"Aviso: usando link original retornado pela API para item "
                f"{product['itemId']}; confira a atribuição de afiliado."
            )

        message = build_message(product, link)
        if dry_run:
            print("\n" + "-" * 48 + "\n" + message)
        else:
            send(product, message)
            state[str(product["itemId"])] = now
            time.sleep(CFG.get("send_pause_seconds", 2))

    if not dry_run:
        STATE_FILE.write_text(
            json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
