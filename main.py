import os
import time
import requests
import logging
import sqlite3
import threading
import json
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request
import telegram_v13 as tg13

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
    c.execute("""
        CREATE TABLE IF NOT EXISTS alertas_nivel (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ativo TEXT NOT NULL,
            nivel REAL NOT NULL,
            condicao TEXT NOT NULL,
            nota TEXT,
            disparado INTEGER DEFAULT 0,
            criado_em TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS alertas_preco (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ativo       TEXT UNIQUE,
            suporte     REAL,
            resistencia REAL,
            criado_em   TEXT
        )
    """)
    conn.commit()
    conn.close()
    tg13.migrar_db()

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

def enviar_telegram(msg, topic="geral", reply_to=None, keyboard=None, pin=False):
    """Wrapper v13 — roteia mensagens por Topic; compatível com chamadas antigas."""
    return tg13.enviar(msg, topic=topic, reply_to=reply_to, keyboard=keyboard, pin=pin)

def get_updates(offset=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {
        "timeout": 25,
        "offset": offset,
        "limit": 10,
        "allowed_updates": ["message", "callback_query"]
    }
    try:
        r = requests.get(url, params=params, timeout=30)
        data = r.json()
        if not data.get("ok"):
            log.error("getUpdates API: %s", data.get("description", data))
            return []
        return data.get("result", [])
    except Exception as e:
        log.error("get_updates erro: %s", e)
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
# SCANNER METODOLOGIA AGREGADO v12.10
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
    Scanner Wyckoff v12.10 — logica correta:
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

    # ── Funding Rate ─────────────────────────────────────────
    # Descartar apenas quando funding < -1% (shorts sobrecarregados = capitulacao extrema)
    # Funding positivo alto nao descarta — e normal em tendencia de alta
    funding = buscar_funding_rapido(symbol)
    if funding is not None and funding < -1.0:
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
        f"🔍 <b>SCANNER WYCKOFF v12.10</b>\n"
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
            f"✅ <b>SCAN CONCLUIDO v12.10</b>\n"
            f"{brt}\n"
            f"Analisados: {n_analisados} ativos\n"
            f"Nenhum ativo com Lateralizacao + Tuk Tuk + Funding &lt;1%."
        )
        return

    longs  = [r for r in resultados if r["tuk_tuk"] == "LONG"]
    shorts = [r for r in resultados if r["tuk_tuk"] == "SHORT"]

    linhas = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 <b>SCANNER WYCKOFF v12.10</b>",
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

    log.info(f"Scanner v12.10: {len(resultados)} setups de {n_analisados} ativos.")

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
        f"🔍 <b>SCANNER AGREGADO v13.0</b>\n"
        f"{brt}\n"
        f"Top-Down 1H→15M | Tuk Tuk | VWAP Ancorado\n"
        f"Analisando ativos...",
        topic="scanner",
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

    wl_rows = tg13.watchlist_listar()
    if wl_rows:
        def _norm(s):
            return s.upper().replace("/", "").replace("-", "").replace("_", "")
        wl_norm = {_norm(a) for a, _ in wl_rows}
        pares = [p for p in pares if _norm(p) in wl_norm]

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
            f"Nenhum ativo com Tuk Tuk + Bias 1H + Vol ≥5x alinhados.",
            topic="scanner",
        )
        return

    # Separar por prioridade e direcao
    prio   = [r for r in resultados if r.get("score", 0) >= 80]
    demais = [r for r in resultados if r.get("score", 0) < 80]
    longs  = [r for r in resultados if r["bias_1h"] == "LONG"]
    shorts = [r for r in resultados if r["bias_1h"] == "SHORT"]

    linhas = [
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 <b>SCANNER AGREGADO {tg13.VERSION}</b>",
        f"🕐 {brt}",
        f"✅ {len(resultados)} setups | {n_analisados} analisados",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        ""
    ]

    if prio:
        linhas.append(f"🚨 <b>PRIORIDADE — Score ≥ 80 ({len(prio)})</b>\n")
        for i, r in enumerate(prio[:10], 1):
            linhas.append(
                f"{i}. <b>{r['symbol']}</b> {r['bias_1h']} "
                f"| Score <b>{r['score']}</b> | 💲{r['preco']:.6g}"
            )
        linhas.append("")

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
        enviar_telegram(mensagem, topic="scanner")
    else:
        bloco = []
        chars = 0
        for linha in linhas:
            if chars + len(linha) > 3800:
                enviar_telegram("\n".join(bloco), topic="scanner")
                bloco = [linha]
                chars = len(linha)
                time.sleep(1)
            else:
                bloco.append(linha)
                chars += len(linha)
        if bloco:
            enviar_telegram("\n".join(bloco), topic="scanner")

    log.info(f"Scanner Agregado: {len(resultados)} setups de {n_analisados} ativos.")

