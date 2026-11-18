"""Claudia — agente de promoções Shopee Afiliados -> Telegram.

Teste local:  python agent.py --dry-run    (não envia nem grava histórico)
Forçar agora: python agent.py --force      (ignora horário e intervalo)

O GitHub Actions acorda a Claudia a cada 30 min. Ela só dispara se estiver
dentro da janela de horário e se já passou o intervalo desde o último disparo
(tudo configurável no config.json ou no painel.html).
"""
import argparse
import hashlib
import html
import json
import math
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ENDPOINT = "https://open-api.affiliate.shopee.com.br/graphql"
BASE = Path(__file__).parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
STATE_FILE = BASE / "postados.json"
NAME = CFG.get("name", "Claudia")

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


def reject_reason(product: dict) -> str | None:
    """Devolve o motivo de rejeição (ou None se o produto passa em todos os filtros)."""
    f = CFG["filters"]
    discount = num(product.get("priceDiscountRate"))
    if not product.get("productName"):
        return "sem nome"
    if not (product.get("offerLink") or product.get("productLink")):
        return "sem link"
    if discount < f["min_discount"]:
        return "desconto abaixo do mínimo"
    if discount > f.get("max_discount", 100):
        return "desconto acima do máximo"
    if num(product.get("ratingStar")) < f["min_rating"]:
        return "nota abaixo do mínimo"
    if num(product.get("sales")) < f["min_sales"]:
        return "poucas vendas"
    if num(product.get("commissionRate")) < f["min_commission_rate"]:
        return "comissão abaixo do mínimo"
    price = num(product.get("price"))
    if not (0 < price <= f["max_price"]):
        return "preço acima do máximo"
    return None


def passes(product: dict) -> bool:
    return reject_reason(product) is None


def active_categories() -> dict[str, list[str]]:
    """Categorias ligadas -> palavras de busca. Aceita o formato antigo (keywords)."""
    cats = CFG.get("categories")
    if cats:
        return {name: c.get("keywords", []) for name, c in cats.items()
                if c.get("enabled", True) and c.get("keywords")}
    return {"geral": CFG.get("keywords", [])}


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
    """Categoria = a da palavra de busca que achou o produto (ou inferida pelo nome no formato antigo)."""
    if product.get("_category"):
        return product["_category"]
    name = str(product.get("productName", "")).lower()
    for category, terms in CFG.get("category_keywords", {}).items():
        if any(term.lower() in name for term in terms):
            return category
    return "outros"


STOPWORDS = {
    "para", "com", "sem", "por", "uma", "uns", "kit", "original", "oferta", "promocao",
    "frete", "gratis", "novo", "nova", "pronta", "entrega", "lancamento", "unidade",
    "unidades", "mais", "top", "premium", "importado", "brasil",
}
SIMILARITY_LEVELS = {"baixo": 0.8, "medio": 0.6, "alto": 0.45}


