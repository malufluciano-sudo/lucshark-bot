# LucSharkTrade v13.0 — Guia de Migração

## O que mudou

- **Topics** — mensagens separadas por canal (Geral, Trades, Alertas, Scanner, Relatórios)
- **Dashboard pinado** — lista de trades abertos fixada no topic Trades
- **Mensagem mãe + replies** — cada trade = 1 card; eventos (A1, stop…) viram reply (notificação push)
- **Botões inline** — A1 / A2 / A3 / STOP / FECHAR nos trades
- **/alertas interativo** — botões para remover alertas
- **/debug_topics** — descobre IDs dos tópicos automaticamente
- **Fallback** — sem Topics configurados, tudo funciona no chat atual (como hoje)

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

## Saved Messages (opcional)

No celular: encaminhe o dashboard pinado para **Mensagens Salvas** e fixe lá — seu resumo portátil de trades abertos.
