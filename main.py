import os, sqlite3, time, requests, threading, logging
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
CAPITAL_INICIAL  = float(os.environ.get("CAPITAL_INICIAL", "1000"))
TOLERANCIA_PCT   = float(os.environ.get("TOLERANCIA_PCT", "0.005"))
INTERVALO_MON    = int(os.environ.get("INTERVALO_MON", "120"))
INTERVALO_SCAN   = int(os.environ.get("INTERVALO_SCAN", "3600"))
PORT             = int(os.environ.get("PORT", "8080"))

BRT = timezone(timedelta(hours=-3))
DB  = "/app/data/lucshark.db"
os.makedirs("/app/data", exist_ok=True)

ATIVOS = [
    "btc_usdt","eth_usdt","bnb_usdt","xrp_usdt","sol_usdt",
    "link_usdt","ltc_usdt","hyper_usdt","astr_usdt"
]

PRECO_MAP = {
    "BTCUSDT":   ("cg","bitcoin"),    "ETHUSDT":  ("cg","ethereum"),
    "BNBUSDT":   ("cg","binancecoin"),"XRPUSDT":  ("cg","ripple"),
    "SOLUSDT":   ("cg","solana"),     "LINKUSDT": ("cg","chainlink"),
    "LTCUSDT":   ("cg","litecoin"),   "HYPEUSDT": ("cg","hyperliquid"),
    "ASTRUSDT":  ("cg","astar-network"),
    "XAGUSD":    ("lb","xag_usdt"),   "XAUUSD":   ("lb","xau_usdt"),
    "XPTUSD":    ("lb","xpt_usdt"),   "XPDUSD":   ("lb","xpd_usdt"),
    "XTIUSD":    ("lb","xti_usdt"),
}

# ── Banco ─────────────────────────────────────────────────────
conn = sqlite3.connect(DB, check_same_thread=False)
cur  = conn.cursor()
cur.executescript("""
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT UNIQUE, data_hora TEXT, ativo TEXT, direcao TEXT,
    tf_ctx TEXT DEFAULT "1H", tf_ent TEXT DEFAULT "5min",
    entrada REAL, stop REAL, alvo1 REAL, alvo2 REAL, alvo3 REAL,
    resultado TEXT DEFAULT "MONITORANDO",
    preco_saida REAL DEFAULT 0, alerta_enviado INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS scanner_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data_hora TEXT, ativo TEXT, sinal TEXT, detalhes TEXT, enviado INTEGER DEFAULT 0
);
""")
conn.commit()

estado = {
    "ultimo_update_id": 0,
    "trade_counter": cur.execute("SELECT COUNT(*) FROM trades").fetchone()[0],
    "monitorando": True,
    "ativos_monitor": list(ATIVOS),
    "ultimo_scan": None
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
    sym = symbol.upper().replace(".P","")
    fonte = PRECO_MAP.get(sym)
    if not fonte: return None
    tipo, src = fonte
    if tipo == "cg":
        try:
            r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                params={"ids":src,"vs_currencies":"usd"}, timeout=8)
            if r.status_code == 200:
                return r.json().get(src,{}).get("usd")
        except: pass
    if tipo == "lb":
        try:
            r = requests.get("https://api.lbkex.com/v1/ticker.do",
                params={"symbol":src}, timeout=8)
            if r.status_code == 200:
                v = float(r.json().get("ticker",{}).get("latest",0))
                return v if v > 0 else None
        except: pass
    return None

