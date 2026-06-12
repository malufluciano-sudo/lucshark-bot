"""
Watchlist canônica LucShark — BingX futuros + TradFi.
Carrega dados/watchlist_canon.txt no SQLite na primeira execução.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import telegram_v13 as tg13

log = logging.getLogger(__name__)

ROOT = Path(__file__).parent
CANON_PATH = ROOT / "dados" / "watchlist_canon.txt"
CANON_VERSION = "v1.0"

# TradFi: símbolo UI → par BingX (ccxt swap)
BINGX_TRADFI: dict[str, str] = {
    "XAUUSDT": "NCCOGOLD2USD/USDT:USDT",
    "XAGUSDT": "NCCOXAG2USD/USDT:USDT",
    "XPTUSDT": "NCCOXPT2USD/USDT:USDT",
    "XPDUSDT": "NCCOPALLADIUM2USD/USDT:USDT",
    "WTIUSDT": "NCCO1OILWTI2USD/USDT:USDT",
    "BRENTUSDT": "NCCO1OILBRENT2USD/USDT:USDT",
    "NATGASUSDT": "NCCONATURALGAS2USD/USDT:USDT",
    "COPPERUSDT": "NCCOCOPPER2USD/USDT:USDT",
    "COCOAUSDT": "NCCOCOCOA2USD/USDT:USDT",
    "SOYBUSDT": "NCCOSOYBEANS2USD/USDT:USDT",
    "US100USDT": "NCSINASDAQ1002USD/USDT:USDT",
    "US500USDT": "NCSI724SP5002USD/USDT:USDT",
    "GER40USDT": "NCSIGER2USD/USDT:USDT",
    "JPN225USDT": "NCSINIKKEI2252USD/USDT:USDT",
    "US30USDT": "NCSIDOWJONES2USD/USDT:USDT",
    "TSLAUSDT": "NCSKTSLA2USD/USDT:USDT",
    "NVDAUSDT": "NCSKNVDA2USD/USDT:USDT",
    "COINUSDT": "NCSKCOIN2USD/USDT:USDT",
    "AMDUSDT": "NCSKAMD2USD/USDT:USDT",
    "MSTRUSDT": "NCSKMSTR2USD/USDT:USDT",
    "METAUSDT": "NCSKMETA2USD/USDT:USDT",
    "PLTRUSDT": "NCSKPLTR2USD/USDT:USDT",
    "SMCIUSDT": "NCSKSMCI2USD/USDT:USDT",
    "GOOGLUSDT": "NCSKGOOGL2USD/USDT:USDT",
    "AMZNUSDT": "NCSKAMZN2USD/USDT:USDT",
    "GBPUSD": "NCFXGBP2USD/USDT:USDT",
    "USDBRL": "NCFXUSDBRL2USD/USDT:USDT",
    "AUDUSD": "NCFXAUD2USD/USDT:USDT",
    "NZDUSD": "NCFXNZD2USD/USDT:USDT",
    "GBPJPY": "NCFXGBP2JPY/USDT:USDT",
}


def _norm_pair(s: str) -> str:
    """Normaliza par ccxt ou símbolo UI para comparação."""
    s = s.split("@")[0].upper()
    if ":" in s:
        s = s.split(":")[0]
    return s.replace("/", "").replace("-", "").replace("_", "")


def parse_canon_file() -> list[dict]:
    if not CANON_PATH.exists():
        log.warning("watchlist_canon.txt não encontrado: %s", CANON_PATH)
        return []
    items: list[dict] = []
    for raw in CANON_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        categoria, simbolo = parts[0], parts[1].upper()
        bingx = parts[2] if len(parts) > 2 and parts[2] else BINGX_TRADFI.get(simbolo, "")
        items.append({
            "categoria": categoria,
            "simbolo": simbolo,
            "bingx": bingx,
        })
    return items


def match_keys_from_db() -> set[str]:
    rows = tg13.watchlist_listar()
    keys: set[str] = set()
    for simbolo, _ in rows:
        s = simbolo.upper()
        keys.add(_norm_pair(s))
        bx = BINGX_TRADFI.get(s)
        if bx:
            keys.add(_norm_pair(bx))
    return keys


def filtrar_pares(pares: list[str]) -> list[str]:
    keys = match_keys_from_db()
    if not keys:
        return pares
    out = []
    for p in pares:
        if _norm_pair(p) in keys:
            out.append(p)
    return out


def seed_watchlist(force: bool = False) -> tuple[int, dict[str, int]]:
    """Popula SQLite a partir do arquivo. Retorna (total, por_categoria)."""
    existentes = tg13.watchlist_listar()
    versao_db = tg13.sistema_get("watchlist_version") or ""
    precisa_upgrade = versao_db != CANON_VERSION
    if existentes and not force and not precisa_upgrade:
        log.info("Watchlist já tem %d ativos — seed ignorado.", len(existentes))
        return len(existentes), {}

    items = parse_canon_file()
    if not items:
        return 0, {}

    if force and existentes:
        conn = tg13._db()
        conn.execute("DELETE FROM watchlist")
        conn.commit()
        conn.close()

    por_cat: dict[str, int] = {}
    for it in items:
        tg13.watchlist_add(it["simbolo"])
        por_cat[it["categoria"]] = por_cat.get(it["categoria"], 0) + 1

    total = len(items)
    tg13.sistema_set("watchlist_version", CANON_VERSION)
    log.info("Watchlist seed: %d ativos (%s)", total, por_cat)
    return total, por_cat


def resumo_watchlist() -> str:
    rows = tg13.watchlist_listar()
    if not rows:
        return (
            "📋 <b>Watchlist vazia</b>\n"
            "Use <code>/reload_watchlist</code> para carregar os 130 ativos canônicos."
        )
    items = parse_canon_file()
    cat_map = {it["simbolo"]: it["categoria"] for it in items}
    por_cat: dict[str, list[str]] = {}
    for ativo, _ in rows:
        cat = cat_map.get(ativo, "outros")
        por_cat.setdefault(cat, []).append(ativo)

    linhas = [
        f"📋 <b>WATCHLIST CANÔNICA</b> — {len(rows)} ativos\n",
        "BingX futuros + TradFi | Scanner usa só esta lista\n",
    ]
    ordem = ["cripto", "commodity", "indice", "acao", "forex", "outros"]
    labels = {
        "cripto": "🪙 Cripto (top 100)",
        "commodity": "🛢 Commodities (10)",
        "indice": "📈 Índices (5)",
        "acao": "🇺🇸 Ações EUA (10)",
        "forex": "💱 Forex (5)",
        "outros": "📌 Outros",
    }
    for cat in ordem:
        lst = por_cat.get(cat)
        if not lst:
            continue
        amostra = ", ".join(lst[:8])
        extra = f" +{len(lst) - 8}" if len(lst) > 8 else ""
        linhas.append(f"{labels.get(cat, cat)}: <b>{len(lst)}</b>")
        linhas.append(f"  <i>{amostra}{extra}</i>\n")

    linhas.append(
        "🔄 <code>/reload_watchlist</code> — recarrega do arquivo\n"
        "🔍 <code>/scan</code> — varre esta lista (2–5 min)"
    )
    return "\n".join(linhas)
