# LucShark Bot — Deploy no Render

## Correção do build (pandas / Python 3.14)

O bot (`main.py`) **não usa pandas**. Dependências pesadas ficam só em `requirements_dashboard.txt`.

| Arquivo | Função |
|---------|--------|
| `requirements.txt` | Bot: flask, requests, ccxt |
| `.python-version` | Render usa Python **3.11.9** |
| `render.yaml` | Build/start + health `/health` |

## Passos no Render (serviço já criado)

1. **Environment** → adicione se não existir:
   - `PYTHON_VERSION` = `3.11.9`
2. **Manual Deploy** → **Deploy latest commit**
3. Logs devem mostrar Python 3.11.x e `Successfully installed flask requests ccxt`
4. Teste Telegram: `/ping@LucSharkBot`

## Variáveis obrigatórias (Environment)

```
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=-1001975710117
TELEGRAM_GROUP_ID=-1001975710117
TOPIC_GERAL=8
TOPIC_TRADES=9
TOPIC_ALERTAS=10
TOPIC_SCANNER=11
TOPIC_RELATORIOS=12
TOPIC_ANALISES=13
```

## Webhook Jeova Jireh

```
LUCSHARK_WEBHOOK_URL=https://SEU-SERVICO.onrender.com/api/jj/sinal
```

(URL pública do serviço no Render → Settings → URL)

## Atenção — plano Free do Render

O plano **Free** pode **dormir** após ~15 min sem tráfego HTTP. Bot com **polling** precisa processo sempre ligado.

- Para testar build: Free serve.
- Para **24/7 real**: plano **Starter** (~US$ 7/mês) ou outro host always-on.

Para reduzir sleep no Free: cron externo (UptimeRobot) pingando `https://SEU-SERVICO.onrender.com/health` a cada 5 min — ajuda no HTTP, mas o polling pode ainda falhar em cold start.
