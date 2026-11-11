"""BuscaAI - Telas Home, Busca e Resultados (provisória).

Rodar:  streamlit run app.py
"""
from __future__ import annotations

import time

import streamlit as st

from buscai.agent import run_search_flow
from service.agent_gateway import ask_agent
from ui import components as c
from ui.styles import GLOBAL_CSS


REDIRECT_MESSAGE = "Preparando resultados da busca ..."
REDIRECT_DELAY_SECONDS = 1.5
ENTRY_PAGES = ("home", "busca")  # telas que podem ser abertas por link (?page=...)

st.set_page_config(
    page_title="BuscAI - Guia de Compras Inteligente",
    page_icon="🛒",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# ---------- Estado ----------
def init_state() -> None:
    ss = st.session_state
    requested = st.query_params.get("page", "home")
    ss.setdefault("page", requested if requested in ENTRY_PAGES else "home")  # "home" | "busca" | "resultados"
    ss.setdefault("messages", [])             # [{"role": "user" | "agent", "text": str}]
    ss.setdefault("awaiting_agent", False)    # True enquanto o agente processa
    ss.setdefault("status", "idle")           # "idle" | "redirecting"
    ss.setdefault("search_result", None)      # IntentSearchResult devolvido pelo agente


def reset_conversation() -> None:
    ss = st.session_state
    ss.page = "busca"
    ss.messages = []
    ss.awaiting_agent = False
    ss.status = "idle"
    ss.search_result = None


def md(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


# ---------- Tela: Home ----------
def render_home() -> None:
    md(c.header())
    md(c.home_body())
    md(c.footer())


# ---------- Tela: Busca ----------
def render_assistant() -> None:
    ss = st.session_state
    busy = ss.awaiting_agent or ss.status == "redirecting"

    with st.container(key="janela_assistente"):
        md(c.window_header("Conversa com o Assistente"))
        md(c.chat_log(ss.messages, typing=ss.awaiting_agent))

        with st.form("form_busca", clear_on_submit=True):
            col_input, col_button = st.columns([5, 1.3], vertical_alignment="center")
            text = col_input.text_input(
                "Mensagem",
                placeholder="Ex.: notebook até R$ 4.000 para programar e jogar casualmente",
                label_visibility="collapsed",
                disabled=busy,
            )
            sent = col_button.form_submit_button("Enviar", use_container_width=True, disabled=busy)

    if sent and text.strip():
        ss.messages.append({"role": "user", "text": text.strip()})
        ss.awaiting_agent = True
        st.rerun()


def process_pending() -> None:
    """Roda depois de a tela ser desenhada, para o usuário ver o estado "analisando"."""
    ss = st.session_state

    if ss.awaiting_agent:
        user_texts = [m["text"] for m in ss.messages if m["role"] == "user"]
        reply = ask_agent(user_texts)
        ss.awaiting_agent = False
        if reply.kind == "results":
            ss.search_result = reply.payload
            ss.messages.append({"role": "agent", "text": REDIRECT_MESSAGE})
            ss.status = "redirecting"
        else:  # "clarification" ou "error": o agente responde no chat
            ss.messages.append({"role": "agent", "text": reply.text})
        st.rerun()

    elif ss.status == "redirecting":
        time.sleep(REDIRECT_DELAY_SECONDS)
        ss.page = "resultados"
        ss.status = "idle"
        st.rerun()


def render_search() -> None:
    md(c.header())
    render_assistant()
    md(c.footer())
    process_pending()


# ---------- Tela: Resultados (provisória) ----------
def render_results_placeholder() -> None:
    # TODO: substituir pela tela de resultados. Os dados estão em st.session_state.search_result.
    md(c.header())
    md(c.placeholder_box("Resultados da busca", "Esta tela ainda será implementada."))
    if st.button("Nova busca"):
        reset_conversation()
        st.rerun()
    md(c.footer())


# ---------- Roteamento ----------
init_state()
md(f"<style>{GLOBAL_CSS}</style>")

PAGES = {
    "home": render_home,
    "busca": render_search,
    "resultados": render_results_placeholder,
}
PAGES[st.session_state.page]()
