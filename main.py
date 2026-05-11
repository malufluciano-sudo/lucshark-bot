import os, sqlite3, time, requests, threading, logging
import pandas as pd
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# ── Config via variáveis de ambiente ─────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY", "")
CAPITAL_INICIAL  = float(os.environ.get("CAPITAL_INICIAL", "1000"))
TOLERANCIA_PCT   = float(os.environ.get("TOLERANCIA_PCT", "0.005"))
INTERVALO_SEG    = int(os.environ.get("INTERVALO_SEG", "120"))
PORT             = int(os.environ.get("PORT", "8080"))

BRT = timezone(timedelta(hours=-3))
DB  = "/app/data/lucshark.db"
os.makedirs("/app/data", exist_ok=True)

PRECO_MAP = {
    "BTCUSDT":    ("cg", "bitcoin"),
    "ETHUSDT":    ("cg", "ethereum"),
    "BNBUSDT":    ("cg", "binancecoin"),
    "XRPUSDT":    ("cg", "ripple"),
    "SOLUSDT":    ("cg", "solana"),
    "LINKUSDT":   ("cg", "chainlink"),
    "LTCUSDT":    ("cg", "litecoin"),
    "HYPEUSDT":   ("cg", "hyperliquid"),
    "ASTRUSDT":   ("cg", "astar-network"),
    "SIRENUSDT":  ("cg", "siren-token"),
    "XAGUSD":     ("lb",  "xag_usdt"),
    "XAUUSD":     ("lb",  "xau_usdt"),
    "XPTUSD":     ("lb",  "xpt_usdt"),
    "XPDUSD":     ("lb",  "xpd_usdt"),
    "XTIUSD":     ("lb",  "xti_usdt"),
}

# ── Banco de dados ────────────────────────────────────────────
conn = sqlite3.connect(DB, check_same_thread=False)
cur  = conn.cursor()
cur.executescript("""
CREATE TABLE IF NOT EXISTS trades (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id       TEXT UNIQUE,
    data_hora      TEXT,
    ativo          TEXT,
    direcao        TEXT,
    tf_ctx         TEXT DEFAULT "1H",
    tf_ent         TEXT DEFAULT "5min",
    entrada        REAL,
    stop           REAL,
    alvo1          REAL,
    alvo2          REAL,
    alvo3          REAL,
    resultado      TEXT DEFAULT "MONITORANDO",
    preco_saida    REAL DEFAULT 0,
    alerta_enviado INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS aprendizados (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    data_hora TEXT,
    ativo     TEXT,
    trade_id  TEXT,
    erro      TEXT,
    correcao  TEXT
);
""")
conn.commit()

estado = {
    "ultimo_update_id": 0,
    "trade_counter": cur.execute("SELECT COUNT(*) FROM trades").fetchone()[0],
    "monitorando": True
}

# ── Helpers ───────────────────────────────────────────────────
def tg(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": str(msg)[:4000]},
            timeout=10)
    except Exception as e:
        log.error(f"TG: {e}")

def get_preco(symbol):
    sym   = symbol.upper().replace(".P", "")
    fonte = PRECO_MAP.get(sym)
    if not fonte: return None
    tipo, src = fonte
    if tipo == "cg":
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": src, "vs_currencies": "usd"}, timeout=8)
            if r.status_code == 200:
                return r.json().get(src, {}).get("usd")
        except: pass
    if tipo == "lb":
        try:
            r = requests.get("https://api.lbkex.com/v1/ticker.do",
                params={"symbol": src}, timeout=8)
            if r.status_code == 200:
                v = float(r.json().get("ticker", {}).get("latest", 0))
                return v if v > 0 else None
        except: pass
    return None

def rr(entrada, stop, alvo, direcao):
    try:
        risco = abs(float(entrada) - float(stop))
        lucro = abs(float(alvo) - float(entrada))
        return round(lucro/risco, 2) if risco > 0 else 0
    except: return 0