def get_klines(symbol, period, size=100):
    try:
        r = requests.get("https://api.lbkex.com/v1/kline.do",
            params={"symbol":symbol,"size":size,"type":period}, timeout=10)
        if r.status_code != 200: return None
        data = r.json()
        if not isinstance(data,list) or len(data)==0: return None
        df = pd.DataFrame(data, columns=["timestamp","open","close","high","low","volume"])
        for c in ["open","close","high","low","volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df
    except: return None

def calcular(df):
    if df is None or len(df) < 20: return df
    df = df.copy()
    df["ema9"]  = df["close"].ewm(span=9,  adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    d = df["close"].diff()
    g = d.clip(lower=0).ewm(com=13,adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(com=13,adjust=False).mean()
    df["rsi"]  = 100-(100/(1+g/l))
    df["vwap"] = (df["close"]*df["volume"]).cumsum()/df["volume"].cumsum()
    df["vol_med"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"]/df["vol_med"]
    # Bollinger Bands
    df["bb_mid"] = df["close"].rolling(20).mean()
    std = df["close"].rolling(20).std()
    df["bb_up"]  = df["bb_mid"] + 2*std
    df["bb_dn"]  = df["bb_mid"] - 2*std
    # ATR
    h,l2,c = df["high"],df["low"],df["close"].shift(1)
    tr = pd.concat([h-l2,(h-c).abs(),(l2-c).abs()],axis=1).max(axis=1)
    df["atr"] = tr.ewm(com=13,adjust=False).mean()
    return df

# ── Scanner ───────────────────────────────────────────────────
def analisar_ativo(symbol):
    sinais = []
    try:
        df1h = calcular(get_klines(symbol, "1hour", 100))
        df5m = calcular(get_klines(symbol, "5min",  100))
        if df1h is None or df5m is None: return []

        u1  = df1h.iloc[-1]
        u5  = df5m.iloc[-1]
        p2  = df1h.iloc[-2]
        p5  = df5m.iloc[-2]

        preco   = float(u1["close"])
        rsi1h   = float(u1["rsi"])
        vol1h   = float(u1["vol_ratio"])
        ema9    = float(u1["ema9"])
        ema21   = float(u1["ema21"])
        vwap    = float(u1["vwap"])
        atr     = float(u1["atr"])

        max20_1h = float(df1h["high"].tail(20).max())
        min20_1h = float(df1h["low"].tail(20).min())
        max5_5m  = float(df5m["high"].tail(5).max())
        min5_5m  = float(df5m["low"].tail(5).min())

        sym_upper = symbol.replace("_usdt","usdt").upper()

        # ── SINAL 1: Rompimento de lateralizacao com volume (Tuk Tuk) ──
        # Preco rompe maxima das ultimas 20 barras com volume > 1.5x media
        if preco >= max20_1h * 0.998 and vol1h >= 1.5:
            sinais.append({
                "tipo": "TUK TUK LONG",
                "forca": "FORTE",
                "descricao": f"Rompimento de maxima {max20_1h:.4f} com volume {vol1h:.1f}x acima da media",
                "acao": f"Aguardar pullback para entrada LONG | Envie print para analise"
            })

        if preco <= min20_1h * 1.002 and vol1h >= 1.5:
            sinais.append({
                "tipo": "TUK TUK SHORT",
                "forca": "FORTE",
                "descricao": f"Rompimento de minima {min20_1h:.4f} com volume {vol1h:.1f}x acima da media",
                "acao": f"Aguardar pullback para entrada SHORT | Envie print para analise"
            })

        # ── SINAL 2: Cruzamento de EMAs com volume ──
        ema9_ant  = float(p2["ema9"])
        ema21_ant = float(p2["ema21"])
        if ema9_ant < ema21_ant and ema9 > ema21 and vol1h >= 1.2:
            sinais.append({
                "tipo": "EMA CROSS LONG",
                "forca": "MEDIA",
                "descricao": f"EMA9 cruzou EMA21 para cima com volume {vol1h:.1f}x",
                "acao": "Potencial LONG | Envie print para confirmar estrutura"
            })

        if ema9_ant > ema21_ant and ema9 < ema21 and vol1h >= 1.2:
            sinais.append({
                "tipo": "EMA CROSS SHORT",
                "forca": "MEDIA",
                "descricao": f"EMA9 cruzou EMA21 para baixo com volume {vol1h:.1f}x",
                "acao": "Potencial SHORT | Envie print para confirmar estrutura"
            })

        # ── SINAL 3: RSI extremo com reversao ──
        rsi_ant = float(p2["rsi"])
        if rsi1h <= 30 and rsi1h > rsi_ant:
            sinais.append({
                "tipo": "RSI SOBREVENDA",
                "forca": "MEDIA",
                "descricao": f"RSI em {rsi1h:.1f} (sobrevenda) com virada para cima",
                "acao": "Potencial LONG | Aguardar confirmacao no 5min"
            })

        if rsi1h >= 70 and rsi1h < rsi_ant:
            sinais.append({
                "tipo": "RSI SOBRECOMPRA",
                "forca": "MEDIA",
                "descricao": f"RSI em {rsi1h:.1f} (sobrecompra) com virada para baixo",
                "acao": "Potencial SHORT | Aguardar confirmacao no 5min"
            })

        # ── SINAL 4: Preco tocando VWAP com volume ──
        dist_vwap = abs(preco - vwap) / vwap
        if dist_vwap <= 0.003 and vol1h >= 1.3:
            direcao_vwap = "LONG (suporte)" if preco > vwap else "SHORT (resistencia)"
            sinais.append({
                "tipo": f"TOQUE VWAP {direcao_vwap}",
                "forca": "MEDIA",
                "descricao": f"Preco ${preco:.4f} tocando VWAP ${vwap:.4f} com volume {vol1h:.1f}x",
                "acao": "Potencial entrada | Envie print para analise"
            })

        # ── SINAL 5: Volume climático (possivel Spring Wyckoff) ──
        if vol1h >= 3.0 and rsi1h < 40:
            sinais.append({
                "tipo": "VOLUME CLIMATICO BAIXA",
                "forca": "FORTE",
                "descricao": f"Volume {vol1h:.1f}x acima da media com RSI {rsi1h:.1f} - possivel capitulacao/Spring",
                "acao": "ATENCAO: Possivel Spring Wyckoff | Envie print urgente para analise"
            })

        if vol1h >= 3.0 and rsi1h > 60:
            sinais.append({
                "tipo": "VOLUME CLIMATICO ALTA",
                "forca": "FORTE",
                "descricao": f"Volume {vol1h:.1f}x acima da media com RSI {rsi1h:.1f} - possivel exaustao",
                "acao": "ATENCAO: Possivel exaustao compradora | Envie print urgente"
            })

        # Adicionar preco atual a todos os sinais
        for s in sinais:
            s["ativo"]  = sym_upper
            s["preco"]  = preco
            s["rsi"]    = round(rsi1h, 1)
            s["volume"] = round(vol1h, 2)

    except Exception as e:
        log.error(f"Scanner [{symbol}]: {e}")

    return sinais

def rodar_scanner():
    log.info("Scanner iniciado!")
    while True:
        try:
            agora = datetime.now(BRT).strftime("%d/%m/%Y %H:%M BRT")
            log.info(f"[{agora}] Iniciando scan de {len(estado['ativos_monitor'])} ativos...")
            todos_sinais = []

            for symbol in estado["ativos_monitor"]:
                sinais = analisar_ativo(symbol)
                for s in sinais:
                    todos_sinais.append(s)
                    cur.execute(
                        "INSERT INTO scanner_log (data_hora,ativo,sinal,detalhes) VALUES (?,?,?,?)",
                        (agora, s["ativo"], s["tipo"], s["descricao"])
                    )
                conn.commit()
                time.sleep(2)

            # Enviar alertas dos sinais fortes primeiro
            fortes  = [s for s in todos_sinais if s["forca"] == "FORTE"]
            medios  = [s for s in todos_sinais if s["forca"] == "MEDIA"]

            if not todos_sinais:
                tg(f"SCANNER LucSharkTrade\n{agora}\nNenhum sinal identificado neste ciclo.\nPrximo scan em {INTERVALO_SCAN//60} min.")
            else:
                # Resumo
                msg_resumo = (
                    f"SCANNER LucSharkTrade\n"
                    f"{agora}\n"
                    f"Sinais encontrados: {len(todos_sinais)}\n"
                    f"Fortes: {len(fortes)} | Medios: {len(medios)}\n"
                )
                tg(msg_resumo)

                # Sinais fortes — alerta individual
                for s in fortes:
                    tg(
                        f"SINAL FORTE\n"
                        f"Ativo: {s['ativo']} | ${s['preco']:,.4f}\n"
                        f"Tipo: {s['tipo']}\n"
                        f"RSI: {s['rsi']} | Volume: {s['volume']}x\n"
                        f"Detalhe: {s['descricao']}\n"
                        f"Acao: {s['acao']}\n"
                        f"Envie o print para analise completa!"
                    )
                    time.sleep(1)

                # Sinais medios — resumo agrupado
                if medios:
                    linhas = ["SINAIS MEDIOS:"]
                    for s in medios:
                        linhas.append(f"• {s['ativo']}: {s['tipo']} | ${s['preco']:,.4f}")
                    tg("\n".join(linhas))

            estado["ultimo_scan"] = agora
            log.info(f"Scan concluido: {len(todos_sinais)} sinais")
            time.sleep(INTERVALO_SCAN)

        except Exception as e:
            log.error(f"Scanner erro: {e}")
            time.sleep(60)

# ── Monitor de precos ─────────────────────────────────────────
def rr(entrada, stop, alvo, direcao):
    try:
        risco = abs(float(entrada)-float(stop))
        lucro = abs(float(alvo)-float(entrada))
        return round(lucro/risco,2) if risco > 0 else 0
    except: return 0

def monitorar():
    log.info("Monitor iniciado!")
    while estado["monitorando"]:
        try:
            rows = cur.execute(
                "SELECT trade_id,ativo,direcao,entrada,stop,alvo1,alvo2,alvo3,alerta_enviado "
                "FROM trades WHERE resultado=\"MONITORANDO\""
            ).fetchall()
            agora = datetime.now(BRT).strftime("%H:%M BRT")
            log.info(f"[{agora}] Monitor: {len(rows)} trade(s)")
            for tid,ativo,direcao,entrada,stop,a1,a2,a3,ja_alertou in rows:
                entrada=float(entrada); stop=float(stop)
                a1=float(a1); a2=float(a2); a3=float(a3)
                preco = get_preco(ativo)
                if not preco: continue
                dist = abs(preco-entrada)/entrada
                log.info(f"  {ativo} {direcao}: ${preco:,.4f} | dist {dist*100:.2f}%")
                rr1=rr(entrada,stop,a1,direcao)
                rr2=rr(entrada,stop,a2,direcao)
                rr3=rr(entrada,stop,a3,direcao)
                if dist <= TOLERANCIA_PCT and not ja_alertou:
                    cur.execute("UPDATE trades SET alerta_enviado=1 WHERE trade_id=?",(tid,))
                    conn.commit()
                    tg(f"ALERTA DE ENTRADA\nLucSharkTrade | ID: {tid}\nAtivo: {ativo} | {direcao}\nPreco: ${preco:,.4f} | Entrada: ${entrada:,.4f}\nDist: {dist*100:.2f}%\nStop: ${stop:,.4f}\nA1 (RR {rr1}:1): ${a1:,.4f} -> 25%\nA2 (RR {rr2}:1): ${a2:,.4f} -> 50%\nA3 (RR {rr3}:1): ${a3:,.4f} -> 80%\n/resultado {ativo} WIN A1 ou /resultado {ativo} LOSS")
                    continue
                if not ja_alertou: continue
                stop_hit = (direcao=="LONG" and preco<=stop) or (direcao=="SHORT" and preco>=stop)
                if stop_hit:
                    cur.execute("UPDATE trades SET resultado=\"STOP\",preco_saida=? WHERE trade_id=?",(preco,tid))
                    conn.commit()
                    tg(f"STOP ATINGIDO\nID: {tid} | {ativo} {direcao}\nEntrada: ${entrada:,.4f} | Saida: ${preco:,.4f}\n/resultado {ativo} LOSS")
                    continue
                for val,nome,pct in [(a1,"A1",25),(a2,"A2",50),(a3,"A3",80)]:
                    hit=(direcao=="LONG" and preco>=val) or (direcao=="SHORT" and preco<=val)
                    if hit:
                        if nome=="A3":
                            cur.execute("UPDATE trades SET resultado=\"WIN_A3\",preco_saida=? WHERE trade_id=?",(preco,tid))
                            conn.commit()
                        tg(f"{nome} ATINGIDO!\nID: {tid} | {ativo} {direcao}\nPreco: ${preco:,.4f}\nRealizar {pct}%\n/resultado {ativo} WIN {nome}")
                        break
            time.sleep(INTERVALO_MON)
        except Exception as e:
            log.error(f"Monitor: {e}")
            time.sleep(30)

# ── Comandos Telegram ─────────────────────────────────────────
def novo_tid():
    estado["trade_counter"] += 1
    return f"LS{estado['trade_counter']:04d}"

def processar_cmd():
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset":estado["ultimo_update_id"]+1,"timeout":5},
            timeout=10)
        if r.status_code != 200: return
        for upd in r.json().get("result",[]):
            estado["ultimo_update_id"] = upd["update_id"]
            txt = upd.get("message",{}).get("text","").strip()
            if not txt: continue
            p = txt.split(); cmd = p[0].lower()
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
                    rr1=rr(entrada,stop,a1,direcao)
                    if rr1 < 1.0:
                        tg(f"Trade DESCARTADO! A1 RR {rr1}:1 (minimo 1:1).")
                        continue
                    tid = novo_tid()
                    agora = datetime.now(BRT).strftime("%d/%m/%Y %H:%M BRT")
                    cur.execute("INSERT OR REPLACE INTO trades (trade_id,data_hora,ativo,direcao,tf_ctx,tf_ent,entrada,stop,alvo1,alvo2,alvo3) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (tid,agora,ativo,direcao,tf_ctx,tf_ent,entrada,stop,a1,a2,a3))
                    conn.commit()
                    preco=get_preco(ativo)
                    ps=f"${preco:,.4f}" if preco else "Indisponivel"
                    ds=f"{abs(preco-entrada)/entrada*100:.2f}%" if preco else "N/A"
                    rr2=rr(entrada,stop,a2,direcao); rr3=rr(entrada,stop,a3,direcao)
                    tg(f"TRADE CADASTRADO\nID: {tid} | {ativo} {direcao} | {tf_ctx}/{tf_ent}\nPreco: {ps} ({ds} da entrada)\nEntrada: ${entrada:,.4f} | Stop: ${stop:,.4f}\nA1 (RR {rr1}:1): ${a1:,.4f}\nA2 (RR {rr2}:1): ${a2:,.4f}\nA3 (RR {rr3}:1): ${a3:,.4f}\nMonitorando a cada {INTERVALO_MON//60} min")
                except Exception as e:
                    tg(f"Erro: {e}")

            elif cmd == "/scan":
                tg("Iniciando scan manual...")
                threading.Thread(target=rodar_scanner, daemon=True).start()

            elif cmd == "/trades":
                rows=cur.execute("SELECT trade_id,ativo,direcao,entrada,resultado FROM trades ORDER BY id DESC LIMIT 15").fetchall()
                if not rows: tg("Nenhum trade.")
                else:
                    linhas=["Trades:"]
                    for tid,ativo,direcao,entrada,res in rows:
                        linhas.append(f"{tid} | {ativo} {direcao} ${float(entrada):,.2f} | {res}")
                    tg("\n".join(linhas))

            elif cmd == "/cancelar" and len(p)>=2:
                ativo=p[1].upper()
                cur.execute("UPDATE trades SET resultado=\"CANCELADO\" WHERE ativo=? AND resultado=\"MONITORANDO\"",(ativo,))
                conn.commit(); tg(f"{ativo}: cancelado.")

            elif cmd == "/resultado" and len(p)>=3:
                ativo=p[1].upper(); res=p[2].upper()
                nivel=p[3].upper() if len(p)>=4 else ""
                res_final=f"WIN_{nivel}" if res=="WIN" and nivel else res
                cur.execute("UPDATE trades SET resultado=? WHERE ativo=? AND resultado=\"MONITORANDO\"",(res_final,ativo))
                conn.commit(); tg(f"{ativo}: {res_final} registrado!")

            elif cmd == "/relatorio":
                total=cur.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
                wins=cur.execute("SELECT COUNT(*) FROM trades WHERE resultado LIKE \"WIN%\"").fetchone()[0]
                losses=cur.execute("SELECT COUNT(*) FROM trades WHERE resultado IN (\"STOP\",\"LOSS\")").fetchone()[0]
                mon=cur.execute("SELECT COUNT(*) FROM trades WHERE resultado=\"MONITORANDO\"").fetchone()[0]
                wr=wins/(wins+losses)*100 if (wins+losses)>0 else 0
                sinais_hoje=cur.execute("SELECT COUNT(*) FROM scanner_log WHERE data_hora LIKE ?",
                    (datetime.now(BRT).strftime("%d/%m/%Y")+"%" ,)).fetchone()[0]
                tg(f"RELATORIO LucSharkTrade\n{datetime.now(BRT).strftime('%d/%m/%Y %H:%M BRT')}\nTotal: {total} | Wins: {wins} | Losses: {losses} | Monitor: {mon}\nWin Rate: {wr:.1f}%\nSinais hoje: {sinais_hoje}\nCapital: ${CAPITAL_INICIAL:,.2f}")

            elif cmd == "/status":
                mon=cur.execute("SELECT COUNT(*) FROM trades WHERE resultado=\"MONITORANDO\"").fetchone()[0]
                total=cur.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
                tg(f"Status LucSharkTrade\nMonitorando: {mon} trades\nTotal: {total} trades\nUltimo scan: {estado.get('ultimo_scan','Nunca')}\nAtivos scanner: {len(estado['ativos_monitor'])}\nProximo scan: {INTERVALO_SCAN//60} min\n{datetime.now(BRT).strftime('%d/%m/%Y %H:%M BRT')}")

            elif cmd == "/ajuda":
                tg("Comandos LucSharkTrade:\n/trade ATIVO DIR ENTRADA STOP A1 A2 A3\n/scan — rodar scanner agora\n/trades — ver trades\n/cancelar ATIVO\n/resultado ATIVO WIN A1\n/resultado ATIVO LOSS\n/relatorio\n/status\n/ajuda")

    except Exception as e:
        log.error(f"Cmd: {e}")

# ── Flask ─────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def index():
    mon=cur.execute("SELECT COUNT(*) FROM trades WHERE resultado=\"MONITORANDO\"").fetchone()[0]
    total=cur.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    sinais=cur.execute("SELECT COUNT(*) FROM scanner_log").fetchone()[0]
    return jsonify({"status":"online","monitorando":mon,"total_trades":total,
        "sinais_scanner":sinais,"ultimo_scan":estado.get("ultimo_scan"),
        "horario":datetime.now(BRT).strftime("%d/%m/%Y %H:%M BRT")})

@app.route("/sinais")
def ver_sinais():
    rows=cur.execute("SELECT * FROM scanner_log ORDER BY id DESC LIMIT 50").fetchall()
    cols=[d[0] for d in cur.description]
    return jsonify([dict(zip(cols,r)) for r in rows])

# ── Iniciar ───────────────────────────────────────────────────
def cmd_loop():
    while True:
        processar_cmd()
        time.sleep(15)

if __name__ == "__main__":
    agora=datetime.now(BRT).strftime("%d/%m/%Y %H:%M BRT")
    mon=cur.execute("SELECT COUNT(*) FROM trades WHERE resultado=\"MONITORANDO\"").fetchone()[0]
    tg(f"LucSharkTrade v9 ONLINE!\nMonitor: {mon} trades\nScanner: {len(ATIVOS)} ativos\nScan a cada {INTERVALO_SCAN//60} min\n{agora}\n/ajuda para comandos")
    log.info("Bot v9 iniciado!")
    threading.Thread(target=monitorar,  daemon=True).start()
    threading.Thread(target=rodar_scanner, daemon=True).start()
    threading.Thread(target=cmd_loop,   daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
