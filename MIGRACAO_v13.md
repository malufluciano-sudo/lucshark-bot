# LucSharkTrade v13.1 — Guia de Migração

## O que mudou (v13.1)

- **Topics** — mensagens por canal (Geral, Trades, Alertas, Scanner, Relatórios, Análises)
- **Dashboard pinado** + **preço ao vivo** nos cards de trade
- **Botões** — ENTRADA / A1-A3 / STOP e FECHAR com **confirmação em 2 toques** / EDITAR
- **/preco** / **/watch** / **/watchlist** — consulta e scanner focado
- **Scanner** — bloco 🚨 PRIORIDADE (score ≥ 80)
- **Topic Análises** — envie print; bot responde com checklist Wyckoff
- **Menu de comandos** — `/` no Telegram lista comandos
- **Integração JJ** — webhook `POST /api/jj/sinal` + variáveis no rastreador
- **Fallback** — sem Topics, tudo no chat atual

## Passo 1 — Grupo no Telegram (única parte manual)

1. Telegram → Novo Grupo → adicione o bot
2. Nome: `LucShark Trading`
3. Configurações do grupo → **Topics** → Ativar → Save
4. Crie os tópicos (só texto, sem emoji obrigatório):
   - Geral
   - Trades
   - Alertas
   - Scanner
   - Relatorios
   - Analises
5. Administradores → bot como **admin** com permissão de **fixar mensagens**

> A API do Telegram **não permite** criar grupos/tópicos por bot — são ~2 minutos no celular/desktop.

## Passo 2 — Deploy no Railway (automático via GitHub)

Arquivos novos/alterados:
- `main.py` (v13.0)
- `telegram_v13.py` (novo)

```bash
cd D:\lucshark-bot
git add main.py telegram_v13.py MIGRACAO_v13.md
git commit -m "feat: v13.0 Topics + InlineKeyboard + dashboard pinado"
git push
```

Railway redeploy em ~2 minutos.

## Passo 3 — Configurar variáveis (com /debug_topics)

**Sem configurar Topics**, o bot já funciona no chat privado/grupo principal.

Quando o grupo estiver pronto:

1. Entre no tópico **Geral** → envie `/debug_topics`
2. Anote o `message_thread_id` exibido
3. Repita em cada tópico (Trades, Alertas, Scanner, Relatorios, Analises)
4. No Railway → Variables:

| Variável | Valor |
|----------|-------|
| `TELEGRAM_CHAT_ID` | ID do grupo (número negativo, ex: `-1001234567890`) |
| `TOPIC_GERAL` | ID do tópico Geral |
| `TOPIC_TRADES` | ID do tópico Trades |
| `TOPIC_ALERTAS` | ID do tópico Alertas |
| `TOPIC_SCANNER` | ID do tópico Scanner |
| `TOPIC_RELATORIOS` | ID do tópico Relatorios |
| `TOPIC_ANALISES` | ID do tópico Analises |

5. Redeploy ou aguarde restart

### Como obter TELEGRAM_CHAT_ID do grupo

Envie qualquer mensagem no grupo e abra no navegador:

```
https://api.telegram.org/bot<SEU_TOKEN>/getUpdates
```

Procure `"chat":{"id":-100...}` — esse número negativo é o chat ID.

## Passo 4 — Testar

| Comando | Onde deve aparecer |
|---------|-------------------|
| `/status` | Geral |
| `/trade BTCUSDT LONG ...` | Trades + dashboard atualizado |
| `/alertas` | Alertas com botões |
| `/scan` | Scanner |
| `/relatorio` | Relatórios |
| Toque em ✅ A1 num trade | Reply + card atualizado |

## Integração Jeova Jireh (opcional)

No **Railway** (lucshark-bot):
```
JJ_WEBHOOK_SECRET=uma_senha_forte
```

No **JJ** `.env` (escolha uma opção):

**Opção A — Webhook** (LucShark roteia para topic Scanner):
```
LUCSHARK_WEBHOOK_URL=https://seu-app.railway.app/api/jj/sinal
LUCSHARK_WEBHOOK_SECRET=mesma_senha
```

**Opção B — Telegram direto** com topic:
```
TELEGRAM_TOPIC_SCANNER=12345
```

## Saved Messages (opcional)

No celular: encaminhe o dashboard pinado para **Mensagens Salvas** e fixe lá — seu resumo portátil de trades abertos.