def novo_tid():
    estado["trade_counter"] += 1
    return f"LS{estado['trade_counter']:04d}"

def cadastrar(ativo, direcao, entrada, stop, a1, a2, a3, tf_ctx="1H", tf_ent="5min"):
    rr1 = rr(entrada, stop, a1, direcao)
    if rr1 < 1.0:
        tg(f"Trade DESCARTADO! A1 tem RR {rr1}:1 (minimo 1:1).")
        return None
    tid   = novo_tid()
    agora = datetime.now(BRT).strftime("%d/%m/%Y %H:%M BRT")
    cur.execute("""
        INSERT OR REPLACE INTO trades
        (trade_id,data_hora,ativo,direcao,tf_ctx,tf_ent,entrada,stop,alvo1,alvo2,alvo3)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (tid, agora, ativo.upper(), direcao.upper(), tf_ctx, tf_ent,
          float(entrada), float(stop), float(a1), float(a2), float(a3)))
    conn.commit()
    return tid

def relatorio():
    try:
        todos  = pd.read_sql_query("SELECT * FROM trades ORDER BY id DESC", conn)
        total  = len(todos)
        wins   = len(todos[todos.resultado.str.contains("WIN", na=False)])
        losses = len(todos[todos.resultado.str.contains("STOP|LOSS", na=False, regex=True)])
        mon    = len(todos[todos.resultado == "MONITORANDO"])
        wr     = wins/(wins+losses)*100 if (wins+losses) > 0 else 0
        tg(
            f"RELATORIO LucSharkTrade\n"
            f"{datetime.now(BRT).strftime('%d/%m/%Y %H:%M BRT')}\n"
            f"Total: {total} | Wins: {wins} | Losses: {losses} | Monitor: {mon}\n"
            f"Win Rate: {wr:.1f}%\n"
            f"Capital: ${CAPITAL_INICIAL:,.2f}"
        )
    except Exception as e:
        log.error(f"Relatorio: {e}")

# ── Monitor de preços ─────────────────────────────────────────
def monitorar():
    log.info("Monitor iniciado!")
    while estado["monitorando"]:
        try:
            rows = cur.execute(
                "SELECT trade_id,ativo,direcao,entrada,stop,alvo1,alvo2,alvo3,alerta_enviado "
                "FROM trades WHERE resultado=\"MONITORANDO\""
            ).fetchall()
            agora = datetime.now(BRT).strftime("%H:%M BRT")
            log.info(f"[{agora}] Monitorando {len(rows)} trade(s)...")
            for tid, ativo, direcao, entrada, stop, a1, a2, a3, ja_alertou in rows:
                entrada=float(entrada); stop=float(stop)
                a1=float(a1); a2=float(a2); a3=float(a3)
                preco = get_preco(ativo)
                if not preco:
                    log.warning(f"  {ativo}: sem preco")
                    continue
                dist = abs(preco - entrada) / entrada
                log.info(f"  {ativo} {direcao}: ${preco:,.4f} | entrada ${entrada:,.4f} | dist {dist*100:.2f}%")
                rr1 = rr(entrada,stop,a1,direcao)
                rr2 = rr(entrada,stop,a2,direcao)
                rr3 = rr(entrada,stop,a3,direcao)
                if dist <= TOLERANCIA_PCT and not ja_alertou:
                    cur.execute("UPDATE trades SET alerta_enviado=1 WHERE trade_id=?", (tid,))
                    conn.commit()
                    tg(
                        f"ALERTA DE ENTRADA\n"
                        f"LucSharkTrade | ID: {tid}\n"
                        f"Ativo: {ativo} | {direcao}\n"
                        f"Preco: ${preco:,.4f} | Entrada: ${entrada:,.4f}\n"
                        f"Distancia: {dist*100:.2f}%\n"
                        f"Stop: ${stop:,.4f}\n"
                        f"A1 (RR {rr1}:1): ${a1:,.4f} -> 25% + TS\n"
                        f"A2 (RR {rr2}:1): ${a2:,.4f} -> 50%\n"
                        f"A3 (RR {rr3}:1): ${a3:,.4f} -> 80%\n"
                        f"/resultado {ativo} WIN A1 ou /resultado {ativo} LOSS"
                    )
                    log.info(f"  ALERTA ENVIADO: {ativo}!")
                    continue
                if not ja_alertou: continue
                stop_hit = (direcao=="LONG" and preco<=stop) or (direcao=="SHORT" and preco>=stop)
                if stop_hit:
                    cur.execute("UPDATE trades SET resultado=\"STOP\",preco_saida=? WHERE trade_id=?", (preco,tid))
                    conn.commit()
                    tg(f"STOP ATINGIDO\nID: {tid} | {ativo} {direcao}\nEntrada: ${entrada:,.4f}\nSaida: ${preco:,.4f}\n/resultado {ativo} LOSS")
                    continue
                for val, nome, pct in [(a1,"A1",25),(a2,"A2",50),(a3,"A3",80)]:
                    hit = (direcao=="LONG" and preco>=val) or (direcao=="SHORT" and preco<=val)
                    if hit:
                        if nome == "A3":
                            cur.execute("UPDATE trades SET resultado=\"WIN_A3\",preco_saida=? WHERE trade_id=?", (preco,tid))
                            conn.commit()
                        tg(f"{nome} ATINGIDO!\nID: {tid} | {ativo} {direcao}\nPreco: ${preco:,.4f}\nRealizar {pct}%\n/resultado {ativo} WIN {nome}")
                        log.info(f"  {nome} ATINGIDO: {ativo}")
                        break
            time.sleep(INTERVALO_SEG)
        except Exception as e:
            log.error(f"Monitor erro: {e}")
            time.sleep(30)

# ── Comandos Telegram ─────────────────────────────────────────
def processar_cmd():
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": estado["ultimo_update_id"]+1, "timeout": 5},
            timeout=10)
        if r.status_code != 200: return
        for upd in r.json().get("result", []):
            estado["ultimo_update_id"] = upd["update_id"]
            txt = upd.get("message", {}).get("text", "").strip()
            if not txt: continue
            p = txt.split()
            cmd = p[0].lower()
            log.info(f"CMD: {txt}")
            if cmd == "/trade":
                if len(p) < 8:
                    tg("Formato:\n/trade ATIVO DIR ENTRADA STOP A1 A2 A3\nEx: /trade BTCUSDT LONG 84000 82000 86000 88000 90000")
                    continue
                try:
                    ativo=p[1].upper(); direcao=p[2].upper()
                    entrada=float(p[3]); stop=float(p[4])
                    a1=float(p[5]); a2=float(p[6]); a3=float(p[7])
                    tf_ctx=p[8].upper() if len(p)>8 else "1H"
                    tf_ent=p[9] if len(p)>9 else "5min"
                    if ativo not in PRECO_MAP:
                        tg(f"Ativo {ativo} nao mapeado.\nDisponiveis: {', '.join(PRECO_MAP.keys())}")
                        continue
                    tid = cadastrar(ativo, direcao, entrada, stop, a1, a2, a3, tf_ctx, tf_ent)
                    if not tid: continue
                    preco = get_preco(ativo)
                    ps = f"${preco:,.4f}" if preco else "Indisponivel"
                    ds = f"{abs(preco-entrada)/entrada*100:.2f}%" if preco else "N/A"
                    rr1=rr(entrada,stop,a1,direcao); rr2=rr(entrada,stop,a2,direcao); rr3=rr(entrada,stop,a3,direcao)
                    tg(f"TRADE CADASTRADO\nID: {tid} | {ativo} {direcao} | {tf_ctx}/{tf_ent}\nPreco atual: {ps}\nEntrada: ${entrada:,.4f} ({ds})\nStop: ${stop:,.4f}\nA1 (RR {rr1}:1): ${a1:,.4f}\nA2 (RR {rr2}:1): ${a2:,.4f}\nA3 (RR {rr3}:1): ${a3:,.4f}\nMonitorando a cada {INTERVALO_SEG//60} min")
                except Exception as e:
                    tg(f"Erro ao cadastrar: {e}")
            elif cmd == "/trades":
                rows = cur.execute("SELECT trade_id,ativo,direcao,entrada,resultado FROM trades ORDER BY id DESC LIMIT 15").fetchall()
                if not rows: tg("Nenhum trade cadastrado.")
                else:
                    linhas = [f"Trades ({len(rows)}):"]
                    for tid,ativo,direcao,entrada,res in rows:
                        linhas.append(f"{tid} | {ativo} {direcao} ${float(entrada):,.2f} | {res}")
                    tg("\n".join(linhas))
            elif cmd == "/cancelar" and len(p)>=2:
                ativo = p[1].upper()
                cur.execute("UPDATE trades SET resultado=\"CANCELADO\" WHERE ativo=? AND resultado=\"MONITORANDO\"", (ativo,))
                conn.commit()
                tg(f"{ativo}: cancelado.")
            elif cmd == "/resultado" and len(p)>=3:
                ativo=p[1].upper(); res=p[2].upper()
                nivel=p[3].upper() if len(p)>=4 else ""
                res_final = f"WIN_{nivel}" if res=="WIN" and nivel else res
                cur.execute("UPDATE trades SET resultado=? WHERE ativo=? AND resultado=\"MONITORANDO\"", (res_final, ativo))
                conn.commit()
                tg(f"{ativo}: {res_final} registrado!")
            elif cmd == "/relatorio": relatorio()
            elif cmd == "/status":
                mon = cur.execute("SELECT COUNT(*) FROM trades WHERE resultado=\"MONITORANDO\"").fetchone()[0]
                total = cur.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
                tg(f"Status LucSharkTrade\nMonitorando: {mon} trades\nTotal: {total} trades\nIntervalo: {INTERVALO_SEG//60} min\nTolerancia: {TOLERANCIA_PCT*100}%\n{datetime.now(BRT).strftime('%d/%m/%Y %H:%M BRT')}")
            elif cmd == "/ajuda":
                tg("Comandos LucSharkTrade:\n/trade ATIVO DIR ENTRADA STOP A1 A2 A3\n/trades — ver ativos\n/cancelar ATIVO\n/resultado ATIVO WIN A1\n/resultado ATIVO LOSS\n/relatorio\n/status")
    except Exception as e:
        log.error(f"Cmd erro: {e}")

# ── Flask keep-alive ──────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def index():
    mon = cur.execute("SELECT COUNT(*) FROM trades WHERE resultado=\"MONITORANDO\"").fetchone()[0]
    total = cur.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    return jsonify({
        "status": "online",
        "monitorando": mon,
        "total_trades": total,
        "horario": datetime.now(BRT).strftime("%d/%m/%Y %H:%M BRT")
    })

@app.route("/trades")
def ver_trades():
    rows = cur.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 20").fetchall()
    cols = [d[0] for d in cur.description]
    return jsonify([dict(zip(cols,r)) for r in rows])

# ── Iniciar ───────────────────────────────────────────────────
def cmd_loop():
    while True:
        processar_cmd()
        time.sleep(15)

if __name__ == "__main__":
    agora = datetime.now(BRT).strftime("%d/%m/%Y %H:%M BRT")
    mon = cur.execute("SELECT COUNT(*) FROM trades WHERE resultado=\"MONITORANDO\"").fetchone()[0]
    tg(f"LucSharkTrade ONLINE (nuvem)!\nMonitorando: {mon} trades\nIntervalo: {INTERVALO_SEG//60} min\n{agora}\nUse /ajuda para comandos")
    log.info("Bot iniciado!")
    threading.Thread(target=monitorar, daemon=True).start()
    threading.Thread(target=cmd_loop,  daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
