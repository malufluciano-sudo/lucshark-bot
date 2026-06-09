"""
LucSharkTrade v13.0 — Telegram Topics, InlineKeyboard, Dashboard pinado.
Funciona com ou sem Topics (TOPIC_* = 0 → chat principal).
"""
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta

import requests

log = logging.getLogger(__name__)

VERSION = "v13.5"
OFFSET_BRT = -3

TOPIC_PLAN = [
    ("geral", "Geral"),
    ("trades", "Trades"),
    ("alertas", "Alertas"),
    ("scanner", "Scanner"),
    ("relatorios", "Relatorios"),
    ("analises", "Analises"),
]


def _env_int(key: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(key, str(default)) or default)
    except (TypeError, ValueError):
        return default


TOPICS = {
    "geral": _env_int("TOPIC_GERAL"),
    "trades": _env_int("TOPIC_TRADES"),
    "alertas": _env_int("TOPIC_ALERTAS"),
    "scanner": _env_int("TOPIC_SCANNER"),
    "relatorios": _env_int("TOPIC_RELATORIOS"),
    "analises": _env_int("TOPIC_ANALISES"),
}

CMD_TOPIC = {
    "/start": "geral",
    "/ajuda": "geral",
    "/status": "geral",
    "/debug": "geral",
    "/debug_topics": "geral",
    "/parar": "geral",
    "/trade": "trades",
    "/resultado": "trades",
    "/fechar": "trades",
    "/trades": "trades",
    "/alerta": "alertas",
    "/alertas": "alertas",
    "/deletar_alerta": "alertas",
    "/delalerta": "alertas",
    "/scan": "scanner",
    "/ativos": "scanner",
    "/relatorio": "relatorios",
    "/semana": "relatorios",
    "/bloquear": "geral",
    "/desbloquear": "geral",
    "/blacklist": "geral",
    "/preco": "geral",
    "/watch": "scanner",
    "/unwatch": "scanner",
    "/watchlist": "scanner",
    "/editar": "trades",
    "/setup_topics": "geral",
    "/auto_setup": "geral",
    "/limpar_duplicados": "geral",
    "/ping": "geral",
}

_rediscover_cache_ts = 0.0

BOT_COMMANDS = [
    {"command": "ajuda", "description": "Menu de comandos"},
    {"command": "trade", "description": "Cadastrar trade monitorado"},
    {"command": "trades", "description": "Trades abertos"},
    {"command": "preco", "description": "Preço atual do ativo"},
    {"command": "alerta", "description": "Alerta de nível ou faixa"},
    {"command": "alertas", "description": "Listar alertas ativos"},
    {"command": "scan", "description": "Rodar scanner agora"},
    {"command": "watchlist", "description": "Ver watchlist"},
    {"command": "watch", "description": "Adicionar à watchlist"},
    {"command": "relatorio", "description": "Estatísticas e P&L"},
    {"command": "status", "description": "Status do bot"},
    {"command": "debug_topics", "description": "ID do tópico atual"},
    {"command": "setup_topics", "description": "Guia rápido Topics"},
    {"command": "auto_setup", "description": "Criar Topics automaticamente"},
    {"command": "limpar_duplicados", "description": "Remover tópicos duplicados"},
]

# Nomes equivalentes (evita criar 2x Geral / General)
_NOME_ALIASES = {
    "geral": ("geral", "general"),
    "trades": ("trades", "trade"),
    "alertas": ("alertas", "alerta"),
    "scanner": ("scanner", "scan"),
    "relatorios": ("relatorios", "relatorio"),
    "analises": ("analises", "analise", "análises"),
}


def carregar_topics_persistidos():
    """Carrega chat/tópicos salvos no SQLite (após /auto_setup). Env tem prioridade."""
    global TOPICS
    for key, _ in TOPIC_PLAN:
        env_key = f"TOPIC_{key.upper()}"
        env_val = _env_int(env_key)
        if env_val:
            TOPICS[key] = env_val
            continue
        db_val = sistema_get(f"topic_{key}")
        if db_val:
            try:
                TOPICS[key] = int(db_val)
            except ValueError:
                pass


def rediscover_topics(chat_id: int) -> bool:
    """
    Reconstrói IDs dos tópicos pelo nome (útil após redeploy Railway).
    Se houver duplicatas, mantém o tópico com ID maior (criado pelo auto_setup).
    """
    todos = _listar_forum_topics_all(chat_id)
    if not todos:
        return False
    encontrados = 0
    for key, nome in TOPIC_PLAN:
        aliases = _NOME_ALIASES.get(key, (nome.lower(),))
        candidatos = [
            t for t in todos
            if not t["is_general"] and t["name"].lower() in aliases
        ]
        if not candidatos:
            continue
        escolhido = max(candidatos, key=lambda x: x["id"])
        sistema_set(f"topic_{key}", escolhido["id"])
        TOPICS[key] = escolhido["id"]
        encontrados += 1
    if encontrados:
        sistema_set("telegram_chat_id", str(chat_id))
        log.info("Topics redescobertos: %s em chat %s", encontrados, chat_id)
    return encontrados > 0