def normalized_words(name: str) -> set[str]:
    """Palavras relevantes do nome: sem acento, sem tamanhos (30ml, 500g) e sem palavras de marketing."""
    text = unicodedata.normalize("NFD", str(name).lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    words = re.findall(r"[a-z0-9]+", text)
    return {w for w in words
            if len(w) > 2 and not re.fullmatch(r"\d+[a-z]{0,3}", w) and w not in STOPWORDS}


def similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    # pega tanto nomes quase iguais (Jaccard) quanto um nome contido no outro (sobreposição)
    return max(inter / len(a | b), 0.9 * inter / min(len(a), len(b)))


def too_similar(product: dict, selected: list[dict], recent_sets=()) -> bool:
    """Parecido com algo já escolhido neste disparo ou enviado recentemente?"""
    words = normalized_words(product.get("productName", ""))
    if not words:
        return False
    limit = SIMILARITY_LEVELS.get(CFG.get("similarity_level", "medio"), 0.6)
    others = [normalized_words(p.get("productName", "")) for p in selected] + list(recent_sets)
    return any(similarity(words, other) >= limit for other in others)


def category_weights(eligible: list[dict]) -> dict[str, float] | None:
    """Percentuais por categoria (só das ativas e com produto elegível). None = formato antigo."""
    cats = CFG.get("categories") or {}
    if not any("percent" in c for c in cats.values()):
        return None
    have = {category_for(p) for p in eligible}
    weights = {name: float(c.get("percent") or 0) for name, c in cats.items()
               if c.get("enabled", True) and c.get("keywords") and name in have}
    weights = {k: v for k, v in weights.items() if v > 0}
    return weights or None


def allocate_quotas(weights: dict[str, float], limit: int, credit: dict) -> dict[str, int]:
    """Divide os produtos do disparo por categoria. As frações sobram como 'crédito' para o
    próximo disparo, então a média ao longo do tempo respeita os percentuais."""
    total = sum(weights.values())
    for c, w in weights.items():
        credit[c] = credit.get(c, 0.0) + limit * w / total
    quotas = {c: max(0, int(credit[c])) for c in weights}
    order = sorted(weights, key=lambda c: (max(credit[c] - int(credit[c]), 0), weights[c]), reverse=True)
    while sum(quotas.values()) < limit:
        for c in order:
            quotas[c] += 1
            if sum(quotas.values()) >= limit:
                break
    while sum(quotas.values()) > limit:
        for c in reversed(order):
            if quotas[c] > 0:
                quotas[c] -= 1
                break
    return quotas


def choose_products(products: list[dict], limit: int, recent_sets=(), quotas=None) -> list[dict]:
    """Escolhe pelo score respeitando: cotas por categoria, no máximo N do mesmo tipo (palavra de
    busca) e da mesma loja por disparo, e nada parecido com o que já foi escolhido/enviado."""
    ranked = sorted(products, key=score, reverse=True)
    selected: list[dict] = []
    chosen_ids: set[str] = set()
    cat_n, kw_n, shop_n = Counter(), Counter(), Counter()
    max_kw, max_shop = CFG.get("max_per_keyword", 1), CFG.get("max_per_shop", 1)
    legacy_cap = CFG.get("max_per_category", 2)

    def keyword_of(p):
        return (p.get("_matched_keywords") or [""])[0]

    def shop_of(p):
        return str(p.get("shopName") or p.get("itemId"))

    def take(p):
        selected.append(p)
        chosen_ids.add(str(p.get("itemId")))
        cat_n[category_for(p)] += 1
        kw_n[keyword_of(p)] += 1
        shop_n[shop_of(p)] += 1

    def run_pass(respect_quota: bool, respect_type_shop: bool) -> None:
        for p in ranked:
            if len(selected) >= limit:
                return
            if str(p.get("itemId")) in chosen_ids:
                continue
            cat = category_for(p)
            if respect_quota:
                cap = quotas.get(cat, 0) if quotas is not None else legacy_cap
                if cat_n[cat] >= cap:
                    continue
            if respect_type_shop and (kw_n[keyword_of(p)] >= max_kw or shop_n[shop_of(p)] >= max_shop):
                continue
            if too_similar(p, selected, recent_sets):
                continue
            take(p)

    run_pass(True, True)    # 1) tudo respeitado
    run_pass(True, False)   # 2) mantém as cotas, relaxa tipo/loja
    run_pass(False, False)  # 3) completa com os melhores restantes (ainda sem parecidos)
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
    def empty():
        return {"postados": {}, "last_dispatch": None, "recentes": [], "credito": {}}
    if not STATE_FILE.exists():
        return empty()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("Aviso: histórico inválido/indisponível; iniciando histórico vazio.")
        return empty()
    if "postados" not in data:  # formato antigo: {itemId: timestamp}
        data = {"postados": data, "last_dispatch": None}
    data["postados"] = {k: v for k, v in data["postados"].items() if isinstance(v, (int, float))}
    data.setdefault("last_dispatch", None)
    data.setdefault("recentes", [])
    data.setdefault("credito", {})
    return data


def save_state(state: dict, now: float) -> None:
    state["postados"] = {k: v for k, v in state["postados"].items() if now - v < 30 * 86400}
    janela = CFG.get("similarity_window_days", 3) * 86400
    state["recentes"] = [r for r in state["recentes"] if now - r["t"] <= janela][-300:]
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)


def parse_hhmm(text: str):
    h, m = map(int, text.split(":"))
    return h * 60 + m


