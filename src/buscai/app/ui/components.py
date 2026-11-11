"""Blocos HTML reutilizáveis (cada função devolve uma string para st.markdown).

O HTML é compactado por `_h` porque o Markdown do Streamlit trata linhas
indentadas ou em branco como código/parágrafo e quebra o layout.

Navegação: os links usam query params (`?page=busca`) com `target="_self"`.
Isso recarrega o app, então só serve para entrar em uma tela; dentro de uma
tela a troca é feita por `st.session_state.page`.
"""
from __future__ import annotations

import html
import math

CART_SVG = (
    '<svg viewBox="0 0 64 48" width="{w}" height="{h}" shape-rendering="crispEdges" aria-hidden="true">'
    '<rect x="0" y="10" width="14" height="3" fill="#4466FF"/>'
    '<rect x="4" y="17" width="12" height="3" fill="#F5C400"/>'
    '<rect x="2" y="24" width="10" height="3" fill="#4466FF"/>'
    '<path d="M16 4H25L31 30H56" fill="none" stroke="#00008B" stroke-width="3"/>'
    '<polygon points="28,10 60,10 56,30 31,30" fill="#DFE3FF" stroke="#00008B" stroke-width="3"/>'
    '<path d="M37 10V30M45 10V30M53 10V30M30 18H59M31 24H57" stroke="#00008B" stroke-width="1.5"/>'
    '<circle cx="36" cy="40" r="4" fill="#00008B"/><circle cx="52" cy="40" r="4" fill="#00008B"/>'
    "</svg>"
)

HOME_HREF = "?page=home"
SEARCH_HREF = "?page=busca"

CATEGORIES = [
    ("💻", "Informática"),
    ("📺", "Eletrônicos"),
    ("📖", "Livros"),
    ("🏠", "Casa &amp; Decoração"),
    ("⚽", "Esportes"),
    ("👕", "Moda"),
    ("🎮", "Games"),
]
BENEFITS = [
    "Recomendações personalizadas",
    "Comparação inteligente",
    "Melhores preços",
    "Compra segura",
]
NAV = [
    ("Home", HOME_HREF),
    ("Como Funciona", "#"),
    ("Categorias", "#"),
    ("Recomendações", "#"),
    ("Contato", "#"),
    ("Ajuda", "#"),
]
QUICK_LINKS = [
    ("🔍", "Busque", SEARCH_HREF),
    ("📖", "Compare", "#"),
    ("⭐", "Recomende", "#"),
    ("📊", "Economize", "#"),
]
BUSCAAI = "BuscaAI"
BUSCAAI_ALL_CAPS = "BUSCAAI"


def _h(markup: str) -> str:
    return "".join(line.strip() for line in markup.splitlines())


def _burst(spikes: int, r_out: float = 50.0, r_in: float = 41.0) -> str:
    pts = []
    for i in range(spikes * 2):
        ang = math.pi * i / spikes - math.pi / 2
        r = r_out if i % 2 == 0 else r_in
        pts.append(f"{50 + r * math.cos(ang):.1f}% {50 + r * math.sin(ang):.1f}%")
    return "polygon(" + ",".join(pts) + ")"


def _text(value: str) -> str:
    """Texto do usuário/agente para dentro do HTML (escapa e preserva quebras)."""
    safe = html.escape(value, quote=True).replace("$", "&#36;")
    return safe.replace("\n", "<br>")


def _link(label: str, href: str) -> str:
    return f'<a href="{href}" target="_self">{label}</a>'


# ---------- Estrutura da página (comum a todas as telas) ----------
def title_bar() -> str:
    return _h(f'<div class="bleed titlebar">🛒<span>{BUSCAAI} - Guia de Compras Inteligente</span></div>')


def site_header() -> str:
    nav = "".join(_link(label, href) for label, href in NAV)
    return _h(
        f"""
        <div class="bw">
        <div class="site-head">
        <div class="brand">{CART_SVG.format(w=62, h=46)}
        <div><div class="brand-name">{BUSCAAI_ALL_CAPS}</div><div class="brand-tag">Sua busca termina aqui!</div></div></div>
        <nav class="site-nav">{nav}</nav>
        <div class="seal"><span style="font-size:26px">🔒</span><div><b>COMPRA SEGURA</b>Seus dados protegidos</div></div>
        </div>
        <div class="rule"></div>
        </div>
        """
    )


def header() -> str:
    """Barra de título + cabeçalho do site (colados no topo da página)."""
    return title_bar() + site_header()


def rule() -> str:
    return '<div class="rule"></div>'


def footer_badges() -> str:
    return _h(
        """
        <div class="bw badges">
        <div class="badge"><span style="font-size:30px">🛡️</span><div>SITE BLINDADO<small>AUDITADO 24 HORAS</small></div></div>
        <div class="badge cards"><span>ACEITAMOS</span>
        <span><span class="chip visa">VISA</span><span class="chip mc">MasterCard</span><span class="chip amex">AMEX</span></span></div>
        <div class="badge"><span style="font-size:30px">🚚</span><div>ENTREGA PARA TODO BRASIL<small>Rápido e seguro!</small></div></div>
        <div class="badge"><span style="font-size:30px">🧑‍💼</span><div>ATENDIMENTO ONLINE<small><a href="#">Dúvidas? Clique aqui!</a></small></div></div>
        </div>
        """
    )