def garantir_topics_grupo(chat_id: int, force: bool = False) -> bool:
    global _rediscover_cache_ts
    carregar_topics_persistidos()
    if topics_ativos():
        return True
    if not force and (time.time() - _rediscover_cache_ts) < 1800:
        return False
    ok = rediscover_topics(chat_id)
    if ok:
        _rediscover_cache_ts = time.time()
    return ok


def deletar_webhook():
    token = _token()
    if not token:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/deleteWebhook",
            json={"drop_pending_updates": False},
            timeout=10,
        )
    except Exception as e:
        log.warning("deleteWebhook: %s", e)


def responder_comando(msg: dict, texto: str, keyboard: dict | None = None):
    """
    Responde NO MESMO tópico onde o comando foi enviado.
    Corrige /status sem resposta quando há 2x Geral.
    """
    chat = msg.get("chat", {})
    cid = chat.get("id")
    thread = msg.get("message_thread_id")
    reply = msg.get("message_id")
    if chat.get("type") in ("group", "supergroup") and cid:
        return enviar_para_chat(
            cid, texto, thread=thread, reply_to=reply, keyboard=keyboard
        )
    return enviar(texto, topic="geral", reply_to=reply, keyboard=keyboard)


def _chat_id_persistido() -> str | None:
    return sistema_get("telegram_chat_id")


def brt_agora():
    return datetime.now(timezone(timedelta(hours=OFFSET_BRT)))


def _token() -> str:
    return os.environ.get("TELEGRAM_TOKEN", "")


def _chat_id() -> str:
    persisted = _chat_id_persistido()
    if persisted:
        return persisted
    return os.environ.get("TELEGRAM_CHAT_ID", "")


def _thread(topic_key: str):
    tid = TOPICS.get(topic_key, 0)
    return tid if tid else None


def tg_request(method: str, payload: dict):
    token = _token()
    chat = _chat_id()
    if not token or not chat:
        log.warning("Telegram não configurado.")
        return None
    if "chat_id" not in payload:
        payload["chat_id"] = chat
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        r = requests.post(url, json=payload, timeout=15)
        data = r.json()
        if not data.get("ok"):
            log.warning("tg %s: %s", method, data.get("description", data))
        return data
    except Exception as e:
        log.error("tg %s: %s", method, e)
        return None


def enviar_para_chat(
    chat_id: int | str,
    msg: str,
    thread: int | None = None,
    reply_to: int | None = None,
    keyboard: dict | None = None,
    pin: bool = False,
):
    """Envia para chat_id explícito (ex: resposta do /auto_setup no grupo)."""
    payload = {
        "chat_id": chat_id,
        "text": msg,
        "parse_mode": "HTML",
    }
    if thread:
        payload["message_thread_id"] = thread
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    if keyboard:
        payload["reply_markup"] = keyboard
    data = tg_request("sendMessage", payload)
    if not data or not data.get("ok"):
        return None
    msg_id = data["result"]["message_id"]
    if pin:
        tg_request("pinChatMessage", {
            "chat_id": chat_id,
            "message_id": msg_id,
            "disable_notification": True,
            **({"message_thread_id": thread} if thread else {}),
        })
    return msg_id


def enviar(
    msg: str,
    topic: str = "geral",
    reply_to: int | None = None,
    keyboard: dict | None = None,
    pin: bool = False,
    disable_notification: bool = False,
):
    payload = {
        "text": msg,
        "parse_mode": "HTML",
        "disable_notification": disable_notification,
    }
    thread = _thread(topic)
    if thread:
        payload["message_thread_id"] = thread
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    if keyboard:
        payload["reply_markup"] = keyboard
    data = tg_request("sendMessage", payload)
    if not data or not data.get("ok"):
        return None
    msg_id = data["result"]["message_id"]
    if pin:
        pin_msg(msg_id, topic)
    return msg_id


def editar(msg_id: int, msg: str, topic: str = "trades", keyboard: dict | None = None):
    payload = {
        "message_id": msg_id,
        "text": msg,
        "parse_mode": "HTML",
    }
    thread = _thread(topic)
    if thread:
        payload["message_thread_id"] = thread
    if keyboard:
        payload["reply_markup"] = keyboard
    return tg_request("editMessageText", payload)


def pin_msg(msg_id: int, topic: str = "trades"):
    payload = {"message_id": msg_id, "disable_notification": True}
    thread = _thread(topic)
    if thread:
        payload["message_thread_id"] = thread
    return tg_request("pinChatMessage", payload)


def answer_callback(callback_id: str, text: str = ""):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text[:200]
        payload["show_alert"] = len(text) > 60
    return tg_request("answerCallbackQuery", payload)


def topic_for_cmd(cmd: str) -> str:
    return CMD_TOPIC.get(cmd.lower().split("@")[0], "geral")


def topics_ativos() -> bool:
    return any(v > 0 for v in TOPICS.values())