def rodar_scanner_debug():
    brt = brt_agora().strftime("%d/%m/%Y %H:%M BRT")
    enviar_telegram(f"🔧 <b>MODO DEBUG</b>\n{brt}\nTestando 5 ativos...", topic="geral")
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
    enviar_telegram("\n".join(msg), topic="geral")

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

    # v12.10 CORREÇÃO DEFINITIVA:
    # NÃO buscar candles LBank para high/low — ativos de outras exchanges
    # (BingX, Binance) retornam candles de símbolos errados na LBank,
    # causando acionamentos falsos de stop e alertas.
    # O "last" do ticker ccxt é o único valor confiável para qualquer exchange.
    # Stops e alertas usam SOMENTE o last para comparação.
    if last is None:
        return None

    return {
        "preco": last,
        "high":  last,
        "low":   last,
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
# ALERTAS DE NIVEL — v12.10
# Dispara APENAS quando o preco cruza o valor exato registrado.
# Usa nivel_cruzado() — mesma logica do monitoramento de trades.
# Sem tolerancias, sem regioes, sem high/low de candle.
# ============================================================

_alerta_preco_anterior = {}

def salvar_alerta_nivel(ativo, nivel, condicao, nota=""):
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    agora = brt_agora().strftime("%Y-%m-%d %H:%M")
    c.execute("""
        INSERT INTO alertas_nivel (ativo, nivel, condicao, nota, criado_em)
        VALUES (?,?,?,?,?)
    """, (ativo.upper(), nivel, condicao.upper(), nota, agora))
    conn.commit()
    aid = c.lastrowid
    conn.close()
    return aid

def listar_alertas_nivel():
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("""
        SELECT id, ativo, nivel, condicao, nota, criado_em
        FROM alertas_nivel WHERE disparado=0 ORDER BY id DESC
    """)
    rows = c.fetchall()
    conn.close()
    return rows

# ─── ALERTAS DE PREÇO (FAIXA SUPORTE/RESISTÊNCIA) ────────────────────────────
def salvar_alerta_preco(ativo, suporte, resistencia):
    with sqlite3.connect("trades.db") as con:
        con.execute("""
            INSERT INTO alertas_preco (ativo, suporte, resistencia, criado_em)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ativo) DO UPDATE SET
                suporte=excluded.suporte,
                resistencia=excluded.resistencia,
                criado_em=excluded.criado_em
        """, (ativo.upper(), suporte, resistencia, brt_agora().strftime("%Y-%m-%d %H:%M")))

def remover_alerta_preco(ativo):
    with sqlite3.connect("trades.db") as con:
        cur = con.execute("DELETE FROM alertas_preco WHERE ativo=?", (ativo.upper(),))
        return cur.rowcount > 0

def listar_alertas_preco():
    with sqlite3.connect("trades.db") as con:
        return con.execute(
            "SELECT ativo, suporte, resistencia, criado_em FROM alertas_preco ORDER BY criado_em DESC"
        ).fetchall()

def deletar_alerta_nivel(aid):
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("UPDATE alertas_nivel SET disparado=1 WHERE id=?", (aid,))
    conn.commit()
    conn.close()

def monitorar_alertas_nivel():
    global _alerta_preco_anterior
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("SELECT id, ativo, nivel, condicao, nota FROM alertas_nivel WHERE disparado=0")
    alertas = c.fetchall()
    conn.close()
    if not alertas:
        return
    for alerta in alertas:
        aid, ativo, nivel, condicao, nota = alerta
        dados = buscar_preco_atual(ativo)
        if not dados:
            continue
        preco     = dados["preco"]
        chave_ant = f"alerta_{aid}_{ativo}"
        preco_ant = _alerta_preco_anterior.get(chave_ant)

        # Usar nivel_cruzado — mesmo mecanismo dos trades
        # Sem tolerancia, sem high/low, apenas preco last do ticker
        disparar = False
        if condicao == "ACIMA"  and nivel_cruzado(preco_ant, preco, nivel, "ACIMA"):
            disparar = True
        elif condicao == "ABAIXO" and nivel_cruzado(preco_ant, preco, nivel, "ABAIXO"):
            disparar = True

        if disparar:
            deletar_alerta_nivel(aid)
            seta  = "▲" if condicao == "ACIMA" else "▼"
            nota_txt = f"\n📝 {nota}" if nota else ""
            enviar_telegram(
                f"🔔 <b>ALERTA #{aid} ACIONADO</b>\n"
                f"📊 {ativo} | ${preco:.6g} {seta} ${nivel}{nota_txt}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Envie o gráfico para análise completa.",
                topic="alertas",
            )
            log.info(f"Alerta #{aid} {ativo} disparado em ${preco:.6g}")
        else:
            _alerta_preco_anterior[chave_ant] = preco

# Dicionario global para guardar preco anterior de cada trade
# Permite detectar cruzamento de nivel mesmo que o ciclo pule sobre ele
_preco_anterior = {}

def nivel_cruzado(preco_ant, preco_atual, nivel, direcao):
    """
    Detecta se o nivel foi cruzado entre duas leituras de preco.
    Garante que o alerta dispara mesmo que o bot nao capture o momento exato.
    direcao ABAIXO: preco caiu atraves do nivel (entrada LONG, stop LONG, alvos SHORT)
    direcao ACIMA:  preco subiu atraves do nivel (entrada SHORT, stop SHORT, alvos LONG)
    """
    if preco_ant is None:
        # Primeira leitura: comparar apenas com preco atual
        return preco_atual <= nivel if direcao == "ABAIXO" else preco_atual >= nivel
    if direcao == "ABAIXO":
        return preco_ant > nivel >= preco_atual
    else:
        return preco_ant < nivel <= preco_atual

def monitorar_trades():
    global _preco_anterior
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("SELECT * FROM trades WHERE resultado='ABERTO'")
    abertos = c.fetchall()
    conn.close()

    agora_brt = brt_agora()

    for trade in abertos:
        tid, ativo, direcao, entrada, stop, a1, a2, a3, tf_ctx, tf_ent, resultado, criado = trade

        # Filtro temporal: ignorar nos primeiros 120s apos cadastro
        try:
            dt_criado = datetime.strptime(criado, "%Y-%m-%d %H:%M")
            dt_criado = dt_criado.replace(tzinfo=timezone(timedelta(hours=OFFSET_BRT)))
            if (agora_brt - dt_criado).total_seconds() < 120:
                continue
        except Exception as e:
            log.warning(f"Filtro temporal trade #{tid}: {e}")

        dados = buscar_preco_atual(ativo)
        if not dados:
            log.warning(f"Sem preco para {ativo}")
            continue

        preco     = dados["preco"]
        base      = f"{tid}_{ativo}"
        chave_ant = f"ant_{tid}_{ativo}"
        preco_ant = _preco_anterior.get(chave_ant)

        if direcao == "LONG":

            # STOP: preco cruzou para baixo o nivel do stop
            if nivel_cruzado(preco_ant, preco, stop, "ABAIXO") and not alerta_ja_enviado(f"{base}_stop"):
                marcar_alerta(f"{base}_stop")
                atualizar_resultado(ativo, "LOSS")
                duracao = calcular_duracao(criado)
                tg13.fechar_trade_card(
                    tid, "LOSS",
                    nota=(
                        f"🛑 STOP LONG | ${preco:.6g} | ⏱ {duracao}"
                    ),
                )
                _preco_anterior[chave_ant] = preco
                continue

            # ALVOS: so verificar apos entrada confirmada
            if alerta_ja_enviado(f"{base}_entrada"):
                if nivel_cruzado(preco_ant, preco, a3, "ACIMA") and not alerta_ja_enviado(f"{base}_a3"):
                    marcar_alerta(f"{base}_a3")
                    marcar_alerta(f"{base}_a2")
                    marcar_alerta(f"{base}_a1")
                    atualizar_resultado(ativo, "WIN_A3")
                    duracao = calcular_duracao(criado)
                    tg13.fechar_trade_card(tid, "WIN_A3", nota=f"🏆 A3 LONG | ${preco:.6g} | ⏱ {duracao}")
                elif nivel_cruzado(preco_ant, preco, a2, "ACIMA") and not alerta_ja_enviado(f"{base}_a2"):
                    marcar_alerta(f"{base}_a2")
                    marcar_alerta(f"{base}_a1")
                    atualizar_resultado(ativo, "WIN_A2")
                    tg13.notify_trade_event(
                        tid,
                        f"🎯 <b>A2 LONG #{tid}</b> {ativo} @ ${preco:.6g}",
                        estado="A2",
                    )
                elif nivel_cruzado(preco_ant, preco, a1, "ACIMA") and not alerta_ja_enviado(f"{base}_a1"):
                    marcar_alerta(f"{base}_a1")
                    atualizar_resultado(ativo, "WIN_A1")
                    tg13.notify_trade_event(
                        tid,
                        f"🎯 <b>A1 LONG #{tid}</b> {ativo} @ ${preco:.6g} — breakeven",
                        estado="A1",
                    )

            # ENTRADA: preco cruzou para baixo o nivel de entrada
            if not alerta_ja_enviado(f"{base}_entrada"):
                if nivel_cruzado(preco_ant, preco, entrada, "ABAIXO"):
                    marcar_alerta(f"{base}_entrada")
                    tg13.notify_trade_event(
                        tid,
                        f"🟢 <b>ENTRADA LONG #{tid}</b> {ativo} @ ${preco:.6g}",
                        estado="ENTRADA",
                    )

        elif direcao == "SHORT":

            # STOP: preco cruzou para cima o nivel do stop
            if nivel_cruzado(preco_ant, preco, stop, "ACIMA") and not alerta_ja_enviado(f"{base}_stop"):
                marcar_alerta(f"{base}_stop")
                atualizar_resultado(ativo, "LOSS")
                duracao = calcular_duracao(criado)
                tg13.fechar_trade_card(
                    tid, "LOSS",
                    nota=f"🛑 STOP SHORT | ${preco:.6g} | ⏱ {duracao}",
                )
                _preco_anterior[chave_ant] = preco
                continue

            # ALVOS: so verificar apos entrada confirmada
            if alerta_ja_enviado(f"{base}_entrada"):
                if nivel_cruzado(preco_ant, preco, a3, "ABAIXO") and not alerta_ja_enviado(f"{base}_a3"):
                    marcar_alerta(f"{base}_a3")
                    marcar_alerta(f"{base}_a2")
                    marcar_alerta(f"{base}_a1")
                    atualizar_resultado(ativo, "WIN_A3")
                    duracao = calcular_duracao(criado)
                    tg13.fechar_trade_card(tid, "WIN_A3", nota=f"🏆 A3 SHORT | ${preco:.6g} | ⏱ {duracao}")
                elif nivel_cruzado(preco_ant, preco, a2, "ABAIXO") and not alerta_ja_enviado(f"{base}_a2"):
                    marcar_alerta(f"{base}_a2")
                    marcar_alerta(f"{base}_a1")
                    atualizar_resultado(ativo, "WIN_A2")
                    tg13.notify_trade_event(
                        tid,
                        f"🎯 <b>A2 SHORT #{tid}</b> {ativo} @ ${preco:.6g}",
                        estado="A2",
                    )
                elif nivel_cruzado(preco_ant, preco, a1, "ABAIXO") and not alerta_ja_enviado(f"{base}_a1"):
                    marcar_alerta(f"{base}_a1")
                    atualizar_resultado(ativo, "WIN_A1")
                    tg13.notify_trade_event(
                        tid,
                        f"🎯 <b>A1 SHORT #{tid}</b> {ativo} @ ${preco:.6g} — breakeven",
                        estado="A1",
                    )

            # ENTRADA: preco cruzou para cima o nivel de entrada
            if not alerta_ja_enviado(f"{base}_entrada"):
                if nivel_cruzado(preco_ant, preco, entrada, "ACIMA"):
                    marcar_alerta(f"{base}_entrada")
                    tg13.notify_trade_event(
                        tid,
                        f"🔴 <b>ENTRADA SHORT #{tid}</b> {ativo} @ ${preco:.6g}",
                        estado="ENTRADA",
                    )

        # Guardar preco atual para proxima iteracao
        _preco_anterior[chave_ant] = preco


    # ─── MONITORAR ALERTAS DE PREÇO (FAIXA SUPORTE/RESISTÊNCIA) ─────────────
    alertas_p = listar_alertas_preco()
    for (ativo_a, suporte, resistencia, _) in alertas_p:
        try:
            preco_info = buscar_preco_atual(ativo_a)
            if not preco_info:
                continue
            low_a  = preco_info.get("low",  preco_info["preco"])
            high_a = preco_info.get("high", preco_info["preco"])
            chave_sup = f"ap_{ativo_a}_suporte_{suporte}"
            chave_res = f"ap_{ativo_a}_resistencia_{resistencia}"
            if low_a <= suporte * 1.002 and not alerta_ja_enviado(chave_sup):
                marcar_alerta(chave_sup)
                enviar_telegram(
                    f"📍 <b>ALERTA DE PREÇO — {ativo_a}</b>\n"
                    f"🟢 Tocou <b>SUPORTE</b> ${suporte:,.2f}\n"
                    f"Preço atual: ${preco_info['preco']:,.2f}\n"
                    f"⚠️ Zona de decisão — aguardar confirmação de setup",
                    topic="alertas",
                )
            if high_a >= resistencia * 0.998 and not alerta_ja_enviado(chave_res):
                marcar_alerta(chave_res)
                enviar_telegram(
                    f"📍 <b>ALERTA DE PREÇO — {ativo_a}</b>\n"
                    f"🔴 Tocou <b>RESISTÊNCIA</b> ${resistencia:,.2f}\n"
                    f"Preço atual: ${preco_info['preco']:,.2f}\n"
                    f"⚠️ Zona de decisão — aguardar confirmação de setup",
                    topic="alertas",
                )
        except Exception as e:
            log.error(f"Erro monitorando alerta_preco {ativo_a}: {e}")

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
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        topic="relatorios",
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
        enviar_telegram(
            f"📊 <b>Relatório Semanal</b>\n{brt}\nNenhum trade na última semana.",
            topic="relatorios",
        )
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
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        topic="relatorios",
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
    return jsonify({"status": "online", "version": "v13.5"})


@flask_app.route("/api/jj/sinal", methods=["POST"])
def api_jj_sinal():
    """Recebe sinais externos (ex: Jeova Jireh rastreador) — opcional."""
    secret = os.environ.get("JJ_WEBHOOK_SECRET", "")
    if secret and request.headers.get("X-JJ-Secret") != secret:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    msg = data.get("mensagem") or data.get("message")
    if not msg:
        return jsonify({"error": "mensagem obrigatória"}), 400
    tipo = data.get("tipo", "scanner")
    prioridade = bool(data.get("prioridade", False))
    tg13.enviar_sinal_externo(str(msg), tipo=tipo, prioridade=prioridade)
    return jsonify({"ok": True, "version": "v13.1"})

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

def enviar_lista_alertas():
    rows_nivel = listar_alertas_nivel()
    rows_preco = listar_alertas_preco()
    if not rows_nivel and not rows_preco:
        enviar_telegram("📭 Nenhum alerta ativo.", topic="alertas")
        return
    texto = tg13.texto_lista_alertas(rows_nivel, rows_preco)
    kb = tg13.keyboard_alertas(rows_nivel, rows_preco)
    enviar_telegram(texto, topic="alertas", keyboard=kb)


def processar_comando(texto):
    partes = texto.strip().split()
    cmd    = partes[0].lower()

    if cmd in ["/start", "/ajuda"]:
        topics_txt = "✅ Topics ativos" if tg13.topics_ativos() else "⚠️ Topics OFF — chat único (configure TOPIC_* no Railway)"
        return (
            f"🤖 <b>LucSharkTrade {tg13.VERSION} — Comandos</b>\n"
            f"{topics_txt}\n\n"
            "<b>📊 TRADES</b>\n"
            "/trade ATIVO DIR ENTRADA STOP A1 A2 A3 TF_CTX TF_ENT\n"
            "/editar ID entrada|stop|a1|a2|a3 VALOR\n"
            "/fechar ID PRECO [motivo] | /trades\n"
            "Botões: ENTRADA / A1-A3 / STOP / FECHAR / EDITAR\n\n"
            "<b>🔔 ALERTAS</b>\n"
            "/alerta … | /alertas\n\n"
            "<b>🔍 SCANNER</b>\n"
            "/scan | /watch ATIVO | /unwatch ATIVO | /watchlist\n\n"
            "<b>📈 MERCADO</b>\n"
            "/preco ATIVO\n\n"
            "<b>📊 RELATÓRIOS</b>\n"
            "/relatorio | /semana\n\n"
            "<b>⚙️ SISTEMA</b>\n"
            "/status | /auto_setup | /setup_topics\n"
            "/parar | /ajuda"
        )

    elif cmd == "/setup_topics":
        return tg13.setup_topics_guia()

    elif cmd == "/preco":
        if len(partes) < 2:
            return "❌ Formato: /preco ATIVO"
        ativo = partes[1].upper()
        dados = buscar_preco_atual(ativo)
        if not dados:
            return f"❌ Sem preço para {ativo}"
        p = dados["preco"]
        return (
            f"💲 <b>{ativo}</b>\n"
            f"Preço: <b>${p:,.6g}</b>\n"
            f"Bid: ${dados.get('bid', p):,.6g} | Ask: ${dados.get('ask', p):,.6g}"
        )

    elif cmd == "/watch":
        if len(partes) < 2:
            return "❌ Formato: /watch ATIVO"
        ativo = partes[1].upper()
        tg13.watchlist_add(ativo)
        return f"✅ {ativo} adicionado à watchlist."

    elif cmd == "/unwatch":
        if len(partes) < 2:
            return "❌ Formato: /unwatch ATIVO"
        ativo = partes[1].upper()
        if tg13.watchlist_remove(ativo):
            return f"✅ {ativo} removido da watchlist."
        return f"⚠️ {ativo} não estava na watchlist."

    elif cmd == "/watchlist":
        rows = tg13.watchlist_listar()
        if not rows:
            return "📋 Watchlist vazia — scanner usa todos os ativos.\n/watch ATIVO para focar."
        linhas = ["📋 <b>WATCHLIST</b> (scanner focado)\n"]
        for ativo, criado in rows:
            linhas.append(f"  • {ativo} ({criado})")
        linhas.append("\n/unwatch ATIVO para remover")
        return "\n".join(linhas)

    elif cmd == "/editar":
        if len(partes) < 4:
            return "❌ Formato: /editar ID campo valor\nEx: /editar 1 stop 58500"
        try:
            trade_id = int(partes[1])
            campo = partes[2].lower()
            valor = float(partes[3])
            err = tg13.editar_trade_nivel(trade_id, campo, valor)
            if err:
                return f"❌ {err}"
            tg13.atualizar_dashboard()
            return f"✅ Trade #{trade_id} — {campo} → ${valor:g}"
        except Exception as e:
            return f"❌ Erro: {e}"

    elif cmd == "/trade":
        if len(partes) < 10:
            return "❌ Formato: /trade ATIVO DIR ENTRADA STOP A1 A2 A3 TF_CTX TF_ENT"
        try:
            ativo, direcao = partes[1].upper(), partes[2].upper()
            entrada, stop  = float(partes[3]), float(partes[4])
            a1, a2, a3     = float(partes[5]), float(partes[6]), float(partes[7])
            tf_ctx, tf_ent = partes[8], partes[9]
            tid = salvar_trade(ativo, direcao, entrada, stop, a1, a2, a3, tf_ctx, tf_ent)
            return f"TRADE_CRIADO:{tid}"
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
            marcar_alerta(f"{trade_id}_{ativo_f}_entrada")
            duracao_f = calcular_duracao(criado_f)
            sinal_r = "+" if r_obtido >= 0 else ""
            tg13.fechar_trade_card(
                trade_id,
                resultado_f,
                nota=(
                    f"Manual | Saída ${exit_price} | {sinal_r}{r_obtido:.2f}R | ⏱ {duracao_f}"
                ),
            )
            return (
                f"🔒 <b>TRADE #{trade_id} FECHADO</b>\n"
                f"📊 {ativo_f} {dir_f} → {resultado_f}"
            )
        except Exception as e:
            return f"❌ Erro: {e}"







    elif cmd == "/alerta":
        # Formato faixa: /alerta ATIVO SUPORTE RESISTENCIA (dois números)
        # Formato nível: /alerta ATIVO NIVEL ACIMA|ABAIXO [nota]
        if len(partes) < 3:
            return (
                "⚠️ Uso:\n"
                "📍 Faixa: /alerta ATIVO SUPORTE RESISTENCIA\n"
                "   Ex: /alerta BTCUSDT 59000 64500\n\n"
                "🔔 Nível: /alerta ATIVO NIVEL ACIMA|ABAIXO [nota]\n"
                "   Ex: /alerta BTCUSDT 72000 ACIMA rompimento"
            )
        ativo_a = partes[1].upper()
        terceiro = partes[3].upper() if len(partes) > 3 else ""
        if terceiro in ("ACIMA", "ABAIXO") or len(partes) == 3:
            # FORMATO NÍVEL
            if len(partes) < 4:
                return "❌ Nível: /alerta ATIVO NIVEL ACIMA|ABAIXO [nota]"
            try:
                nivel    = float(partes[2])
                condicao = partes[3].upper()
                nota     = " ".join(partes[4:]) if len(partes) > 4 else ""
                if condicao not in ("ACIMA", "ABAIXO"):
                    return "❌ Condição deve ser ACIMA ou ABAIXO"
                aid  = salvar_alerta_nivel(ativo_a, nivel, condicao, nota)
                seta = "▲" if condicao == "ACIMA" else "▼"
                return (
                    f"🔔 <b>Alerta nível #{aid} cadastrado!</b>\n"
                    f"📊 {ativo_a} | ${nivel} {seta} ({condicao})\n"
                    f"{'📝 ' + nota if nota else ''}\n"
                    f"✅ Dispara quando preço cruzar ${nivel}"
                )
            except Exception as e:
                return f"❌ Erro: {e}"
        else:
            # FORMATO FAIXA (SUPORTE + RESISTÊNCIA)
            if len(partes) != 4:
                return "⚠️ Faixa: /alerta ATIVO SUPORTE RESISTENCIA\nEx: /alerta BTCUSDT 59000 64500"
            try:
                sup = float(partes[2])
                res = float(partes[3])
            except ValueError:
                return "⚠️ Preços inválidos. Use números."
            if sup >= res:
                return "⚠️ Suporte deve ser menor que resistência."
            salvar_alerta_preco(ativo_a, sup, res)
            return (
                f"📍 <b>Alerta de faixa cadastrado!</b>\n"
                f"📊 Ativo: <b>{ativo_a}</b>\n"
                f"🟢 Suporte: ${sup:,.2f}\n"
                f"🔴 Resistência: ${res:,.2f}\n"
                f"🔄 Monitorando 24/7..."
            )

    elif cmd == "/alertas":
        return "ALERTAS_LIST"

    elif cmd == "/deletar_alerta":
        if len(partes) < 2:
            return "❌ Formato: /deletar_alerta ID"
        try:
            deletar_alerta_nivel(int(partes[1]))
            return f"🗑 Alerta #{partes[1]} removido."
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

    elif cmd == "/delalerta":
        partes = texto.strip().split()
        if len(partes) != 2:
            return "⚠️ Uso: /delalerta ATIVO\nEx: /delalerta BTCUSDT"
        ativo_a = partes[1].upper()
        ok = remover_alerta_preco(ativo_a)
        if ok:
            with sqlite3.connect("trades.db") as con:
                con.execute("DELETE FROM alertas_log WHERE chave LIKE ?", (f"ap_{ativo_a}_%",))
            return f"🗑 Alerta de faixa de <b>{ativo_a}</b> removido."
        return f"⚠️ Nenhum alerta de faixa encontrado para {ativo_a}."

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
        topics_txt = "Topics ON" if tg13.topics_ativos() else "Topics OFF (chat único)"
        return (
            f"✅ <b>LucSharkTrade {tg13.VERSION} ONLINE</b>\n"
            f"🕐 {brt}\n"
            f"📡 Exchanges: {ex_online}/3 online\n"
            f"💰 Capital: ${CAPITAL_INICIAL:,.2f}\n"
            f"🔄 Trades abertos: {n_abertos}\n"
            f"📌 Dashboard pinado + botões inline\n"
            f"🗂 {topics_txt}\n"
            f"⏱ Monitor: {INTERVALO_SEG}s"
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
            tg13.atualizar_precos_live(buscar_preco_atual)
        except Exception as e:
            log.debug(f"precos live: {e}")

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
    """Thread principal — comandos + callbacks inline (v13.0)."""
    log.info("Thread de comandos Telegram iniciada (v13.0).")
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

            cb = upd.get("callback_query")
            if cb:
                data = cb.get("data", "")
                cid = cb.get("id", "")
                try:
                    result = tg13.processar_callback(data, cid)
                    if result == "ALERTAS_REFRESH":
                        enviar_lista_alertas()
                    elif result:
                        enviar_telegram(result, topic="trades")
                except Exception as e:
                    log.error(f"callback erro {data}: {e}")
                    tg13.answer_callback(cid, f"Erro: {e}")
                continue

            msg   = upd.get("message", {})
            texto = (msg.get("text") or "").strip()

            if msg.get("photo"):
                try:
                    tg13.responder_analise(msg)
                except Exception as e:
                    log.error(f"analise foto: {e}")
                continue

            if not texto or not texto.startswith("/"):
                continue

            msg_date = msg.get("date", 0)
            if msg_date and (time.time() - msg_date) > 600:
                log.warning(f"Comando MUITO antigo ignorado (>10min): {texto}")
                continue

            cmd_base = texto.split()[0].lower().split("@")[0]
            topic = tg13.topic_for_cmd(cmd_base)
            chat  = msg.get("chat", {})
            log.info(f"Comando recebido: {texto} → topic {topic}")

            try:
                if cmd_base == "/ping":
                    tg13.responder_comando(
                        msg,
                        f"🏓 <b>PONG</b> — {tg13.VERSION} ONLINE\n"
                        f"Topics: {'ON' if tg13.topics_ativos() else 'OFF'}",
                    )
                    continue

                if chat.get("type") in ("group", "supergroup"):
                    try:
                        tg13.garantir_topics_grupo(chat["id"])
                    except Exception as e:
                        log.warning(f"garantir topics: {e}")

                if cmd_base == "/debug_topics":
                    tg13.responder_comando(msg, tg13.debug_topics_text(msg))
                    continue

                if cmd_base == "/limpar_duplicados":
                    if chat.get("type") not in ("group", "supergroup"):
                        tg13.responder_comando(
                            msg,
                            "❌ Use /limpar_duplicados no grupo LucShark Trading.",
                        )
                        continue
                    tg13.responder_comando(
                        msg, tg13.limpar_topics_duplicados(chat["id"])
                    )
                    continue

                if cmd_base == "/auto_setup":
                    chat = msg.get("chat", {})
                    if chat.get("type") not in ("group", "supergroup"):
                        enviar_telegram(
                            "❌ Abra o grupo <b>LucShark Trading</b> e envie /auto_setup lá.",
                            topic="geral",
                        )
                        continue
                    resultado = tg13.auto_setup_grupo(chat["id"])
                    tg13.enviar_para_chat(
                        chat["id"],
                        resultado,
                        thread=msg.get("message_thread_id"),
                    )
                    if tg13.topics_ativos():
                        try:
                            tg13.atualizar_dashboard()
                        except Exception:
                            pass
                    continue

                resposta = processar_comando(texto)
                if resposta == "SCAN_SOLICITADO":
                    enviar_telegram("🔍 Scanner iniciado manualmente...", topic="scanner")
                    threading.Thread(target=rodar_scanner, daemon=True).start()
                elif resposta == "DEBUG_SOLICITADO":
                    threading.Thread(target=rodar_scanner_debug, daemon=True).start()
                elif resposta and resposta.startswith("TRADE_CRIADO:"):
                    tid = int(resposta.split(":")[1])
                    tg13.criar_trade_mae(tid)
                    enviar_telegram(
                        f"✅ Trade #{tid} no painel — toque 📥 ENTRADA quando executar.",
                        topic="trades",
                    )
                elif resposta == "ALERTAS_LIST":
                    enviar_lista_alertas()
                elif resposta:
                    if chat.get("type") in ("group", "supergroup"):
                        tg13.responder_comando(msg, resposta)
                    else:
                        enviar_telegram(resposta, topic=topic)
            except Exception as e:
                log.error(f"Erro processando '{texto}': {e}")
                try:
                    tg13.responder_comando(
                        msg, f"⚠️ Erro ao processar {texto}: {e}"
                    ) if chat.get("type") in ("group", "supergroup") else enviar_telegram(
                        f"⚠️ Erro ao processar {texto}: {e}", topic="geral"
                    )
                except Exception:
                    pass

def main():
    init_db()
    tg13.deletar_webhook()
    tg13.carregar_topics_persistidos()
    grp = os.environ.get("TELEGRAM_GROUP_ID", "") or tg13._chat_id_persistido()
    if grp:
        try:
            tg13.garantir_topics_grupo(int(grp))
        except Exception as e:
            log.warning(f"rediscover startup: {e}")
    init_exchanges()
    try:
        tg13.registrar_menu_comandos()
        log.info("Menu de comandos Telegram registrado.")
    except Exception as e:
        log.warning(f"setMyCommands: {e}")

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
        topics_txt = (
            "🗂 <b>Topics ativos</b> — mensagens por canal"
            if tg13.topics_ativos()
            else "⚠️ <b>Topics OFF</b> — funciona no chat atual. Use /debug_topics em cada tópico."
        )
        enviar_telegram(
            f"🚀 <b>LucSharkTrade {tg13.VERSION} ONLINE!</b>\n"
            f"📅 {brt}\n\n"
            f"📌 Dashboard pinado | 💲 Preço ao vivo nos cards\n"
            f"🔘 ENTRADA + A1/A2/A3 + STOP/FECHAR (2 toques) + EDITAR\n"
            f"🔔 Alertas interativos | /preco | /watchlist\n"
            f"{topics_txt}\n\n"
            f"/ajuda — menu | /setup_topics — guia Topics"
            ,
            topic="geral",
        )
        try:
            tg13.atualizar_dashboard()
        except Exception as e:
            log.warning(f"dashboard inicial: {e}")

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
