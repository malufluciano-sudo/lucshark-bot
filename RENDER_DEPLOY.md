# LucShark Bot — Deploy no Render

> **Plano B (24/7 grátis):** quando quiser sair do Render Free, use **`MIGRAR_ORACLE.md`**.

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

## Erro: `Conflict: terminated by other getUpdates request`

**Duas instâncias** usam o mesmo `TELEGRAM_TOKEN` (ex: Render + Railway).

1. **Railway** → projeto intuição prática → serviço → **Settings → Delete Service** (ou Pause)
2. **Render** → **Scaling** → confirmar **1 instância**
3. Seu PC → não rode `python main.py` local com o mesmo token
4. **Render** → Manual Deploy → aguarde 1 min → `/ping@LucSharkBot`

Só **um** servidor pode rodar o bot por vez.

## Atenção — plano Free do Render

O plano **Free** pode **dormir** após ~15 min sem tráfego HTTP. Bot com **polling** precisa processo sempre ligado.

- Para testar build: Free serve.
- Para **24/7 real**: plano **Starter** (~US$ 7/mês) ou outro host always-on.

Para reduzir sleep no Free: cron externo (UptimeRobot) pingando `https://SEU-SERVICO.onrender.com/health` a cada 5 min — ajuda no HTTP, mas o polling pode ainda falhar em cold start.
