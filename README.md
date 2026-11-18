# Agente de promoções Shopee (R$0)

Busca ofertas de beleza/autocuidado na API oficial de afiliados da Shopee, filtra,
gera link de afiliado e manda pro seu Telegram. Você revisa e cola no grupo do WhatsApp.

## Passo a passo
1. **Shopee:** no painel de afiliados (affiliate.shopee.com.br), abra a seção *Open API* e copie o **App ID** e o **App Secret**.
2. **Telegram:** fale com @BotFather, crie um bot (/newbot) e copie o **token**. Mande qualquer mensagem pro bot e abra
   `https://api.telegram.org/bot<TOKEN>/getUpdates` para achar seu **chat id**.
3. **GitHub:** crie um repositório (pode ser privado), suba estes arquivos e cadastre em
   *Settings → Secrets and variables → Actions* estes 4 secrets:
   `SHOPEE_APP_ID`, `SHOPEE_SECRET`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`.
4. Vá em *Actions → Agente Shopee → Run workflow* para testar. Depois ele roda sozinho 5x ao dia.

## Testar no computador
```
pip install -r requirements.txt
export SHOPEE_APP_ID=... SHOPEE_SECRET=...
python agent.py --dry-run
```

## Ajustes (config.json)
- `keywords`: o que buscar. `top_n`: quantas ofertas por rodada.
- `filters`: desconto mínimo, nota, vendas, comissão mínima (0.05 = 5%) e preço máximo.
- `repeat_after_days`: dias antes de poder repetir o mesmo produto.

## Se der erro de campo (GraphQL)
A Shopee pode ajustar o schema. Edite a lista `FIELDS` no topo do `agent.py`.
