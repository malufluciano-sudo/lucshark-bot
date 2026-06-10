# Migrar LucShark Bot — Render → Oracle Cloud (grátis 24/7)

Guia para **leigo**. Hoje o bot roda no **Render** (Free dorme após ~15 min). Na Oracle a VM fica **ligada sempre**, sem mensalidade (Always Free).

**Tempo:** ~30 min na primeira vez. **Seu PC pode ficar desligado.**

---

## Antes de começar — checklist

Copie do **Render → lucshark-bot → Environment** (print ou bloco de notas):

| Variável | Valor atual (já configurado) |
|----------|----------------------------|
| `TELEGRAM_TOKEN` | (secreto — copie do Render) |
| `TELEGRAM_CHAT_ID` | `-1001975710117` |
| `TELEGRAM_GROUP_ID` | `-1001975710117` |
| `TOPIC_GERAL` | `8` |
| `TOPIC_TRADES` | `9` |
| `TOPIC_ALERTAS` | `10` |
| `TOPIC_SCANNER` | `11` |
| `TOPIC_RELATORIOS` | `12` |
| `TOPIC_ANALISES` | `13` |

Grupo Telegram **LucShark Trading** e Topics **não mudam** — só troca o servidor.

---

## Visão do processo

```
1. Criar VM na Oracle (navegador)
2. Instalar bot na VM (terminal Oracle — colar comandos)
3. Testar /ping (Oracle ainda NÃO ligado no Telegram se Render estiver ON)
4. PARAR Render  →  bot Oracle assume
5. Testar de novo + atualizar Jeova Jireh (webhook opcional)
```

**Regra de ouro:** só **um** servidor com o mesmo `TELEGRAM_TOKEN`. Senão: erro `Conflict getUpdates`.

---

## Fase 1 — Conta e VM Oracle (você, no navegador)

Detalhes completos: **`OCI_DEPLOY.md`**

Resumo:

1. https://www.oracle.com/cloud/free/ → criar conta (cartão = verificação)
2. **Compute → Instances → Create instance**
3. Ubuntu **22.04 aarch64**, shape **Ampere A1.Flex** (1 CPU, **6 GB** RAM)
4. **Public IPv4** = Sim
5. SSH: **Generate key pair** → salvar arquivo `.key`
6. Anotar **IP público** (ex: `150.136.x.x`)
7. **Security List** → liberar TCP **22** e **8080**

> Shape A1 indisponível? Troque região (ex: US East Ashburn).

---

## Fase 2 — Instalar bot na VM (terminal Oracle)

Abra terminal na Oracle (**Connect** na instância ou **Cloud Shell** `>_`).

### Bloco 1 — clone + install

```bash
cd ~
git clone https://github.com/malufluciano-sudo/lucshark-bot.git
cd lucshark-bot/deploy/oracle
sudo bash setup.sh
```

### Bloco 2 — colar o `.env`

Quando o instalador pedir, abra outro terminal ou pause e rode:

```bash
sudo nano /opt/lucshark-bot/.env
```

Cole **todas** as variáveis (token real no lugar de `COLE_SEU_TOKEN_AQUI`):

```env
TELEGRAM_TOKEN=SEU_TOKEN_DO_RENDER
TELEGRAM_CHAT_ID=-1001975710117
TELEGRAM_GROUP_ID=-1001975710117
TOPIC_GERAL=8
TOPIC_TRADES=9
TOPIC_ALERTAS=10
TOPIC_SCANNER=11
TOPIC_RELATORIOS=12
TOPIC_ANALISES=13
CAPITAL_INICIAL=1000
INTERVALO_SEG=30
INTERVALO_SCAN=3600
PORT=8080
```

Salvar: `Ctrl+O` → Enter → `Ctrl+X` → voltar ao `setup.sh` → Enter.

### Bloco 3 — verificar na VM

```bash
sudo bash verificar.sh
```

Deve mostrar health `v13.5` (ou versão atual) e serviço **active**.

---

## Fase 3 — Cutover (trocar Render → Oracle)

**Ordem obrigatória:**

### 1. Parar o Render

1. https://dashboard.render.com → **lucshark-bot**
2. **Settings** → role até **Delete Web Service** ou **Suspend**
3. Confirme — aguarde **2 minutos**

### 2. Reiniciar bot na Oracle (garante token livre)

```bash
sudo systemctl restart lucshark
sudo journalctl -u lucshark -n 20
```

Não deve aparecer `Conflict`.

### 3. Testar Telegram

Grupo **LucShark Trading**, tópico **Geral**:

```
/ping@LucSharkBot
/status@LucSharkBot
```

Esperado: **PONG v13.5**, **Topics: ON**.

---

## Fase 4 — Jeova Jireh (opcional)

No `.env` do Jeova Jireh (`D:\Jeova Jireh\.env`), troque a URL do webhook:

```env
LUCSHARK_WEBHOOK_URL=http://SEU_IP_ORACLE:8080/api/jj/sinal
LUCSHARK_WEBHOOK_SECRET=mesma_senha_do_JJ_WEBHOOK_SECRET
```

No `.env` da Oracle (`/opt/lucshark-bot/.env`), adicione:

```env
JJ_WEBHOOK_SECRET=mesma_senha
```

Reinicie: `sudo systemctl restart lucshark` e reinicie o painel JJ (ícone Área de Trabalho).

Teste health externo (no seu PC):

```
http://SEU_IP_ORACLE:8080/health
```

---

## Depois da migração — manutenção

| Tarefa | Comando (na VM Oracle) |
|--------|-------------------------|
| Logs ao vivo | `sudo journalctl -u lucshark -f` |
| Reiniciar | `sudo systemctl restart lucshark` |
| Atualizar código | `sudo bash /opt/lucshark-bot/deploy/oracle/update.sh` |
| Verificar saúde | `sudo bash /opt/lucshark-bot/deploy/oracle/verificar.sh` |

---

## Comparativo

| | Render Free | Oracle Always Free |
|--|-------------|-------------------|
| Custo | $0 | $0 |
| 24/7 real | Não (dorme) | **Sim** |
| SQLite / config | Efêmero | **Persiste no disco** |
| Conflito token | 1 instância | 1 instância |
| Setup | Fácil | ~30 min uma vez |

---

## Problemas comuns

| Sintoma | Solução |
|---------|---------|
| `Conflict getUpdates` | Render ainda ligado — suspenda Render |
| Shape A1 não aparece | Outra região Oracle |
| `/ping` sem resposta | `sudo journalctl -u lucshark -n 30` — token no `.env` |
| Health OK, Telegram mudo | Tópico Geral errado — use `/debug_topics` |
| `pip` falha no setup | `sudo apt install -y build-essential python3-dev` e rode `setup.sh` de novo |

---

## Arquivos do pacote Oracle

| Arquivo | Função |
|---------|--------|
| `MIGRAR_ORACLE.md` | Este guia (cutover Render → Oracle) |
| `OCI_DEPLOY.md` | Detalhe VM, portas, SSH |
| `deploy/oracle/setup.sh` | Instalação inicial |
| `deploy/oracle/update.sh` | `git pull` + restart |
| `deploy/oracle/verificar.sh` | Health + status |
| `deploy/oracle/env.template` | Modelo do `.env` |

---

*Quando quiser migrar: diga **"vamos Oracle"** e envie o **IP público** da VM — guio o cutover ao vivo.*
