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
INTERVALO_SEG    = int(os.environ.get("INTERVALO_SEG", "120"))
INTERVALO_SCAN   = int(os.environ.get("INTERVALO_SCAN", "3600"))
MIN_VOLUME_24H   = float(os.environ.get("MIN_VOLUME_24H", "100000"))

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

    return {
        "symbol": symbol.upper(),
        "preco": preco,
        "vol_rel": vol_rel,
        "rsi": rsi,
        "vwap": round(vwap, 4),
        "suporte": round(suporte, 4),
        "resistencia": round(resist, 4),
        "sinais": sinais,
        "forca_max": forca_max
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

    for symbol in pares:
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
            linhas.append(
                f"{i}. <b>{r['symbol']}</b> {r['vies_emoji']} {r['vies']}"
                f" | Vol <b>{r['vol_rel']}x</b> | RSI {r['rsi']}"
                f" | 💲{r['preco']:.6g}"
            )
        linhas.append("")

    if alta_prioridade:
        linhas.append(f"⚡ <b>ALTA PRIORIDADE — Vol 5–10x</b>")
        linhas.append("")
        for i, r in enumerate(alta_prioridade, 1):
            linhas.append(
                f"{i}. <b>{r['symbol']}</b> {r['vies_emoji']} {r['vies']}"
                f" | Vol <b>{r['vol_rel']}x</b> | RSI {r['rsi']}"
                f" | 💲{r['preco']:.6g}"
            )
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


def monitorar_trades():
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("SELECT * FROM trades WHERE resultado='ABERTO'")
    abertos = c.fetchall()
    conn.close()

    for trade in abertos:
        tid, ativo, direcao, entrada, stop, a1, a2, a3, tf_ctx, tf_ent, resultado, criado = trade
        symbol = ativo.lower().replace("/", "_").replace("-", "_")
        candles = buscar_candles(symbol, "kline_5min", 5)
        if not candles:
            continue

        preco = float(candles[-1][4])
        tol   = entrada * TOLERANCIA_PCT

        if direcao == "LONG" and abs(preco - entrada) <= tol:
            enviar_telegram(
                f"🟢 <b>ENTRADA LONG — {ativo}</b>\n"
                f"💲 Preço: ${preco:.6g}\n"
                f"📥 Entrada: ${entrada} | Stop: ${stop}\n"
                f"🎯 A1: ${a1} | A2: ${a2} | A3: ${a3}"
            )
        elif direcao == "SHORT" and abs(preco - entrada) <= tol:
            enviar_telegram(
                f"🔴 <b>ENTRADA SHORT — {ativo}</b>\n"
                f"💲 Preço: ${preco:.6g}\n"
                f"📥 Entrada: ${entrada} | Stop: ${stop}\n"
                f"🎯 A1: ${a1} | A2: ${a2} | A3: ${a3}"
            )

# ─────────────────────────────────────────────
# COMANDOS DO TELEGRAM
# ─────────────────────────────────────────────
def processar_comando(texto):
    partes = texto.strip().split()
    cmd = partes[0].lower()

    if cmd in ["/start", "/ajuda"]:
        return (
            "🤖 <b>LucSharkTrade v10 — Comandos</b>\n\n"
            "/trade ATIVO DIR ENTRADA STOP A1 A2 A3 TF_CTX TF_ENT\n"
            "/resultado ATIVO WIN_A1 | LOSS\n"
            "/trades — trades abertos\n"
            "/relatorio — estatísticas\n"
            "/scan — rodar scanner agora\n"
            "/ativos — total de ativos monitorados\n"
            "/status — status do bot\n"
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
                f"⏱ {tf_ctx} / {tf_ent} | 🔄 Monitorando 24/7..."
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
            linhas.append(f"{emoji} #{tid} {ativo} {direcao} ${entrada} → {resultado} ({criado})")
        return "\n".join(linhas)

    elif cmd == "/relatorio":
        total, wins, loss, abertos, wr = relatorio()
        return (
            f"📈 <b>Relatório LucSharkTrade</b>\n\n"
            f"Total: {total} | ✅ {wins} | ❌ {loss} | 🔄 {abertos}\n"
            f"🎯 Win Rate: {wr:.1f}%"
        )

    elif cmd == "/scan":
        return "SCAN_SOLICITADO"

    elif cmd == "/debug":
        return "DEBUG_SOLICITADO"

    elif cmd == "/ativos":
        tickers  = buscar_ticker_24h()
        pares    = buscar_todos_pares()
        validos  = sum(1 for p in pares
                       if tickers.get(p) and float(tickers[p].get("turnover", 0)) >= MIN_VOLUME_24H)
        return (
            f"📡 <b>Ativos Monitorados</b>\n\n"
            f"Total LBank: {len(pares)}\n"
            f"Com volume ≥ ${MIN_VOLUME_24H:,.0f}: {validos}\n"
            f"Timeframe: 15M\n"
            f"FORTE ≥{MULT_FORTE}x | MÉDIO ≥{MULT_MEDIO}x | ALERTA ≥{MULT_ALERTA}x"
        )

    elif cmd == "/status":
        brt = brt_agora().strftime("%d/%m/%Y %H:%M BRT")
        return f"✅ <b>Bot ONLINE</b>\n🕐 {brt}\n💰 Capital: ${CAPITAL_INICIAL:,.2f}"

    return None

# ─────────────────────────────────────────────
# LOOP PRINCIPAL
# ─────────────────────────────────────────────
def main():
    init_db()
    brt = brt_agora().strftime("%d/%m/%Y %H:%M BRT")
    enviar_telegram(
        f"🚀 <b>LucSharkTrade v10 ONLINE!</b>\n"
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

        time.sleep(INTERVALO_SEG)

if __name__ == "__main__":
    main()
