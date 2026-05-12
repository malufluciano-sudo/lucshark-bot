import streamlit as st
import sqlite3
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from datetime import datetime, timedelta
import os

# ── Configuração ──
st.set_page_config(
    page_title="LucSharkTrade Dashboard",
    page_icon="🦈",
    layout="wide",
    initial_sidebar_state="collapsed"
)

DB_PATH = os.environ.get("DB_PATH", "trades.db")

# ── CSS ──
st.markdown("""
<style>
    .main { background-color: #0e1117; }
    .metric-card {
        background: #1e2130;
        border-radius: 12px;
        padding: 20px;
        text-align: center;
        border: 1px solid #2d3250;
    }
    .metric-val { font-size: 2rem; font-weight: bold; }
    .win  { color: #00d4aa; }
    .loss { color: #ff4b4b; }
    .open { color: #ffa500; }
    h1 { color: #00d4aa; }
</style>
""", unsafe_allow_html=True)

# ── Carregar dados ──
@st.cache_data(ttl=30)
def carregar_trades():
    try:
        conn = sqlite3.connect(DB_PATH)
        df = pd.read_sql_query("SELECT * FROM trades ORDER BY id DESC", conn)
        conn.close()
        return df
    except:
        return pd.DataFrame()

def calcular_pnl(resultado, risco=20):
    if not resultado:
        return 0
    if "A3" in resultado: return risco * 3
    if "A2" in resultado: return risco * 2
    if "WIN" in resultado: return risco
    if resultado == "LOSS": return -risco
    return 0

# ── Header ──
col_logo, col_title = st.columns([1, 8])
with col_logo:
    st.markdown("# 🦈")
with col_title:
    st.markdown("# LucSharkTrade Dashboard")
    st.caption(f"Atualizado: {datetime.now().strftime('%d/%m/%Y %H:%M')} BRT")

st.divider()

# ── Carregar dados ──
df = carregar_trades()

if df.empty:
    st.warning("Nenhum trade encontrado. Verifique o banco de dados.")
    st.stop()

# ── Calcular métricas ──
df["pnl"] = df["resultado"].apply(calcular_pnl)
df["is_win"]  = df["resultado"].apply(lambda x: bool(x and x.startswith("WIN")))
df["is_loss"] = df["resultado"].apply(lambda x: x == "LOSS")
df["is_open"] = df["resultado"].apply(lambda x: not x or x == "ABERTO")

total   = len(df)
wins    = df["is_win"].sum()
losses  = df["is_loss"].sum()
abertos = df["is_open"].sum()
wr      = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
pnl_total = df["pnl"].sum()
capital = 1000 + pnl_total

# ── Cards de métricas ──
c1, c2, c3, c4, c5, c6 = st.columns(6)
with c1:
    st.metric("Total Trades", total)
with c2:
    st.metric("✅ Wins", int(wins))
with c3:
    st.metric("❌ Losses", int(losses))
with c4:
    st.metric("🔄 Abertos", int(abertos))
with c5:
    st.metric("🎯 Win Rate", f"{wr:.1f}%")
with c6:
    delta_color = "normal" if pnl_total >= 0 else "inverse"
    st.metric("💰 P&L", f"${pnl_total:+.2f}", f"Capital: ${capital:,.2f}")

st.divider()

# ── Gráficos ──
col_left, col_right = st.columns([3, 2])

with col_left:
    st.subheader("📈 Curva de Capital")
    df_sorted = df.sort_values("id")
    df_sorted["pnl_cum"] = df_sorted["pnl"].cumsum()
    df_sorted["capital_cur"] = 1000 + df_sorted["pnl_cum"]

    fig_capital = go.Figure()
    fig_capital.add_trace(go.Scatter(
        x=list(range(len(df_sorted))),
        y=df_sorted["capital_cur"],
        fill="tozeroy",
        fillcolor="rgba(0, 212, 170, 0.1)",
        line=dict(color="#00d4aa", width=2),
        name="Capital"
    ))
    fig_capital.add_hline(y=1000, line_dash="dash", line_color="gray", opacity=0.5)
    fig_capital.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=300,
        showlegend=False,
        margin=dict(l=0, r=0, t=10, b=0),
        yaxis=dict(tickprefix="$"),
    )
    st.plotly_chart(fig_capital, use_container_width=True)

