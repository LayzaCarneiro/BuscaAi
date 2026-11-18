"""BuscaAI - Telas Home, Busca e Resultados.

Rodar:  streamlit run app.py
"""
from __future__ import annotations

import math
import re
import time
from decimal import Decimal, InvalidOperation

import streamlit as st

from service.agent_gateway import ask_agent
from ui import components as c
from ui.styles import GLOBAL_CSS


REDIRECT_MESSAGE = "Preparando resultados da busca ..."
REDIRECT_DELAY_SECONDS = 1.5
PAGE_SIZE = 6  # produtos por página na tela de resultados
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
    ss.setdefault("search_result", None)      # SearchFlowResult (ou IntentSearchResult) devolvido pelo agente
    ss.setdefault("filters", {"min": None, "max": None})  # filtro de preço (Decimal ou None)
    ss.setdefault("filter_error", None)
    ss.setdefault("filter_nonce", 0)          # muda para esvaziar os campos de filtro
    ss.setdefault("result_page", 1)


def reset_conversation() -> None:
    ss = st.session_state
    ss.page = "busca"
    ss.messages = []
    ss.awaiting_agent = False
    ss.status = "idle"
    ss.search_result = None
    ss.filters = {"min": None, "max": None}
    ss.filter_error = None
    ss.filter_nonce += 1
    ss.result_page = 1


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


# ---------- Tela: Resultados ----------
def search_from(payload):
    """Extrai o IntentSearchResult de um SearchFlowResult (ou aceita o próprio)."""
    search = getattr(payload, "search", payload)
    return search if hasattr(search, "results") else None


def parse_price(text: str) -> Decimal | None:
    """'1500', '1.500,50', 'R$ 1.500' -> Decimal. Vazio -> None. Inválido -> ValueError."""
    value = text.strip().replace("R$", "").replace(" ", "")
    if not value:
        return None
    if "," in value:
        value = value.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", value):
        value = value.replace(".", "")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(value) from exc
    if not number.is_finite() or number < 0:
        raise ValueError(value)
    return number


def apply_price_filter(products: list, flt: dict) -> list:
    lo, hi = flt.get("min"), flt.get("max")
    return [p for p in products if (lo is None or p.price >= lo) and (hi is None or p.price <= hi)]


def go_page(number: int) -> None:
    st.session_state.result_page = number


def render_filters(search, currency: str | None) -> None:
    ss = st.session_state
    symbol = c.currency_symbol(currency)
    suffix = f" ({symbol})" if symbol else ""

    with st.container(key="painel_filtros"):
        md(c.window_header("Filtrar resultados"))
        with st.form("form_filtros"):
            lo_text = st.text_input(f"Preço mínimo{suffix}", placeholder="Ex.: 1.500,00", key=f"f_min_{ss.filter_nonce}")
            hi_text = st.text_input(f"Preço máximo{suffix}", placeholder="Ex.: 3.000,00", key=f"f_max_{ss.filter_nonce}")
            col_apply, col_clear = st.columns(2)
            apply = col_apply.form_submit_button("Filtrar", use_container_width=True)
            clear = col_clear.form_submit_button("Limpar", use_container_width=True)

        if clear:
            ss.filters = {"min": None, "max": None}
            ss.filter_error = None
            ss.filter_nonce += 1
            ss.result_page = 1
            st.rerun()
        if apply:
            try:
                lo, hi = parse_price(lo_text), parse_price(hi_text)
            except ValueError:
                ss.filter_error = "Use apenas números, como 1500 ou 1.500,00."
            else:
                if lo is not None and hi is not None and lo > hi:
                    ss.filter_error = "O preço mínimo é maior que o máximo."
                else:
                    ss.filters = {"min": lo, "max": hi}
                    ss.filter_error = None
                    ss.result_page = 1
        if ss.filter_error:
            md(f'<div class="form-error">{ss.filter_error}</div>')

        if st.button("« Nova busca", key="nova_busca", use_container_width=True):
            reset_conversation()
            st.rerun()
        md(c.criteria_summary(search.criteria, search.unapplied_preferences, currency))


def render_pagination(page: int, total_pages: int, start: int, end: int, count: int) -> None:
    first = max(1, min(page - 2, total_pages - 4))
    numbers = list(range(first, min(total_pages, first + 4) + 1))

    with st.container(key="paginacao"):
        cols = st.columns([3.2, 2] + [1] * len(numbers) + [2], vertical_alignment="center", gap="small")
        with cols[0]:
            md(c.page_info(start, end, count))
        cols[1].button("« Anterior", key="pg_prev", disabled=page <= 1,
                       on_click=go_page, args=(page - 1,), use_container_width=True)
        for col, number in zip(cols[2:], numbers):
            col.button(str(number), key="pg_atual" if number == page else f"pg_{number}",
                       disabled=number == page, on_click=go_page, args=(number,), use_container_width=True)
        cols[-1].button("Próxima »", key="pg_next", disabled=page >= total_pages,
                        on_click=go_page, args=(page + 1,), use_container_width=True)


def render_results_window(search, filtered: list) -> None:
    ss = st.session_state
    count = len(filtered)
    total_pages = max(1, math.ceil(count / PAGE_SIZE))
    page = min(max(1, ss.result_page), total_pages)
    ss.result_page = page

    offset = (page - 1) * PAGE_SIZE
    chunk = filtered[offset:offset + PAGE_SIZE]
    page_data = search.results

    with st.container(key="janela_resultados"):
        md(c.results_panel(
            chunk,
            count=count,
            loaded=len(page_data.products),
            total_matches=page_data.total_matches,
            filtered=any(v is not None for v in ss.filters.values()),
            first_index=offset,
        ))
        render_pagination(page, total_pages, offset + 1, offset + len(chunk), count)


def render_results() -> None:
    search = search_from(st.session_state.search_result)

    md(c.header())
    if search is None:  # abriram a tela sem ter feito uma busca
        md(c.placeholder_box("Nenhuma busca", "Faça uma busca para ver os resultados."))
        if st.button("Fazer uma busca"):
            reset_conversation()
            st.rerun()
        md(c.footer())
        return

    products = list(search.results.products)
    currencies = {p.currency for p in products if p.currency}
    currency = currencies.pop() if len(currencies) == 1 else None

    left, right = st.columns([1, 3.6], gap="medium")
    with left:  # primeiro: o envio do filtro atualiza session_state antes de a lista ser desenhada
        render_filters(search, currency)
    with right:
        render_results_window(search, apply_price_filter(products, st.session_state.filters))
    md(c.footer())


# ---------- Roteamento ----------
init_state()
md(f"<style>{GLOBAL_CSS}</style>")

PAGES = {
    "home": render_home,
    "busca": render_search,
    "resultados": render_results,
}
PAGES[st.session_state.page]()