def footer_bar() -> str:
    sep = '<span class="sep">|</span>'
    return _h(
        '<div class="bleed footbar">'
        f"{BUSCAAI} © 2001 - Todos os direitos reservados{sep}Política de Privacidade"
        f"{sep}Termos de Uso{sep}Trabalhe Conosco</div>"
    )


def footer() -> str:
    """Selos + barra azul. A classe `footer-block` faz o CSS empurrá-lo para a base."""
    return _h(f'<div class="footer-block">{rule()}{footer_badges()}{footer_bar()}</div>')


# ---------- Tela: Home ----------
def categories_box() -> str:
    items = "".join(
        f'<li><span class="ico">{icon}</span>{_link(name, "#")}</li>' for icon, name in CATEGORIES
    )
    return _h(
        f"""
        <div class="bw box fill">
        <div class="box-h">Categorias</div>
        <ul>{items}<li>&raquo;&nbsp;{_link("Ver todas", "#")}</li></ul>
        </div>
        """
    )


def benefits_box() -> str:
    items = "".join(f'<li><span class="tick">✔</span><span>{b}</span></li>' for b in BENEFITS)
    return _h(
        f"""
        <div class="box benefits">
        <div class="box-h">Por que usar o {BUSCAAI}?</div>
        <ul>{items}</ul>
        </div>
        """
    )


def promo_burst() -> str:
    clip = _burst(14)
    return _h(
        f"""
        <div class="burst-wrap"><div class="burst" style="clip-path:{clip}">
        <div class="burst-in" style="clip-path:{clip}">PROMOÇÕES<br>TODOS OS DIAS!</div></div></div>
        """
    )


def hero() -> str:
    cart = CART_SVG.format(w=52, h=38)
    return _h(
        f"""
        <div class="hero">
        <div class="hero-title"><span>{BUSCAAI}</span>{cart}</div>
        <div class="hero-sub">Guia de Compras Inteligente<br>Sistema Semântico de Recomendação</div>
        </div>
        """
    )


def cta_button() -> str:
    return _h(f'<div class="bw cta-wrap"><a class="btn" href="{SEARCH_HREF}" target="_self">🔍 Buscar produtos</a></div>')


def quick_links() -> str:
    links = "".join(
        f'<span><span class="ico">{icon}</span>{_link(label, href)}</span>' for icon, label, href in QUICK_LINKS
    )
    return _h(f'<div class="bw quick">{links}</div>')


def novo_strip() -> str:
    clip = _burst(9, 50, 38)
    return _h(
        f"""
        <div class="bw strip"><div class="novo" style="clip-path:{clip}">NOVO!</div>
        <b>Agora com recomendações ainda mais inteligentes!</b> &nbsp; <a href="#">Saiba mais</a></div>
        """
    )


def home_body() -> str:
    """Corpo da home: 3 colunas que se esticam até o footer."""
    return _h(
        f"""
        <div class="home-grid">
        <div class="col-left">{categories_box()}</div>
        <div class="stage">{hero()}{quick_links()}{novo_strip()}</div>
        <div class="col-right">{benefits_box()}{promo_burst()}</div>
        </div>
        """
    )


# ---------- Tela: Busca ----------
def window_header(title: str) -> str:
    return f'<div class="win-h">{_text(title)}</div>'


def chat_log(messages: list[dict], typing: bool = False) -> str:
    rows = []
    for m in messages:
        role = "user" if m["role"] == "user" else "agent"
        who = "Você" if role == "user" else BUSCAAI
        rows.append(
            f'<div class="msg {role}"><div class="bubble"><span class="who">{who}</span>{_text(m["text"])}</div></div>'
        )
    if typing:
        rows.append(
            f'<div class="msg agent"><div class="bubble"><span class="who">{BUSCAAI}</span>'
            'Analisando seu pedido<span class="dots"><i></i><i></i><i></i></span></div></div>'
        )
    if rows:
        inner = f'<div class="chat-inner">{"".join(rows)}</div>'
    else:
        inner = (
            '<div class="chat-empty">Conte em poucas palavras o que você procura: '
            "o produto, para que vai usar e quanto quer gastar.</div>"
        )
    return _h(f'<div class="chat-scroll">{inner}</div>')


# ---------- Provisório ----------
def placeholder_box(title: str, text: str) -> str:
    return _h(
        f"""
        <div class="page-fill"><div class="box" style="width:100%;max-width:520px">
        <div class="box-h">{_text(title)}</div>
        <div style="padding:16px;font-size:15px">{_text(text)}</div>
        </div></div>
        """
    )
