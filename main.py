# ─────────────────────────────────────────────
# CORREÇÃO: monitorar_trades()
# Bugs corrigidos:
# 1. Lógica elif quebrada → substituída por checks independentes
# 2. tol_entrada muito restrita → usa high/low do candle
# 3. Alerta de zona conflitando com alerta de entrada
# 4. init_exchanges com timeout para não crashar no Railway
# ─────────────────────────────────────────────

CORRECAO = '''
def init_exchanges():
    """Inicializa instâncias ccxt — com timeout para não crashar no Railway."""
    import ccxt as _ccxt_local
    for ex in EXCHANGES_CONFIG:
        try:
            instance = getattr(_ccxt_local, ex["id"])({"enableRateLimit": True})
            # Timeout de 10s para não travar o startup
            instance.timeout = 10000
            instance.load_markets()
            ex["instance"] = instance
            log.info(f"Exchange {ex['label']}: OK ({len(instance.markets)} mercados)")
        except Exception as e:
            log.warning(f"Exchange {ex['label']}: falhou — {e}")
            ex["instance"] = None  # Garantir None em caso de falha


def monitorar_trades():
    conn = sqlite3.connect("trades.db")
    c = conn.cursor()
    c.execute("SELECT * FROM trades WHERE resultado=\'ABERTO\'")
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

        base = f"{tid}_{ativo}"

        # ── Tolerância de entrada: 0.5% ou range high/low do candle ──
        tol_entrada = entrada * 0.005  # 0.5% — mais realista

        if direcao == "LONG":

            # ── STOP primeiro — prioridade máxima ──
            if low <= stop and not alerta_ja_enviado(f"{base}_stop"):
                marcar_alerta(f"{base}_stop")
                atualizar_resultado(ativo, "LOSS")
                duracao = calcular_duracao(criado)
                enviar_telegram(
                    f"🛑 <b>STOP — {ativo} LONG #{tid}</b>\\n"
                    f"💲 Preço: ${preco:.6g} | Stop: ${stop}\\n"
                    f"❌ Sair imediatamente!\\n"
                    f"━━━━━━━━━━━━━━━━━━\\n"
                    f"📋 Entrada: ${entrada} | Saída: ${preco:.6g}\\n"
                    f"❌ LOSS | ⏱ {duracao}"
                )
                continue  # Não verificar alvos se stop foi atingido

            # ── ALVOS — do maior para o menor (checks independentes) ──
            if high >= a3 and not alerta_ja_enviado(f"{base}_a3"):
                marcar_alerta(f"{base}_a3")
                # Marcar a1 e a2 também para não gerar alertas duplicados
                marcar_alerta(f"{base}_a1")
                marcar_alerta(f"{base}_a2")
                atualizar_resultado(ativo, "WIN_A3")
                duracao = calcular_duracao(criado)
                enviar_telegram(
                    f"🏆 <b>A3 ATINGIDO — {ativo} LONG #{tid}</b>\\n"
                    f"💲 Preço: ${preco:.6g} | A3: ${a3}\\n"
                    f"✅ Realizar 80% | 🎉 Trailing Stop no restante\\n"
                    f"━━━━━━━━━━━━━━━━━━\\n"
                    f"📋 Entrada: ${entrada} → A1 → A2 → A3\\n"
                    f"✅ WIN A3 (RR 3:1) | ⏱ {duracao}"
                )

            elif high >= a2 and not alerta_ja_enviado(f"{base}_a2"):
                marcar_alerta(f"{base}_a2")
                marcar_alerta(f"{base}_a1")
                atualizar_resultado(ativo, "WIN_A2")
                enviar_telegram(
                    f"🎯 <b>A2 ATINGIDO — {ativo} LONG #{tid}</b>\\n"
                    f"💲 Preço: ${preco:.6g} | A2: ${a2}\\n"
                    f"✅ Realizar 50% | ⏳ Aguardar A3: ${a3}"
                )

            elif high >= a1 and not alerta_ja_enviado(f"{base}_a1"):
                marcar_alerta(f"{base}_a1")
                atualizar_resultado(ativo, "WIN_A1")
                enviar_telegram(
                    f"🎯 <b>A1 ATINGIDO — {ativo} LONG #{tid}</b>\\n"
                    f"💲 Preço: ${preco:.6g} | A1: ${a1}\\n"
                    f"✅ Realizar 25%\\n"
                    f"🔒 Mover Stop para ${entrada} (breakeven)\\n"
                    f"⏳ Aguardar A2: ${a2}"
                )

            # ── ENTRADA ACIONADA ──
            # Verifica se high tocou a entrada (mais confiável que last price)
            elif (low <= entrada <= high or abs(preco - entrada) <= tol_entrada) \\
                    and not alerta_ja_enviado(f"{base}_entrada"):
                marcar_alerta(f"{base}_entrada")
                marcar_alerta(f"{base}_zona")  # Evitar alerta de zona após entrada
                enviar_telegram(
                    f"🟢 <b>ENTRADA LONG ACIONADA — {ativo} #{tid}</b>\\n"
                    f"💲 Preço: ${preco:.6g}\\n"
                    f"📥 Entrada: ${entrada} | Stop: ${stop}\\n"
                    f"🎯 A1: ${a1} | A2: ${a2} | A3: ${a3}"
                )

            # ── ZONA DE APROXIMAÇÃO (apenas se ainda não entrou) ──
            else:
                distancia_pct = (preco - entrada) / entrada * 100
                if 0 < distancia_pct <= 3.0 \\
                        and not alerta_ja_enviado(f"{base}_zona") \\
                        and not alerta_ja_enviado(f"{base}_entrada"):
                    marcar_alerta(f"{base}_zona")
                    enviar_telegram(
                        f"👀 <b>ZONA DE ENTRADA — {ativo} LONG #{tid}</b>\\n"
                        f"💲 Preço: ${preco:.6g} | Entrada: ${entrada}\\n"
                        f"📍 Preço a {round(distancia_pct,2)}% acima da entrada\\n"
                        f"⏳ Aguardando pullback para acionar..."
                    )

        elif direcao == "SHORT":

            # ── STOP primeiro — prioridade máxima ──
            if high >= stop and not alerta_ja_enviado(f"{base}_stop"):
                marcar_alerta(f"{base}_stop")
                atualizar_resultado(ativo, "LOSS")
                duracao = calcular_duracao(criado)
                enviar_telegram(
                    f"🛑 <b>STOP — {ativo} SHORT #{tid}</b>\\n"
                    f"💲 Preço: ${preco:.6g} | Stop: ${stop}\\n"
                    f"❌ Sair imediatamente!\\n"
                    f"━━━━━━━━━━━━━━━━━━\\n"
                    f"📋 Entrada: ${entrada} | Saída: ${preco:.6g}\\n"
                    f"❌ LOSS | ⏱ {duracao}"
                )
                continue  # Não verificar alvos se stop foi atingido

            # ── ALVOS — do maior para o menor ──
            if low <= a3 and not alerta_ja_enviado(f"{base}_a3"):
                marcar_alerta(f"{base}_a3")
                marcar_alerta(f"{base}_a1")
                marcar_alerta(f"{base}_a2")
                atualizar_resultado(ativo, "WIN_A3")
                duracao = calcular_duracao(criado)
                enviar_telegram(
                    f"🏆 <b>A3 ATINGIDO — {ativo} SHORT #{tid}</b>\\n"
                    f"💲 Preço: ${preco:.6g} | A3: ${a3}\\n"
                    f"✅ Realizar 80% | 🎉 Trailing Stop no restante\\n"
                    f"━━━━━━━━━━━━━━━━━━\\n"
                    f"📋 Entrada: ${entrada} → A1 → A2 → A3\\n"
                    f"✅ WIN A3 (RR 3:1) | ⏱ {duracao}"
                )

            elif low <= a2 and not alerta_ja_enviado(f"{base}_a2"):
                marcar_alerta(f"{base}_a2")
                marcar_alerta(f"{base}_a1")
                atualizar_resultado(ativo, "WIN_A2")
                enviar_telegram(
                    f"🎯 <b>A2 ATINGIDO — {ativo} SHORT #{tid}</b>\\n"
                    f"💲 Preço: ${preco:.6g} | A2: ${a2}\\n"
                    f"✅ Realizar 50% | ⏳ Aguardar A3: ${a3}"
                )

            elif low <= a1 and not alerta_ja_enviado(f"{base}_a1"):
                marcar_alerta(f"{base}_a1")
                atualizar_resultado(ativo, "WIN_A1")
                enviar_telegram(
                    f"🎯 <b>A1 ATINGIDO — {ativo} SHORT #{tid}</b>\\n"
                    f"💲 Preço: ${preco:.6g} | A1: ${a1}\\n"
                    f"✅ Realizar 25%\\n"
                    f"🔒 Mover Stop para ${entrada} (breakeven)\\n"
                    f"⏳ Aguardar A2: ${a2}"
                )

            # ── ENTRADA ACIONADA ──
            elif (low <= entrada <= high or abs(preco - entrada) <= tol_entrada) \\
                    and not alerta_ja_enviado(f"{base}_entrada"):
                marcar_alerta(f"{base}_entrada")
                marcar_alerta(f"{base}_zona")
                enviar_telegram(
                    f"🔴 <b>ENTRADA SHORT ACIONADA — {ativo} #{tid}</b>\\n"
                    f"💲 Preço: ${preco:.6g}\\n"
                    f"📥 Entrada: ${entrada} | Stop: ${stop}\\n"
                    f"🎯 A1: ${a1} | A2: ${a2} | A3: ${a3}"
                )

            # ── ZONA DE APROXIMAÇÃO ──
            else:
                distancia_pct = (entrada - preco) / entrada * 100
                if 0 < distancia_pct <= 3.0 \\
                        and not alerta_ja_enviado(f"{base}_zona") \\
                        and not alerta_ja_enviado(f"{base}_entrada"):
                    marcar_alerta(f"{base}_zona")
                    enviar_telegram(
                        f"👀 <b>ZONA DE ENTRADA — {ativo} SHORT #{tid}</b>\\n"
                        f"💲 Preço: ${preco:.6g} | Entrada: ${entrada}\\n"
                        f"📍 Preço a {round(distancia_pct,2)}% abaixo da entrada\\n"
                        f"⏳ Aguardando subida para acionar..."
                    )
'''

print(CORRECAO)