def debug_topics_text(msg: dict) -> str:
    thread = msg.get("message_thread_id", "— (chat principal)")
    chat = msg.get("chat", {})
    chat_id = chat.get("id", "—")
    chat_title = chat.get("title", chat.get("first_name", "—"))
    brt = brt_agora().strftime("%d/%m/%Y %H:%M BRT")
    lines = [
        f"🔧 <b>DEBUG TOPICS v13.0</b>",
        f"🕐 {brt}",
        f"💬 Chat: <b>{chat_title}</b>",
        f"🆔 Chat ID: <code>{chat_id}</code>",
        f"📌 Topic ID (message_thread_id): <code>{thread}</code>",
        "",
        "<b>Variáveis Railway (copie este ID):</b>",
    ]
    name_map = {
        "geral": "TOPIC_GERAL",
        "trades": "TOPIC_TRADES",
        "alertas": "TOPIC_ALERTAS",
        "scanner": "TOPIC_SCANNER",
        "relatorios": "TOPIC_RELATORIOS",
        "analises": "TOPIC_ANALISES",
    }
    if thread and thread != "— (chat principal)":
        lines.append(f"→ Se este é o tópico atual: use <code>{thread}</code>")
    lines += [
        "",
        "<b>Configuradas agora:</b>",
    ]
    for key, var in name_map.items():
        val = TOPICS.get(key, 0)
        status = f"<code>{val}</code>" if val else "0 (desativado)"
        lines.append(f"  {var} = {status}")
    lines += [
        "",
        f"TELEGRAM_CHAT_ID = <code>{_chat_id() or 'não definido'}</code>",
        "",
        "Envie /debug_topics <b>dentro de cada tópico</b> para obter o ID correto.",
    ]
    return "\n".join(lines)


# ── DB helpers (trades.db) ─────────────────────────────────────────────────

def _db():
    return sqlite3.connect("trades.db")


