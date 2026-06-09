"""
LucSharkTrade v13.0 — Telegram Topics, InlineKeyboard, Dashboard pinado.
Funciona com ou sem Topics (TOPIC_* = 0 → chat principal).
"""
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta

import requests

log = logging.getLogger(__name__)

VERSION = "v13.0"
OFFSET_BRT = -3


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
}


def brt_agora():
    return datetime.now(timezone(timedelta(hours=OFFSET_BRT)))


def _token() -> str:
    return os.environ.get("TELEGRAM_TOKEN", "")


def _chat_id() -> str:
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
    conn.commit()
    conn.close()


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


def keyboard_trade(trade_id: int, estado: str | None = None):
    if estado is None:
        row = get_trade(trade_id)
        estado = row[13] if row and len(row) > 13 else "AGUARDANDO"
    if estado == "FECHADO":
        return None
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
    ]
    return {"inline_keyboard": rows}


def texto_trade_card(row) -> str:
    tid, ativo, direcao, entrada, stop, a1, a2, a3, tf_ctx, tf_ent, resultado, criado = row[:12]
    estado = row[13] if len(row) > 13 else "AGUARDANDO"
    emoji = "🟢" if direcao == "LONG" else "🔴"
    status = resultado if resultado and resultado != "ABERTO" else estado
    return (
        f"{emoji} <b>TRADE #{tid} — {ativo} {direcao}</b>\n"
        f"📥 Entrada: {fmt_preco(entrada)} | Stop: {fmt_preco(stop)}\n"
        f"🎯 A1: {fmt_preco(a1)} | A2: {fmt_preco(a2)} | A3: {fmt_preco(a3)}\n"
        f"⏱ {tf_ctx}/{tf_ent} | Status: <b>{status}</b>\n"
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

        mapa = {
            "A1": "WIN_A1",
            "A2": "WIN_A2",
            "A3": "WIN_A3",
            "STOP": "LOSS",
            "FECHAR": "BREAKEVEN",
        }
        resultado = mapa.get(acao)
        if not resultado:
            answer_callback(callback_id, "Ação desconhecida.")
            return None
        _marcar_trade_fechado(trade_id)
        fechar_trade_card(trade_id, resultado, nota=f"Confirmado via botão ({acao})")
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
