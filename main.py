import os
import time
import requests
import logging
import sqlite3
import threading
import json
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify

flask_app = Flask(__name__)

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CAPITAL_INICIAL  = float(os.environ.get("CAPITAL_INICIAL", "1000"))
TOLERANCIA_PCT   = float(os.environ.get("TOLERANCIA_PCT", "0.005"))
INTERVALO_SEG    = int(os.environ.get("INTERVALO_SEG", "30"))
INTERVALO_SCAN   = int(os.environ.get("INTERVALO_SCAN", "3600"))
MIN_VOLUME_24H   = float(os.environ.get("MIN_VOLUME_24H", "100000"))
COINALYZE_KEY    = os.environ.get("COINALYZE_KEY", "376762b9-d136-4457-a192-9cd0a7865d43")
COINALYZE_BASE   = "https://api.coinalyze.net/v1"

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
    # v12.5: alertas de nivel de preco independentes de trades
    c.execute("""
        CREATE TABLE IF NOT EXISTS alertas_nivel (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ativo TEXT NOT NULL,
            direcao TEXT NOT NULL,
            nivel REAL NOT NULL,
            condicao TEXT NOT NULL,
            nota TEXT,
            ativo_flag INTEGER DEFAULT 1,
            disparado INTEGER DEFAULT 0,
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

# ── FIX CRÍTICO: atualizar_resultado sem ORDER BY no UPDATE (inválido no SQLite) ──
def atualizar_resultado(ativo, resultado):
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    # Busca o id do último trade aberto para esse ativo
    c.execute("""
        SELECT id FROM trades
        WHERE ativo=? AND resultado='ABERTO'
        ORDER BY id DESC LIMIT 1
    """, (ativo.upper(),))
    row = c.fetchone()
    if row:
        trade_id = row[0]
        c.execute("UPDATE trades SET resultado=? WHERE id=?", (resultado, trade_id))
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

LBANK_BASE = "https://api.lbank.info"

EXCHANGES_CONFIG = [
    {"id": "lbank",   "label": "LBank",   "instance": None},
    {"id": "binance", "label": "Binance", "instance": None},
    {"id": "bybit",   "label": "Bybit",   "instance": None},
]

def init_exchanges():
    import ccxt as _ccxt_local
    for ex in EXCHANGES_CONFIG:
        try:
            instance = getattr(_ccxt_local, ex["id"])({
                "enableRateLimit": True,
                "timeout": 10000,
            })
            instance.load_markets()
            ex["instance"] = instance
            log.info(f"Exchange {ex['label']}: OK ({len(instance.markets)} mercados)")
        except Exception as e:
            log.warning(f"Exchange {ex['label']}: falhou — {e}")
            ex["instance"] = None

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


# ============================================================
# SCANNER METODOLOGIA AGREGADO v12.7
# Wyckoff CORRETO: Lateralizacao + Compressao + Vol Crescente
# O Tuk Tuk e a COMPRESSAO dentro do range, nao o rompimento.
# ============================================================

def calcular_vwap_ancorado(candles):
    num, den = 0.0, 0.0
    for c in candles:
        tp   = (c["h"] + c["l"] + c["c"]) / 3
        num += tp * c["v"]
        den += c["v"]
    return num / den if den > 0 else 0

def calcular_atr(candles, periodo=14):
    if len(candles) < periodo + 1:
        return 0
    trs = []
    for i in range(1, len(candles)):
        hl = candles[i]["h"] - candles[i]["l"]
        hc = abs(candles[i]["h"] - candles[i-1]["c"])
        lc = abs(candles[i]["l"] - candles[i-1]["c"])
        trs.append(max(hl, hc, lc))
    return sum(trs[-periodo:]) / periodo if trs else 0

def detectar_lateralizacao(candles, atr):
    """
    Detecta se o mercado esta em lateralizacao real.
    Usa as ultimas N barras e verifica:
    - Range (max high - min low) < FATOR x ATR  => mercado comprimido
    - Preco oscilando entre suporte e resistencia sem tendencia clara
    Retorna: (range_high, range_low, True/False)
    """
    N = 40  # janela de lateralizacao (40 barras 1H = ~2 dias)
    if len(candles) < N or atr <= 0:
        return 0, 0, False

    janela = candles[-N:]
    rh = max(c["h"] for c in janela)
    rl = min(c["l"] for c in janela)
    largura = rh - rl

    # Lateralizacao: largura < 5x ATR
    # Para EDEN (exemplo): ATR 1H ~0.003, range ~0.015 = 5x => lateral
    FATOR_LATERAL = 5.0
    if largura >= atr * FATOR_LATERAL:
        return rh, rl, False

    # Confirmar que preco nao esta em tendencia forte
    # Verificar se os ultimos closes estao dentro do range (nao rompendo)
    closes = [c["c"] for c in janela[-10:]]
    meio   = (rh + rl) / 2
    acima  = sum(1 for c in closes if c > meio)
    abaixo = sum(1 for c in closes if c <= meio)

    # Se tudo no mesmo lado = tendencia, nao lateral
    if acima == len(closes) or abaixo == len(closes):
        return rh, rl, False

    return rh, rl, True

def detectar_spring_upthrust(candles, range_high, range_low, atr):
    """
    Detecta Spring ou Upthrust nas ultimas 10 barras dentro do range.
    Spring: barra que penetra abaixo do range_low e fecha acima = acumulacao
    Upthrust: barra que penetra acima do range_high e fecha abaixo = distribuicao
    Retorna: "SPRING", "UPTHRUST" ou None
    """
    if not candles or atr <= 0:
        return None

    for c in reversed(candles[-10:]):
        # Spring
        if c["l"] < range_low and c["c"] > range_low and c["c"] >= c["o"]:
            return "SPRING"
        # Upthrust
        if c["h"] > range_high and c["c"] < range_high and c["c"] <= c["o"]:
            return "UPTHRUST"
    return None

def detectar_tuk_tuk_wyckoff(candles, atr, range_high, range_low):
    """
    DEFINICAO CORRETA DO TUK TUK (Wyckoff):
    Sequencia de velas de BAIXA AMPLITUDE com VOLUME CRESCENTE
    DENTRO de uma lateralizacao. O segredo esta no aumento de volume
    relativo entre as velas — nao na amplitude do rompimento.

    Detecta nas ultimas barras dentro do range:
    - Pelo menos 3 velas consecutivas de baixa amplitude (< 0.6x ATR)
    - Volume de cada vela MAIOR que a anterior (crescente)
    - Todas as velas dentro do range (nao romperam ainda)

    Retorna: "LONG" (acima do meio do range), "SHORT" (abaixo), ou None
    """
    if len(candles) < 6 or atr <= 0:
        return None

    FATOR_COMP  = 0.70   # vela pequena = amplitude < 0.70x ATR
    MIN_VELAS   = 3      # minimo de velas comprimidas consecutivas
    JANELA_BUSCA = 15    # quantas barras recentes verificar

    meio_range = (range_high + range_low) / 2
    janela = candles[-JANELA_BUSCA:]

    # Percorre a janela procurando sequencias validas
    melhor_seq = 0
    melhor_dir = None

    i = 0
    while i < len(janela):
        c = janela[i]
        amp = c["h"] - c["l"]

        # Inicio de possivel sequencia: vela pequena dentro do range
        if amp < atr * FATOR_COMP and rl_in_range(c, range_high, range_low):
            seq      = [c]
            j        = i + 1
            vol_ok   = True

            while j < len(janela):
                prox = janela[j]
                amp_prox = prox["h"] - prox["l"]

                # Proximo candle deve: amplitude pequena + volume maior + dentro do range
                if (amp_prox < atr * FATOR_COMP
                        and prox["v"] >= seq[-1]["v"]   # volume crescente ou igual
                        and rl_in_range(prox, range_high, range_low)):
                    seq.append(prox)
                    j += 1
                else:
                    break

            if len(seq) >= MIN_VELAS:
                # Verificar se volume e genuinamente crescente (nao apenas estavel)
                vols = [c["v"] for c in seq]
                crescentes = sum(1 for k in range(1, len(vols)) if vols[k] > vols[k-1])
                if crescentes >= len(vols) - 1:  # maioria crescente
                    if len(seq) > melhor_seq:
                        melhor_seq = len(seq)
                        # Direcao baseada na posicao no range
                        ultimo_close = seq[-1]["c"]
                        if ultimo_close >= meio_range:
                            melhor_dir = "LONG"   # compressao na metade superior = rompimento para cima
                        else:
                            melhor_dir = "SHORT"  # compressao na metade inferior = rompimento para baixo
            i = j
        else:
            i += 1

    if melhor_seq >= MIN_VELAS:
        return melhor_dir
    return None

def rl_in_range(candle, rh, rl):
    """Verifica se o corpo da vela esta dentro do range."""
    body_h = max(candle["o"], candle["c"])
    body_l = min(candle["o"], candle["c"])
    return body_h <= rh * 1.005 and body_l >= rl * 0.995

def normalizar_symbol_coinalyze_early(symbol):
    s = symbol.upper().split("@")[0]
    for suf in ["_PERP.A", "_PERP.0", "_PERP.6", ".P", "-PERP"]:
        s = s.replace(suf, "")
    s = s.replace("-","").replace("_","").replace("/","")
    if not s.endswith("USDT") and not s.endswith("USDC"):
        s = s + "USDT"
    return f"{s}_PERP.A"

def buscar_funding_rapido(symbol):
    try:
        sym_cg  = normalizar_symbol_coinalyze_early(symbol)
        headers = {"api_key": COINALYZE_KEY}
        r = requests.get(
            f"{COINALYZE_BASE}/funding-rate",
            params={"symbols": sym_cg},
            headers=headers, timeout=5
        )
        if r.status_code == 200:
            data = r.json()
            if data:
                return round(float(data[0].get("value", 0)) * 100, 4)
    except:
        pass
    return None

def analisar_ativo_agregado(symbol):
    """
    Scanner Wyckoff v12.7 — logica correta:
    1. Detectar lateralizacao no 1H
    2. Identificar Spring ou Upthrust dentro do range
    3. Identificar Tuk Tuk: velas pequenas com volume crescente dentro do range
    4. Funding < 1% absoluto
    5. Alertar para iminencia de rompimento
    """
    # ── 1H: lateralizacao ───────────────────────────────────
    candles_1h_raw = buscar_candles(symbol, "hour1", 80)
    if not candles_1h_raw:
        return None
    candles_1h = [parse_candle(c) for c in candles_1h_raw]
    candles_1h = [c for c in candles_1h if c]
    if len(candles_1h) < 40:
        return None

    atr_1h = calcular_atr(candles_1h, 14)
    if atr_1h <= 0:
        return None

    range_high, range_low, is_lateral = detectar_lateralizacao(candles_1h, atr_1h)
    if not is_lateral:
        return None  # sem lateralizacao = sem Wyckoff

    # ── Spring ou Upthrust dentro do range ──────────────────
    sinal_wyckoff = detectar_spring_upthrust(candles_1h, range_high, range_low, atr_1h)
    # Spring ou Upthrust sao opcionais — aumentam o score mas nao bloqueiam

    # ── Tuk Tuk: compressao + volume crescente no range ─────
    tuk_dir = detectar_tuk_tuk_wyckoff(candles_1h, atr_1h, range_high, range_low)
    if tuk_dir is None:
        return None  # sem Tuk Tuk = sem sinal

    # ── Funding Rate < 1% absoluto ──────────────────────────
    funding = buscar_funding_rapido(symbol)
    if funding is not None and abs(funding) >= 1.0:
        return None

    # ── Preco atual ─────────────────────────────────────────
    preco = candles_1h[-1]["c"]

    # ── VWAP ancorado no inicio do range ────────────────────
    candles_ancora = candles_1h[-40:]
    vwap_ancorado  = calcular_vwap_ancorado(candles_ancora)

    # ── Score de qualidade ───────────────────────────────────
    score = 50  # lateralizacao confirmada
    score += 30  # Tuk Tuk confirmado
    if sinal_wyckoff == "SPRING"   and tuk_dir == "LONG":  score += 20
    if sinal_wyckoff == "UPTHRUST" and tuk_dir == "SHORT": score += 20
    if funding is not None and abs(funding) < 0.05: score += 10

    largura_range = range_high - range_low
    largura_pct   = round(largura_range / range_low * 100, 1) if range_low > 0 else 0

    return {
        "symbol":        symbol.split("@")[0].upper(),
        "preco":         preco,
        "tuk_tuk":       tuk_dir,
        "wyckoff":       sinal_wyckoff or "-",
        "range_high":    round(range_high, 6),
        "range_low":     round(range_low,  6),
        "largura_pct":   largura_pct,
        "vwap_ancorado": round(vwap_ancorado, 6),
        "funding":       funding,
        "score":         min(score, 100),
    }

def rodar_scanner():
    brt = brt_agora().strftime("%d/%m/%Y %H:%M BRT")
    enviar_telegram(
        f"🔍 <b>SCANNER WYCKOFF v12.7</b>\n"
        f"{brt}\n"
        f"Lateralizacao + Tuk Tuk + Spring/UT | Funding &lt;1%\n"
        f"Analisando ativos..."
    )
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

    blacklist    = get_blacklist()
    resultados   = []
    n_analisados = 0

    for symbol in pares:
        if symbol.upper() in blacklist:
            continue
        try:
            resultado = analisar_ativo_agregado(symbol)
            n_analisados += 1
            if resultado:
                resultados.append(resultado)
            time.sleep(0.25)
        except Exception as e:
            log.error(f"Scanner {symbol}: {e}")

    resultados.sort(key=lambda x: x["score"], reverse=True)

    if not resultados:
        enviar_telegram(
            f"✅ <b>SCAN CONCLUIDO v12.7</b>\n"
            f"{brt}\n"
            f"Analisados: {n_analisados} ativos\n"
            f"Nenhum ativo com Lateralizacao + Tuk Tuk + Funding &lt;1%."
        )
        return

    longs  = [r for r in resultados if r["tuk_tuk"] == "LONG"]
    shorts = [r for r in resultados if r["tuk_tuk"] == "SHORT"]

    linhas = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 <b>SCANNER WYCKOFF v12.7</b>",
        f"🕐 {brt}",
        f"✅ {len(resultados)} setups | {n_analisados} analisados",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        ""
    ]

    def fmt(r, i):
        wyck_txt = f" | {r['wyckoff']}" if r["wyckoff"] != "-" else ""
        fund_txt = f" | FR {r['funding']:+.4f}%" if r["funding"] is not None else ""
        return (
            f"{i}. <b>{r['symbol']}</b>"
            f" | Range {r['largura_pct']}%"
            f" | Score {r['score']}"
            f" | 💲{r['preco']:.6g}"
            f"\n   Sup ${r['range_low']:.6g} → Res ${r['range_high']:.6g}"
            f"{wyck_txt}{fund_txt}"
        )

    if longs:
        linhas.append("🟢 <b>LONG — Tuk Tuk ▲ iminente</b>\n")
        for i, r in enumerate(longs, 1):
            linhas.append(fmt(r, i))
            sent     = buscar_sentimento(r["symbol"])
            sent_txt = formatar_sentimento(sent)
            if sent_txt:
                linhas.append(sent_txt)
            linhas.append("")

    if shorts:
        linhas.append("🔴 <b>SHORT — Tuk Tuk ▼ iminente</b>\n")
        for i, r in enumerate(shorts, 1):
            linhas.append(fmt(r, i))
            sent     = buscar_sentimento(r["symbol"])
            sent_txt = formatar_sentimento(sent)
            if sent_txt:
                linhas.append(sent_txt)
            linhas.append("")

    linhas += [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "⚠️ Sinal de IMINENCIA — envie grafico para analise completa!"
    ]

    mensagem = "\n".join(linhas)
    if len(mensagem) <= 4000:
        enviar_telegram(mensagem)
    else:
        bloco, chars = [], 0
        for linha in linhas:
            if chars + len(linha) > 3800:
                enviar_telegram("\n".join(bloco))
                bloco, chars = [linha], len(linha)
                time.sleep(1)
            else:
                bloco.append(linha)
                chars += len(linha)
        if bloco:
            enviar_telegram("\n".join(bloco))

    log.info(f"Scanner v12.7: {len(resultados)} setups de {n_analisados} ativos.")

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
    enviar_telegram(
        f"🔍 <b>SCANNER AGREGADO v12.6</b>\n"
        f"{brt}\n"
        f"Top-Down 1H→15M | Tuk Tuk | VWAP Ancorado\n"
        f"Analisando ativos..."
    )
    tickers   = buscar_ticker_24h()
    pares_raw = buscar_todos_pares()

    # Filtro de volume minimo de mercado
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

    blacklist  = get_blacklist()
    resultados = []
    n_analisados = 0

    for symbol in pares:
        if symbol.upper() in blacklist:
            continue
        try:
            resultado = analisar_ativo_agregado(symbol)
            n_analisados += 1
            if resultado:
                resultados.append(resultado)
            time.sleep(0.2)  # respeitar rate limit
        except Exception as e:
            log.error(f"Scanner {symbol}: {e}")

    # Ordenar por score decrescente
    resultados.sort(key=lambda x: x["score"], reverse=True)

    if not resultados:
        enviar_telegram(
            f"✅ <b>SCAN CONCLUÍDO</b>\n"
            f"{brt}\n"
            f"Analisados: {n_analisados} ativos\n"
            f"Nenhum ativo com Tuk Tuk + Bias 1H + Vol ≥5x alinhados."
        )
        return

    # Separar por prioridade e direcao
    longs  = [r for r in resultados if r["bias_1h"] == "LONG"]
    shorts = [r for r in resultados if r["bias_1h"] == "SHORT"]

    linhas = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 <b>SCANNER AGREGADO</b>",
        f"🕐 {brt}",
        f"✅ {len(resultados)} setups | {n_analisados} analisados",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        ""
    ]

    if longs:
        linhas.append(f"🟢 <b>LONG — Tuk Tuk ▲ alinhado com 1H</b>\n")
        for i, r in enumerate(longs, 1):
            vwap_txt = f" | VWAP ${r['vwap_ancorado']:.5g}" if r["vwap_ancorado"] > 0 else ""
            linhas.append(
                f"{i}. <b>{r['symbol']}</b> ▲"
                f" | Vol <b>{r['vol_rel_15m']}x</b>"
                f" | Score {r['score']}"
                f" | 💲{r['preco']:.6g}"
                f"{vwap_txt}"
            )
            # Sentimento resumido
            sent     = buscar_sentimento(r["symbol"])
            sent_txt = formatar_sentimento(sent)
            if sent_txt:
                linhas.append(sent_txt)
            linhas.append("")

    if shorts:
        linhas.append(f"🔴 <b>SHORT — Tuk Tuk ▼ alinhado com 1H</b>\n")
        for i, r in enumerate(shorts, 1):
            vwap_txt = f" | VWAP ${r['vwap_ancorado']:.5g}" if r["vwap_ancorado"] > 0 else ""
            linhas.append(
                f"{i}. <b>{r['symbol']}</b> ▼"
                f" | Vol <b>{r['vol_rel_15m']}x</b>"
                f" | Score {r['score']}"
                f" | 💲{r['preco']:.6g}"
                f"{vwap_txt}"
            )
            sent     = buscar_sentimento(r["symbol"])
            sent_txt = formatar_sentimento(sent)
            if sent_txt:
                linhas.append(sent_txt)
            linhas.append("")

    linhas += [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "👁 Envie o gráfico do ativo para análise completa!"
    ]

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

    log.info(f"Scanner Agregado: {len(resultados)} setups de {n_analisados} ativos.")

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
    # ── CORREÇÃO CRÍTICA v12.5 ──────────────────────────────────────────────
    # ticker.get("high"/"low") retorna high/low das ÚLTIMAS 24H — não do
    # momento atual. Isso causava acionamentos falsos: um trade cadastrado
    # com entrada $10.00 era "acionado" porque o high 24h havia tocado $10.87
    # horas antes do cadastro.
    #
    # Solução: buscar os 3 últimos candles de 5 minutos e usar o high/low
    # do candle MAIS RECENTE como referência intracandle real.
    # O "preco" continua sendo o last do ticker (mais rápido/preciso).
    # ────────────────────────────────────────────────────────────────────────
    symbol    = normalizar_symbol_ccxt(ativo)
    last      = None
    bid, ask  = None, None

    # 1. Pega last/bid/ask do ticker (rápido, sem usar high/low 24h)
    if CCXT_AVAILABLE:
        try:
            ticker = _exchange.fetch_ticker(symbol)
            last = ticker.get("last")
            bid  = ticker.get("bid") or last
            ask  = ticker.get("ask") or last
        except Exception as e:
            log.debug(f"Ticker {symbol}: {e}")

    if last is None:
        for ex in EXCHANGES_CONFIG:
            if ex["instance"]:
                try:
                    ticker = ex["instance"].fetch_ticker(symbol)
                    last = ticker.get("last")
                    bid  = ticker.get("bid") or last
                    ask  = ticker.get("ask") or last
                    break
                except:
                    continue

    # 2. Pega high/low do candle atual (5m) — precisão intracandle real
    sym_lbank = symbol.lower().replace("/", "_")
    candles   = buscar_candles(sym_lbank, "minute5", 3)
    high_real = None
    low_real  = None
    if candles:
        parsed = [parse_candle(c) for c in candles]
        parsed = [p for p in parsed if p is not None]
        if parsed:
            ultimo    = parsed[-1]
            high_real = ultimo["h"]
            low_real  = ultimo["l"]
            if last is None:
                last = ultimo["c"]
                bid  = last
                ask  = last

    if last is None:
        return None

    # High/low = candle atual; se falhou, usa o last como fallback seguro
    return {
        "preco": last,
        "high":  high_real if high_real is not None else last,
        "low":   low_real  if low_real  is not None else last,
        "bid":   bid  or last,
        "ask":   ask  or last,
    }

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


# ============================================================
# ALERTAS DE NIVEL DE PRECO (v12.5)
# ============================================================
# Permite cadastrar alertas de nivel sem abrir trade.
# Comandos: /alerta, /alertas, /deletar_alerta
# Monitorado no mesmo loop de monitorar_trades.
# ============================================================

def salvar_alerta_nivel(ativo, direcao, nivel, condicao, nota=""):
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    agora = brt_agora().strftime("%Y-%m-%d %H:%M")
    c.execute("""
        INSERT INTO alertas_nivel (ativo, direcao, nivel, condicao, nota, criado_em)
        VALUES (?,?,?,?,?,?)
    """, (ativo.upper(), direcao.upper(), nivel, condicao, nota, agora))
    conn.commit()
    aid = c.lastrowid
    conn.close()
    return aid

def listar_alertas_nivel():
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("""
        SELECT id, ativo, direcao, nivel, condicao, nota, criado_em
        FROM alertas_nivel
        WHERE ativo_flag=1 AND disparado=0
        ORDER BY id DESC
    """)
    rows = c.fetchall()
    conn.close()
    return rows

def deletar_alerta_nivel(aid):
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("UPDATE alertas_nivel SET ativo_flag=0 WHERE id=?", (aid,))
    conn.commit()
    conn.close()

def marcar_alerta_nivel_disparado(aid):
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("UPDATE alertas_nivel SET disparado=1, ativo_flag=0 WHERE id=?", (aid,))
    conn.commit()
    conn.close()

def monitorar_alertas_nivel():
    """Verifica alertas de nivel de preco e dispara notificacao."""
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("""
        SELECT id, ativo, direcao, nivel, condicao, nota
        FROM alertas_nivel
        WHERE ativo_flag=1 AND disparado=0
    """)
    alertas = c.fetchall()
    conn.close()

    for alerta in alertas:
        aid, ativo, direcao, nivel, condicao, nota = alerta
        dados = buscar_preco_atual(ativo)
        if not dados:
            continue
        preco = dados["preco"]
        high  = dados.get("high", preco)
        low   = dados.get("low", preco)

        disparar = False
        # ACIMA: preco sobe e toca o nivel
        if condicao == "ACIMA" and high >= nivel:
            disparar = True
        # ABAIXO: preco cai e toca o nivel
        elif condicao == "ABAIXO" and low <= nivel:
            disparar = True

        if disparar:
            marcar_alerta_nivel_disparado(aid)
            emoji = "🔔"
            seta  = "▲" if condicao == "ACIMA" else "▼"
            msg_nota = f"\n📝 {nota}" if nota else ""
            enviar_telegram(
                f"{emoji} <b>ALERTA ACIONADO #{aid}</b>\n"
                f"📊 {ativo} | {direcao}\n"
                f"💲 Preço: ${preco:.6g} | Nível: ${nivel} {seta}\n"
                f"✅ Nível atingido — verificar setup!{msg_nota}\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"Envie o gráfico para análise completa."
            )
            log.info(f"Alerta #{aid} {ativo} disparado em ${preco:.6g}")

def monitorar_trades():
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("SELECT * FROM trades WHERE resultado='ABERTO'")
    abertos = c.fetchall()
    conn.close()

    agora_brt = brt_agora()

    for trade in abertos:
        tid, ativo, direcao, entrada, stop, a1, a2, a3, tf_ctx, tf_ent, resultado, criado = trade

        # ── CORREÇÃO CRÍTICA v12.5 — FILTRO TEMPORAL ───────────────────────
        # Um trade PENDING só deve ser monitorado APÓS o momento do cadastro.
        # Isso evita que o bot ative entradas usando dados de preço anteriores
        # ao cadastro (ex: LINK cadastrado às 13:57 com entrada $10.00 —
        # o alto do dia havia sido $10.87 muito antes, mas o bot disparava
        # o acionamento usando o high 24h do ticker).
        #
        # Regra: se o trade foi cadastrado há menos de 2 minutos, pular.
        # Isso dá tempo para o ciclo de monitoramento se estabilizar.
        # ────────────────────────────────────────────────────────────────────
        try:
            fmt_criado  = "%Y-%m-%d %H:%M"
            dt_criado   = datetime.strptime(criado, fmt_criado)
            dt_criado   = dt_criado.replace(tzinfo=timezone(timedelta(hours=OFFSET_BRT)))
            segundos_desde_cadastro = (agora_brt - dt_criado).total_seconds()
            if segundos_desde_cadastro < 120:
                log.info(f"Trade #{tid} {ativo} ignorado — cadastrado há {int(segundos_desde_cadastro)}s (aguardando 120s)")
                continue
        except Exception as e:
            log.warning(f"Filtro temporal trade #{tid}: {e}")

        dados = buscar_preco_atual(ativo)
        if not dados:
            log.warning(f"Sem preco para {ativo}")
            continue

        preco = dados["preco"]
        high  = dados.get("high", preco)
        low   = dados.get("low", preco)

        base = f"{tid}_{ativo}"

        if direcao == "LONG":
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
                continue
            # Alvos: só acionar se entrada já foi confirmada
            if alerta_ja_enviado(f"{base}_entrada"):
                if high >= a3 and not alerta_ja_enviado(f"{base}_a3"):
                    marcar_alerta(f"{base}_a3")
                    marcar_alerta(f"{base}_a2")
                    marcar_alerta(f"{base}_a1")
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
                    marcar_alerta(f"{base}_a1")
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
            # Entrada: acionar SOMENTE se high/low intracandle cruzam o nível
            # NUNCA usar abs(preco - entrada) — isso causava falso acionamento
            if not alerta_ja_enviado(f"{base}_entrada"):
                if low <= entrada <= high:
                    # Confirmação: preco atual deve estar próximo da entrada (não divergido)
                    if abs(preco - entrada) <= entrada * 0.015:
                        marcar_alerta(f"{base}_entrada")
                        marcar_alerta(f"{base}_zona")
                        enviar_telegram(
                            f"🟢 <b>ENTRADA LONG ACIONADA — {ativo} #{tid}</b>\n"
                            f"💲 Preço: ${preco:.6g}\n"
                            f"📥 Entrada: ${entrada} | Stop: ${stop}\n"
                            f"🎯 A1: ${a1} | A2: ${a2} | A3: ${a3}"
                        )
                elif preco < entrada:
                    distancia_pct = (entrada - preco) / entrada * 100
                    if distancia_pct <= 3.0 and not alerta_ja_enviado(f"{base}_zona"):
                        marcar_alerta(f"{base}_zona")
                        enviar_telegram(
                            f"👀 <b>ZONA DE ENTRADA — {ativo} LONG #{tid}</b>\n"
                            f"💲 Preço: ${preco:.6g} | Entrada: ${entrada}\n"
                            f"📍 Preço a {round(distancia_pct,2)}% abaixo da entrada\n"
                            f"⏳ Aguardando subida para acionar..."
                        )

        elif direcao == "SHORT":
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
                continue
            # Alvos: só acionar se entrada já foi confirmada
            if alerta_ja_enviado(f"{base}_entrada"):
                if low <= a3 and not alerta_ja_enviado(f"{base}_a3"):
                    marcar_alerta(f"{base}_a3")
                    marcar_alerta(f"{base}_a2")
                    marcar_alerta(f"{base}_a1")
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
                    marcar_alerta(f"{base}_a1")
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
            # Entrada: acionar SOMENTE se high/low intracandle cruzam o nível
            if not alerta_ja_enviado(f"{base}_entrada"):
                if low <= entrada <= high:
                    if abs(preco - entrada) <= entrada * 0.015:
                        marcar_alerta(f"{base}_entrada")
                        marcar_alerta(f"{base}_zona")
                        enviar_telegram(
                            f"🔴 <b>ENTRADA SHORT ACIONADA — {ativo} #{tid}</b>\n"
                            f"💲 Preço: ${preco:.6g}\n"
                            f"📥 Entrada: ${entrada} | Stop: ${stop}\n"
                            f"🎯 A1: ${a1} | A2: ${a2} | A3: ${a3}"
                        )
                elif preco > entrada:
                    distancia_pct = (preco - entrada) / entrada * 100
                    if distancia_pct <= 3.0 and not alerta_ja_enviado(f"{base}_zona"):
                        marcar_alerta(f"{base}_zona")
                        enviar_telegram(
                            f"👀 <b>ZONA DE ENTRADA — {ativo} SHORT #{tid}</b>\n"
                            f"💲 Preço: ${preco:.6g} | Entrada: ${entrada}\n"
                            f"📍 Preço a {round(distancia_pct,2)}% acima da entrada\n"
                            f"⏳ Aguardando queda para acionar..."
                        )

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
    return jsonify({"status": "online", "version": "v12.7"})

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
<h1>🦈 LucSharkTrade Dashboard v12.5</h1>
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
    'Atualizado: '+new Date().toLocaleTimeString('pt-BR')+' BRT | v12.5 — Auto-refresh 30s';
}
loadData();
setInterval(loadData,30000);
</script>
</body></html>"""

def processar_comando(texto):
    partes = texto.strip().split()
    cmd    = partes[0].lower()

    if cmd in ["/start", "/ajuda"]:
        return (
            "🤖 <b>LucSharkTrade v12.7 — Comandos</b>\n\n"
            "<b>📊 TRADES</b>\n"
            "/trade ATIVO DIR ENTRADA STOP A1 A2 A3 TF_CTX TF_ENT\n"
            "/resultado ATIVO WIN_A1 | WIN_A2 | WIN_A3 | LOSS\n"
            "/fechar ID PRECO [motivo] — fechar trade pelo ID\n"
            "/trades — trades abertos\n"
            "/relatorio — estatísticas e P&L\n"
            "/semana — relatório da semana\n\n"
            "<b>🔔 ALERTAS DE NÍVEL</b>\n"
            "/alerta ATIVO DIR NIVEL ACIMA|ABAIXO [nota]\n"
            "/alertas — ver alertas ativos\n"
            "/deletar_alerta ID — remover alerta\n\n"
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
    elif cmd == "/fechar":
        # /fechar ID PRECO MOTIVO
        # Fecha manualmente um trade pelo ID com preco de saida real
        if len(partes) < 3:
            return "❌ Formato: /fechar ID PRECO [motivo]\nEx: /fechar 7 9.85 A1"
        try:
            trade_id  = int(partes[1])
            exit_price = float(partes[2])
            motivo    = " ".join(partes[3:]) if len(partes) > 3 else "MANUAL"
            conn_f = sqlite3.connect("trades.db")
            c_f    = conn_f.cursor()
            c_f.execute("SELECT ativo,direcao,entrada,stop,a1,a2,a3,criado_em FROM trades WHERE id=? AND resultado='ABERTO'", (trade_id,))
            row = c_f.fetchone()
            if not row:
                conn_f.close()
                return f"❌ Trade #{trade_id} não encontrado ou já fechado."
            ativo_f, dir_f, entrada_f, stop_f, a1_f, a2_f, a3_f, criado_f = row
            risco_f = abs(entrada_f - stop_f)
            if risco_f == 0:
                conn_f.close()
                return "❌ Risco zerado — não é possível calcular R."
            if dir_f == "LONG":
                r_obtido = (exit_price - entrada_f) / risco_f
            else:
                r_obtido = (entrada_f - exit_price) / risco_f
            resultado_f = motivo.upper()
            if r_obtido >= 2.5:   resultado_f = "WIN_A3"
            elif r_obtido >= 1.5: resultado_f = "WIN_A2"
            elif r_obtido >= 0.8: resultado_f = "WIN_A1"
            elif r_obtido < 0:    resultado_f = "LOSS"
            else:                 resultado_f = "BREAKEVEN"
            c_f.execute("UPDATE trades SET resultado=? WHERE id=?", (resultado_f, trade_id))
            conn_f.commit()
            conn_f.close()
            # Marcar alerta de entrada como enviado para evitar reativação
            marcar_alerta(f"{trade_id}_{ativo_f}_entrada")
            duracao_f = calcular_duracao(criado_f)
            sinal_r = "+" if r_obtido >= 0 else ""
            return (
                f"🔒 <b>TRADE #{trade_id} FECHADO</b>\n"
                f"📊 {ativo_f} {dir_f}\n"
                f"📥 Entrada: ${entrada_f} | Saída: ${exit_price}\n"
                f"📈 R obtido: {sinal_r}{r_obtido:.2f}R\n"
                f"✅ Resultado: {resultado_f}\n"
                f"⏱ Duração: {duracao_f}"
            )
        except Exception as e:
            return f"❌ Erro: {e}"



    elif cmd == "/alerta":
        # /alerta ATIVO DIRECAO NIVEL CONDICAO [nota...]
        # Ex: /alerta HYPEUSDT LONG 44000 ACIMA gatilho possivel long
        # Ex: /alerta HYPEUSDT SHORT 38000 ABAIXO possivel short
        if len(partes) < 5:
            return (
                "❌ Formato: /alerta ATIVO DIRECAO NIVEL CONDICAO [nota]\n\n"
                "DIRECAO: LONG ou SHORT\n"
                "CONDICAO: ACIMA ou ABAIXO\n\n"
                "Exemplos:\n"
                "/alerta HYPEUSDT LONG 44000 ACIMA gatilho long\n"
                "/alerta BTCUSDT SHORT 82000 ACIMA rejeicao resistencia"
            )
        try:
            ativo     = partes[1].upper()
            direcao   = partes[2].upper()
            nivel     = float(partes[3])
            condicao  = partes[4].upper()
            nota      = " ".join(partes[5:]) if len(partes) > 5 else ""
            if direcao not in ("LONG", "SHORT"):
                return "❌ DIRECAO deve ser LONG ou SHORT"
            if condicao not in ("ACIMA", "ABAIXO"):
                return "❌ CONDICAO deve ser ACIMA ou ABAIXO"
            aid = salvar_alerta_nivel(ativo, direcao, nivel, condicao, nota)
            seta = "▲" if condicao == "ACIMA" else "▼"
            return (
                f"🔔 <b>Alerta #{aid} cadastrado!</b>\n"
                f"📊 {ativo} | {direcao}\n"
                f"💲 Nível: ${nivel} {seta} ({condicao})\n"
                f"{'📝 ' + nota if nota else ''}\n"
                f"✅ Monitorando 24/7 — aviso quando atingir o nível."
            )
        except Exception as e:
            return f"❌ Erro: {e}"

    elif cmd == "/alertas":
        rows = listar_alertas_nivel()
        if not rows:
            return "📭 Nenhum alerta de nível ativo."
        linhas = ["🔔 <b>Alertas de Nível Ativos</b>\n"]
        for r in rows:
            aid, ativo, direcao, nivel, condicao, nota, criado = r
            seta  = "▲" if condicao == "ACIMA" else "▼"
            emoji = "🟢" if direcao == "LONG" else "🔴"
            linha = f"{emoji} #{aid} {ativo} {direcao} | ${nivel} {seta}"
            if nota:
                linha += f" | {nota}"
            linhas.append(linha)
        linhas.append("\n/deletar_alerta ID — para remover um alerta")
        return "\n".join(linhas)

    elif cmd == "/deletar_alerta":
        if len(partes) < 2:
            return "❌ Formato: /deletar_alerta ID"
        try:
            aid = int(partes[1])
            deletar_alerta_nivel(aid)
            return f"🗑 Alerta #{aid} removido."
        except Exception as e:
            return f"❌ Erro: {e}"
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
        conn_st = sqlite3.connect("trades.db")
        c_st    = conn_st.cursor()
        c_st.execute("SELECT COUNT(*) FROM trades WHERE resultado='ABERTO'")
        n_abertos = c_st.fetchone()[0]
        conn_st.close()
        return (
            f"✅ <b>LucSharkTrade v12.7 ONLINE</b>\n"
            f"🕐 {brt}\n"
            f"📡 Exchanges: {ex_online}/3 online\n"
            f"💰 Capital: ${CAPITAL_INICIAL:,.2f}\n"
            f"🔄 Trades abertos: {n_abertos}\n"
            f"⏱ Intervalo monitor: {INTERVALO_SEG}s\n"
            f"🔧 v12.5: intracandle high/low + filtro temporal + alvos pos-entrada"
        )

    return None

def iniciar_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# =========================================================================
# v12.5 — FIX: Comandos Telegram não respondiam
#   1. RELAXADO filtro de idade (60s -> 600s) — era a causa raiz dos descartes
#   2. SEPARADO loop de comandos (rápido) do loop de monitoramento (lento)
#   3. monitorar_trades() agora roda em thread dedicada
#   4. STARTUP preserva comandos recentes (<120s) em vez de descartar todos
#   5. Scanner manual roda em thread para não bloquear próximos comandos
# =========================================================================

# Estado compartilhado entre threads
_estado = {
    "ultimo_offset":         None,
    "ultimo_monitor":        0,
    "ultimo_relatorio_dia":  None,
    "ultimo_relatorio_sem":  None,
}
_estado_lock = threading.Lock()

def loop_monitor_trades():
    """Thread dedicada para monitorar trades abertos. Não bloqueia o Telegram."""
    log.info("Thread de monitoramento iniciada.")
    while True:
        try:
            monitorar_trades()
            with _estado_lock:
                _estado["ultimo_monitor"] = time.time()
        except Exception as e:
            log.error(f"monitorar_trades erro: {e}")

        try:
            monitorar_alertas_nivel()
        except Exception as e:
            log.error(f"monitorar_alertas_nivel erro: {e}")

        # Relatórios automáticos (deduplicados via chave de janela)
        try:
            agora_brt = brt_agora()
            chave_dia = agora_brt.strftime("%Y-%m-%d")
            chave_sem = agora_brt.strftime("%Y-W%W")
            if agora_brt.hour == 18 and agora_brt.minute == 0:
                with _estado_lock:
                    ja_rodou = _estado["ultimo_relatorio_dia"] == chave_dia
                if not ja_rodou:
                    relatorio_diario()
                    with _estado_lock:
                        _estado["ultimo_relatorio_dia"] = chave_dia
            if agora_brt.weekday() == 0 and agora_brt.hour == 9 and agora_brt.minute == 0:
                with _estado_lock:
                    ja_rodou = _estado["ultimo_relatorio_sem"] == chave_sem
                if not ja_rodou:
                    relatorio_semanal()
                    with _estado_lock:
                        _estado["ultimo_relatorio_sem"] = chave_sem
        except Exception as e:
            log.error(f"relatorios automaticos erro: {e}")

        time.sleep(INTERVALO_SEG)

def loop_comandos_telegram():
    """Thread principal — processa comandos do Telegram com baixa latência."""
    log.info("Thread de comandos Telegram iniciada.")
    while True:
        try:
            with _estado_lock:
                offset_atual = _estado["ultimo_offset"]
            updates = get_updates(offset_atual)
        except Exception as e:
            log.error(f"get_updates erro: {e}")
            time.sleep(3)
            continue

        for upd in updates:
            update_id = upd.get("update_id")
            if update_id is None:
                continue
            with _estado_lock:
                _estado["ultimo_offset"] = update_id + 1

            msg   = upd.get("message", {})
            texto = msg.get("text", "")
            if not texto or not texto.startswith("/"):
                continue

            # FIX v12.5: filtro relaxado — só descarta msgs MUITO antigas (>10min).
            # Antes era 60s, o que descartava comandos legítimos porque o loop
            # ficava bloqueado em monitorar_trades() + sleep(30s) na mesma thread.
            msg_date = msg.get("date", 0)
            if msg_date and (time.time() - msg_date) > 600:
                log.warning(f"Comando MUITO antigo ignorado (>10min): {texto}")
                continue

            log.info(f"Comando recebido: {texto}")
            try:
                resposta = processar_comando(texto)
                if resposta == "SCAN_SOLICITADO":
                    enviar_telegram("🔍 Scanner iniciado manualmente...")
                    # roda em thread para não bloquear próximos comandos
                    threading.Thread(target=rodar_scanner, daemon=True).start()
                elif resposta == "DEBUG_SOLICITADO":
                    threading.Thread(target=rodar_scanner_debug, daemon=True).start()
                elif resposta:
                    enviar_telegram(resposta)
            except Exception as e:
                log.error(f"Erro processando '{texto}': {e}")
                try:
                    enviar_telegram(f"⚠️ Erro ao processar {texto}: {e}")
                except:
                    pass

def main():
    init_db()
    init_exchanges()

    flask_thread = threading.Thread(target=iniciar_flask, daemon=True)
    flask_thread.start()
    log.info("Flask API iniciado")

    brt = brt_agora().strftime("%d/%m/%Y %H:%M BRT")

    # Detecta restart recente (evita spam de mensagem ONLINE em deploys curtos)
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
            f"🚀 <b>LucSharkTrade v12.7 ONLINE!</b>\n"
            f"📅 {brt}\n\n"
            f"✅ FIX: comandos Telegram agora respondem em &lt;3s\n"
            f"✅ FIX: monitoramento em thread dedicada\n"
            f"✅ FIX: filtro de idade de mensagem (60s→600s)\n"
            f"✅ Monitoramento HIGH/LOW intracandle\n"
            f"✅ Stop com prioridade máxima\n\n"
            f"Envie /ajuda para ver os comandos."
        )

    # FIX v12.5: preserva comandos recentes (<120s) em vez de descartar tudo
    ultimo_offset = None
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": 0, "limit": 100, "timeout": 0}, timeout=10
        ).json()
        pending = r.get("result", [])
        if pending:
            agora_ts = time.time()
            primeiro_recente = None
            for upd in pending:
                m = upd.get("message", {})
                if (agora_ts - m.get("date", 0)) <= 120 and m.get("text", "").startswith("/"):
                    primeiro_recente = upd["update_id"]
                    break
            if primeiro_recente is not None:
                ultimo_offset = primeiro_recente
                n_recentes = len([u for u in pending if u["update_id"] >= primeiro_recente])
                log.info(f"Mantendo {n_recentes} comandos recentes para processar.")
            else:
                ultimo_offset = pending[-1]["update_id"] + 1
                requests.get(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                    params={"offset": ultimo_offset, "limit": 1, "timeout": 0}, timeout=10
                )
                log.info(f"Descartadas {len(pending)} mensagens antigas (>120s).")
    except Exception as e:
        log.warning(f"Erro ao processar pendentes: {e}")
        ultimo_offset = None

    with _estado_lock:
        _estado["ultimo_offset"] = ultimo_offset

    # FIX v12.5: monitoramento em thread separada — não bloqueia comandos
    monitor_thread = threading.Thread(target=loop_monitor_trades, daemon=True)
    monitor_thread.start()

    # Loop principal = APENAS Telegram (máxima responsividade)
    loop_comandos_telegram()

if __name__ == "__main__":
    main()