def should_dispatch(state: dict, now_dt: datetime, now: float, force: bool) -> tuple[bool, str]:
    if force:
        return True, "disparo manual"
    s = CFG.get("schedule", {})
    if now_dt.weekday() not in s.get("days", [0, 1, 2, 3, 4, 5, 6]):
        return False, "hoje não é dia ativo"
    start, end = parse_hhmm(s.get("active_from", "08:00")), parse_hhmm(s.get("active_until", "22:00"))
    cur = now_dt.hour * 60 + now_dt.minute
    inside = start <= cur <= end if start <= end else (cur >= start or cur <= end)
    if not inside:
        return False, f"fora da janela {s.get('active_from', '08:00')}–{s.get('active_until', '22:00')}"
    last = state.get("last_dispatch")
    interval = s.get("interval_minutes", 120) * 60
    tolerance = 10 * 60  # o cron do GitHub atrasa; aceita até 10 min antes
    if last and now - last < interval - tolerance:
        faltam = int((interval - tolerance - (now - last)) / 60) + 1
        return False, f"intervalo ainda não completou (faltam ~{faltam} min)"
    return True, "no horário"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--qtd", type=int, default=None)
    args = parser.parse_args()
    dry_run = args.dry_run
    force = args.force or os.environ.get("FORCE_DISPATCH", "").lower() == "true"
    qtd = args.qtd or int(os.environ.get("QTD_AGORA") or 0) or None

    now = time.time()
    now_dt = datetime.now(ZoneInfo(CFG.get("timezone", "America/Sao_Paulo")))
    print(f"{NAME} acordou às {now_dt:%d/%m %H:%M}.")

    state = load_state()
    go, why = should_dispatch(state, now_dt, now, force)
    if not go:
        print(f"Sem disparo agora: {why}.")
        return
    print(f"Disparando ({why}).")

    limit = qtd or CFG["top_n"]
    categories = active_categories()
    if not categories:
        print("ERRO: nenhuma categoria ativa com palavras-chave. Configure no painel.")
        sys.exit(1)
    cooldown = CFG["repeat_after_days"] * 86400

    found = {}
    successful_searches = 0
    total_searches = sum(len(kws) for kws in categories.values())
    for category, keywords in categories.items():
        for keyword in keywords:
            try:
                products = search(keyword)
                successful_searches += 1
                for product in products:
                    item_id = str(product.get("itemId", ""))
                    if not item_id:
                        continue
                    if item_id not in found:
                        product["_category"] = category
                        product["_matched_keywords"] = [keyword]
                        found[item_id] = product
                    elif keyword not in found[item_id]["_matched_keywords"]:
                        found[item_id]["_matched_keywords"].append(keyword)
            except Exception as exc:
                print(f"Falha na busca '{keyword}': {exc}")
            time.sleep(CFG.get("request_pause_seconds", 1))

    if successful_searches == 0:
        print(f"ERRO: nenhuma das {total_searches} buscas concluiu. "
              "Verifique SHOPEE_APP_ID/SHOPEE_SECRET e o status da API (erro 10000 = instabilidade).")
        sys.exit(1)

    rejected = Counter()
    eligible = []
    for item_id, product in found.items():
        reason = reject_reason(product)
        if reason is None and now - state["postados"].get(item_id, 0) <= cooldown:
            reason = f"já enviado nos últimos {CFG['repeat_after_days']} dias"
        if reason:
            rejected[reason] += 1
        else:
            eligible.append(product)

    window = CFG.get("similarity_window_days", 3) * 86400
    recent_sets = [normalized_words(r["n"]) for r in state["recentes"] if now - r["t"] <= window]
    weights = category_weights(eligible)
    quotas = allocate_quotas(weights, limit, state["credito"]) if weights else None
    selected = choose_products(eligible, limit, recent_sets, quotas)
    if quotas is not None:
        print("Cotas por categoria: " + ", ".join(f"{c} {q}" for c, q in quotas.items()))
        got = Counter(category_for(p) for p in selected)
        print("Escolhidos por categoria: " + (", ".join(f"{c} {n}" for c, n in got.items()) or "nenhum"))
        for c in weights:  # acerta o crédito com o que realmente saiu (limitado para evitar "explosões")
            state["credito"][c] = max(-2.0, min(2.0, state["credito"][c] - got.get(c, 0)))
    print(f"{len(found)} produtos encontrados; {len(eligible)} aprovados; "
          f"{len(selected)} selecionados; {successful_searches}/{total_searches} buscas concluídas.")
    if rejected:
        print("Rejeitados por: " + "; ".join(f"{r} ({n})" for r, n in rejected.most_common()))
    if len(eligible) < limit:
        print("ATENÇÃO: poucas ofertas elegíveis. Afrouxe filtros, adicione palavras-chave "
              "ou reduza 'repetir após (dias)' no painel.")

    sent = 0
    for product in selected:
        original_url = product.get("offerLink") or product.get("productLink")
        affiliate_url = short_link(original_url)
        if affiliate_url:
            link = affiliate_url
            print(f"Link curto gerado pela API de afiliados para item {product['itemId']}.")
        else:
            link = original_url
            print(f"Aviso: usando link original da API para o item {product['itemId']}; "
                  "confira a atribuição de afiliado.")

        message = build_message(product, link)
        if dry_run:
            print("\n" + "-" * 48 + "\n" + message)
            sent += 1
            continue
        try:
            send(product, message)
        except Exception as exc:  # um envio com erro não derruba o resto da rodada
            print(f"Falha ao enviar o item {product['itemId']}: {exc}")
            continue
        state["postados"][str(product["itemId"])] = now
        state["recentes"].append({"n": str(product.get("productName", "")), "t": now})
        sent += 1
        save_state(state, now)  # grava a cada envio: nada se perde se a rodada cair no meio
        time.sleep(CFG.get("send_pause_seconds", 2))

    if dry_run:
        return
    if sent:
        state["last_dispatch"] = now
        save_state(state, now)
        print(f"Disparo concluído: {sent} produto(s) enviado(s).")
    else:
        print("ERRO: nada foi enviado neste disparo (será tentado de novo na próxima rodada).")
        sys.exit(1)


if __name__ == "__main__":
    main()
