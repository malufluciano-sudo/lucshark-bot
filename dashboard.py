import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import requests
import os
from datetime import datetime

st.set_page_config(
    page_title="LucSharkTrade Dashboard",
    page_icon="🦈",
    layout="wide"
)

st.markdown("""
<style>
    [data-testid="stAppViewContainer"] { background-color: #0e1117; }
    .metric-box {
        background: #1e2130; border-radius: 12px;
        padding: 16px; border: 1px solid #2d3250;
        text-align: center;
    }
</style>
""", unsafe_allow_html=True)

# URL do bot (carefree-embrace)
BOT_URL = os.environ.get("BOT_API_URL", "https://carefree-embrace-production.up.railway.app")

def calcular_pnl(resultado, risco=20):
    if not resultado: return 0
    if "A3" in resultado: return risco * 3
    if "A2" in resultado: return risco * 2
    if "WIN" in resultado: return risco
    if resultado == "LOSS": return -risco
    return 0

@st.cache_data(ttl=30)
def carregar_dados():
    try:
        r = requests.get(f"{BOT_URL}/api/trades", timeout=10)
        if r.status_code == 200:
            trades = r.json().get("trades", [])
            return pd.DataFrame(trades)
    except Exception as e:
        st.error(f"Erro ao conectar ao bot: {e}")
    return pd.DataFrame()

@st.cache_data(ttl=30)
def carregar_stats():
    try:
        r = requests.get(f"{BOT_URL}/api/stats", timeout=10)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return {}

# ── Header ──
col_logo, col_title, col_time = st.columns([1, 6, 2])
with col_logo:
    st.markdown("## 🦈")
with col_title:
    st.markdown("## LucSharkTrade Dashboard")
with col_time:
    st.markdown(f"**Atualizado:** {datetime.now().strftime('%H:%M:%S')} BRT")

st.divider()

# ── Carregar dados ──
df    = carregar_dados()
stats = carregar_stats()

if df.empty or not stats:
    st.warning("⏳ Aguardando dados do bot... Verifique se o bot está online.")
    if st.button("🔄 Tentar novamente"):
        st.cache_data.clear()
        st.rerun()
    st.stop()

df["pnl"]     = df["resultado"].apply(calcular_pnl)
df["is_win"]  = df["resultado"].apply(lambda x: bool(x and x.startswith("WIN")))
df["is_loss"] = df["resultado"].apply(lambda x: x == "LOSS")

# ── Cards de métricas ──
c1, c2, c3, c4, c5, c6 = st.columns(6)
with c1: st.metric("📊 Total", stats.get("total", 0))
with c2: st.metric("✅ Wins", stats.get("wins", 0))
with c3: st.metric("❌ Losses", stats.get("losses", 0))
with c4: st.metric("🔄 Abertos", stats.get("abertos", 0))
with c5: st.metric("🎯 Win Rate", f"{stats.get('win_rate', 0):.1f}%")
with c6:
    pnl = stats.get("pnl", 0)
    cap = stats.get("capital", 1000)
    st.metric("💰 P&L", f"${pnl:+.2f}", f"Capital: ${cap:,.2f}")

st.divider()

col_left, col_right = st.columns([3, 2])

with col_left:
    st.subheader("📈 Curva de Capital")
    df_s = df.sort_values("id")
    df_s["pnl_cum"] = df_s["pnl"].cumsum()
    df_s["capital"] = 1000 + df_s["pnl_cum"]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=list(range(len(df_s))), y=df_s["capital"],
        fill="tozeroy", fillcolor="rgba(0,212,170,0.1)",
        line=dict(color="#00d4aa", width=2)
    ))
    fig.add_hline(y=1000, line_dash="dash", line_color="gray", opacity=0.5)
    fig.update_layout(
        template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)", height=280,
        showlegend=False, margin=dict(l=0,r=0,t=10,b=0),
        yaxis=dict(tickprefix="$")
    )
    st.plotly_chart(fig, use_container_width=True)

with col_right:
    st.subheader("🥧 Resultados")
    wins    = stats.get("wins", 0)
    losses  = stats.get("losses", 0)
    abertos = stats.get("abertos", 0)
    fig2 = go.Figure(go.Pie(
        labels=["Wins","Losses","Abertos"],
        values=[wins, losses, abertos],
        marker_colors=["#00d4aa","#ff4b4b","#ffa500"],
        hole=0.5, textinfo="label+percent"
    ))
    fig2.update_layout(
        template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
        height=280, margin=dict(l=0,r=0,t=10,b=0), showlegend=False
    )
    st.plotly_chart(fig2, use_container_width=True)

# ── P&L por ativo ──
st.subheader("📊 P&L por Ativo")
perf = df.groupby("ativo").agg(
    Trades=("id","count"), Wins=("is_win","sum"),
    Losses=("is_loss","sum"), PnL=("pnl","sum")
).reset_index().sort_values("PnL", ascending=False)

fig3 = px.bar(
    perf, x="ativo", y="PnL",
    color="PnL", color_continuous_scale=["#ff4b4b","#ffa500","#00d4aa"],
    text="PnL"
)
fig3.update_traces(texttemplate="$%{text:.0f}", textposition="outside")
fig3.update_layout(
    template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)", height=280,
    showlegend=False, coloraxis_showscale=False,
    margin=dict(l=0,r=0,t=10,b=0)
)
st.plotly_chart(fig3, use_container_width=True)

# ── Tabela ──
st.subheader("📋 Histórico de Trades")
col_f1, col_f2, col_f3 = st.columns(3)
with col_f1: filtro_dir = st.selectbox("Direção", ["Todos","LONG","SHORT"])
with col_f2: filtro_res = st.selectbox("Resultado", ["Todos","WIN","LOSS","ABERTO"])
with col_f3: filtro_ativo = st.text_input("Buscar ativo", "")

df_f = df.copy()
if filtro_dir != "Todos": df_f = df_f[df_f["direcao"] == filtro_dir]
if filtro_res == "WIN":   df_f = df_f[df_f["resultado"].str.startswith("WIN", na=False)]
elif filtro_res == "LOSS": df_f = df_f[df_f["resultado"] == "LOSS"]
elif filtro_res == "ABERTO": df_f = df_f[df_f["resultado"].isna() | (df_f["resultado"] == "ABERTO")]
if filtro_ativo: df_f = df_f[df_f["ativo"].str.contains(filtro_ativo.upper(), na=False)]

cols = ["id","criado_em","ativo","direcao","entrada","stop","a1","a2","a3","resultado","pnl"]
cols_ex = [c for c in cols if c in df_f.columns]
df_show = df_f[cols_ex].copy()

def color_res(val):
    if not val or val == "ABERTO": return "color:#ffa500"
    if str(val).startswith("WIN"): return "color:#00d4aa;font-weight:bold"
    if val == "LOSS": return "color:#ff4b4b;font-weight:bold"
    return ""

st.dataframe(
    df_show.style.applymap(color_res, subset=["resultado"] if "resultado" in df_show.columns else []),
    use_container_width=True, height=400
)

st.divider()
st.caption("🦈 LucSharkTrade v12 | Auto-refresh a cada 30s")
st.markdown('<meta http-equiv="refresh" content="30">', unsafe_allow_html=True)
