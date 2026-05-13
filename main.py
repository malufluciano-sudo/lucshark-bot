import os
import time
import requests
import logging
import sqlite3
import threading
import json
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify

# Flask app para expor dados ao dashboard
flask_app = Flask(__name__)

# ─────────────────────────────────────────────
# CONFIGURAÇÃO
# ─────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CAPITAL_INICIAL  = float(os.environ.get("CAPITAL_INICIAL", "1000"))
TOLERANCIA_PCT   = float(os.environ.get("TOLERANCIA_PCT", "0.005"))
INTERVALO_SEG    = int(os.environ.get("INTERVALO_SEG", "30"))
INTERVALO_SCAN   = int(os.environ.get("INTERVALO_SCAN", "3600"))
MIN_VOLUME_24H   = float(os.environ.get("MIN_VOLUME_24H", "100000"))
COINALYZE_KEY    = os.environ.get("COINALYZE_KEY", "376762b9-d136-4457-a192-9cd0a7865d43")
COINALYZE_BASE   = "https://api.coinalyze.net/v1"

# ── Parâmetros do scanner ──
TIMEFRAME_SCAN     = "minute15"
CANDLES_ANALISE    = 50
MULT_FORTE         = 1.8
MULT_MEDIO         = 1.3
MULT_ALERTA        = 1.1
MIN_CANDLES_RANGE  = 5
RSI_SOBREVENDA     = 32
RSI_SOBRECOMPRA    = 68
OFFSET_BRT         = -3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# BANCO DE DADOS
# ─────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ativo TEXT, direcao TEXT,
            entrada REAL, stop REAL,
            a1 REAL, a2 REAL, a3 REAL,
            tf_ctx TEXT, tf_ent TEXT,
            resultado TEXT DEFAULT 'ABERTO',
            criado_em TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS blacklist (
            ativo TEXT PRIMARY KEY,
            motivo TEXT,
            criado_em TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS alertas_log (
            chave TEXT PRIMARY KEY,
            criado_em TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS sistema_log (
            chave TEXT PRIMARY KEY,
            valor TEXT,
            criado_em TEXT
        )
    """)
    conn.commit()
    conn.close()

def salvar_trade(ativo, direcao, entrada, stop, a1, a2, a3, tf_ctx, tf_ent):
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    agora = brt_agora().strftime("%Y-%m-%d %H:%M")
    c.execute("""
        INSERT INTO trades (ativo,direcao,entrada,stop,a1,a2,a3,tf_ctx,tf_ent,criado_em)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (ativo, direcao, entrada, stop, a1, a2, a3, tf_ctx, tf_ent, agora))
    conn.commit()
    tid = c.lastrowid
    conn.close()
    return tid

def atualizar_resultado(ativo, resultado):
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("""
        UPDATE trades SET resultado=?
        WHERE ativo=? AND resultado='ABERTO'
        ORDER BY id DESC LIMIT 1
    """, (resultado, ativo.upper()))
    conn.commit()
    conn.close()

def listar_trades():
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 20")
    rows = c.fetchall()
    conn.close()
    return rows

def relatorio():
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("SELECT resultado FROM trades")
    todos = c.fetchall()
    conn.close()
    total   = len(todos)
    wins    = sum(1 for r in todos if r[0] and r[0].startswith("WIN"))
    loss    = sum(1 for r in todos if r[0] == "LOSS")
    abertos = sum(1 for r in todos if r[0] == "ABERTO")
    wr = (wins / (wins + loss) * 100) if (wins + loss) > 0 else 0
    return total, wins, loss, abertos, wr

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def brt_agora():
    return datetime.now(timezone(timedelta(hours=OFFSET_BRT)))

def enviar_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram não configurado.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        log.error(f"Telegram erro: {e}")

def get_updates(offset=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {
        "timeout": 25,
        "offset": offset,
        "limit": 10,
        "allowed_updates": ["message"]
    }
    try:
        r = requests.get(url, params=params, timeout=30)
        return r.json().get("result", [])
    except:
        return []

# ─────────────────────────────────────────────
# EXCHANGES
# ─────────────────────────────────────────────
LBANK_BASE = "https://api.lbank.info"

EXCHANGES_CONFIG = [
    {"id": "lbank",   "label": "LBank",   "instance": None},
    {"id": "binance", "label": "Binance", "instance": None},
    {"id": "bybit",   "label": "Bybit",   "instance": None},
]

# ── FIX 4: init_exchanges com timeout para não crashar no Railway ──
def init_exchanges():
    """Inicializa ccxt com timeout — evita crash no startup do Railway."""
    import ccxt as _ccxt_local
    for ex in EXCHANGES_CONFIG:
        try:
            instance = getattr(_ccxt_local, ex["id"])({
                "enableRateLimit": True,
                "timeout": 10000,  # FIX: timeout 10s evita hang no Railway
            })
            instance.load_markets()
            ex["instance"] = instance
            log.info(f"Exchange {ex['label']}: OK ({len(instance.markets)} mercados)")
        except Exception as e:
            log.warning(f"Exchange {ex['label']}: falhou — {e}")
            ex["instance"] = None  # FIX: garantir None explícito em falha

def buscar_todos_pares():
    todos = []
    for ex in EXCHANGES_CONFIG:
        if not ex["instance"]:
            continue
        try:
            mercados = ex["instance"].markets
            for symbol, info in mercados.items():
                if info.get("active") and info.get("quote") in ["USDT","USDC"]:
                    todos.append(f"{symbol}@{ex['id']}")
        except Exception as e:
            log.error(f"Pares {ex['label']}: {e}")
    if not todos:
        try:
            r = requests.get(f"{LBANK_BASE}/v2/currencyPairs.do", timeout=15)
            dados = r.json()
            if dados.get("result") == "true":
                todos = [f"{p}@lbank" for p in dados.get("data", [])]
        except Exception as e:
            log.error(f"LBank fallback: {e}")
    return todos

def buscar_ticker_24h():
    tickers = {}
    for ex in EXCHANGES_CONFIG:
        if not ex["instance"]:
            continue
        try:
            raw = ex["instance"].fetch_tickers()
            for symbol, t in raw.items():
                chave = f"{symbol}@{ex['id']}"
                tickers[chave] = {
                    "turnover": t.get("quoteVolume", 0) or 0,
                    "vol":      t.get("baseVolume", 0) or 0,
                    "last":     t.get("last", 0) or 0,
                    "exchange": ex["label"],
                }
        except Exception as e:
            log.error(f"Ticker {ex['label']}: {e}")
    return tickers

def extrair_volume(ticker_data):
    if not ticker_data:
        return 0
    for campo in ["turnover", "quoteVolume", "vol", "volume"]:
        try:
            v = float(ticker_data.get(campo, 0) or 0)
            if v > 0:
                return v
        except:
            pass
    return 0

LBANK_TF_MAP = {
    "minute15": "15m", "minute5": "5m", "minute1": "1m",
    "minute30": "30m", "hour1": "1h", "hour4": "4h", "day1": "1d",
    "kline_15min": "15m", "kline_5min": "5m", "kline_1h": "1h",
    "15m": "15m", "5m": "5m", "1h": "1h",
}

try:
    import ccxt as _ccxt
    _exchange = _ccxt.lbank({"enableRateLimit": True, "timeout": 10000})
    _exchange.load_markets()
    CCXT_AVAILABLE = True
    log.info("ccxt LBank carregado com sucesso")
except Exception as e:
    CCXT_AVAILABLE = False
    log.warning(f"ccxt indisponivel: {e}")

def buscar_candles(symbol, tf=None, tamanho=50):
    raw_tf  = tf or TIMEFRAME_SCAN
    ccxt_tf = LBANK_TF_MAP.get(raw_tf, "15m")
    exchange_id = "lbank"
    sym_clean   = symbol
    if "@" in symbol:
        sym_clean, exchange_id = symbol.rsplit("@", 1)
    sym_upper = sym_clean.upper().replace("_", "/")
    if "/" not in sym_upper:
        for q in ["USDT","USDC","BTC","ETH"]:
            if sym_upper.endswith(q):
                sym_upper = sym_upper[:-len(q)] + "/" + q
                break
    ex_instance = None
    for ex in EXCHANGES_CONFIG:
        if ex["id"] == exchange_id and ex["instance"]:
            ex_instance = ex["instance"]
            break
    if ex_instance is None and CCXT_AVAILABLE:
        ex_instance = _exchange
    if ex_instance:
        try:
            ohlcv = ex_instance.fetch_ohlcv(sym_upper, ccxt_tf, limit=tamanho)
            return [[c[0]//1000, c[1], c[2], c[3], c[4], c[5]] for c in ohlcv if c]
        except Exception as e:
            log.debug(f"ccxt {symbol}: {e}")
    try:
        ts_sec = int(time.time())
        r = requests.get("https://api.lbank.info/v2/kline.do", params={
            "symbol": sym_clean.lower().replace("/","_"),
            "size":   tamanho,
            "type":   raw_tf if raw_tf in ["minute15","minute5","hour1"] else "minute15",
            "time":   ts_sec
        }, timeout=10)
        dados = r.json()
        if isinstance(dados, dict) and dados.get("result") in ("true", True):
            return dados.get("data", [])
        if isinstance(dados, list):
            return dados
    except Exception as e:
        log.error(f"Candles fallback {symbol}: {e}")
    return []

def calcular_ema(valores, periodo):
    if len(valores) < periodo:
        return []
    k = 2 / (periodo + 1)
    emas = [sum(valores[:periodo]) / periodo]
    for v in valores[periodo:]:
        emas.append(v * k + emas[-1] * (1 - k))
    return emas

def calcular_rsi(closes, periodo=14):
    if len(closes) < periodo + 1:
        return 50
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    ganhos = [d if d > 0 else 0 for d in deltas]
    perdas = [-d if d < 0 else 0 for d in deltas]
    mg = sum(ganhos[:periodo]) / periodo
    mp = sum(perdas[:periodo]) / periodo
    for i in range(periodo, len(deltas)):
        mg = (mg * (periodo - 1) + ganhos[i]) / periodo
        mp = (mp * (periodo - 1) + perdas[i]) / periodo
    if mp == 0:
        return 100
    return round(100 - (100 / (1 + mg / mp)), 2)

def calcular_vwap(candles):
    num = sum((float(c[2]) + float(c[3]) + float(c[4])) / 3 * float(c[5]) for c in candles)
    den = sum(float(c[5]) for c in candles)
    return num / den if den > 0 else 0

def volume_relativo(volumes, n=20):
    if len(volumes) < n + 1:
        return 1.0
    media = sum(volumes[-n-1:-1]) / n
    return round(volumes[-1] / media, 2) if media > 0 else 1.0

def parse_candle(c):
    try:
        if isinstance(c, (list, tuple)) and len(c) >= 6:
            return {
                "ts": int(float(c[0])),
                "o":  float(c[1]),
                "h":  float(c[2]),
                "l":  float(c[3]),
                "c":  float(c[4]),
                "v":  float(c[5])
            }
    except:
        pass
    return None

def analisar_ativo(symbol, candles):
    parsed = [parse_candle(c) for c in candles]
    parsed = [p for p in parsed if p is not None]
    if len(parsed) < 20:
        return None
    highs   = [p["h"] for p in parsed]
    lows    = [p["l"] for p in parsed]
    closes  = [p["c"] for p in parsed]
    volumes = [p["v"] for p in parsed]
    preco   = closes[-1]
    vol_rel = volume_relativo(volumes)
    rsi     = calcular_rsi(closes)
    vwap    = calcular_vwap(candles[-20:])
    ema9    = calcular_ema(closes, 9)
    ema21   = calcular_ema(closes, 21)
    janela  = closes[-MIN_CANDLES_RANGE:]
    suporte = min(janela)
    resist  = max(janela)
    sinais  = []
    if vol_rel >= MULT_ALERTA:
        if closes[-1] > resist:
            forca = "FORTE" if vol_rel >= MULT_FORTE else "MÉDIO" if vol_rel >= MULT_MEDIO else "ALERTA"
            sinais.append({"tipo": f"🚀 Breakout LONG [{forca}]", "forca": forca,
                           "detalhe": f"Rompeu ${resist:.4f} | Vol {vol_rel}x"})
        elif closes[-1] < suporte:
            forca = "FORTE" if vol_rel >= MULT_FORTE else "MÉDIO" if vol_rel >= MULT_MEDIO else "ALERTA"
            sinais.append({"tipo": f"📉 Breakout SHORT [{forca}]", "forca": forca,
                           "detalhe": f"Rompeu ${suporte:.4f} | Vol {vol_rel}x"})
    if len(closes) >= 15:
        amp_rec = sum(highs[i] - lows[i] for i in range(-5, 0)) / 5
        amp_ant = sum(highs[i] - lows[i] for i in range(-15, -5)) / 10
        vol_rec = sum(volumes[-5:]) / 5
        vol_ant = sum(volumes[-15:-5]) / 10
        if amp_ant > 0 and amp_rec < amp_ant * 0.6 and vol_rec < vol_ant * 0.8:
            sinais.append({"tipo": "⚡ Compressão [ALERTA]", "forca": "ALERTA",
                           "detalhe": f"Volatilidade -{round((1-amp_rec/amp_ant)*100)}% | Vol caindo"})
    if vwap > 0 and abs(preco - vwap) / vwap < 0.003 and vol_rel >= MULT_MEDIO:
        direcao = "LONG" if preco > vwap else "SHORT"
        forca = "MÉDIO" if vol_rel >= MULT_MEDIO else "ALERTA"
        sinais.append({"tipo": f"📍 Toque VWAP {direcao} [{forca}]", "forca": forca,
                       "detalhe": f"VWAP ${vwap:.4f} | Preço ${preco:.4f}"})
    if rsi < RSI_SOBREVENDA and closes[-1] > closes[-2]:
        sinais.append({"tipo": "🔄 RSI Reversão LONG [ALERTA]", "forca": "ALERTA",
                       "detalhe": f"RSI {rsi} | Sobrevenda com vela de recuperação"})
    elif rsi > RSI_SOBRECOMPRA and closes[-1] < closes[-2]:
        sinais.append({"tipo": "🔄 RSI Reversão SHORT [ALERTA]", "forca": "ALERTA",
                       "detalhe": f"RSI {rsi} | Sobrecompra com vela de rejeição"})
    if vol_rel >= 3.0:
        sinais.append({"tipo": "🌊 Volume Climático [FORTE]", "forca": "FORTE",
                       "detalhe": f"Vol {vol_rel}x → possível Spring ou Upthrust Wyckoff"})
    if len(ema9) >= 2 and len(ema21) >= 2:
        if ema9[-2] <= ema21[-2] and ema9[-1] > ema21[-1] and vol_rel >= MULT_MEDIO:
            sinais.append({"tipo": "✂️ EMA Cross LONG [MÉDIO]", "forca": "MÉDIO",
                           "detalhe": f"EMA9 cruzou EMA21 para cima | Vol {vol_rel}x"})
        elif ema9[-2] >= ema21[-2] and ema9[-1] < ema21[-1] and vol_rel >= MULT_MEDIO:
            sinais.append({"tipo": "✂️ EMA Cross SHORT [MÉDIO]", "forca": "MÉDIO",
                           "detalhe": f"EMA9 cruzou EMA21 para baixo | Vol {vol_rel}x"})
    if not sinais:
        return None
    ordem = {"FORTE": 3, "MÉDIO": 2, "ALERTA": 1}
    forca_max = max(sinais, key=lambda s: ordem.get(s["forca"], 0))["forca"]
    score = 0
    if vol_rel >= 10:   score += 40
    elif vol_rel >= 5:  score += 25
    elif vol_rel >= 2:  score += 10
    if rsi < 25 or rsi > 75:   score += 20
    elif rsi < 35 or rsi > 65: score += 10
    if len(ema9) >= 2 and len(ema21) >= 2:
        bull_trend  = ema9[-1] > ema21[-1] and closes[-1] > ema9[-1]
        bear_trend  = ema9[-1] < ema21[-1] and closes[-1] < ema9[-1]
        long_sinal  = any("LONG" in s["tipo"] for s in sinais)
        short_sinal = any("SHORT" in s["tipo"] for s in sinais)
        if (long_sinal and bull_trend) or (short_sinal and bear_trend):
            score += 20
    if forca_max == "FORTE":   score += 20
    elif forca_max == "MÉDIO": score += 10
    exchange_label = ""
    if "@" in symbol:
        _, ex_id = symbol.rsplit("@", 1)
        for ex in EXCHANGES_CONFIG:
            if ex["id"] == ex_id:
                exchange_label = ex["label"]
                break
    return {
        "symbol":      symbol.split("@")[0].upper(),
        "preco":       preco,
        "vol_rel":     vol_rel,
        "rsi":         rsi,
        "vwap":        round(vwap, 4),
        "suporte":     round(suporte, 4),
        "resistencia": round(resist, 4),
        "sinais":      sinais,
        "forca_max":   forca_max,
        "score":       min(score, 100),
        "exchange":    exchange_label,
    }

# ─────────────────────────────────────────────
# SENTIMENTO
# ─────────────────────────────────────────────
def normalizar_symbol_coinalyze(symbol):
    s = symbol.upper().strip()
    for suf in ["_PERP.A", "_PERP.0", "_PERP.6", ".P", "-PERP"]:
        s = s.replace(suf, "")
    s = s.replace("-", "").replace("_", "").replace("/", "")
    if not s.endswith("USDT") and not s.endswith("USDC"):
        s = s + "USDT"
    return f"{s}_PERP.A"

def buscar_sentimento(symbol):
    sym_cg  = normalizar_symbol_coinalyze(symbol)
    headers = {"api_key": COINALYZE_KEY}
    ts_now  = int(time.time())
    ts_1h   = ts_now - 3600
    ts_24h  = ts_now - 86400
    resultado = {"symbol_cg": sym_cg}
    try:
        r = requests.get(f"{COINALYZE_BASE}/funding-rate",
                         params={"symbols": sym_cg}, headers=headers, timeout=8)
        if r.status_code == 200:
            data = r.json()
            if data:
                resultado["funding"] = round(float(data[0].get("value", 0)) * 100, 4)
    except Exception as e:
        log.debug(f"Funding {sym_cg}: {e}")
    try:
        r = requests.get(f"{COINALYZE_BASE}/predicted-funding-rate",
                         params={"symbols": sym_cg}, headers=headers, timeout=8)
        if r.status_code == 200:
            data = r.json()
            if data:
                resultado["funding_pred"] = round(float(data[0].get("value", 0)) * 100, 4)
    except Exception as e:
        log.debug(f"Funding previsto {sym_cg}: {e}")
    try:
        r = requests.get(f"{COINALYZE_BASE}/open-interest",
                         params={"symbols": sym_cg, "convert_to_usd": "true"},
                         headers=headers, timeout=8)
        if r.status_code == 200:
            data = r.json()
            if data:
                resultado["oi_usd"] = float(data[0].get("value", 0))
    except Exception as e:
        log.debug(f"OI {sym_cg}: {e}")
    try:
        r = requests.get(f"{COINALYZE_BASE}/long-short-ratio-history",
                         params={"symbols": sym_cg, "interval": "1hour",
                                 "from": ts_1h, "to": ts_now},
                         headers=headers, timeout=8)
        if r.status_code == 200:
            data = r.json()
            if data and data[0].get("history"):
                hist   = data[0]["history"]
                ultimo = hist[-1]
                resultado["ls_ratio"]     = round(float(ultimo.get("r", 1)), 3)
                resultado["ls_long_pct"]  = round(float(ultimo.get("l", 50)), 1)
                resultado["ls_short_pct"] = round(float(ultimo.get("s", 50)), 1)
    except Exception as e:
        log.debug(f"L/S {sym_cg}: {e}")
    try:
        r = requests.get(f"{COINALYZE_BASE}/liquidation-history",
                         params={"symbols": sym_cg, "interval": "1hour",
                                 "from": ts_24h, "to": ts_now, "convert_to_usd": "true"},
                         headers=headers, timeout=8)
        if r.status_code == 200:
            data = r.json()
            if data and data[0].get("history"):
                hist      = data[0]["history"]
                liq_long  = sum(float(h.get("l", 0)) for h in hist)
                liq_short = sum(float(h.get("s", 0)) for h in hist)
                resultado["liq_long_usd"]  = liq_long
                resultado["liq_short_usd"] = liq_short
    except Exception as e:
        log.debug(f"Liq {sym_cg}: {e}")
    return resultado if len(resultado) > 1 else None

def fmt_usd(val):
    if val >= 1_000_000_000: return f"${val/1_000_000_000:.2f}B"
    elif val >= 1_000_000:   return f"${val/1_000_000:.1f}M"
    elif val >= 1_000:       return f"${val/1_000:.0f}K"
    return f"${val:,.0f}"

def formatar_sentimento(sent):
    if not sent or len(sent) <= 1:
        return ""
    sym   = sent.get("symbol_cg", "")
    linhas = [f"📡 <b>SENTIMENTO AGREGADO</b> — {sym}"]
    if "funding" in sent:
        fr = sent["funding"]
        if fr < -0.005:    emoji, desc = "🟢🟢", "muito negativo → forte pressão LONG"
        elif fr < 0:       emoji, desc = "🟢", "negativo → pressão LONG"
        elif fr < 0.01:    emoji, desc = "⚪", "neutro"
        elif fr < 0.03:    emoji, desc = "🔴", "positivo → pressão SHORT"
        else:              emoji, desc = "🔴🔴", "muito positivo → forte pressão SHORT"
        linhas.append(f"  Funding atual: {emoji} {fr:+.4f}% ({desc})")
    if "funding_pred" in sent:
        linhas.append(f"  Funding previsto: {sent['funding_pred']:+.4f}%")
    if "oi_usd" in sent:
        linhas.append(f"  OI agregado: {fmt_usd(sent['oi_usd'])}")
    if "ls_ratio" in sent:
        ls = sent["ls_ratio"]
        lp = sent.get("ls_long_pct", 0)
        sp = sent.get("ls_short_pct", 0)
        if ls > 1.5:       emoji, desc = "🟢🟢", "longs dominam fortemente"
        elif ls > 1.1:     emoji, desc = "🟢", "longs dominam"
        elif ls < 0.67:    emoji, desc = "🔴🔴", "shorts dominam fortemente"
        elif ls < 0.9:     emoji, desc = "🔴", "shorts dominam"
        else:              emoji, desc = "⚪", "equilibrado"
        linhas.append(f"  L/S Ratio: {emoji} {ls} ({lp:.1f}%L / {sp:.1f}%S — {desc})")
    if "liq_long_usd" in sent and "liq_short_usd" in sent:
        ll    = sent["liq_long_usd"]
        ls_liq = sent["liq_short_usd"]
        total  = ll + ls_liq
        if total > 0:
            dom = "longs liq." if ll > ls_liq else "shorts liq."
            linhas.append(f"  Liq 24h: {fmt_usd(total)} ({dom} | L:{fmt_usd(ll)} S:{fmt_usd(ls_liq)})")
    return "\n".join(linhas)

# ─────────────────────────────────────────────
# SCANNER
# ─────────────────────────────────────────────
def get_blacklist():
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("SELECT ativo FROM blacklist")
    rows = c.fetchall()
    conn.close()
    return {r[0].upper() for r in rows}

def adicionar_blacklist(ativo, motivo=""):
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    agora = brt_agora().strftime("%Y-%m-%d %H:%M")
    c.execute("INSERT OR REPLACE INTO blacklist VALUES (?,?,?)", (ativo.upper(), motivo, agora))
    conn.commit()
    conn.close()

def remover_blacklist(ativo):
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("DELETE FROM blacklist WHERE ativo=?", (ativo.upper(),))
    conn.commit()
    conn.close()

def rodar_scanner():
    brt = brt_agora().strftime("%d/%m/%Y %H:%M BRT")
    enviar_telegram(f"🔍 <b>SCANNER LucSharkTrade</b>\n{brt} | TF: 15M\nAnalisando ativos...")
    tickers   = buscar_ticker_24h()
    pares_raw = buscar_todos_pares()
    pares = []
    for p in pares_raw:
        t = tickers.get(p)
        if t:
            vol = extrair_volume(t)
            if vol == 0 or vol >= MIN_VOLUME_24H:
                pares.append(p)
        else:
            pares.append(p)
    pares = pares[:600]
    log.info(f"Analisando {len(pares)} ativos...")
    prioridade_max  = []
    alta_prioridade = []
    blacklist = get_blacklist()
    for symbol in pares:
        if symbol.upper() in blacklist:
            continue
        try:
            candles = buscar_candles(symbol)
            if not candles:
                continue
            parsed = [parse_candle(c) for c in candles]
            parsed = [p for p in parsed if p is not None]
            if len(parsed) < 20:
                continue
            closes  = [p["c"] for p in parsed]
            volumes = [p["v"] for p in parsed]
            vol_rel = volume_relativo(volumes)
            if vol_rel < 5.0:
                continue
            rsi = calcular_rsi(closes)
            if rsi >= 40 and rsi <= 60:
                continue
            vies       = "LONG" if rsi < 40 else "SHORT"
            vies_emoji = "🟢" if vies == "LONG" else "🔴"
            vwap       = calcular_vwap(parsed[-20:])
            suporte    = round(min(p["l"] for p in parsed[-5:]), 6)
            resist     = round(max(p["h"] for p in parsed[-5:]), 6)
            preco      = closes[-1]
            resultado  = {
                "symbol": symbol.upper(), "preco": preco,
                "vol_rel": vol_rel, "rsi": rsi,
                "vwap": round(vwap, 6), "suporte": suporte,
                "resist": resist, "vies": vies, "vies_emoji": vies_emoji
            }
            if vol_rel >= 10.0:
                prioridade_max.append(resultado)
            else:
                alta_prioridade.append(resultado)
            time.sleep(0.15)
        except Exception as e:
            log.error(f"Erro {symbol}: {e}")
    prioridade_max.sort(key=lambda x: x["vol_rel"], reverse=True)
    alta_prioridade.sort(key=lambda x: x["vol_rel"], reverse=True)
    total = len(prioridade_max) + len(alta_prioridade)
    if total == 0:
        enviar_telegram(f"✅ <b>SCAN CONCLUÍDO</b>\n{brt}\nNenhum ativo com Vol ≥5x e RSI extremo.")
        return
    linhas = [f"━━━━━━━━━━━━━━━━━━━━━━━━", f"📊 <b>SCANNER LucSharkTrade</b>",
              f"🕐 {brt}", f"━━━━━━━━━━━━━━━━━━━━━━━━", ""]
    if prioridade_max:
        linhas.append(f"🚨 <b>PRIORIDADE MÁXIMA — Vol &gt;10x</b>\n")
        for i, r in enumerate(prioridade_max, 1):
            sent     = buscar_sentimento(r["symbol"])
            sent_txt = formatar_sentimento(sent)
            linhas.append(
                f"{i}. <b>{r['symbol']}</b> {r['vies_emoji']} {r['vies']}"
                f" | Vol <b>{r['vol_rel']}x</b> | RSI {r['rsi']} | 💲{r['preco']:.6g}"
            )
            if sent_txt:
                linhas.append(sent_txt)
            linhas.append("")
    if alta_prioridade:
        linhas.append(f"⚡ <b>ALTA PRIORIDADE — Vol 5–10x</b>\n")
        for i, r in enumerate(alta_prioridade, 1):
            sent     = buscar_sentimento(r["symbol"])
            sent_txt = formatar_sentimento(sent)
            linhas.append(
                f"{i}. <b>{r['symbol']}</b> {r['vies_emoji']} {r['vies']}"
                f" | Vol <b>{r['vol_rel']}x</b> | RSI {r['rsi']} | 💲{r['preco']:.6g}"
            )
            if sent_txt:
                linhas.append(sent_txt)
            linhas.append("")
    linhas += [f"━━━━━━━━━━━━━━━━━━━━━━━━",
               f"Total analisados: {len(pares)}", "",
               f"👁 Escolha o ativo e envie o print para análise!"]
    mensagem = "\n".join(linhas)
    if len(mensagem) <= 4000:
        enviar_telegram(mensagem)
    else:
        bloco = []
        chars = 0
        for linha in linhas:
            if chars + len(linha) > 3800:
                enviar_telegram("\n".join(bloco))
                bloco = [linha]
                chars = len(linha)
                time.sleep(1)
            else:
                bloco.append(linha)
                chars += len(linha)
        if bloco:
            enviar_telegram("\n".join(bloco))
    log.info(f"Scan concluído. Max:{len(prioridade_max)} Alta:{len(alta_prioridade)}")

def rodar_scanner_debug():
    brt = brt_agora().strftime("%d/%m/%Y %H:%M BRT")
    enviar_telegram(f"🔧 <b>MODO DEBUG</b>\n{brt}\nTestando 5 ativos...")
    tickers   = buscar_ticker_24h()
    pares_raw = buscar_todos_pares()
    amostra   = pares_raw[:5] if pares_raw else []
    msg = [f"📋 Total pares: {len(pares_raw)}\nTickers: {len(tickers)}\n"]
    ts  = int(time.time())
    msg.append("<b>Testando v2/kline.do:</b>")
    for symbol in ["btc_usdt", "eth_usdt", "sol_usdt"]:
        try:
            r = requests.get("https://api.lbank.info/v2/kline.do", params={
                "symbol": symbol, "size": 3, "type": "minute15", "time": ts}, timeout=8)
            dados = r.json()
            ok = isinstance(dados, list) and len(dados) > 0
            msg.append(f"  {symbol}: {'OK ' + str(len(dados)) + ' candles' if ok else 'ERRO: ' + str(dados)[:60]}")
        except Exception as e:
            msg.append(f"  {symbol}: EXCECAO {e}")
    enviar_telegram("\n".join(msg))

# ─────────────────────────────────────────────
# PREÇO ATUAL
# ─────────────────────────────────────────────
def normalizar_symbol_ccxt(ativo):
    s = ativo.upper().strip()
    if "/" in s: return s
    if "-" in s: return s.replace("-", "/")
    if "_" in s: return s.replace("_", "/")
    for quote in ["USDT", "USDC", "BTC", "ETH", "BNB"]:
        if s.endswith(quote) and len(s) > len(quote):
            return f"{s[:-len(quote)]}/{quote}"
    return s

def buscar_preco_atual(ativo):
    """
    Busca preço atual com high/low do candle recente.
    FIX: retorna high e low reais do último candle — essencial para
    detectar acionamentos intracandle.
    """
    symbol = normalizar_symbol_ccxt(ativo)
    # 1. Tentar fetch_ticker (mais rápido)
    if CCXT_AVAILABLE:
        try:
            ticker = _exchange.fetch_ticker(symbol)
            return {
                "preco": ticker["last"],
                "high":  ticker.get("high") or ticker["last"],
                "low":   ticker.get("low") or ticker["last"],
                "bid":   ticker.get("bid") or ticker["last"],
                "ask":   ticker.get("ask") or ticker["last"],
            }
        except Exception as e:
            log.debug(f"Ticker {symbol}: {e}")
    # 2. Tentar via exchanges configuradas
    for ex in EXCHANGES_CONFIG:
        if ex["instance"]:
            try:
                ticker = ex["instance"].fetch_ticker(symbol)
                return {
                    "preco": ticker["last"],
                    "high":  ticker.get("high") or ticker["last"],
                    "low":   ticker.get("low") or ticker["last"],
                    "bid":   ticker.get("bid") or ticker["last"],
                    "ask":   ticker.get("ask") or ticker["last"],
                }
            except:
                continue
    # 3. Fallback: candles (high/low do candle recente)
    sym_lbank = symbol.lower().replace("/", "_")
    candles   = buscar_candles(sym_lbank, "minute5", 3)
    if candles:
        parsed = [parse_candle(c) for c in candles]
        parsed = [p for p in parsed if p is not None]
        if parsed:
            ultimo = parsed[-1]
            return {
                "preco": ultimo["c"],
                "high":  ultimo["h"],
                "low":   ultimo["l"],
                "bid":   ultimo["c"],
                "ask":   ultimo["c"],
            }
    return None

# ─────────────────────────────────────────────
# CONTROLE DE ALERTAS
# ─────────────────────────────────────────────
_alertas_enviados = {}

def alerta_ja_enviado(chave):
    if chave in _alertas_enviados:
        return True
    try:
        conn = sqlite3.connect("trades.db")
        c = conn.cursor()
        c.execute("SELECT 1 FROM alertas_log WHERE chave=?", (chave,))
        existe = c.fetchone() is not None
        conn.close()
        if existe:
            _alertas_enviados[chave] = True
        return existe
    except:
        return False

def marcar_alerta(chave):
    _alertas_enviados[chave] = True
    try:
        conn = sqlite3.connect("trades.db")
        c = conn.cursor()
        agora = brt_agora().strftime("%Y-%m-%d %H:%M")
        c.execute("INSERT OR IGNORE INTO alertas_log VALUES (?,?)", (chave, agora))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"marcar_alerta {chave}: {e}")

def calcular_duracao(criado_em):
    try:
        fmt      = "%Y-%m-%d %H:%M"
        abertura = datetime.strptime(criado_em, fmt)
        agora    = brt_agora().replace(tzinfo=None)
        delta    = agora - abertura
        horas    = int(delta.total_seconds() // 3600)
        minutos  = int((delta.total_seconds() % 3600) // 60)
        if horas >= 24:
            dias = horas // 24
            return f"{dias}d {horas % 24}h {minutos}m"
        return f"{horas}h {minutos}m"
    except:
        return "—"

# ─────────────────────────────────────────────
# MONITORAR TRADES — VERSÃO CORRIGIDA v2
# ─────────────────────────────────────────────
def monitorar_trades():
    """
    FIX v2 — Bugs corrigidos:
    1. Checks independentes (não elif encadeado) para stop/alvos/entrada
    2. Stop verifica ANTES dos alvos e usa `continue`
    3. Entrada usa low <= entrada <= high (intracandle) + tol 0.5%
    4. Zona de aproximação só dispara se entrada ainda não acionada
    5. Alvos superiores marcam alvos inferiores como enviados (sem spam)
    """
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("SELECT * FROM trades WHERE resultado='ABERTO'")
    abertos = c.fetchall()
    conn.close()

    for trade in abertos:
        tid, ativo, direcao, entrada, stop, a1, a2, a3, tf_ctx, tf_ent, resultado, criado = trade

        dados = buscar_preco_atual(ativo)
        if not dados:
            log.warning(f"Sem preco para {ativo}")
            continue

        preco = dados["preco"]
        high  = dados.get("high", preco)
        low   = dados.get("low", preco)

        base        = f"{tid}_{ativo}"
        tol_entrada = entrada * 0.005  # FIX: 0.5% (era 0.3% — muito restrito)

        # ══════════════════════════════════════
        # LONG
        # ══════════════════════════════════════
        if direcao == "LONG":

            # ── STOP — verificar PRIMEIRO com continue ──
            if low <= stop and not alerta_ja_enviado(f"{base}_stop"):
                marcar_alerta(f"{base}_stop")
                atualizar_resultado(ativo, "LOSS")
                duracao = calcular_duracao(criado)
                enviar_telegram(
                    f"🛑 <b>STOP — {ativo} LONG #{tid}</b>\n"
                    f"💲 Preço: ${preco:.6g} | Stop: ${stop}\n"
                    f"❌ Sair imediatamente!\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📋 Entrada: ${entrada} | Saída: ${preco:.6g}\n"
                    f"❌ LOSS | ⏱ {duracao}"
                )
                continue  # FIX: não verificar alvos após stop

            # ── A3 ──
            if high >= a3 and not alerta_ja_enviado(f"{base}_a3"):
                marcar_alerta(f"{base}_a3")
                marcar_alerta(f"{base}_a2")  # FIX: marcar inferiores (sem spam)
                marcar_alerta(f"{base}_a1")
                marcar_alerta(f"{base}_entrada")
                atualizar_resultado(ativo, "WIN_A3")
                duracao = calcular_duracao(criado)
                enviar_telegram(
                    f"🏆 <b>A3 ATINGIDO — {ativo} LONG #{tid}</b>\n"
                    f"💲 Preço: ${preco:.6g} | A3: ${a3}\n"
                    f"✅ Realizar 80% | 🎉 Trailing Stop no restante\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📋 Entrada: ${entrada} → A1 → A2 → A3\n"
                    f"✅ WIN A3 (RR 3:1) | ⏱ {duracao}"
                )

            # ── A2 ──
            elif high >= a2 and not alerta_ja_enviado(f"{base}_a2"):
                marcar_alerta(f"{base}_a2")
                marcar_alerta(f"{base}_a1")  # FIX: marcar inferior
                marcar_alerta(f"{base}_entrada")
                atualizar_resultado(ativo, "WIN_A2")
                enviar_telegram(
                    f"🎯 <b>A2 ATINGIDO — {ativo} LONG #{tid}</b>\n"
                    f"💲 Preço: ${preco:.6g} | A2: ${a2}\n"
                    f"✅ Realizar 50% | ⏳ Aguardar A3: ${a3}"
                )

            # ── A1 ──
            elif high >= a1 and not alerta_ja_enviado(f"{base}_a1"):
                marcar_alerta(f"{base}_a1")
                marcar_alerta(f"{base}_entrada")
                atualizar_resultado(ativo, "WIN_A1")
                enviar_telegram(
                    f"🎯 <b>A1 ATINGIDO — {ativo} LONG #{tid}</b>\n"
                    f"💲 Preço: ${preco:.6g} | A1: ${a1}\n"
                    f"✅ Realizar 25%\n"
                    f"🔒 Mover Stop para ${entrada} (breakeven)\n"
                    f"⏳ Aguardar A2: ${a2}"
                )

            # ── ENTRADA ACIONADA ──
            # FIX: usa low <= entrada <= high (intracandle) OU tolerância 0.5%
            elif (low <= entrada <= high or abs(preco - entrada) <= tol_entrada) \
                    and not alerta_ja_enviado(f"{base}_entrada"):
                marcar_alerta(f"{base}_entrada")
                marcar_alerta(f"{base}_zona")
                enviar_telegram(
                    f"🟢 <b>ENTRADA LONG ACIONADA — {ativo} #{tid}</b>\n"
                    f"💲 Preço: ${preco:.6g}\n"
                    f"📥 Entrada: ${entrada} | Stop: ${stop}\n"
                    f"🎯 A1: ${a1} | A2: ${a2} | A3: ${a3}"
                )

            # ── ZONA DE APROXIMAÇÃO ──
            # FIX: só dispara se entrada ainda NÃO foi acionada
            else:
                if not alerta_ja_enviado(f"{base}_entrada"):
                    distancia_pct = (preco - entrada) / entrada * 100
                    if 0 < distancia_pct <= 3.0 and not alerta_ja_enviado(f"{base}_zona"):
                        marcar_alerta(f"{base}_zona")
                        enviar_telegram(
                            f"👀 <b>ZONA DE ENTRADA — {ativo} LONG #{tid}</b>\n"
                            f"💲 Preço: ${preco:.6g} | Entrada: ${entrada}\n"
                            f"📍 Preço a {round(distancia_pct,2)}% acima da entrada\n"
                            f"⏳ Aguardando pullback para acionar..."
                        )

        # ══════════════════════════════════════
        # SHORT
        # ══════════════════════════════════════
        elif direcao == "SHORT":

            # ── STOP — verificar PRIMEIRO com continue ──
            if high >= stop and not alerta_ja_enviado(f"{base}_stop"):
                marcar_alerta(f"{base}_stop")
                atualizar_resultado(ativo, "LOSS")
                duracao = calcular_duracao(criado)
                enviar_telegram(
                    f"🛑 <b>STOP — {ativo} SHORT #{tid}</b>\n"
                    f"💲 Preço: ${preco:.6g} | Stop: ${stop}\n"
                    f"❌ Sair imediatamente!\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📋 Entrada: ${entrada} | Saída: ${preco:.6g}\n"
                    f"❌ LOSS | ⏱ {duracao}"
                )
                continue  # FIX: não verificar alvos após stop

            # ── A3 ──
            if low <= a3 and not alerta_ja_enviado(f"{base}_a3"):
                marcar_alerta(f"{base}_a3")
                marcar_alerta(f"{base}_a2")
                marcar_alerta(f"{base}_a1")
                marcar_alerta(f"{base}_entrada")
                atualizar_resultado(ativo, "WIN_A3")
                duracao = calcular_duracao(criado)
                enviar_telegram(
                    f"🏆 <b>A3 ATINGIDO — {ativo} SHORT #{tid}</b>\n"
                    f"💲 Preço: ${preco:.6g} | A3: ${a3}\n"
                    f"✅ Realizar 80% | 🎉 Trailing Stop no restante\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📋 Entrada: ${entrada} → A1 → A2 → A3\n"
                    f"✅ WIN A3 (RR 3:1) | ⏱ {duracao}"
                )

            # ── A2 ──
            elif low <= a2 and not alerta_ja_enviado(f"{base}_a2"):
                marcar_alerta(f"{base}_a2")
                marcar_alerta(f"{base}_a1")
                marcar_alerta(f"{base}_entrada")
                atualizar_resultado(ativo, "WIN_A2")
                enviar_telegram(
                    f"🎯 <b>A2 ATINGIDO — {ativo} SHORT #{tid}</b>\n"
                    f"💲 Preço: ${preco:.6g} | A2: ${a2}\n"
                    f"✅ Realizar 50% | ⏳ Aguardar A3: ${a3}"
                )

            # ── A1 ──
            elif low <= a1 and not alerta_ja_enviado(f"{base}_a1"):
                marcar_alerta(f"{base}_a1")
                marcar_alerta(f"{base}_entrada")
                atualizar_resultado(ativo, "WIN_A1")
                enviar_telegram(
                    f"🎯 <b>A1 ATINGIDO — {ativo} SHORT #{tid}</b>\n"
                    f"💲 Preço: ${preco:.6g} | A1: ${a1}\n"
                    f"✅ Realizar 25%\n"
                    f"🔒 Mover Stop para ${entrada} (breakeven)\n"
                    f"⏳ Aguardar A2: ${a2}"
                )

            # ── ENTRADA ACIONADA ──
            elif (low <= entrada <= high or abs(preco - entrada) <= tol_entrada) \
                    and not alerta_ja_enviado(f"{base}_entrada"):
                marcar_alerta(f"{base}_entrada")
                marcar_alerta(f"{base}_zona")
                enviar_telegram(
                    f"🔴 <b>ENTRADA SHORT ACIONADA — {ativo} #{tid}</b>\n"
                    f"💲 Preço: ${preco:.6g}\n"
                    f"📥 Entrada: ${entrada} | Stop: ${stop}\n"
                    f"🎯 A1: ${a1} | A2: ${a2} | A3: ${a3}"
                )

            # ── ZONA DE APROXIMAÇÃO ──
            else:
                if not alerta_ja_enviado(f"{base}_entrada"):
                    distancia_pct = (entrada - preco) / entrada * 100
                    if 0 < distancia_pct <= 3.0 and not alerta_ja_enviado(f"{base}_zona"):
                        marcar_alerta(f"{base}_zona")
                        enviar_telegram(
                            f"👀 <b>ZONA DE ENTRADA — {ativo} SHORT #{tid}</b>\n"
                            f"💲 Preço: ${preco:.6g} | Entrada: ${entrada}\n"
                            f"📍 Preço a {round(distancia_pct,2)}% abaixo da entrada\n"
                            f"⏳ Aguardando subida para acionar..."
                        )

# ─────────────────────────────────────────────
# RELATÓRIOS
# ─────────────────────────────────────────────
def relatorio_diario():
    brt = brt_agora().strftime("%d/%m/%Y %H:%M BRT")
    total, wins, loss, abertos, wr = relatorio()
    risco_por_trade = CAPITAL_INICIAL * 0.02
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("SELECT resultado FROM trades WHERE DATE(criado_em) = DATE('now')")
    hoje = c.fetchall()
    conn.close()
    pnl = 0
    wins_hoje = 0
    loss_hoje = 0
    for r in hoje:
        res = r[0] or ""
        if "WIN_A1" in res:   pnl += risco_por_trade * 1.0; wins_hoje += 1
        elif "WIN_A2" in res: pnl += risco_por_trade * 2.0; wins_hoje += 1
        elif "WIN_A3" in res: pnl += risco_por_trade * 3.0; wins_hoje += 1
        elif res == "LOSS":   pnl -= risco_por_trade; loss_hoje += 1
    sinal_pnl = "+" if pnl >= 0 else ""
    emoji_pnl = "📈" if pnl >= 0 else "📉"
    enviar_telegram(
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>RELATÓRIO DIÁRIO — LucSharkTrade</b>\n"
        f"📅 {brt}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>HOJE</b>\n"
        f"✅ Wins: {wins_hoje} | ❌ Losses: {loss_hoje}\n"
        f"{emoji_pnl} P&L: {sinal_pnl}${pnl:.2f}\n\n"
        f"<b>ACUMULADO</b>\n"
        f"Total trades: {total}\n"
        f"✅ Wins: {wins} | ❌ Losses: {loss} | 🔄 Abertos: {abertos}\n"
        f"🎯 Win Rate: {wr:.1f}%\n"
        f"💰 Capital: ${CAPITAL_INICIAL:,.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    )

def relatorio_semanal():
    brt  = brt_agora().strftime("%d/%m/%Y %H:%M BRT")
    conn = sqlite3.connect("trades.db")
    c    = conn.cursor()
    c.execute("""
        SELECT ativo, direcao, entrada, resultado, criado_em FROM trades
        WHERE DATE(criado_em) >= DATE('now', '-7 days') ORDER BY criado_em
    """)
    semana = c.fetchall()
    conn.close()
    if not semana:
        enviar_telegram(f"📊 <b>Relatório Semanal</b>\n{brt}\nNenhum trade na última semana.")
        return
    wins   = [t for t in semana if t[3] and t[3].startswith("WIN")]
    losses = [t for t in semana if t[3] == "LOSS"]
    wr     = (len(wins)/(len(wins)+len(losses))*100) if (wins or losses) else 0
    RISCO  = 20
    pnl    = 0
    for t in wins:
        if "A3" in (t[3] or ""):   pnl += RISCO * 3
        elif "A2" in (t[3] or ""): pnl += RISCO * 2
        else:                       pnl += RISCO
    pnl -= len(losses) * RISCO
    contagem = {}
    for t in semana:
        a = t[0]
        if a not in contagem: contagem[a] = {"w": 0, "l": 0}
        if t[3] and t[3].startswith("WIN"): contagem[a]["w"] += 1
        elif t[3] == "LOSS":                 contagem[a]["l"] += 1
    melhor = max(contagem, key=lambda a: contagem[a]["w"] - contagem[a]["l"]) if contagem else "—"
    pior   = min(contagem, key=lambda a: contagem[a]["w"] - contagem[a]["l"]) if contagem else "—"
    sinal  = "+" if pnl >= 0 else ""
    emoji  = "📈" if pnl >= 0 else "📉"
    enviar_telegram(
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>RELATÓRIO SEMANAL — LucSharkTrade</b>\n"
        f"📅 {brt}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>SEMANA</b>\n"
        f"Total trades: {len(semana)}\n"
        f"✅ Wins: {len(wins)} | ❌ Losses: {len(losses)}\n"
        f"🎯 Win Rate: {wr:.1f}%\n"
        f"{emoji} P&L: {sinal}${pnl:.2f}\n\n"
        f"<b>DESTAQUES</b>\n"
        f"🏆 Melhor ativo: {melhor} ({contagem.get(melhor,{}).get('w',0)}W/{contagem.get(melhor,{}).get('l',0)}L)\n"
        f"⚠️ Ativo problemático: {pior}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    )

# ─────────────────────────────────────────────
# FLASK API
# ─────────────────────────────────────────────
@flask_app.route("/api/trades")
def api_trades():
    try:
        conn = sqlite3.connect("trades.db")
        c = conn.cursor()
        c.execute("SELECT * FROM trades ORDER BY id DESC")
        rows = c.fetchall()
        conn.close()
        trades = [{"id":r[0],"ativo":r[1],"direcao":r[2],"entrada":r[3],
                   "stop":r[4],"a1":r[5],"a2":r[6],"a3":r[7],
                   "tf_ctx":r[8],"tf_ent":r[9],"resultado":r[10],"criado_em":r[11]}
                  for r in rows]
        return jsonify({"trades": trades, "total": len(trades)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@flask_app.route("/api/stats")
def api_stats():
    try:
        conn = sqlite3.connect("trades.db")
        c = conn.cursor()
        c.execute("SELECT resultado FROM trades")
        todos = c.fetchall()
        conn.close()
        wins    = sum(1 for r in todos if r[0] and r[0].startswith("WIN"))
        losses  = sum(1 for r in todos if r[0] == "LOSS")
        abertos = sum(1 for r in todos if not r[0] or r[0] == "ABERTO")
        wr = (wins/(wins+losses)*100) if (wins+losses) > 0 else 0
        RISCO = 20
        pnl   = 0
        for r in todos:
            res = r[0] or ""
            if "A3" in res:     pnl += RISCO * 3
            elif "A2" in res:   pnl += RISCO * 2
            elif "WIN" in res:  pnl += RISCO
            elif res == "LOSS": pnl -= RISCO
        return jsonify({"total":len(todos),"wins":wins,"losses":losses,
                        "abertos":abertos,"win_rate":round(wr,1),
                        "pnl":round(pnl,2),"capital":round(1000+pnl,2)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@flask_app.route("/health")
def health():
    return jsonify({"status": "online", "version": "v12.1"})

@flask_app.route("/")
@flask_app.route("/dashboard")
def dashboard():
    return """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LucSharkTrade Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#0e1117; color:#fff; font-family:Arial,sans-serif; padding:20px; }
  h1 { color:#00d4aa; margin-bottom:20px; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:16px; margin-bottom:24px; }
  .card { background:#1e2130; border-radius:12px; padding:20px; text-align:center; border:1px solid #2d3250; }
  .card-val { font-size:2rem; font-weight:bold; color:#00d4aa; }
  .card-val.red { color:#ff4b4b; }
  .card-val.orange { color:#ffa500; }
  .card-label { font-size:0.85rem; color:#aaa; margin-top:4px; }
  .charts { display:grid; grid-template-columns:2fr 1fr; gap:16px; margin-bottom:24px; }
  .chart-box { background:#1e2130; border-radius:12px; padding:20px; }
  table { width:100%; border-collapse:collapse; background:#1e2130; border-radius:12px; overflow:hidden; }
  th { background:#1b3a6b; padding:12px; text-align:left; font-size:0.85rem; }
  td { padding:10px 12px; border-bottom:1px solid #2d3250; font-size:0.85rem; }
  tr:hover { background:#252840; }
  .win { color:#00d4aa; font-weight:bold; }
  .loss { color:#ff4b4b; font-weight:bold; }
  .open { color:#ffa500; }
  .refresh { color:#aaa; font-size:0.8rem; margin-bottom:16px; }
  @media(max-width:600px){ .charts{grid-template-columns:1fr;} }
</style>
</head>
<body>
<h1>🦈 LucSharkTrade Dashboard v12.1</h1>
<p class="refresh" id="refresh-time">Carregando...</p>
<div class="cards" id="cards"></div>
<div class="charts">
  <div class="chart-box"><canvas id="capitalChart"></canvas></div>
  <div class="chart-box"><canvas id="pieChart"></canvas></div>
</div>
<table>
  <thead><tr><th>#</th><th>Data</th><th>Ativo</th><th>Dir</th><th>Entrada</th><th>Stop</th><th>A1</th><th>A2</th><th>A3</th><th>Resultado</th><th>P&L</th></tr></thead>
  <tbody id="trades-body"></tbody>
</table>
<script>
function pnl(res){
  if(!res)return 0;
  if(res.includes('A3'))return 60;
  if(res.includes('A2'))return 40;
  if(res.startsWith('WIN'))return 20;
  if(res==='LOSS')return -20;
  return 0;
}
function colorRes(res){
  if(!res||res==='ABERTO')return 'open';
  if(res.startsWith('WIN'))return 'win';
  if(res==='LOSS')return 'loss';
  return '';
}
let capitalChartInst=null, pieChartInst=null;
async function loadData(){
  const[tradesResp,statsResp]=await Promise.all([
    fetch('/api/trades').then(r=>r.json()).catch(()=>({trades:[]})),
    fetch('/api/stats').then(r=>r.json()).catch(()=>({}))
  ]);
  const trades=tradesResp.trades||[];
  const stats=statsResp;
  document.getElementById('cards').innerHTML=`
    <div class="card"><div class="card-val">${stats.total||0}</div><div class="card-label">Total Trades</div></div>
    <div class="card"><div class="card-val">${stats.wins||0}</div><div class="card-label">✅ Wins</div></div>
    <div class="card"><div class="card-val red">${stats.losses||0}</div><div class="card-label">❌ Losses</div></div>
    <div class="card"><div class="card-val orange">${stats.abertos||0}</div><div class="card-label">🔄 Abertos</div></div>
    <div class="card"><div class="card-val">${(stats.win_rate||0).toFixed(1)}%</div><div class="card-label">🎯 Win Rate</div></div>
    <div class="card"><div class="card-val ${(stats.pnl||0)>=0?'':'red'}">${(stats.pnl||0)>=0?'+':''}$${(stats.pnl||0).toFixed(2)}</div><div class="card-label">💰 P&L</div></div>
  `;
  const sorted=[...trades].sort((a,b)=>a.id-b.id);
  let cap=1000;
  const caps=sorted.map(t=>{cap+=pnl(t.resultado);return cap;});
  if(capitalChartInst)capitalChartInst.destroy();
  capitalChartInst=new Chart(document.getElementById('capitalChart'),{
    type:'line',
    data:{labels:sorted.map((_,i)=>i+1),datasets:[{label:'Capital',data:caps,
      borderColor:'#00d4aa',backgroundColor:'rgba(0,212,170,0.1)',fill:true,tension:0.3}]},
    options:{plugins:{legend:{display:false}},scales:{y:{ticks:{callback:v=>'$'+v}}},
      responsive:true,maintainAspectRatio:true}
  });
  if(pieChartInst)pieChartInst.destroy();
  pieChartInst=new Chart(document.getElementById('pieChart'),{
    type:'doughnut',
    data:{labels:['Wins','Losses','Abertos'],
      datasets:[{data:[stats.wins||0,stats.losses||0,stats.abertos||0],
        backgroundColor:['#00d4aa','#ff4b4b','#ffa500']}]},
    options:{plugins:{legend:{position:'bottom',labels:{color:'#fff'}}},responsive:true}
  });
  document.getElementById('trades-body').innerHTML=trades.map(t=>`<tr>
    <td>${t.id}</td><td>${(t.criado_em||'').slice(0,10)}</td>
    <td><b>${t.ativo}</b></td>
    <td style="color:${t.direcao==='LONG'?'#00d4aa':'#ff4b4b'}">${t.direcao}</td>
    <td>$${t.entrada}</td><td>$${t.stop}</td>
    <td>$${t.a1}</td><td>$${t.a2}</td><td>$${t.a3}</td>
    <td class="${colorRes(t.resultado)}">${t.resultado||'ABERTO'}</td>
    <td class="${pnl(t.resultado)>=0?'win':'loss'}">${pnl(t.resultado)>=0?'+':''}$${pnl(t.resultado)}</td>
  </tr>`).join('');
  document.getElementById('refresh-time').textContent=
    'Atualizado: '+new Date().toLocaleTimeString('pt-BR')+' BRT | v12.1 — Auto-refresh 30s';
}
loadData();
setInterval(loadData,30000);
</script>
</body></html>"""

# ─────────────────────────────────────────────
# COMANDOS TELEGRAM
# ─────────────────────────────────────────────
def processar_comando(texto):
    partes = texto.strip().split()
    cmd    = partes[0].lower()

    if cmd in ["/start", "/ajuda"]:
        return (
            "🤖 <b>LucSharkTrade v12.1 — Comandos</b>\n\n"
            "<b>📊 TRADES</b>\n"
            "/trade ATIVO DIR ENTRADA STOP A1 A2 A3 TF_CTX TF_ENT\n"
            "/resultado ATIVO WIN_A1 | WIN_A2 | WIN_A3 | LOSS\n"
            "/trades — trades abertos\n"
            "/relatorio — estatísticas e P&L\n"
            "/semana — relatório da semana\n\n"
            "<b>🔍 SCANNER</b>\n"
            "/scan — rodar scanner agora\n"
            "/ativos — exchanges monitoradas\n\n"
            "<b>🚫 BLACKLIST</b>\n"
            "/bloquear ATIVO [motivo]\n"
            "/desbloquear ATIVO\n"
            "/blacklist — ver bloqueados\n\n"
            "<b>⚙️ SISTEMA</b>\n"
            "/parar — limpar fila\n"
            "/status — status do bot\n"
            "/debug — diagnóstico da API\n"
            "/ajuda — este menu"
        )

    elif cmd == "/trade":
        if len(partes) < 10:
            return "❌ Formato: /trade ATIVO DIR ENTRADA STOP A1 A2 A3 TF_CTX TF_ENT"
        try:
            ativo, direcao = partes[1].upper(), partes[2].upper()
            entrada, stop  = float(partes[3]), float(partes[4])
            a1, a2, a3     = float(partes[5]), float(partes[6]), float(partes[7])
            tf_ctx, tf_ent = partes[8], partes[9]
            tid = salvar_trade(ativo, direcao, entrada, stop, a1, a2, a3, tf_ctx, tf_ent)
            return (
                f"✅ <b>Trade #{tid} cadastrado!</b>\n"
                f"📊 {ativo} {direcao}\n"
                f"📥 Entrada: ${entrada} | Stop: ${stop}\n"
                f"🎯 A1: ${a1} | A2: ${a2} | A3: ${a3}\n"
                f"⏱ {tf_ctx}/{tf_ent} | 🔄 Monitorando 24/7..."
            )
        except Exception as e:
            return f"❌ Erro: {e}"

    elif cmd == "/resultado":
        if len(partes) < 3:
            return "❌ Formato: /resultado ATIVO WIN_A1 | LOSS"
        ativo = partes[1].upper()
        res   = " ".join(partes[2:]).upper()
        atualizar_resultado(ativo, res)
        return f"✅ {ativo} → {res}"

    elif cmd == "/trades":
        rows = listar_trades()
        if not rows:
            return "📭 Nenhum trade registrado."
        linhas = ["📊 <b>Últimos Trades</b>\n"]
        for r in rows:
            tid, ativo, direcao, entrada, stop, a1, a2, a3, _, _, resultado, criado = r
            emoji = "🟢" if direcao == "LONG" else "🔴"
            linhas.append(f"{emoji} #{tid} {ativo} {direcao} ${entrada} → {resultado or 'ABERTO'} ({criado})")
        return "\n".join(linhas)

    elif cmd == "/relatorio":
        total, wins, loss, abertos, wr = relatorio()
        RISCO = 20
        conn  = sqlite3.connect("trades.db")
        c     = conn.cursor()
        c.execute("SELECT resultado FROM trades")
        todos = c.fetchall()
        conn.close()
        pnl = 0
        for r in todos:
            res = r[0] or ""
            if "A3" in res:     pnl += RISCO * 3
            elif "A2" in res:   pnl += RISCO * 2
            elif "WIN" in res:  pnl += RISCO
            elif res == "LOSS": pnl -= RISCO
        sinal = "+" if pnl >= 0 else ""
        return (
            f"📈 <b>Relatório LucSharkTrade</b>\n\n"
            f"Total: {total} | ✅ {wins} | ❌ {loss} | 🔄 {abertos}\n"
            f"🎯 Win Rate: {wr:.1f}%\n"
            f"💰 P&L: {sinal}${pnl:.2f}"
        )

    elif cmd == "/semana":
        relatorio_semanal()
        return None

    elif cmd == "/parar":
        try:
            resultado_flush = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": 0, "limit": 100, "timeout": 0}, timeout=10
            ).json()
            pendentes = resultado_flush.get("result", [])
            if pendentes:
                novo_offset = pendentes[-1]["update_id"] + 1
                requests.get(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                    params={"offset": novo_offset, "limit": 1, "timeout": 0}, timeout=10
                )
                n = len(pendentes)
            else:
                n = 0
            enviar_telegram(
                f"🛑 <b>PARAR executado</b>\n"
                f"✅ {n} mensagens pendentes descartadas\n"
                f"✅ Fila limpa — bot aguardando novos comandos."
            )
        except Exception as e:
            enviar_telegram(f"Erro ao parar: {e}")
        return None

    elif cmd == "/scan":
        return "SCAN_SOLICITADO"

    elif cmd == "/debug":
        return "DEBUG_SOLICITADO"

    elif cmd == "/bloquear":
        if len(partes) < 2:
            return "❌ Formato: /bloquear ATIVO [motivo]"
        ativo  = partes[1].upper()
        motivo = " ".join(partes[2:]) if len(partes) > 2 else "Manual"
        adicionar_blacklist(ativo, motivo)
        return f"🚫 {ativo} adicionado à blacklist\nMotivo: {motivo}"

    elif cmd == "/desbloquear":
        if len(partes) < 2:
            return "❌ Formato: /desbloquear ATIVO"
        remover_blacklist(partes[1].upper())
        return f"✅ {partes[1].upper()} removido da blacklist"

    elif cmd == "/blacklist":
        bl = get_blacklist()
        if not bl:
            return "📋 Blacklist vazia"
        return "🚫 <b>Blacklist</b>\n" + "\n".join(f"  • {a}" for a in sorted(bl))

    elif cmd == "/ativos":
        linhas = ["📡 <b>Ativos — Multi-Exchange</b>\n"]
        total  = 0
        for ex in EXCHANGES_CONFIG:
            if ex["instance"]:
                n = len([m for m in ex["instance"].markets.values()
                         if m.get("active") and m.get("quote") in ["USDT","USDC"]])
                linhas.append(f"  ✅ {ex['label']}: {n} ativos")
                total += n
            else:
                linhas.append(f"  ❌ {ex['label']}: offline")
        linhas += [f"\n📊 Total: {total}", f"⏱ TF: 15M | Vol >5x | RSI <40 ou >60"]
        return "\n".join(linhas)

    elif cmd == "/status":
        brt       = brt_agora().strftime("%d/%m/%Y %H:%M BRT")
        ex_online = sum(1 for ex in EXCHANGES_CONFIG if ex["instance"])
        return (
            f"✅ <b>LucSharkTrade v12.1 ONLINE</b>\n"
            f"🕐 {brt}\n"
            f"📡 Exchanges: {ex_online}/3 online\n"
            f"💰 Capital: ${CAPITAL_INICIAL:,.2f}\n"
            f"🔧 Monitoramento: HIGH/LOW intracandle\n"
            f"🔧 Tolerância entrada: 0.5%"
        )

    return None

# ─────────────────────────────────────────────
# FLASK THREAD
# ─────────────────────────────────────────────
def iniciar_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    init_db()
    init_exchanges()

    flask_thread = threading.Thread(target=iniciar_flask, daemon=True)
    flask_thread.start()
    log.info("Flask API iniciado")

    brt = brt_agora().strftime("%d/%m/%Y %H:%M BRT")

    # Evitar mensagem duplicada em restarts rápidos
    try:
        conn_s = sqlite3.connect("trades.db")
        cs     = conn_s.cursor()
        cs.execute("SELECT valor FROM sistema_log WHERE chave='ultimo_start'")
        row       = cs.fetchone()
        agora_ts  = time.time()
        enviar_online = True
        if row:
            ultimo_start = float(row[0])
            if agora_ts - ultimo_start < 300:
                enviar_online = False
        cs.execute("INSERT OR REPLACE INTO sistema_log VALUES ('ultimo_start', ?, ?)",
                   (str(agora_ts), brt))
        conn_s.commit()
        conn_s.close()
    except:
        enviar_online = True

    if enviar_online:
        enviar_telegram(
            f"🚀 <b>LucSharkTrade v12.1 ONLINE!</b>\n"
            f"📅 {brt}\n\n"
            f"✅ Monitoramento HIGH/LOW intracandle\n"
            f"✅ Tolerância entrada: 0.5%\n"
            f"✅ Stop com prioridade máxima\n"
            f"✅ Alertas sem duplicatas\n\n"
            f"Envie /ajuda para ver os comandos."
        )

    # Descartar mensagens pendentes
    ultimo_offset = None
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": 0, "limit": 100, "timeout": 0}, timeout=10
        ).json()
        pending = r.get("result", [])
        if pending:
            ultimo_offset = pending[-1]["update_id"] + 1
            requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": ultimo_offset, "limit": 1, "timeout": 0}, timeout=10
            )
            log.info(f"Descartadas {len(pending)} mensagens pendentes.")
    except Exception as e:
        log.warning(f"Erro ao descartar pendentes: {e}")
        ultimo_offset = None

    while True:
        try:
            updates = get_updates(ultimo_offset)
        except Exception as e:
            log.error(f"get_updates erro: {e}")
            time.sleep(5)
            continue

        for upd in updates:
            update_id = upd.get("update_id")
            if update_id is None:
                continue
            ultimo_offset = update_id + 1
            msg   = upd.get("message", {})
            texto = msg.get("text", "")
            if not texto or not texto.startswith("/"):
                continue
            msg_date = msg.get("date", 0)
            if msg_date and (time.time() - msg_date) > 60:
                log.debug(f"Mensagem antiga ignorada: {texto}")
                continue
            log.info(f"Comando recebido: {texto}")
            try:
                resposta = processar_comando(texto)
                if resposta == "SCAN_SOLICITADO":
                    enviar_telegram("🔍 Scanner iniciado manualmente...")
                    rodar_scanner()
                elif resposta == "DEBUG_SOLICITADO":
                    rodar_scanner_debug()
                elif resposta:
                    enviar_telegram(resposta)
            except Exception as e:
                log.error(f"Erro processando {texto}: {e}")

        monitorar_trades()

        # Relatórios automáticos
        agora_brt = brt_agora()
        if agora_brt.hour == 18 and agora_brt.minute == 0:
            relatorio_diario()
            time.sleep(60)
        if agora_brt.weekday() == 0 and agora_brt.hour == 9 and agora_brt.minute == 0:
            relatorio_semanal()
            time.sleep(60)

        time.sleep(INTERVALO_SEG)

if __name__ == "__main__":
    main()
