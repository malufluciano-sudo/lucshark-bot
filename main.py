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
MIN_VOLUME_24H   = float(os.environ.get("MIN_VOLUME_24H", "100000"))
PORT             = int(os.environ.get("PORT", "8080"))

BRT = timezone(timedelta(hours=-3))
DB  = "/app/data/lucshark.db"
os.makedirs("/app/data", exist_ok=True)

PRECO_MAP_CG = {
    "BTCUSDT": "bitcoin", "ETHUSDT": "ethereum", "BNBUSDT": "binancecoin",
    "XRPUSDT": "ripple",  "SOLUSDT": "solana",   "LINKUSDT": "chainlink",
    "LTCUSDT": "litecoin","HYPEUSDT":"hyperliquid","ASTRUSDT":"astar-network",
    "XAGUSD":  "silver",  "XAUUSD":  "gold",
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
    data_hora TEXT, ativo TEXT, sinal TEXT, detalhes TEXT
);
CREATE TABLE IF NOT EXISTS ativos_lbank (
    symbol TEXT PRIMARY KEY, vol24h REAL, ativo INTEGER DEFAULT 1,
    ultima_atualizacao TEXT
);
""")
conn.commit()

estado = {
    "ultimo_update_id": 0,
    "trade_counter": cur.execute("SELECT COUNT(*) FROM trades").fetchone()[0],
    "monitorando": True,
    "ultimo_scan": None,
    "total_ativos": 0,
    "ativos_scan": []
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

def get_preco_cg(symbol):
    cg_id = PRECO_MAP_CG.get(symbol.upper())
    if not cg_id: return None
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
            params={"ids": cg_id, "vs_currencies": "usd"}, timeout=8)
        if r.status_code == 200:
            return r.json().get(cg_id, {}).get("usd")
    except: pass
    return None

def get_preco_lbank(symbol):
    try:
        r = requests.get("https://api.lbkex.com/v1/ticker.do",
            params={"symbol": symbol}, timeout=8)
        if r.status_code == 200:
            v = float(r.json().get("ticker", {}).get("latest", 0))
            return v if v > 0 else None
    except: pass
    return None

def get_preco(symbol):
    sym = symbol.upper().replace(".P","")
    p = get_preco_cg(sym)
    if p: return p
    lb_sym = sym.lower().replace("usdt","_usdt").replace("usd","_usd") if "_" not in sym else sym.lower()
    return get_preco_lbank(lb_sym)

# ── Buscar TODOS os ativos da LBank ──────────────────────────
def buscar_ativos_lbank():
    log.info("Buscando todos os ativos da LBank...")
    try:
        # Buscar todos os pares
        r = requests.get("https://api.lbkex.com/v1/currencyPairs.do", timeout=15)
        if r.status_code != 200:
            log.error(f"Erro ao buscar pares: {r.status_code}")
            return []
        todos_pares = r.json()
        log.info(f"Total de pares encontrados: {len(todos_pares)}")

        # Buscar ticker de todos para filtrar por volume
        r2 = requests.get("https://api.lbkex.com/v1/ticker.do",
            params={"symbol": "all"}, timeout=15)

        pares_com_volume = []
        if r2.status_code == 200:
            tickers = r2.json()
            for item in tickers:
                symbol = item.get("symbol","")
                try:
                    vol = float(item.get("ticker",{}).get("vol",0))
                    preco = float(item.get("ticker",{}).get("latest",0))
                    if vol >= MIN_VOLUME_24H and preco > 0:
                        pares_com_volume.append({"symbol": symbol, "vol24h": vol})
                        cur.execute("""
                            INSERT OR REPLACE INTO ativos_lbank (symbol, vol24h, ultima_atualizacao)
                            VALUES (?,?,?)
                        """, (symbol, vol, datetime.now(BRT).strftime("%d/%m/%Y %H:%M")))
                except: continue
            conn.commit()
        else:
            pares_com_volume = [{"symbol": p, "vol24h": 0} for p in todos_pares]

        log.info(f"Ativos com volume > ${MIN_VOLUME_24H:,.0f}: {len(pares_com_volume)}")
        return pares_com_volume

    except Exception as e:
        log.error(f"Erro buscar ativos: {e}")
        return []

# ── Candles e Indicadores ─────────────────────────────────────
def get_klines(symbol, period, size=100):
    try:
        r = requests.get("https://api.lbkex.com/v1/kline.do",
            params={"symbol":symbol,"size":size,"type":period}, timeout=10)
        if r.status_code != 200: return None
        data = r.json()
        if not isinstance(data,list) or len(data)<20: return None
        df = pd.DataFrame(data, columns=["timestamp","open","close","high","low","volume"])
        for c in ["open","close","high","low","volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.dropna()
    except: return None

def calcular(df):
    if df is None or len(df)<20: return None
    df = df.copy()
    df["ema9"]  = df["close"].ewm(span=9,  adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    d = df["close"].diff()
    g = d.clip(lower=0).ewm(com=13, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(com=13, adjust=False).mean()
    df["rsi"]     = 100-(100/(1+g/l))
    df["vwap"]    = (df["close"]*df["volume"]).cumsum()/df["volume"].cumsum()
    df["vol_med"] = df["volume"].rolling(20).mean()
    df["vol_ratio"]= df["volume"]/df["vol_med"].replace(0, np.nan)
    df["bb_mid"]  = df["close"].rolling(20).mean()
    std = df["close"].rolling(20).std()
    df["bb_up"]   = df["bb_mid"]+2*std
    df["bb_dn"]   = df["bb_mid"]-2*std
    h,lo,c = df["high"],df["low"],df["close"].shift(1)
    tr = pd.concat([h-lo,(h-c).abs(),(lo-c).abs()],axis=1).max(axis=1)
    df["atr"] = tr.ewm(com=13,adjust=False).mean()
    return df

# ── Scanner de sinais ─────────────────────────────────────────
def analisar_ativo(symbol):
    sinais = []
    try:
        df1h = calcular(get_klines(symbol, "1hour", 100))
        if df1h is None or len(df1h)<20: return []

        u1  = df1h.iloc[-1]
        p2  = df1h.iloc[-2]
        preco   = float(u1["close"])
        rsi1h   = float(u1["rsi"])
        vol_r   = float(u1["vol_ratio"]) if not pd.isna(u1["vol_ratio"]) else 1.0
        ema9    = float(u1["ema9"])
        ema21   = float(u1["ema21"])
        vwap    = float(u1["vwap"])
        bb_up   = float(u1["bb_up"])
        bb_dn   = float(u1["bb_dn"])
        ema9_p  = float(p2["ema9"])
        ema21_p = float(p2["ema21"])
        rsi_p   = float(p2["rsi"])

        max20 = float(df1h["high"].tail(20).max())
        min20 = float(df1h["low"].tail(20).min())
        sym_up = symbol.replace("_usdt","usdt").replace("_usd","usd").upper()

        # SINAL 1: Tuk Tuk — rompimento com volume
        if preco >= max20*0.998 and vol_r >= 1.5:
            sinais.append({"tipo":"TUK TUK LONG","forca":"FORTE",
                "desc":f"Rompeu maxima {max20:.6f} | Vol {vol_r:.1f}x",
                "acao":"Aguardar pullback para LONG"})

        if preco <= min20*1.002 and vol_r >= 1.5:
            sinais.append({"tipo":"TUK TUK SHORT","forca":"FORTE",
                "desc":f"Rompeu minima {min20:.6f} | Vol {vol_r:.1f}x",
                "acao":"Aguardar pullback para SHORT"})

        # SINAL 2: EMA Cross
        if ema9_p < ema21_p and ema9 > ema21 and vol_r >= 1.2:
            sinais.append({"tipo":"EMA CROSS LONG","forca":"MEDIA",
                "desc":f"EMA9 cruzou EMA21 para cima | Vol {vol_r:.1f}x",
                "acao":"Potencial LONG"})

        if ema9_p > ema21_p and ema9 < ema21 and vol_r >= 1.2:
            sinais.append({"tipo":"EMA CROSS SHORT","forca":"MEDIA",
                "desc":f"EMA9 cruzou EMA21 para baixo | Vol {vol_r:.1f}x",
                "acao":"Potencial SHORT"})

        # SINAL 3: RSI extremo
        if rsi1h <= 28 and rsi1h > rsi_p:
            sinais.append({"tipo":"RSI SOBREVENDA","forca":"MEDIA",
                "desc":f"RSI {rsi1h:.1f} revertendo para cima",
                "acao":"Potencial LONG no 5min"})

        if rsi1h >= 72 and rsi1h < rsi_p:
            sinais.append({"tipo":"RSI SOBRECOMPRA","forca":"MEDIA",
                "desc":f"RSI {rsi1h:.1f} revertendo para baixo",
                "acao":"Potencial SHORT no 5min"})

        # SINAL 4: Toque VWAP
        dist_vwap = abs(preco-vwap)/vwap if vwap > 0 else 1
        if dist_vwap <= 0.003 and vol_r >= 1.2:
            dir_vwap = "LONG" if preco >= vwap else "SHORT"
            sinais.append({"tipo":f"TOQUE VWAP {dir_vwap}","forca":"MEDIA",
                "desc":f"Preco {preco:.6f} tocando VWAP {vwap:.6f} | Vol {vol_r:.1f}x",
                "acao":f"Potencial {dir_vwap}"})

        # SINAL 5: Volume climatico (Spring/Exaustao)
        if vol_r >= 3.0 and rsi1h < 35:
            sinais.append({"tipo":"VOLUME CLIMATICO BAIXA","forca":"FORTE",
                "desc":f"Vol {vol_r:.1f}x | RSI {rsi1h:.1f} — possivel Spring Wyckoff",
                "acao":"URGENTE: Possivel capitulacao/Spring"})

        if vol_r >= 3.0 and rsi1h > 65:
            sinais.append({"tipo":"VOLUME CLIMATICO ALTA","forca":"FORTE",
                "desc":f"Vol {vol_r:.1f}x | RSI {rsi1h:.1f} — possivel exaustao",
                "acao":"URGENTE: Possivel exaustao compradora"})

        # SINAL 6: Bollinger Squeeze (volatilidade comprimida)
        bb_width = (bb_up - bb_dn) / float(u1["bb_mid"]) if float(u1["bb_mid"]) > 0 else 1
        bb_hist  = [(float(df1h["bb_up"].iloc[i])-float(df1h["bb_dn"].iloc[i]))/float(df1h["bb_mid"].iloc[i])
                    for i in range(-20,-1) if float(df1h["bb_mid"].iloc[i]) > 0]
        if bb_hist and bb_width <= min(bb_hist)*1.1:
            sinais.append({"tipo":"BOLLINGER SQUEEZE","forca":"MEDIA",
                "desc":f"Volatilidade comprimida — movimento iminente",
                "acao":"Monitorar rompimento com volume"})

        for s in sinais:
            s["ativo"] = sym_up
            s["preco"] = preco
            s["rsi"]   = round(rsi1h,1)
            s["vol_r"] = round(vol_r,2)

    except Exception as e:
        log.debug(f"Scan [{symbol}]: {e}")
    return sinais

# ── Runner do scanner ─────────────────────────────────────────
def rodar_scanner():
    log.info("Scanner iniciado!")
    while True:
        try:
            agora = datetime.now(BRT).strftime("%d/%m/%Y %H:%M BRT")
            log.info(f"[{agora}] Buscando ativos da LBank...")

            # Buscar todos os ativos com volume relevante
            ativos = buscar_ativos_lbank()
            if not ativos:
                log.warning("Sem ativos — usando lista anterior")
                ativos = [{"symbol":s} for s in estado["ativos_scan"]] if estado["ativos_scan"] else []

            estado["ativos_scan"]  = [a["symbol"] for a in ativos]
            estado["total_ativos"] = len(ativos)

            tg(f"SCANNER LucSharkTrade\n{agora}\nAnalisando {len(ativos)} ativos...\nAguarde os sinais.")

            todos_sinais = []
            for i, ativo in enumerate(ativos):
                symbol = ativo["symbol"]
                sinais = analisar_ativo(symbol)
                for s in sinais:
                    todos_sinais.append(s)
                    cur.execute("INSERT INTO scanner_log (data_hora,ativo,sinal,detalhes) VALUES (?,?,?,?)",
                        (agora, s["ativo"], s["tipo"], s["desc"]))
                conn.commit()
                # Rate limit — pausa a cada 10 ativos
                if (i+1) % 10 == 0:
                    time.sleep(2)

            # Separar por força
            fortes = [s for s in todos_sinais if s["forca"]=="FORTE"]
            medios = [s for s in todos_sinais if s["forca"]=="MEDIA"]

            # Resumo
            tg(f"SCAN CONCLUIDO\n{agora}\nAtivos analisados: {len(ativos)}\nSinais FORTES: {len(fortes)}\nSinais MEDIOS: {len(medios)}\nProximo scan em {INTERVALO_SCAN//60} min")

            # Alertas fortes individualmente
            for s in fortes:
                tg(
                    f"SINAL FORTE — {s['tipo']}\n"
                    f"Ativo: {s['ativo']} | ${s['preco']:,.6f}\n"
                    f"RSI: {s['rsi']} | Volume: {s['vol_r']}x\n"
                    f"Detalhe: {s['desc']}\n"
                    f"Acao: {s['acao']}\n"
                    f"Envie o print para analise!"
                )
                time.sleep(1)

            # Medios agrupados (max 10 por mensagem)
            if medios:
                chunks = [medios[i:i+10] for i in range(0,len(medios),10)]
                for chunk in chunks:
                    linhas = ["SINAIS MEDIOS:"]
                    for s in chunk:
                        linhas.append(f"• {s['ativo']}: {s['tipo']} | ${s['preco']:,.6f}")
                    tg("\n".join(linhas))
                    time.sleep(1)

            estado["ultimo_scan"] = agora
            log.info(f"Scan concluido: {len(todos_sinais)} sinais em {len(ativos)} ativos")
            time.sleep(INTERVALO_SCAN)

        except Exception as e:
            log.error(f"Scanner: {e}")
            time.sleep(60)

# ── Monitor de trades ─────────────────────────────────────────
def rr(entrada, stop, alvo, direcao):
    try:
        risco = abs(float(entrada)-float(stop))
        lucro = abs(float(alvo)-float(entrada))
        return round(lucro/risco,2) if risco>0 else 0
    except: return 0

def monitorar():
    log.info("Monitor iniciado!")
    while estado["monitorando"]:
        try:
            rows = cur.execute(
                'SELECT trade_id,ativo,direcao,entrada,stop,alvo1,alvo2,alvo3,alerta_enviado '
                'FROM trades WHERE resultado="MONITORANDO"'
            ).fetchall()
            log.info(f"Monitor: {len(rows)} trade(s)")
            for tid,ativo,direcao,entrada,stop,a1,a2,a3,ja_alertou in rows:
                entrada=float(entrada); stop=float(stop)
                a1=float(a1); a2=float(a2); a3=float(a3)
                preco = get_preco(ativo)
                if not preco: continue
                dist = abs(preco-entrada)/entrada
                rr1=rr(entrada,stop,a1,direcao)
                rr2=rr(entrada,stop,a2,direcao)
                rr3=rr(entrada,stop,a3,direcao)
                if dist<=TOLERANCIA_PCT and not ja_alertou:
                    cur.execute("UPDATE trades SET alerta_enviado=1 WHERE trade_id=?",(tid,))
                    conn.commit()
                    tg(f"ALERTA DE ENTRADA\nID: {tid} | {ativo} {direcao}\nPreco: ${preco:,.6f} | Entrada: ${entrada:,.6f}\nDist: {dist*100:.2f}%\nStop: ${stop:,.6f}\nA1 (RR {rr1}:1): ${a1:,.6f} -> 25%\nA2 (RR {rr2}:1): ${a2:,.6f} -> 50%\nA3 (RR {rr3}:1): ${a3:,.6f} -> 80%\n/resultado {ativo} WIN A1 ou /resultado {ativo} LOSS")
                    continue
                if not ja_alertou: continue
                stop_hit=(direcao=="LONG" and preco<=stop) or (direcao=="SHORT" and preco>=stop)
                if stop_hit:
                    cur.execute('UPDATE trades SET resultado="STOP",preco_saida=? WHERE trade_id=?',(preco,tid))
                    conn.commit()
                    tg(f"STOP ATINGIDO\nID: {tid} | {ativo} {direcao}\nEntrada: ${entrada:,.6f} | Saida: ${preco:,.6f}\n/resultado {ativo} LOSS")
                    continue
                for val,nome,pct in [(a1,"A1",25),(a2,"A2",50),(a3,"A3",80)]:
                    hit=(direcao=="LONG" and preco>=val) or (direcao=="SHORT" and preco<=val)
                    if hit:
                        if nome=="A3":
                            cur.execute('UPDATE trades SET resultado="WIN_A3",preco_saida=? WHERE trade_id=?',(preco,tid))
                            conn.commit()
                        tg(f"{nome} ATINGIDO!\nID: {tid} | {ativo} {direcao}\nPreco: ${preco:,.6f}\nRealizar {pct}%\n/resultado {ativo} WIN {nome}")
                        break
            time.sleep(INTERVALO_MON)
        except Exception as e:
            log.error(f"Monitor: {e}")
            time.sleep(30)

# ── Comandos ──────────────────────────────────────────────────
def novo_tid():
    estado["trade_counter"] += 1
    return f"LS{estado['trade_counter']:04d}"

def processar_cmd():
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset":estado["ultimo_update_id"]+1,"timeout":5}, timeout=10)
        if r.status_code!=200: return
        for upd in r.json().get("result",[]):
            estado["ultimo_update_id"] = upd["update_id"]
            txt = upd.get("message",{}).get("text","").strip()
            if not txt: continue
            p=txt.split(); cmd=p[0].lower()
            log.info(f"CMD: {txt}")

            if cmd=="/trade":
                if len(p)<8:
                    tg("Formato:\n/trade ATIVO DIR ENTRADA STOP A1 A2 A3\nEx: /trade BTCUSDT LONG 84000 82000 86000 88000 90000")
                    continue
                try:
                    ativo=p[1].upper(); direcao=p[2].upper()
                    entrada=float(p[3]); stop=float(p[4])
                    a1=float(p[5]); a2=float(p[6]); a3=float(p[7])
                    tf_ctx=p[8].upper() if len(p)>8 else "1H"
                    tf_ent=p[9] if len(p)>9 else "5min"
                    rr1=rr(entrada,stop,a1,direcao)
                    if rr1<1.0:
                        tg(f"Trade DESCARTADO! A1 RR {rr1}:1 (minimo 1:1).")
                        continue
                    tid=novo_tid()
                    agora=datetime.now(BRT).strftime("%d/%m/%Y %H:%M BRT")
                    cur.execute("INSERT OR REPLACE INTO trades (trade_id,data_hora,ativo,direcao,tf_ctx,tf_ent,entrada,stop,alvo1,alvo2,alvo3) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (tid,agora,ativo,direcao,tf_ctx,tf_ent,entrada,stop,a1,a2,a3))
                    conn.commit()
                    preco=get_preco(ativo)
                    ps=f"${preco:,.6f}" if preco else "Indisponivel"
                    ds=f"{abs(preco-entrada)/entrada*100:.2f}%" if preco else "N/A"
                    rr2=rr(entrada,stop,a2,direcao); rr3=rr(entrada,stop,a3,direcao)
                    tg(f"TRADE CADASTRADO\nID: {tid} | {ativo} {direcao} | {tf_ctx}/{tf_ent}\nPreco: {ps} ({ds})\nEntrada: ${entrada:,.6f} | Stop: ${stop:,.6f}\nA1 (RR {rr1}:1): ${a1:,.6f}\nA2 (RR {rr2}:1): ${a2:,.6f}\nA3 (RR {rr3}:1): ${a3:,.6f}\nMonitorando a cada {INTERVALO_MON//60} min")
                except Exception as e:
                    tg(f"Erro: {e}")

            elif cmd=="/scan":
                tg("Iniciando scan manual...")
                threading.Thread(target=rodar_scanner, daemon=True).start()

            elif cmd=="/ativos":
                total=cur.execute("SELECT COUNT(*) FROM ativos_lbank WHERE ativo=1").fetchone()[0]
                top=cur.execute("SELECT symbol,vol24h FROM ativos_lbank ORDER BY vol24h DESC LIMIT 10").fetchall()
                linhas=[f"Ativos monitorados: {total}","","Top 10 por volume:"]
                for sym,vol in top:
                    linhas.append(f"• {sym}: ${vol:,.0f}")
                tg("\n".join(linhas))

            elif cmd=="/trades":
                rows=cur.execute("SELECT trade_id,ativo,direcao,entrada,resultado FROM trades ORDER BY id DESC LIMIT 15").fetchall()
                if not rows: tg("Nenhum trade.")
                else:
                    linhas=["Trades:"]
                    for tid,ativo,direcao,entrada,res in rows:
                        linhas.append(f"{tid} | {ativo} {direcao} ${float(entrada):,.4f} | {res}")
                    tg("\n".join(linhas))

            elif cmd=="/cancelar" and len(p)>=2:
                ativo=p[1].upper()
                cur.execute('UPDATE trades SET resultado="CANCELADO" WHERE ativo=? AND resultado="MONITORANDO"',(ativo,))
                conn.commit(); tg(f"{ativo}: cancelado.")

            elif cmd=="/resultado" and len(p)>=3:
                ativo=p[1].upper(); res=p[2].upper()
                nivel=p[3].upper() if len(p)>=4 else ""
                res_final=f"WIN_{nivel}" if res=="WIN" and nivel else res
                cur.execute('UPDATE trades SET resultado=? WHERE ativo=? AND resultado="MONITORANDO"',(res_final,ativo))
                conn.commit(); tg(f"{ativo}: {res_final} registrado!")

            elif cmd=="/relatorio":
                total=cur.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
                wins=cur.execute('SELECT COUNT(*) FROM trades WHERE resultado LIKE "WIN%"').fetchone()[0]
                losses=cur.execute('SELECT COUNT(*) FROM trades WHERE resultado IN ("STOP","LOSS")').fetchone()[0]
                mon=cur.execute('SELECT COUNT(*) FROM trades WHERE resultado="MONITORANDO"').fetchone()[0]
                wr=wins/(wins+losses)*100 if (wins+losses)>0 else 0
                sinais_hoje=cur.execute("SELECT COUNT(*) FROM scanner_log WHERE data_hora LIKE ?",
                    (datetime.now(BRT).strftime("%d/%m/%Y")+"%",)).fetchone()[0]
                tg(f"RELATORIO LucSharkTrade\n{datetime.now(BRT).strftime('%d/%m/%Y %H:%M BRT')}\nTotal: {total} | Wins: {wins} | Losses: {losses} | Monitor: {mon}\nWin Rate: {wr:.1f}%\nSinais hoje: {sinais_hoje}\nAtivos scanner: {estado['total_ativos']}")

            elif cmd=="/status":
                mon=cur.execute('SELECT COUNT(*) FROM trades WHERE resultado="MONITORANDO"').fetchone()[0]
                total=cur.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
                tg(f"Status LucSharkTrade v10\nMonitorando: {mon} trades\nTotal: {total} trades\nAtivos scanner: {estado['total_ativos']}\nUltimo scan: {estado.get('ultimo_scan','Nunca')}\nProximo: {INTERVALO_SCAN//60} min\n{datetime.now(BRT).strftime('%d/%m/%Y %H:%M BRT')}")

            elif cmd=="/ajuda":
                tg("Comandos LucSharkTrade v10:\n/trade ATIVO DIR ENTRADA STOP A1 A2 A3\n/scan — scanner manual agora\n/ativos — ver ativos monitorados\n/trades — ver trades\n/cancelar ATIVO\n/resultado ATIVO WIN A1\n/resultado ATIVO LOSS\n/relatorio\n/status\n/ajuda")

    except Exception as e:
        log.error(f"Cmd: {e}")

# ── Flask ─────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def index():
    mon=cur.execute('SELECT COUNT(*) FROM trades WHERE resultado="MONITORANDO"').fetchone()[0]
    total=cur.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    sinais=cur.execute("SELECT COUNT(*) FROM scanner_log").fetchone()[0]
    return jsonify({"status":"online","monitorando":mon,"total_trades":total,
        "sinais_scanner":sinais,"ativos_scanner":estado["total_ativos"],
        "ultimo_scan":estado.get("ultimo_scan"),
        "horario":datetime.now(BRT).strftime("%d/%m/%Y %H:%M BRT")})

@app.route("/sinais")
def ver_sinais():
    rows=cur.execute("SELECT * FROM scanner_log ORDER BY id DESC LIMIT 100").fetchall()
    cols=[d[0] for d in cur.description]
    return jsonify([dict(zip(cols,r)) for r in rows])

@app.route("/ativos")
def ver_ativos():
    rows=cur.execute("SELECT * FROM ativos_lbank ORDER BY vol24h DESC LIMIT 100").fetchall()
    cols=[d[0] for d in cur.description]
    return jsonify([dict(zip(cols,r)) for r in rows])

# ── Iniciar ───────────────────────────────────────────────────
def cmd_loop():
    while True:
        processar_cmd()
        time.sleep(15)

if __name__=="__main__":
    agora=datetime.now(BRT).strftime("%d/%m/%Y %H:%M BRT")
    mon=cur.execute('SELECT COUNT(*) FROM trades WHERE resultado="MONITORANDO"').fetchone()[0]
    tg(f"LucSharkTrade v10 ONLINE!\nMonitor: {mon} trades\nScanner: TODOS os ativos LBank\nFiltro volume: >${MIN_VOLUME_24H:,.0f}\nScan a cada {INTERVALO_SCAN//60} min\n{agora}\n/ajuda para comandos")
    log.info("Bot v10 iniciado!")
    threading.Thread(target=monitorar,    daemon=True).start()
    threading.Thread(target=rodar_scanner,daemon=True).start()
    threading.Thread(target=cmd_loop,     daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