def migrar_db():
    cols = [
        ("msg_id", "INTEGER"),
        ("estado", "TEXT DEFAULT 'AGUARDANDO'"),
    ]
    conn = _db()
    c = conn.cursor()
    c.execute("PRAGMA table_info(trades)")
    existentes = {row[1] for row in c.fetchall()}
    for col, tipo in cols:
        if col not in existentes:
            try:
                c.execute(f"ALTER TABLE trades ADD COLUMN {col} {tipo}")
                log.info("DB: coluna trades.%s adicionada", col)
            except Exception as e:
                log.warning("DB migrate %s: %s", col, e)
    c.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            ativo TEXT PRIMARY KEY,
            criado_em TEXT
        )
    """)
    conn.commit()
    conn.close()


def registrar_menu_comandos():
    return tg_request("setMyCommands", {"commands": BOT_COMMANDS})


def setup_topics_guia() -> str:
    return (
        f"🗂 <b>GUIA TOPICS {VERSION}</b>\n\n"
        "<b>Automático (recomendado):</b>\n"
        "1. Grupo → Editar → Topics ON → Save\n"
        "2. Bot = admin (Gerenciar tópicos + Fixar)\n"
        "3. No grupo: /auto_setup\n\n"
        "O bot cria os 6 tópicos e salva os IDs sozinho.\n"
        "Não precisa configurar Railway manualmente.\n\n"
        "<b>Manual:</b> /debug_topics em cada tópico + variáveis Railway."
    )


def _bot_user_id() -> int | None:
    data = tg_request("getMe", {})
    if data and data.get("ok"):
        return data["result"]["id"]
    return None


def _listar_forum_topics_all(chat_id: int) -> list:
    """Lista completa de tópicos (suporta nomes duplicados)."""
    out = []
    offset = 0
    while True:
        data = tg_request("getForumTopics", {
            "chat_id": chat_id,
            "limit": 100,
            "offset": offset,
        })
        if not data or not data.get("ok"):
            break
        batch = data["result"].get("topics", [])
        for t in batch:
            tid = t.get("message_thread_id")
            if not tid:
                continue
            out.append({
                "id": int(tid),
                "name": (t.get("name") or "").strip(),
                "is_general": bool(t.get("is_general")),
            })
        if not data["result"].get("has_more_topics", False):
            break
        offset += len(batch)
    return out


def _listar_forum_topics(chat_id: int) -> dict:
    """Mapa nome_lower -> message_thread_id (último visto)."""
    out = {}
    for t in _listar_forum_topics_all(chat_id):
        nl = t["name"].lower()
        if nl:
            out[nl] = t["id"]
        if t["is_general"]:
            out["geral"] = t["id"]
    return out


def _topic_key_por_nome(name: str, is_general: bool = False) -> str | None:
    if is_general:
        return None  # # General nativo — nunca apagar
    nl = name.strip().lower()
    for key, aliases in _NOME_ALIASES.items():
        if nl in aliases:
            return key
    return None


def _deletar_forum_topic(chat_id: int, thread_id: int) -> bool:
    data = tg_request("deleteForumTopic", {
        "chat_id": chat_id,
        "message_thread_id": thread_id,
    })
    return bool(data and data.get("ok"))


def limpar_topics_duplicados(chat_id: int) -> str:
    """
    Remove tópicos duplicados (ex: 2x Geral, 2x Trades).
    Mantém os IDs salvos pelo /auto_setup. Não apaga # General nativo.
    """
    if not garantir_topics_grupo(chat_id):
        return (
            "❌ Não achei tópicos salvos.\n"
            "Envie /auto_setup@LucSharkBot ou confira se Topics está ON."
        )

    carregar_topics_persistidos()
    canonical = {}
    for key, _ in TOPIC_PLAN:
        cid = TOPICS.get(key) or sistema_get(f"topic_{key}")
        if cid:
            try:
                canonical[key] = int(cid)
            except (TypeError, ValueError):
                pass

    if not canonical:
        return "❌ Falha ao mapear tópicos. Tente /auto_setup@LucSharkBot."

    todos = _listar_forum_topics_all(chat_id)
    por_chave: dict[str, list] = {}
    for t in todos:
        if t["is_general"]:
            continue
        chave = _topic_key_por_nome(t["name"])
        if not chave:
            continue
        por_chave.setdefault(chave, []).append(t)

    removidos = []
    mantidos = []
    erros = []

    for chave, lista in por_chave.items():
        if len(lista) <= 1:
            if lista:
                mantidos.append(f"{lista[0]['name']} (id {lista[0]['id']})")
            continue

        canon_id = canonical.get(chave)
        manter = None
        if canon_id:
            manter = next((t for t in lista if t["id"] == canon_id), None)
        if not manter:
            manter = max(lista, key=lambda x: x["id"])

        mantidos.append(f"{manter['name']} (id {manter['id']}) ✅")
        sistema_set(f"topic_{chave}", manter["id"])
        TOPICS[chave] = manter["id"]

        for t in lista:
            if t["id"] == manter["id"]:
                continue
            if _deletar_forum_topic(chat_id, t["id"]):
                removidos.append(f"{t['name']} (id {t['id']})")
            else:
                erros.append(f"{t['name']} (id {t['id']})")

    sistema_set("telegram_chat_id", str(chat_id))
    carregar_topics_persistidos()

    linhas = [
        f"🧹 <b>LIMPEZA DE TÓPICOS — {VERSION}</b>\n",
        f"<b>Mantidos:</b>",
    ]
    linhas += [f"  • {m}" for m in mantidos] or ["  • nenhum duplicado"]
    if removidos:
        linhas += ["", "<b>Removidos:</b>"] + [f"  🗑 {r}" for r in removidos]
    else:
        linhas += ["", "✅ Nenhum duplicado para remover."]
    if erros:
        linhas += ["", "<b>Erros (apague manualmente):</b>"] + [f"  ⚠️ {e}" for e in erros]
    linhas += [
        "",
        "ℹ️ <b># General</b> é nativo do Telegram — permanece, pode ignorar.",
        "Use só o <b>Geral</b> canônico (✅) daqui pra frente.",
    ]
    return "\n".join(linhas)


def auto_setup_grupo(chat_id: int) -> str:
    """
    Cria tópicos via Bot API e persiste IDs no SQLite.
    Requer: grupo com Topics ON + bot admin (can_manage_topics).
    """
    chat_info = tg_request("getChat", {"chat_id": chat_id})
    if not chat_info or not chat_info.get("ok"):
        return "❌ Não consegui ler o grupo. O bot está no grupo?"

    chat = chat_info["result"]
    if not chat.get("is_forum"):
        return (
            "❌ <b>Topics ainda não está ativado</b>\n\n"
            "Só falta 1 coisa manual (30 seg):\n"
            "1. Toque no nome do grupo\n"
            "2. Editar → Topics → ligue ON\n"
            "3. Layout List → <b>Save</b>\n"
            "4. Envie /auto_setup de novo\n\n"
            "Eu faço o resto automaticamente."
        )

    bot_id = _bot_user_id()
    if not bot_id:
        return "❌ Token do bot inválido."

    mem = tg_request("getChatMember", {"chat_id": chat_id, "user_id": bot_id})
    if not mem or not mem.get("ok"):
        return "❌ Bot não encontrado no grupo. Adicione o bot ao grupo."

    status = mem["result"].get("status", "")
    if status != "administrator":
        return (
            "❌ <b>Bot precisa ser administrador</b>\n\n"
            "1. Grupo → Administradores → Adicionar admin\n"
            "2. Selecione o LucSharkTrade\n"
            "3. Ative: Gerenciar tópicos + Fixar mensagens\n"
            "4. Envie /auto_setup novamente"
        )

    perms = mem["result"].get("can_manage_topics", False)
    can_pin = mem["result"].get("can_pin_messages", False)
    if not perms:
        return "❌ Ative a permissão <b>Gerenciar tópicos</b> para o bot."

    todos = _listar_forum_topics_all(chat_id)
    criados = []
    reutilizados = []
    linhas_ids = []

    for key, nome in TOPIC_PLAN:
        aliases = _NOME_ALIASES.get(key, (nome.lower(),))
        candidatos = [
            t for t in todos
            if not t["is_general"] and t["name"].lower() in aliases
        ]
        thread_id = candidatos[0]["id"] if candidatos else None
        if not thread_id:
            resp = tg_request("createForumTopic", {"chat_id": chat_id, "name": nome})
            if not resp or not resp.get("ok"):
                desc = (resp or {}).get("description", "erro desconhecido")
                return f"❌ Erro ao criar tópico <b>{nome}</b>: {desc}"
            thread_id = resp["result"]["message_thread_id"]
            criados.append(nome)
        else:
            reutilizados.append(nome)
        sistema_set(f"topic_{key}", thread_id)
        TOPICS[key] = int(thread_id)
        linhas_ids.append(f"  {nome}: <code>{thread_id}</code>")

    sistema_set("telegram_chat_id", str(chat_id))
    carregar_topics_persistidos()

    pin_txt = "✅" if can_pin else "⚠️ ative Fixar mensagens"
    return (
        f"✅ <b>TOPICS CONFIGURADOS — {VERSION}</b>\n\n"
        f"💬 Chat ID: <code>{chat_id}</code>\n"
        f"📌 Criados: {', '.join(criados) if criados else '—'}\n"
        f"♻️ Já existiam: {', '.join(reutilizados) if reutilizados else '—'}\n"
        f"📍 Fixar mensagens: {pin_txt}\n\n"
        f"<b>Tópicos ativos:</b>\n" + "\n".join(linhas_ids) + "\n\n"
        f"🗂 <b>Topics ON</b> — mensagens já vão para cada canal.\n"
        f"Teste: /status no Geral, /trade no Trades.\n\n"
        f"<b>Copie no Railway → Variables (permanente):</b>\n"
        f"<code>TELEGRAM_CHAT_ID={chat_id}</code>\n"
        f"<code>TELEGRAM_GROUP_ID={chat_id}</code>\n"
        + "\n".join(
            f"<code>TOPIC_{k.upper()}={TOPICS[k]}</code>"
            for k, _ in TOPIC_PLAN if TOPICS.get(k)
        )
    )


def sistema_get(chave: str):
    conn = _db()
    c = conn.cursor()
    c.execute("SELECT valor FROM sistema_log WHERE chave=?", (chave,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def sistema_set(chave: str, valor: str):
    agora = brt_agora().strftime("%Y-%m-%d %H:%M")
    conn = _db()
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO sistema_log VALUES (?,?,?)",
        (chave, str(valor), agora),
    )
    conn.commit()
    conn.close()


def get_trade(trade_id: int):
    conn = _db()
    c = conn.cursor()
    c.execute("SELECT * FROM trades WHERE id=?", (trade_id,))
    row = c.fetchone()
    conn.close()
    return row


def set_trade_msg(trade_id: int, msg_id: int, estado: str = "AGUARDANDO"):
    conn = _db()
    c = conn.cursor()
    c.execute(
        "UPDATE trades SET msg_id=?, estado=? WHERE id=?",
        (msg_id, estado, trade_id),
    )
    conn.commit()
    conn.close()


def set_trade_estado(trade_id: int, estado: str, resultado: str | None = None):
    conn = _db()
    c = conn.cursor()
    if resultado is not None:
        c.execute(
            "UPDATE trades SET estado=?, resultado=? WHERE id=?",
            (estado, resultado, trade_id),
        )
    else:
        c.execute("UPDATE trades SET estado=? WHERE id=?", (estado, trade_id))
    conn.commit()
    conn.close()


def listar_abertos():
    conn = _db()
    c = conn.cursor()
    c.execute(
        "SELECT id, ativo, direcao, entrada, resultado, estado, criado_em "
        "FROM trades WHERE resultado='ABERTO' ORDER BY id"
    )
    rows = c.fetchall()
    conn.close()
    return rows


def fmt_preco(val) -> str:
    if val is None:
        return "—"
    if val >= 1000:
        return f"${val:,.2f}"
    if val >= 1:
        return f"${val:.4f}"
    return f"${val:.6g}"


def keyboard_trade(trade_id: int, estado: str | None = None, modo: str = "normal"):
    if estado is None:
        row = get_trade(trade_id)
        estado = row[13] if row and len(row) > 13 else "AGUARDANDO"
    if estado == "FECHADO":
        return None
    if modo == "confirm_stop":
        return {
            "inline_keyboard": [[
                {"text": "🛑 CONFIRMAR STOP", "callback_data": f"t:{trade_id}:SC"},
                {"text": "❌ Cancelar", "callback_data": f"t:{trade_id}:SX"},
            ]]
        }
    if modo == "confirm_fechar":
        return {
            "inline_keyboard": [[
                {"text": "🔒 CONFIRMAR FECHAR", "callback_data": f"t:{trade_id}:FC"},
                {"text": "❌ Cancelar", "callback_data": f"t:{trade_id}:FX"},
            ]]
        }
    rows = []
    if estado == "AGUARDANDO":
        rows.append([
            {"text": "📥 ENTRADA", "callback_data": f"t:{trade_id}:ENTRADA"},
        ])
    rows += [
        [
            {"text": "✅ A1", "callback_data": f"t:{trade_id}:A1"},
            {"text": "✅ A2", "callback_data": f"t:{trade_id}:A2"},
            {"text": "✅ A3", "callback_data": f"t:{trade_id}:A3"},
        ],
        [
            {"text": "🛑 STOP", "callback_data": f"t:{trade_id}:STOP"},
            {"text": "🔒 FECHAR", "callback_data": f"t:{trade_id}:FECHAR"},
        ],
        [
            {"text": "✏️ EDITAR", "callback_data": f"t:{trade_id}:EDIT"},
        ],
    ]
    return {"inline_keyboard": rows}


def texto_trade_card(row, preco_atual=None) -> str:
    tid, ativo, direcao, entrada, stop, a1, a2, a3, tf_ctx, tf_ent, resultado, criado = row[:12]
    estado = row[13] if len(row) > 13 else "AGUARDANDO"
    emoji = "🟢" if direcao == "LONG" else "🔴"
    status = resultado if resultado and resultado != "ABERTO" else estado
    linha_mercado = ""
    if preco_atual is not None and entrada:
        if direcao == "LONG":
            pct = (preco_atual - entrada) / entrada * 100
        else:
            pct = (entrada - preco_atual) / entrada * 100
        linha_mercado = f"\n💲 Mercado: {fmt_preco(preco_atual)} ({pct:+.2f}%)"
    return (
        f"{emoji} <b>TRADE #{tid} — {ativo} {direcao}</b>\n"
        f"📥 Entrada: {fmt_preco(entrada)} | Stop: {fmt_preco(stop)}\n"
        f"🎯 A1: {fmt_preco(a1)} | A2: {fmt_preco(a2)} | A3: {fmt_preco(a3)}\n"
        f"⏱ {tf_ctx}/{tf_ent} | Status: <b>{status}</b>"
        f"{linha_mercado}\n"
        f"📅 {criado}"
    )


def texto_dashboard() -> str:
    abertos = listar_abertos()
    brt = brt_agora().strftime("%d/%m/%Y %H:%M BRT")
    linhas = [
        f"📌 <b>TRADES ABERTOS</b> — {brt}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if not abertos:
        linhas.append("📭 Nenhum trade aberto no momento.")
    else:
        for tid, ativo, direcao, entrada, resultado, estado, criado in abertos:
            emoji = "🟢" if direcao == "LONG" else "🔴"
            st = estado or "AGUARDANDO"
            linhas.append(
                f"{emoji} #{tid} <b>{ativo}</b> {direcao} {fmt_preco(entrada)} → {st}"
            )
    linhas += [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Total abertos: <b>{len(abertos)}</b>",
        "<i>Atualizado automaticamente pelo bot.</i>",
    ]
    return "\n".join(linhas)


def atualizar_dashboard():
    texto = texto_dashboard()
    pin_id = sistema_get("dashboard_pin_msg_id")
    if pin_id:
        try:
            editar(int(pin_id), texto, topic="trades")
            return
        except Exception:
            pass
    msg_id = enviar(texto, topic="trades", pin=True)
    if msg_id:
        sistema_set("dashboard_pin_msg_id", msg_id)


def criar_trade_mae(trade_id: int):
    row = get_trade(trade_id)
    if not row:
        return
    texto = texto_trade_card(row)
    kb = keyboard_trade(trade_id)
    msg_id = enviar(texto, topic="trades", keyboard=kb)
    if msg_id:
        set_trade_msg(trade_id, msg_id, "AGUARDANDO")
    atualizar_dashboard()
    return msg_id


def reply_evento(trade_id: int, texto: str):
    row = get_trade(trade_id)
    if not row:
        enviar(texto, topic="trades")
        return
    msg_id = row[12] if len(row) > 12 else None
    enviar(texto, topic="trades", reply_to=msg_id)
    if msg_id:
        editar(int(msg_id), texto_trade_card(get_trade(trade_id)), topic="trades",
               keyboard=keyboard_trade(trade_id, "ABERTO"))
    atualizar_dashboard()


def fechar_trade_card(trade_id: int, resultado: str, nota: str = ""):
    set_trade_estado(trade_id, "FECHADO", resultado)
    row = get_trade(trade_id)
    if not row:
        return
    msg_id = row[12] if len(row) > 12 else None
    texto = texto_trade_card(get_trade(trade_id))
    if msg_id:
        editar(int(msg_id), texto, topic="trades", keyboard=None)
        if nota:
            enviar(
                f"🔒 <b>Trade #{trade_id}</b> → {resultado}\n📝 {nota}",
                topic="trades",
                reply_to=int(msg_id),
            )
    atualizar_dashboard()


def notify_trade_event(trade_id: int, texto: str, estado: str | None = None):
    """Reply na mensagem mãe + atualiza card + dashboard (notificação push)."""
    row = get_trade(trade_id)
    if not row:
        enviar(texto, topic="trades")
        return
    if estado:
        set_trade_estado(trade_id, estado)
    msg_id = row[12] if len(row) > 12 else None
    enviar(texto, topic="trades", reply_to=msg_id)
    if msg_id:
        editar(
            int(msg_id),
            texto_trade_card(get_trade(trade_id)),
            topic="trades",
            keyboard=keyboard_trade(trade_id),
        )
    atualizar_dashboard()


def keyboard_alertas(niveis, faixas):
    rows = []
    for aid, ativo, nivel, cond, nota, _ in niveis[:8]:
        rows.append([
            {"text": f"🗑 {ativo} ${nivel:g}", "callback_data": f"a:n:{aid}"}
        ])
    for ativo, sup, res, _ in faixas[:8]:
        rows.append([
            {"text": f"🗑 {ativo} faixa", "callback_data": f"a:f:{ativo}"}
        ])
    if niveis or faixas:
        rows.append([
            {"text": "🧹 Limpar níveis", "callback_data": "a:clr:n"},
            {"text": "🧹 Limpar faixas", "callback_data": "a:clr:f"},
        ])
    return {"inline_keyboard": rows} if rows else None


def texto_lista_alertas(niveis, faixas) -> str:
    linhas = ["🔔 <b>ALERTAS ATIVOS</b>\n"]
    if niveis:
        linhas.append("<b>Níveis</b>")
        for aid, ativo, nivel, cond, nota, criado in niveis:
            n = f" — {nota}" if nota else ""
            linhas.append(f"  #{aid} {ativo} ${nivel:g} {cond}{n}")
    else:
        linhas.append("<b>Níveis</b>: nenhum")
    linhas.append("")
    if faixas:
        linhas.append("<b>Faixas</b>")
        for ativo, sup, res, criado in faixas:
            linhas.append(f"  {ativo} ${sup:g} — ${res:g}")
    else:
        linhas.append("<b>Faixas</b>: nenhuma")
    linhas.append("\n<i>Toque nos botões para remover.</i>")
    return "\n".join(linhas)


_preco_card_cache: dict[int, float] = {}


def watchlist_add(ativo: str):
    agora = brt_agora().strftime("%Y-%m-%d %H:%M")
    conn = _db()
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO watchlist VALUES (?,?)",
        (ativo.upper(), agora),
    )
    conn.commit()
    conn.close()


def watchlist_remove(ativo: str) -> bool:
    conn = _db()
    c = conn.cursor()
    c.execute("DELETE FROM watchlist WHERE ativo=?", (ativo.upper(),))
    ok = c.rowcount > 0
    conn.commit()
    conn.close()
    return ok


def watchlist_listar():
    conn = _db()
    c = conn.cursor()
    c.execute("SELECT ativo, criado_em FROM watchlist ORDER BY ativo")
    rows = c.fetchall()
    conn.close()
    return rows


def editar_trade_nivel(trade_id: int, campo: str, valor: float) -> str | None:
    campos = {
        "entrada": "entrada", "stop": "stop",
        "a1": "a1", "a2": "a2", "a3": "a3",
    }
    col = campos.get(campo.lower())
    if not col:
        return f"Campo inválido: {campo}"
    row = get_trade(trade_id)
    if not row or (row[10] or "ABERTO") != "ABERTO":
        return f"Trade #{trade_id} não encontrado ou já fechado."
    conn = _db()
    c = conn.cursor()
    c.execute(f"UPDATE trades SET {col}=? WHERE id=?", (valor, trade_id))
    conn.commit()
    conn.close()
    refresh_trade_card(trade_id)
    return None


def refresh_trade_card(trade_id: int, preco_atual=None):
    row = get_trade(trade_id)
    if not row:
        return
    msg_id = row[12] if len(row) > 12 else None
    if not msg_id:
        return
    editar(
        int(msg_id),
        texto_trade_card(row, preco_atual=preco_atual),
        topic="trades",
        keyboard=keyboard_trade(trade_id),
    )


def atualizar_precos_live(buscar_preco_fn):
    conn = _db()
    c = conn.cursor()
    c.execute(
        "SELECT id, ativo, entrada, msg_id FROM trades "
        "WHERE resultado='ABERTO' AND msg_id IS NOT NULL"
    )
    rows = c.fetchall()
    conn.close()
    for tid, ativo, entrada, msg_id in rows:
        if not msg_id:
            continue
        try:
            dados = buscar_preco_fn(ativo)
            if not dados:
                continue
            preco = dados.get("preco")
            if preco is None:
                continue
            if _preco_card_cache.get(tid) == preco:
                continue
            _preco_card_cache[tid] = preco
            refresh_trade_card(tid, preco_atual=preco)
        except Exception as e:
            log.debug("preco live #%s: %s", tid, e)


def responder_analise(msg: dict):
    """Topic Análises — acolhe prints com checklist Wyckoff."""
    msg_id = msg.get("message_id")
    caption = msg.get("caption") or ""
    texto = (
        "🧠 <b>ANÁLISE RECEBIDA</b>\n\n"
        "Checklist Wyckoff:\n"
        "1. Lateralização com S/R?\n"
        "2. Tuk Tuk completo (≥3 candles + vol crescente)?\n"
        "3. Breakout com volume alto?\n\n"
        "Se as 3 = SIM → entrada na direção do rompimento.\n"
        "Use /trade para cadastrar se o setup fechar."
    )
    if caption:
        texto = f"📝 <i>{caption[:200]}</i>\n\n" + texto
    enviar(texto, topic="analises", reply_to=msg_id)


def enviar_sinal_externo(mensagem: str, tipo: str = "scanner", prioridade: bool = False):
    prefix = "🚨 " if prioridade else ""
    topic = "scanner" if tipo == "scanner" else "geral"
    return enviar(f"{prefix}{mensagem}", topic=topic)


def _marcar_entrada(trade_id: int):
    row = get_trade(trade_id)
    if not row:
        return
    ativo = row[1]
    chave = f"{trade_id}_{ativo}_entrada"
    agora = brt_agora().strftime("%Y-%m-%d %H:%M")
    conn = _db()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO alertas_log VALUES (?,?)", (chave, agora))
    conn.commit()
    conn.close()


def _marcar_trade_fechado(trade_id: int):
    row = get_trade(trade_id)
    if not row:
        return
    ativo = row[1]
    base = f"{trade_id}_{ativo}"
    agora = brt_agora().strftime("%Y-%m-%d %H:%M")
    conn = _db()
    c = conn.cursor()
    for suf in ("_entrada", "_stop", "_a1", "_a2", "_a3", "_zona"):
        c.execute(
            "INSERT OR IGNORE INTO alertas_log VALUES (?,?)",
            (f"{base}{suf}", agora),
        )
    conn.commit()
    conn.close()


def processar_callback(data: str, callback_id: str, user_ok: bool = True):
    if not user_ok:
        answer_callback(callback_id, "Ação não autorizada.")
        return None

    if data.startswith("t:"):
        parts = data.split(":")
        if len(parts) != 3:
            answer_callback(callback_id, "Dados inválidos.")
            return None
        trade_id = int(parts[1])
        acao = parts[2]
        row = get_trade(trade_id)
        if not row or (row[10] or "ABERTO") != "ABERTO":
            answer_callback(callback_id, f"Trade #{trade_id} não está aberto.")
            return None

        msg_id = row[12] if len(row) > 12 else None

        if acao == "ENTRADA":
            estado_atual = row[13] if len(row) > 13 else "AGUARDANDO"
            if estado_atual != "AGUARDANDO":
                answer_callback(callback_id, f"Entrada já confirmada ({estado_atual}).")
                return None
            ativo, direcao, entrada = row[1], row[2], row[3]
            _marcar_entrada(trade_id)
            set_trade_estado(trade_id, "ENTRADA")
            emoji = "🟢" if direcao == "LONG" else "🔴"
            notify_trade_event(
                trade_id,
                (
                    f"{emoji} <b>ENTRADA CONFIRMADA — #{trade_id}</b>\n"
                    f"{ativo} {direcao} @ {fmt_preco(entrada)}\n"
                    f"✅ Posição ativa — monitorando alvos e stop."
                ),
                estado="ENTRADA",
            )
            answer_callback(callback_id, f"Entrada #{trade_id} confirmada!")
            return None

        if acao == "EDIT":
            enviar(
                (
                    f"✏️ <b>Editar trade #{trade_id}</b>\n\n"
                    f"/editar {trade_id} entrada VALOR\n"
                    f"/editar {trade_id} stop VALOR\n"
                    f"/editar {trade_id} a1 VALOR\n"
                    f"/editar {trade_id} a2 VALOR\n"
                    f"/editar {trade_id} a3 VALOR"
                ),
                topic="trades",
                reply_to=msg_id,
            )
            answer_callback(callback_id, "Comandos de edição enviados.")
            return None

        if acao == "STOP":
            if msg_id:
                editar(
                    int(msg_id),
                    texto_trade_card(row),
                    topic="trades",
                    keyboard=keyboard_trade(trade_id, modo="confirm_stop"),
                )
            answer_callback(callback_id, "Confirme o STOP.")
            return None

        if acao == "FECHAR":
            if msg_id:
                editar(
                    int(msg_id),
                    texto_trade_card(row),
                    topic="trades",
                    keyboard=keyboard_trade(trade_id, modo="confirm_fechar"),
                )
            answer_callback(callback_id, "Confirme o FECHAR.")
            return None

        if acao == "SX":
            if msg_id:
                editar(
                    int(msg_id),
                    texto_trade_card(row),
                    topic="trades",
                    keyboard=keyboard_trade(trade_id),
                )
            answer_callback(callback_id, "STOP cancelado.")
            return None

        if acao == "FX":
            if msg_id:
                editar(
                    int(msg_id),
                    texto_trade_card(row),
                    topic="trades",
                    keyboard=keyboard_trade(trade_id),
                )
            answer_callback(callback_id, "FECHAR cancelado.")
            return None

        mapa = {
            "A1": "WIN_A1",
            "A2": "WIN_A2",
            "A3": "WIN_A3",
            "SC": "LOSS",
            "FC": "BREAKEVEN",
        }
        resultado = mapa.get(acao)
        if not resultado:
            answer_callback(callback_id, "Ação desconhecida.")
            return None
        _marcar_trade_fechado(trade_id)
        label = "STOP" if acao == "SC" else ("FECHAR" if acao == "FC" else acao)
        fechar_trade_card(trade_id, resultado, nota=f"Confirmado via botão ({label})")
        answer_callback(callback_id, f"Trade #{trade_id} → {resultado}")
        return f"✅ Trade #{trade_id} atualizado: {resultado}"

    if data.startswith("a:n:"):
        aid = int(data.split(":")[2])
        conn = _db()
        c = conn.cursor()
        c.execute("UPDATE alertas_nivel SET disparado=1 WHERE id=?", (aid,))
        conn.commit()
        conn.close()
        answer_callback(callback_id, f"Alerta #{aid} removido.")
        return "ALERTAS_REFRESH"

    if data.startswith("a:f:"):
        ativo = data.split(":")[2]
        conn = _db()
        c = conn.cursor()
        c.execute("DELETE FROM alertas_preco WHERE ativo=?", (ativo,))
        conn.commit()
        conn.close()
        answer_callback(callback_id, f"Faixa {ativo} removida.")
        return "ALERTAS_REFRESH"

    if data == "a:clr:n":
        conn = _db()
        c = conn.cursor()
        c.execute("UPDATE alertas_nivel SET disparado=1 WHERE disparado=0")
        conn.commit()
        conn.close()
        answer_callback(callback_id, "Níveis limpos.")
        return "ALERTAS_REFRESH"

    if data == "a:clr:f":
        conn = _db()
        c = conn.cursor()
        c.execute("DELETE FROM alertas_preco")
        conn.commit()
        conn.close()
        answer_callback(callback_id, "Faixas limpas.")
        return "ALERTAS_REFRESH"

    answer_callback(callback_id)
    return None
