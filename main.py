import os
import time
import requests
import logging
import sqlite3
from datetime import datetime, timezone, timedelta

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
COINALYZE_KEY   = os.environ.get("COINALYZE_KEY", "376762b9-d136-4457-a192-9cd0a7865d43")
COINALYZE_BASE  = "https://api.coinalyze.net/v1"

# ── Parâmetros do scanner ──
TIMEFRAME_SCAN     = "minute15"  # official LBank value
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
    # Controle persistente de alertas — evita duplicatas mesmo após restart
    c.execute("""
        CREATE TABLE IF NOT EXISTS alertas_log (
            chave TEXT PRIMARY KEY,
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
    params = {"timeout": 30, "offset": offset}
    try:
        r = requests.get(url, params=params, timeout=35)
        return r.json().get("result", [])
    except:
        return []

# ─────────────────────────────────────────────
# LBANK API
# ─────────────────────────────────────────────
LBANK_BASE = "https://api.lbank.info"

def buscar_todos_pares():
    try:
        r = requests.get(f"{LBANK_BASE}/v2/currencyPairs.do", timeout=15)
        dados = r.json()
        if dados.get("result") == "true":
            return dados.get("data", [])
    except Exception as e:
        log.error(f"Erro ao buscar pares: {e}")
    return []

def buscar_ticker_24h():
    try:
        r = requests.get(f"{LBANK_BASE}/v2/ticker/24hr.do?symbol=all", timeout=15)
        dados = r.json()
        if dados.get("result") == "true":
            return {d["symbol"]: d for d in dados.get("data", [])}
    except Exception as e:
        log.error(f"Erro ticker 24h: {e}")
    return {}

def extrair_volume(ticker_data):
    """Extrai volume do ticker tentando múltiplos campos."""
    if not ticker_data:
        return 0
    for campo in ["turnover", "vol", "volume", "quoteVolume", "quote_volume"]:
        val = ticker_data.get(campo, 0)
        try:
            v = float(val)
            if v > 0:
                return v
        except:
            pass
    return 0

# LBank timeframe map (ccxt standard)
LBANK_TF_MAP = {
    "minute15":    "15m",
    "minute5":     "5m",
    "minute1":     "1m",
    "minute30":    "30m",
    "hour1":       "1h",
    "hour4":       "4h",
    "day1":        "1d",
    "kline_15min": "15m",
    "kline_5min":  "5m",
    "kline_1h":    "1h",
    # already ccxt format
    "15m": "15m", "5m": "5m", "1h": "1h",
}

# Global ccxt exchange instance
try:
    import ccxt as _ccxt
    _exchange = _ccxt.lbank({"enableRateLimit": True})
    _exchange.load_markets()
    CCXT_AVAILABLE = True
    log.info("ccxt LBank carregado com sucesso")
except Exception as e:
    CCXT_AVAILABLE = False
    log.warning(f"ccxt indisponivel: {e}")

def buscar_candles(symbol, tf=None, tamanho=50):
    """
    Busca candles via ccxt (biblioteca testada e mantida).
    Retorna: [[timestamp, open, high, low, close, volume], ...]
    """
    raw_tf   = tf or TIMEFRAME_SCAN
    ccxt_tf  = LBANK_TF_MAP.get(raw_tf, "15m")
    # Normalizar symbol para formato ccxt: btc_usdt -> BTC/USDT
    sym_upper = symbol.upper().replace("_", "/")

    if CCXT_AVAILABLE:
        try:
            ohlcv = _exchange.fetch_ohlcv(sym_upper, ccxt_tf, limit=tamanho)
            # ccxt returns [[ts_ms, o, h, l, c, v], ...]
            # Convert to [ts_sec, o, h, l, c, v]
            return [[c[0]//1000, c[1], c[2], c[3], c[4], c[5]] for c in ohlcv if c]
        except Exception as e:
            log.debug(f"ccxt {symbol}: {e}")

    # Fallback: requests direto com parametros corretos
    try:
        ts_sec = int(time.time())
        r = requests.get("https://api.lbank.info/v2/kline.do", params={
            "symbol": symbol.lower(),
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

# ─────────────────────────────────────────────
# INDICADORES
# ─────────────────────────────────────────────
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

# ─────────────────────────────────────────────
# ANÁLISE DE SINAL
# ─────────────────────────────────────────────
def parse_candle(c):
    """LBank kline format: [timestamp, open, high, low, close, volume]
    Some endpoints return strings, handle both."""
    try:
        if isinstance(c, (list, tuple)) and len(c) >= 6:
            return {
                "ts":  int(float(c[0])),
                "o":   float(c[1]),
                "h":   float(c[2]),
                "l":   float(c[3]),
                "c":   float(c[4]),
                "v":   float(c[5])
            }
    except:
        pass
    return None

def analisar_ativo(symbol, candles):
    # Parse all candles robustly
    parsed = [parse_candle(c) for c in candles]
    parsed = [p for p in parsed if p is not None]

    if len(parsed) < 20:
        return None

    highs   = [p["h"] for p in parsed]
    lows    = [p["l"] for p in parsed]
    closes  = [p["c"] for p in parsed]
    volumes = [p["v"] for p in parsed]

    preco  = closes[-1]
    vol_rel = volume_relativo(volumes)
    rsi    = calcular_rsi(closes)
    vwap   = calcular_vwap(candles[-20:])
    ema9   = calcular_ema(closes, 9)
    ema21  = calcular_ema(closes, 21)

    janela   = closes[-MIN_CANDLES_RANGE:]
    suporte  = min(janela)
    resist   = max(janela)

    sinais = []

    # 1. Breakout de range
    if vol_rel >= MULT_ALERTA:
        if closes[-1] > resist:
            forca = "FORTE" if vol_rel >= MULT_FORTE else "MÉDIO" if vol_rel >= MULT_MEDIO else "ALERTA"
            sinais.append({"tipo": f"🚀 Breakout LONG [{forca}]", "forca": forca,
                           "detalhe": f"Rompeu ${resist:.4f} | Vol {vol_rel}x"})
        elif closes[-1] < suporte:
            forca = "FORTE" if vol_rel >= MULT_FORTE else "MÉDIO" if vol_rel >= MULT_MEDIO else "ALERTA"
            sinais.append({"tipo": f"📉 Breakout SHORT [{forca}]", "forca": forca,
                           "detalhe": f"Rompeu ${suporte:.4f} | Vol {vol_rel}x"})

    # 2. Compressão de volatilidade (pré-Spring Wyckoff)
    if len(closes) >= 15:
        amp_rec = sum(highs[i] - lows[i] for i in range(-5, 0)) / 5
        amp_ant = sum(highs[i] - lows[i] for i in range(-15, -5)) / 10
        vol_rec = sum(volumes[-5:]) / 5
        vol_ant = sum(volumes[-15:-5]) / 10
        if amp_ant > 0 and amp_rec < amp_ant * 0.6 and vol_rec < vol_ant * 0.8:
            sinais.append({"tipo": "⚡ Compressão [ALERTA]", "forca": "ALERTA",
                           "detalhe": f"Volatilidade -{round((1-amp_rec/amp_ant)*100)}% | Vol caindo → Spring próximo"})

    # 3. Toque VWAP
    if vwap > 0 and abs(preco - vwap) / vwap < 0.003 and vol_rel >= MULT_MEDIO:
        direcao = "LONG" if preco > vwap else "SHORT"
        forca = "MÉDIO" if vol_rel >= MULT_MEDIO else "ALERTA"
        sinais.append({"tipo": f"📍 Toque VWAP {direcao} [{forca}]", "forca": forca,
                       "detalhe": f"VWAP ${vwap:.4f} | Preço ${preco:.4f} | Vol {vol_rel}x"})

    # 4. RSI extremo com reversão
    if rsi < RSI_SOBREVENDA and closes[-1] > closes[-2]:
        sinais.append({"tipo": "🔄 RSI Reversão LONG [ALERTA]", "forca": "ALERTA",
                       "detalhe": f"RSI {rsi} | Sobrevenda com vela de recuperação"})
    elif rsi > RSI_SOBRECOMPRA and closes[-1] < closes[-2]:
        sinais.append({"tipo": "🔄 RSI Reversão SHORT [ALERTA]", "forca": "ALERTA",
                       "detalhe": f"RSI {rsi} | Sobrecompra com vela de rejeição"})

    # 5. Volume climático (possível Spring/Upthrust)
    if vol_rel >= 3.0:
        sinais.append({"tipo": "🌊 Volume Climático [FORTE]", "forca": "FORTE",
                       "detalhe": f"Vol {vol_rel}x → possível Spring ou Upthrust Wyckoff"})

    # 6. EMA Cross
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

    # Score de qualidade 0-100
    score = 0
    # Volume (até 40 pts)
    if vol_rel >= 10:   score += 40
    elif vol_rel >= 5:  score += 25
    elif vol_rel >= 2:  score += 10

    # RSI extremo (até 20 pts)
    if rsi < 25 or rsi > 75:  score += 20
    elif rsi < 35 or rsi > 65: score += 10

    # Alinhamento com tendência EMA (até 20 pts)
    if len(ema9) >= 2 and len(ema21) >= 2:
        bull_trend = ema9[-1] > ema21[-1] and closes[-1] > ema9[-1]
        bear_trend = ema9[-1] < ema21[-1] and closes[-1] < ema9[-1]
        long_sinal = any("LONG" in s["tipo"] for s in sinais)
        short_sinal = any("SHORT" in s["tipo"] for s in sinais)
        if (long_sinal and bull_trend) or (short_sinal and bear_trend):
            score += 20

    # Força do sinal (até 20 pts)
    if forca_max == "FORTE":  score += 20
    elif forca_max == "MÉDIO": score += 10

    return {
        "symbol": symbol.upper(),
        "preco": preco,
        "vol_rel": vol_rel,
        "rsi": rsi,
        "vwap": round(vwap, 4),
        "suporte": round(suporte, 4),
        "resistencia": round(resist, 4),
        "sinais": sinais,
        "forca_max": forca_max,
        "score": min(score, 100)
    }

# ─────────────────────────────────────────────
# FORMATAÇÃO DE MENSAGEM
# ─────────────────────────────────────────────
def formatar_sinal(r):
    brt = brt_agora().strftime("%d/%m/%Y %H:%M BRT")
    linhas = [
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 <b>SCANNER LucSharkTrade</b>",
        f"🕐 {brt} | TF: 15M",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"<b>{r['symbol']}</b> | 💲{r['preco']:.6g}",
        f"📈 Vol relativo: {r['vol_rel']}x",
        f"📉 RSI: {r['rsi']} | VWAP: {r['vwap']}",
        f"🟩 Sup: {r['suporte']} | 🟥 Res: {r['resistencia']}",
        "",
    ]
    for s in r["sinais"]:
        linhas.append(f"{s['tipo']}")
        linhas.append(f"   └ {s['detalhe']}")
    linhas += ["", "👁 <b>Abra o gráfico e envie o print para análise!</b>"]
    return "\n".join(linhas)

# ─────────────────────────────────────────────
# SCANNER PRINCIPAL
# ─────────────────────────────────────────────
def get_blacklist():
    """Retorna set de ativos na blacklist."""
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
    c.execute("INSERT OR REPLACE INTO blacklist VALUES (?,?,?)",
              (ativo.upper(), motivo, agora))
    conn.commit()
    conn.close()

def remover_blacklist(ativo):
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("DELETE FROM blacklist WHERE ativo=?", (ativo.upper(),))
    conn.commit()
    conn.close()

def normalizar_symbol_coinalyze(symbol):
    """
    Converte qualquer formato de símbolo para o formato agregado do Coinalyze.
    BTCUSDT / BTC-USDT / BTC_USDT / BTC → BTCUSDT_PERP.A
    Sufixo .A = agregado de TODOS os perpetuais (Binance + Bybit + OKX + Gate + Bitget + 20+ exchanges)
    Máxima visão de mercado possível.
    """
    s = symbol.upper().strip()
    # Remover sufixos de exchange já existentes
    for suf in ["_PERP.A", "_PERP.0", "_PERP.6", ".P", "-PERP"]:
        s = s.replace(suf, "")
    # Normalizar separadores
    s = s.replace("-", "").replace("_", "").replace("/", "")
    # Garantir que termina em USDT
    if not s.endswith("USDT") and not s.endswith("USDC"):
        s = s + "USDT"
    # Aplicar sufixo agregado
    return f"{s}_PERP.A"

def buscar_sentimento(symbol):
    """
    Busca dados de sentimento agregados do Coinalyze (API gratuita).
    Usa sufixo _PERP.A = agregado multi-exchange (Binance+Bybit+OKX+Gate+Bitget+20+).
    Endpoints usados em paralelo:
      - /funding-rate       → Funding Rate atual (OI-weighted agregado)
      - /open-interest      → OI total em USD (convert_to_usd=true)
      - /long-short-ratio-history → L/S Ratio última hora
      - /liquidation-history      → Liquidações últimas 24h (long + short)
    """
    sym_cg  = normalizar_symbol_coinalyze(symbol)
    headers = {"api_key": COINALYZE_KEY}
    ts_now  = int(time.time())
    ts_1h   = ts_now - 3600
    ts_24h  = ts_now - 86400
    resultado = {"symbol_cg": sym_cg}

    # 1. Funding Rate atual (OI-weighted = mais representativo do mercado)
    try:
        r = requests.get(
            f"{COINALYZE_BASE}/funding-rate",
            params={"symbols": sym_cg},
            headers=headers, timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            if data:
                resultado["funding"] = round(float(data[0].get("value", 0)) * 100, 4)
    except Exception as e:
        log.debug(f"Funding {sym_cg}: {e}")

    # 2. Funding Rate previsto (próximo ciclo)
    try:
        r = requests.get(
            f"{COINALYZE_BASE}/predicted-funding-rate",
            params={"symbols": sym_cg},
            headers=headers, timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            if data:
                resultado["funding_pred"] = round(float(data[0].get("value", 0)) * 100, 4)
    except Exception as e:
        log.debug(f"Funding previsto {sym_cg}: {e}")

    # 3. Open Interest em USD (agregado de todas as exchanges)
    try:
        r = requests.get(
            f"{COINALYZE_BASE}/open-interest",
            params={"symbols": sym_cg, "convert_to_usd": "true"},
            headers=headers, timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            if data:
                resultado["oi_usd"] = float(data[0].get("value", 0))
    except Exception as e:
        log.debug(f"OI {sym_cg}: {e}")

    # 4. Long/Short Ratio — última hora (history para pegar tendência)
    try:
        r = requests.get(
            f"{COINALYZE_BASE}/long-short-ratio-history",
            params={
                "symbols":  sym_cg,
                "interval": "1hour",
                "from":     ts_1h,
                "to":       ts_now
            },
            headers=headers, timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            if data and data[0].get("history"):
                hist = data[0]["history"]
                ultimo = hist[-1]
                # r=ratio, l=long%, s=short%
                resultado["ls_ratio"] = round(float(ultimo.get("r", 1)), 3)
                resultado["ls_long_pct"]  = round(float(ultimo.get("l", 50)), 1)
                resultado["ls_short_pct"] = round(float(ultimo.get("s", 50)), 1)
    except Exception as e:
        log.debug(f"L/S {sym_cg}: {e}")

    # 5. Liquidações últimas 24h (long + short separados)
    try:
        r = requests.get(
            f"{COINALYZE_BASE}/liquidation-history",
            params={
                "symbols":        sym_cg,
                "interval":       "1hour",
                "from":           ts_24h,
                "to":             ts_now,
                "convert_to_usd": "true"
            },
            headers=headers, timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            if data and data[0].get("history"):
                hist = data[0]["history"]
                # l=long liquidations, s=short liquidations
                liq_long  = sum(float(h.get("l", 0)) for h in hist)
                liq_short = sum(float(h.get("s", 0)) for h in hist)
                resultado["liq_long_usd"]  = liq_long
                resultado["liq_short_usd"] = liq_short
    except Exception as e:
        log.debug(f"Liq {sym_cg}: {e}")

    return resultado if len(resultado) > 1 else None

def fmt_usd(val):
    """Formata valor em USD de forma compacta."""
    if val >= 1_000_000_000:
        return f"${val/1_000_000_000:.2f}B"
    elif val >= 1_000_000:
        return f"${val/1_000_000:.1f}M"
    elif val >= 1_000:
        return f"${val/1_000:.0f}K"
    return f"${val:,.0f}"

def formatar_sentimento(sent):
    """
    Formata dados de sentimento agregados (multi-exchange via Coinalyze _PERP.A).
    """
    if not sent or len(sent) <= 1:
        return ""

    sym   = sent.get("symbol_cg", "")
    linhas = [f"📡 <b>SENTIMENTO AGREGADO</b> — {sym}"]

    if "funding" in sent:
        fr = sent["funding"]
        if fr < -0.005:
            emoji, desc = "🟢🟢", "muito negativo → forte pressão LONG"
        elif fr < 0:
            emoji, desc = "🟢", "negativo → pressão LONG"
        elif fr < 0.01:
            emoji, desc = "⚪", "neutro"
        elif fr < 0.03:
            emoji, desc = "🔴", "positivo → pressão SHORT"
        else:
            emoji, desc = "🔴🔴", "muito positivo → forte pressão SHORT"
        linhas.append(f"  Funding atual: {emoji} {fr:+.4f}% ({desc})")

    if "funding_pred" in sent:
        fp = sent["funding_pred"]
        linhas.append(f"  Funding previsto: {fp:+.4f}%")

    if "oi_usd" in sent:
        linhas.append(f"  OI agregado: {fmt_usd(sent['oi_usd'])}")

    if "ls_ratio" in sent:
        ls = sent["ls_ratio"]
        lp = sent.get("ls_long_pct", 0)
        sp = sent.get("ls_short_pct", 0)
        if ls > 1.5:
            emoji, desc = "🟢🟢", "longs dominam fortemente"
        elif ls > 1.1:
            emoji, desc = "🟢", "longs dominam"
        elif ls < 0.67:
            emoji, desc = "🔴🔴", "shorts dominam fortemente"
        elif ls < 0.9:
            emoji, desc = "🔴", "shorts dominam"
        else:
            emoji, desc = "⚪", "equilibrado"
        linhas.append(f"  L/S Ratio: {emoji} {ls} ({lp:.1f}%L / {sp:.1f}%S — {desc})")

    if "liq_long_usd" in sent and "liq_short_usd" in sent:
        ll    = sent["liq_long_usd"]
        ls_liq = sent["liq_short_usd"]
        total  = ll + ls_liq
        if total > 0:
            dom = "longs liq." if ll > ls_liq else "shorts liq."
            linhas.append(f"  Liq 24h: {fmt_usd(total)} ({dom} | L:{fmt_usd(ll)} S:{fmt_usd(ls_liq)})")

    return "\n".join(linhas)

def rodar_scanner_debug():
    """Roda em 5 ativos e reporta exatamente o que encontra para diagnóstico."""
    brt = brt_agora().strftime("%d/%m/%Y %H:%M BRT")
    enviar_telegram(f"🔧 <b>MODO DEBUG</b>\n{brt}\nTestando 5 ativos para diagnóstico...")

    tickers   = buscar_ticker_24h()
    pares_raw = buscar_todos_pares()
    amostra   = pares_raw[:5] if pares_raw else []

    msg = [f"📋 Total pares: {len(pares_raw)}\nTickers disponíveis: {len(tickers)}\n"]

    import time as time_mod
    ts = int(time_mod.time())

    msg.append("<b>Testando v1/kline.do (oficial):</b>")
    for symbol in ["btc_usdt", "eth_usdt", "sol_usdt"]:
        try:
            r = requests.get("https://api.lbkex.com/v1/kline.do", params={
                "symbol": symbol, "size": 3,
                "type": "minute15", "time": ts
            }, timeout=8)
            dados = r.json()
            ok = isinstance(dados, list) and len(dados) > 0
            msg.append(f"  {symbol}: {'OK ' + str(len(dados)) + ' candles' if ok else 'ERRO: ' + str(dados)[:60]}")
            if ok:
                msg.append(f"    Candle: {dados[0]}")
        except Exception as e:
            msg.append(f"  {symbol}: EXCECAO {e}")

    msg.append("")
    msg.append("<b>Ticker 24h (amostra):</b>")
    for symbol in amostra[:3]:
        ticker = tickers.get(symbol, {})
        vol = extrair_volume(ticker)
        msg.append(f"  {symbol}: vol={vol:,.0f} | {str(ticker)[:60]}")

    enviar_telegram("\n".join(msg))

def rodar_scanner():
    brt = brt_agora().strftime("%d/%m/%Y %H:%M BRT")
    enviar_telegram(
        f"🔍 <b>SCANNER LucSharkTrade</b>\n"
        f"{brt} | TF: 15M\n"
        f"Analisando ativos... Aguarde."
    )

    tickers   = buscar_ticker_24h()
    pares_raw = buscar_todos_pares()

    # Filtrar por volume mínimo
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

    prioridade_max = []  # vol > 10x
    alta_prioridade = [] # vol 5-10x

    blacklist = get_blacklist()

    for symbol in pares:
        # Ignorar ativos na blacklist
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

            # Volume relativo
            vol_rel = volume_relativo(volumes)

            # Filtro principal: apenas vol >= 5x
            if vol_rel < 5.0:
                continue

            # RSI
            rsi = calcular_rsi(closes)

            # Filtro RSI: apenas extremos
            # LONG: RSI < 40 + volume alto
            # SHORT: RSI > 60 + volume alto
            if rsi >= 40 and rsi <= 60:
                continue  # RSI neutro = ignorar

            # Determinar viés
            if rsi < 40:
                vies = "LONG"
                vies_emoji = "🟢"
            else:
                vies = "SHORT"
                vies_emoji = "🔴"

            # VWAP e suporte/resistência
            vwap     = calcular_vwap(parsed[-20:])
            suporte  = round(min(p["l"] for p in parsed[-5:]), 6)
            resist   = round(max(p["h"] for p in parsed[-5:]), 6)
            preco    = closes[-1]

            resultado = {
                "symbol":   symbol.upper(),
                "preco":    preco,
                "vol_rel":  vol_rel,
                "rsi":      rsi,
                "vwap":     round(vwap, 6),
                "suporte":  suporte,
                "resist":   resist,
                "vies":     vies,
                "vies_emoji": vies_emoji
            }

            if vol_rel >= 10.0:
                prioridade_max.append(resultado)
            else:
                alta_prioridade.append(resultado)

            time.sleep(0.15)

        except Exception as e:
            log.error(f"Erro {symbol}: {e}")

    # Ordenar por volume relativo (maior primeiro)
    prioridade_max.sort(key=lambda x: x["vol_rel"], reverse=True)
    alta_prioridade.sort(key=lambda x: x["vol_rel"], reverse=True)

    total = len(prioridade_max) + len(alta_prioridade)

    if total == 0:
        enviar_telegram(
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ <b>SCAN CONCLUÍDO</b>\n"
            f"{brt}\n"
            f"Nenhum ativo com Vol ≥5x e RSI extremo.\n"
            f"Mercado sem oportunidades claras agora."
        )
        return

    # Montar mensagem única consolidada ordenada por volume
    linhas = [
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 <b>SCANNER LucSharkTrade</b>",
        f"🕐 {brt}",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"",
    ]

    if prioridade_max:
        linhas.append(f"🚨 <b>PRIORIDADE MÁXIMA — Vol &gt;10x</b>")
        linhas.append("")
        for i, r in enumerate(prioridade_max, 1):
            sent = buscar_sentimento(r["symbol"])
            sent_txt = formatar_sentimento(sent)
            score_bar = "█" * (r.get("score",0)//10) + "░" * (10 - r.get("score",0)//10)
            linhas.append(
                f"{i}. <b>{r['symbol']}</b> {r['vies_emoji']} {r['vies']}"
                f" | Vol <b>{r['vol_rel']}x</b> | RSI {r['rsi']}"
                f" | 💲{r['preco']:.6g}"
            )
            linhas.append(f"   Score: {r.get('score',0)}/100 [{score_bar}]")
            if sent_txt:
                linhas.append(sent_txt)
            linhas.append("")

    if alta_prioridade:
        linhas.append(f"⚡ <b>ALTA PRIORIDADE — Vol 5–10x</b>")
        linhas.append("")
        for i, r in enumerate(alta_prioridade, 1):
            sent = buscar_sentimento(r["symbol"])
            sent_txt = formatar_sentimento(sent)
            score_bar = "█" * (r.get("score",0)//10) + "░" * (10 - r.get("score",0)//10)
            linhas.append(
                f"{i}. <b>{r['symbol']}</b> {r['vies_emoji']} {r['vies']}"
                f" | Vol <b>{r['vol_rel']}x</b> | RSI {r['rsi']}"
                f" | 💲{r['preco']:.6g}"
            )
            linhas.append(f"   Score: {r.get('score',0)}/100 [{score_bar}]")
            if sent_txt:
                linhas.append(sent_txt)
            linhas.append("")

    linhas += [
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Total analisados: {len(pares)}",
        f"",
        f"👁 Escolha o ativo e envie o print para análise!",
    ]

    # Telegram tem limite de 4096 chars — enviar em blocos se necessário
    mensagem = "\n".join(linhas)
    if len(mensagem) <= 4000:
        enviar_telegram(mensagem)
    else:
        # Dividir em blocos
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


def normalizar_symbol_ccxt(ativo):
    """Converte BTCUSDT, BTC-USDT, BTC_USDT → BTC/USDT para ccxt."""
    s = ativo.upper().strip()
    # Já tem barra
    if "/" in s:
        return s
    # Tem hífen: BTC-USDT → BTC/USDT
    if "-" in s:
        return s.replace("-", "/")
    # Tem underscore: BTC_USDT → BTC/USDT
    if "_" in s:
        return s.replace("_", "/")
    # Sem separador: BTCUSDT → tentar split em USDT/USDC/BTC/ETH
    for quote in ["USDT", "USDC", "BTC", "ETH", "BNB"]:
        if s.endswith(quote) and len(s) > len(quote):
            base = s[:-len(quote)]
            return f"{base}/{quote}"
    return s

def buscar_preco_atual(ativo):
    """Busca preço atual via ccxt ticker — mais rápido e confiável que candles."""
    symbol = normalizar_symbol_ccxt(ativo)
    if CCXT_AVAILABLE:
        try:
            ticker = _exchange.fetch_ticker(symbol)
            return {
                "preco": ticker["last"],
                "high":  ticker["high"],
                "low":   ticker["low"],
                "bid":   ticker.get("bid", ticker["last"]),
                "ask":   ticker.get("ask", ticker["last"]),
            }
        except Exception as e:
            log.debug(f"Ticker {symbol}: {e}")
    # Fallback: candles
    sym_lbank = symbol.lower().replace("/", "_")
    candles = buscar_candles(sym_lbank, "minute5", 3)
    if candles:
        parsed = [parse_candle(c) for c in candles]
        parsed = [p for p in parsed if p is not None]
        if parsed:
            return {
                "preco": parsed[-1]["c"],
                "high":  parsed[-1]["h"],
                "low":   parsed[-1]["l"],
                "bid":   parsed[-1]["c"],
                "ask":   parsed[-1]["c"],
            }
    return None

# Controle de alertas persistente (sobrevive a restarts do bot)
_alertas_enviados = {}  # cache em memória para performance

def alerta_ja_enviado(chave):
    """Verifica se alerta já foi enviado — memória primeiro, DB como fallback."""
    if chave in _alertas_enviados:
        return True
    try:
        conn = sqlite3.connect("trades.db")
        c = conn.cursor()
        c.execute("SELECT 1 FROM alertas_log WHERE chave=?", (chave,))
        existe = c.fetchone() is not None
        conn.close()
        if existe:
            _alertas_enviados[chave] = True  # popular cache
        return existe
    except:
        return False

def marcar_alerta(chave):
    """Registra alerta como enviado em memória e no DB."""
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
    """Calcula duração do trade desde a abertura."""
    try:
        from datetime import datetime
        fmt = "%Y-%m-%d %H:%M"
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

def monitorar_trades():
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
        high  = dados["high"]
        low   = dados["low"]

        base   = f"{tid}_{ativo}"
        # Tolerâncias
        tol_zona   = entrada * 0.02   # 2% — alerta de aproximação da zona
        tol_entrada = entrada * 0.003  # 0.3% — alerta de entrada exata

        if direcao == "LONG":

            # ── ALERTA 1: Preço se aproximando da zona de entrada ──
            if preco <= entrada * 1.03 and not alerta_ja_enviado(f"{base}_zona"):
                marcar_alerta(f"{base}_zona")
                enviar_telegram(
                    f"👀 <b>ZONA DE ENTRADA — {ativo} LONG #{tid}</b>\n"
                    f"💲 Preço: ${preco:.6g} | Entrada: ${entrada}\n"
                    f"📍 Preço a {round((preco/entrada-1)*100,2)}% da entrada\n"
                    f"⏳ Aguardando acionamento..."
                )

            # ── ALERTA 2: Preço na entrada exata ──
            elif abs(preco - entrada) <= tol_entrada and not alerta_ja_enviado(f"{base}_entrada"):
                marcar_alerta(f"{base}_entrada")
                enviar_telegram(
                    f"🟢 <b>ENTRADA LONG — {ativo} #{tid}</b>\n"
                    f"💲 Preço: ${preco:.6g}\n"
                    f"📥 Entrada: ${entrada} | Stop: ${stop}\n"
                    f"🎯 A1: ${a1} | A2: ${a2} | A3: ${a3}"
                )

            # ── ALVOS (do maior para o menor) ──
            elif high >= a3 and not alerta_ja_enviado(f"{base}_a3"):
                marcar_alerta(f"{base}_a3")
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

            elif high >= a2 and not alerta_ja_enviado(f"{base}_a2"):
                marcar_alerta(f"{base}_a2")
                atualizar_resultado(ativo, "WIN_A2")
                enviar_telegram(
                    f"🎯 <b>A2 ATINGIDO — {ativo} LONG #{tid}</b>\n"
                    f"💲 Preço: ${preco:.6g} | A2: ${a2}\n"
                    f"✅ Realizar 50% | ⏳ Aguardar A3: ${a3}"
                )

            elif high >= a1 and not alerta_ja_enviado(f"{base}_a1"):
                marcar_alerta(f"{base}_a1")
                atualizar_resultado(ativo, "WIN_A1")
                enviar_telegram(
                    f"🎯 <b>A1 ATINGIDO — {ativo} LONG #{tid}</b>\n"
                    f"💲 Preço: ${preco:.6g} | A1: ${a1}\n"
                    f"✅ Realizar 25%\n"
                    f"🔒 Mover Stop para ${entrada} (breakeven)\n"
                    f"⏳ Aguardar A2: ${a2}"
                )

            elif low <= stop and not alerta_ja_enviado(f"{base}_stop"):
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

        elif direcao == "SHORT":

            # ── ALERTA 1: Preço se aproximando da zona de entrada ──
            if preco >= entrada * 0.97 and not alerta_ja_enviado(f"{base}_zona"):
                marcar_alerta(f"{base}_zona")
                enviar_telegram(
                    f"👀 <b>ZONA DE ENTRADA — {ativo} SHORT #{tid}</b>\n"
                    f"💲 Preço: ${preco:.6g} | Entrada: ${entrada}\n"
                    f"📍 Preço a {round((1-preco/entrada)*100,2)}% da entrada\n"
                    f"⏳ Aguardando acionamento..."
                )

            # ── ALERTA 2: Preço na entrada exata ──
            elif abs(preco - entrada) <= tol_entrada and not alerta_ja_enviado(f"{base}_entrada"):
                marcar_alerta(f"{base}_entrada")
                enviar_telegram(
                    f"🔴 <b>ENTRADA SHORT — {ativo} #{tid}</b>\n"
                    f"💲 Preço: ${preco:.6g}\n"
                    f"📥 Entrada: ${entrada} | Stop: ${stop}\n"
                    f"🎯 A1: ${a1} | A2: ${a2} | A3: ${a3}"
                )

            # ── ALVOS (do maior para o menor) ──
            elif low <= a3 and not alerta_ja_enviado(f"{base}_a3"):
                marcar_alerta(f"{base}_a3")
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

            elif low <= a2 and not alerta_ja_enviado(f"{base}_a2"):
                marcar_alerta(f"{base}_a2")
                atualizar_resultado(ativo, "WIN_A2")
                enviar_telegram(
                    f"🎯 <b>A2 ATINGIDO — {ativo} SHORT #{tid}</b>\n"
                    f"💲 Preço: ${preco:.6g} | A2: ${a2}\n"
                    f"✅ Realizar 50% | ⏳ Aguardar A3: ${a3}"
                )

            elif low <= a1 and not alerta_ja_enviado(f"{base}_a1"):
                marcar_alerta(f"{base}_a1")
                atualizar_resultado(ativo, "WIN_A1")
                enviar_telegram(
                    f"🎯 <b>A1 ATINGIDO — {ativo} SHORT #{tid}</b>\n"
                    f"💲 Preço: ${preco:.6g} | A1: ${a1}\n"
                    f"✅ Realizar 25%\n"
                    f"🔒 Mover Stop para ${entrada} (breakeven)\n"
                    f"⏳ Aguardar A2: ${a2}"
                )

            elif high >= stop and not alerta_ja_enviado(f"{base}_stop"):
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


def relatorio_diario():
    """Relatório automático diário às 18h BRT."""
    brt = brt_agora().strftime("%d/%m/%Y %H:%M BRT")
    total, wins, loss, abertos, wr = relatorio()

    # Calcular P&L fictício (risco 2% por trade = $20 em $1000)
    risco_por_trade = CAPITAL_INICIAL * 0.02
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("SELECT resultado FROM trades WHERE DATE(criado_em) = DATE('now')")
    hoje = c.fetchall()
    conn.close()

    pnl = 0
    wins_hoje  = 0
    loss_hoje  = 0
    for r in hoje:
        res = r[0] or ""
        if "WIN_A1" in res:
            pnl += risco_por_trade * 1.0
            wins_hoje += 1
        elif "WIN_A2" in res:
            pnl += risco_por_trade * 2.0
            wins_hoje += 1
        elif "WIN_A3" in res:
            pnl += risco_por_trade * 3.0
            wins_hoje += 1
        elif res == "LOSS":
            pnl -= risco_por_trade
            loss_hoje += 1

    sinal_pnl = "+" if pnl >= 0 else ""
    emoji_pnl = "📈" if pnl >= 0 else "📉"

    msg = (
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>RELATÓRIO DIÁRIO — LucSharkTrade</b>\n"
        f"📅 {brt}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"\n"
        f"<b>HOJE</b>\n"
        f"✅ Wins: {wins_hoje} | ❌ Losses: {loss_hoje}\n"
        f"{emoji_pnl} P&L: {sinal_pnl}${pnl:.2f}\n"
        f"\n"
        f"<b>ACUMULADO</b>\n"
        f"Total trades: {total}\n"
        f"✅ Wins: {wins} | ❌ Losses: {loss} | 🔄 Abertos: {abertos}\n"
        f"🎯 Win Rate: {wr:.1f}%\n"
        f"💰 Capital: ${CAPITAL_INICIAL:,.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    enviar_telegram(msg)
    log.info("Relatório diário enviado.")

def main():
    init_db()
    brt = brt_agora().strftime("%d/%m/%Y %H:%M BRT")
    enviar_telegram(
        f"🚀 <b>LucSharkTrade v11 ONLINE!</b>\n"
        f"📅 {brt}\n\n"
        f"✅ Scanner 15M ativo\n"
        f"✅ 3 níveis: FORTE ({MULT_FORTE}x) | MÉDIO ({MULT_MEDIO}x) | ALERTA ({MULT_ALERTA}x)\n"
        f"✅ Monitoramento de trades 24/7\n\n"
        f"Envie /ajuda para ver os comandos."
    )

    ultimo_offset = None

    while True:
        updates = get_updates(ultimo_offset)
        for upd in updates:
            ultimo_offset = upd["update_id"] + 1
            texto = upd.get("message", {}).get("text", "")
            if texto.startswith("/"):
                resposta = processar_comando(texto)
                if resposta == "SCAN_SOLICITADO":
                    enviar_telegram("🔍 Scanner iniciado manualmente...")
                    rodar_scanner()
                elif resposta == "DEBUG_SOLICITADO":
                    rodar_scanner_debug()
                elif resposta:
                    enviar_telegram(resposta)

        monitorar_trades()

        # Relatório diário automático às 18h BRT
        agora_brt = brt_agora()
        if agora_brt.hour == 18 and agora_brt.minute == 0:
            relatorio_diario()
            time.sleep(60)  # evitar duplo envio no mesmo minuto

        time.sleep(INTERVALO_SEG)

if __name__ == "__main__":
    main()
