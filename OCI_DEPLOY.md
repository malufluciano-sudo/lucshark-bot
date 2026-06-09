# LucShark Bot — Oracle Cloud Always Free (24/7 grátis)

Guia para leigo. O bot roda na nuvem da Oracle — **sua máquina pode ficar desligada**.

Tempo total: ~20–30 minutos (só na primeira vez).

---

## Parte 1 — Você faz no navegador (Oracle)

### 1. Criar conta Oracle Cloud

1. Abra: https://www.oracle.com/cloud/free/
2. **Start for free** → cadastro com e-mail
3. Cartão de crédito (só verificação — instância Always Free **não cobra** se usar o shape certo)
4. Escolha região próxima (ex: **Brazil East São Paulo** se disponível, senão **US East**)

### 2. Criar a VM (instância)

1. Menu ☰ → **Compute** → **Instances** → **Create instance**

2. Preencha:

| Campo | Valor |
|-------|--------|
| Name | `lucshark-bot` |
| Image | **Ubuntu 22.04** (ou 24.04) — **aarch64** |
| Shape | **Ampere** → `VM.Standard.A1.Flex` |
| OCPUs | **1** |
| Memory (GB) | **6** (mínimo 6 no free tier) |
| Boot volume | 50 GB (default) |

3. **Networking**: marque **Assign a public IPv4 address**

4. **Add SSH keys** — escolha **Generate a key pair for me** → **Save private key** (arquivo `.key`) e **Save public key**

5. **Create**

6. Aguarde status **Running** (verde). Anote o **Public IP** (ex: `123.45.67.89`)

> Se não aparecer shape Ampere A1: região sem capacidade free — tente outra região ou aguarde e tente de novo.

### 3. Abrir portas (Security List)

1. Na instância, clique no **Subnet** (link azul)
2. Clique na **Security List** padrão
3. **Add Ingress Rules** — adicione **duas** regras:

| Source CIDR | Protocol | Dest. Port | Descrição |
|-------------|----------|------------|-----------|
| `0.0.0.0/0` | TCP | 22 | SSH |
| `0.0.0.0/0` | TCP | 8080 | Bot Flask / health / webhook JJ |

4. **Add Ingress Rules** em cada uma

---

## Parte 2 — Conectar na VM (sem instalar nada no Windows)

### Opção A — Console no navegador (mais fácil)

1. Na instância `lucshark-bot` → botão **Console connection** → **Launch Cloud Shell connection**  
   **ou** **Serial console** / **Instance access** (varia por região)

2. Se tiver **Cloud Shell** no topo da página Oracle → abra e rode:

```bash
ssh -i ~/.ssh/oci_key ubuntu@SEU_IP_PUBLICO
```

(Se não tiver SSH configurado, use **Connect** → **Public IP** → copie o comando SSH que a Oracle mostra.)

### Opção B — Terminal integrado

Na página da instância → **Connect** → copie o comando `ssh -i ... ubuntu@IP` e execute no **Cloud Shell** da Oracle (ícone `>_` no topo).

---

## Parte 3 — Instalar o bot (cole no terminal da VM)

Cole **bloco por bloco**:

```bash
# 1) Baixar repo e entrar na pasta de deploy
cd ~
git clone https://github.com/malufluciano-sudo/lucshark-bot.git
cd lucshark-bot/deploy/oracle
```

```bash
# 2) Rodar instalador (pede sudo)
sudo bash setup.sh
```

Quando pedir o `.env`:

```bash
sudo nano /opt/lucshark-bot/.env
```

- Troque `COLE_SEU_TOKEN_AQUI` pelo token do **@BotFather**
- Confirme `TELEGRAM_CHAT_ID` e `TOPIC_*` (já vêm preenchidos para o grupo LucShark Trading)
- Salvar: `Ctrl+O` → Enter → `Ctrl+X`
- Volte ao terminal do `setup.sh` e pressione **Enter**

---

## Parte 4 — Testar

### No terminal da VM

```bash
curl -s http://127.0.0.1:8080/health
```

Deve retornar: `{"status":"online","version":"v13.5"}` (ou versão atual).

### No Telegram (grupo LucShark Trading, tópico Geral)

```
/ping@LucSharkBot
/status@LucSharkBot
```

Deve responder **PONG** e status com **Topics ON**.

---

## Comandos úteis (na VM)

| Ação | Comando |
|------|---------|
| Ver logs ao vivo | `sudo journalctl -u lucshark -f` |
| Reiniciar bot | `sudo systemctl restart lucshark` |
| Parar bot | `sudo systemctl stop lucshark` |
| Status | `sudo systemctl status lucshark` |
| Atualizar código | `cd /opt/lucshark-bot && sudo git pull && sudo systemctl restart lucshark` |

---

## Vantagens vs Railway

| | Railway | Oracle VM |
|--|---------|-----------|
| Custo | Trial acabando | **Grátis forever** (Always Free) |
| SQLite / tópicos | Some no redeploy | **Persiste no disco** |
| PC ligado | Não precisa | Não precisa |

---

## Problemas comuns

**Shape A1 não disponível**  
→ Troque região (ex: US East Ashburn) e crie de novo.

**`setup.sh` falha no pip**  
→ `sudo apt install -y build-essential python3-dev` e rode `sudo bash setup.sh` de novo.

**Bot não responde no Telegram**  
→ `sudo journalctl -u lucshark -n 30` — verifique token no `.env`.

**Health OK mas /ping não responde**  
→ Confirme que está no tópico **Geral** certo (id 8), não no duplicado.

---

## Webhook Jeova Jireh (opcional)

No `.env` da VM:

```
JJ_WEBHOOK_SECRET=sua_senha
```

No Jeova Jireh (`.env` local):

```
LUCSHARK_WEBHOOK_URL=http://SEU_IP_PUBLICO:8080/api/jj/sinal
LUCSHARK_WEBHOOK_SECRET=sua_senha
```

Use o IP público da instância Oracle (porta 8080 já aberta no passo 3).

---

*LucShark Bot v13 — Oracle Cloud Always Free*