with col_right:
    st.subheader("🥧 Resultados")
    labels = ["Wins", "Losses", "Abertos"]
    values = [int(wins), int(losses), int(abertos)]
    colors = ["#00d4aa", "#ff4b4b", "#ffa500"]

    fig_pie = go.Figure(go.Pie(
        labels=labels, values=values,
        marker_colors=colors,
        hole=0.5,
        textinfo="label+percent"
    ))
    fig_pie.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        height=300,
        margin=dict(l=0, r=0, t=10, b=0),
        showlegend=False
    )
    st.plotly_chart(fig_pie, use_container_width=True)

# ── Performance por Ativo ──
st.subheader("📊 Performance por Ativo")
if not df.empty:
    perf = df.groupby("ativo").agg(
        Trades=("id", "count"),
        Wins=("is_win", "sum"),
        Losses=("is_loss", "sum"),
        PnL=("pnl", "sum")
    ).reset_index()
    perf["Win Rate"] = (perf["Wins"] / (perf["Wins"] + perf["Losses"]) * 100).fillna(0).round(1)
    perf = perf.sort_values("PnL", ascending=False)

    fig_bar = px.bar(
        perf, x="ativo", y="PnL",
        color="PnL",
        color_continuous_scale=["#ff4b4b", "#ffa500", "#00d4aa"],
        text="PnL"
    )
    fig_bar.update_traces(texttemplate="$%{text:.0f}", textposition="outside")
    fig_bar.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=300,
        showlegend=False,
        margin=dict(l=0, r=0, t=10, b=0),
        coloraxis_showscale=False
    )
    st.plotly_chart(fig_bar, use_container_width=True)

# ── Tabela de Trades ──
st.subheader("📋 Histórico de Trades")

# Filtros
col_f1, col_f2, col_f3 = st.columns(3)
with col_f1:
    filtro_dir = st.selectbox("Direção", ["Todos", "LONG", "SHORT"])
with col_f2:
    filtro_res = st.selectbox("Resultado", ["Todos", "WIN", "LOSS", "ABERTO"])
with col_f3:
    filtro_ativo = st.text_input("Ativo (filtrar)", "")

df_filtrado = df.copy()
if filtro_dir != "Todos":
    df_filtrado = df_filtrado[df_filtrado["direcao"] == filtro_dir]
if filtro_res != "Todos":
    if filtro_res == "WIN":
        df_filtrado = df_filtrado[df_filtrado["resultado"].str.startswith("WIN", na=False)]
    elif filtro_res == "ABERTO":
        df_filtrado = df_filtrado[df_filtrado["resultado"].isna() | (df_filtrado["resultado"] == "ABERTO")]
    else:
        df_filtrado = df_filtrado[df_filtrado["resultado"] == filtro_res]
if filtro_ativo:
    df_filtrado = df_filtrado[df_filtrado["ativo"].str.contains(filtro_ativo.upper(), na=False)]

# Estilizar tabela
def colorir_resultado(val):
    if not val or val == "ABERTO":
        return "color: #ffa500"
    if str(val).startswith("WIN"):
        return "color: #00d4aa; font-weight: bold"
    if val == "LOSS":
        return "color: #ff4b4b; font-weight: bold"
    return ""

cols_show = ["id", "criado_em", "ativo", "direcao", "entrada", "stop", "a1", "a2", "a3", "resultado", "pnl"]
cols_existentes = [c for c in cols_show if c in df_filtrado.columns]
df_show = df_filtrado[cols_existentes].copy()
df_show.columns = ["#", "Data", "Ativo", "Dir", "Entrada", "Stop", "A1", "A2", "A3", "Resultado", "P&L"][:len(cols_existentes)]

st.dataframe(
    df_show.style.applymap(colorir_resultado, subset=["Resultado"] if "Resultado" in df_show.columns else []),
    use_container_width=True,
    height=400
)

# ── Rodapé ──
st.divider()
st.caption("🦈 LucSharkTrade v12 | Atualiza automaticamente a cada 30s | © 2026")

# Auto-refresh
st.markdown("""
<script>
setTimeout(function() { window.location.reload(); }, 30000);
</script>
""", unsafe_allow_html=True)
