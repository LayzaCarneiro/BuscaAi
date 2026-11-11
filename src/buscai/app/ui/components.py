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
from decimal import Decimal

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


# ---------- Tela: Resultados ----------
CURRENCY_SYMBOLS = {"BRL": "R$", "USD": "US$", "EUR": "€"}
ATTR_LABELS = {
    "brand": "Marca",
    "rating": "Avaliação",
    "stock": "Estoque",
    "warranty": "Garantia",
    "warrantyinformation": "Garantia",
    "shipping": "Frete",
    "shippinginformation": "Frete",
    "availabilitystatus": "Disponibilidade",
    "discountpercentage": "Desconto",
    "weight": "Peso",
    "sku": "SKU",
    "tags": "Tags",
}
HIDDEN_ATTRS = {"description", "thumbnail", "image", "images", "reviews", "dimensions", "meta", "id", "title"}
MAX_ATTRS = 6


def currency_symbol(code: str | None) -> str:
    code = (code or "").upper()
    return CURRENCY_SYMBOLS.get(code, code)


def format_money(value: Decimal | float | int | str, currency: str | None = None) -> str:
    """1234.5 -> 'R$ 1.234,50' (formato brasileiro)."""
    number = Decimal(str(value)).quantize(Decimal("0.01"))
    text = f"{number:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{currency_symbol(currency)} {text}".strip()


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _format_attr(key: str, value: object) -> str | None:
    """Valor de atributo em texto curto; None se não for algo simples de mostrar."""
    if value is None or value == "" or isinstance(value, dict):
        return None
    if isinstance(value, bool):
        return "Sim" if value else "Não"
    if isinstance(value, (list, tuple)):
        if not all(isinstance(v, (str, int, float)) for v in value):
            return None
        text = ", ".join(str(v) for v in value)
    elif isinstance(value, float):
        text = f"{value:.1f}".replace(".", ",") if key.lower() == "rating" else f"{value:g}".replace(".", ",")
        if key.lower() == "rating":
            text += " ★"
    else:
        text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _attr_label(key: str) -> str:
    return ATTR_LABELS.get(key.lower(), key.replace("_", " ").strip().capitalize())


def _thumbnail(attrs: dict) -> str:
    """Miniatura do produto: usa a URL do catálogo (se houver) ou um ícone."""
    url = attrs.get("thumbnail") or attrs.get("image")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return f'<img src="{html.escape(url, quote=True)}" alt="" loading="lazy">'
    return "🛒"


def product_row(product, index: int = 0) -> str:
    attrs = dict(getattr(product, "attributes", None) or {})
    items = []
    for key, value in attrs.items():
        if key.lower() in HIDDEN_ATTRS:
            continue
        shown = _format_attr(key, value)
        if shown is not None:
            items.append(f'<span class="attr"><b>{_text(_attr_label(key))}:</b> {_text(shown)}</span>')
        if len(items) >= MAX_ATTRS:
            break

    desc = attrs.get("description")
    desc_html = ""
    if isinstance(desc, str) and desc.strip():
        short = desc.strip() if len(desc.strip()) <= 160 else desc.strip()[:157] + "..."
        desc_html = f'<div class="prod-desc">{_text(short)}</div>'

    tags = []
    if product.category:
        tags.append(f'<span class="tag">{_text(str(product.category))}</span>')
    if product.source:
        tags.append(f'<span class="tag src">Fonte: {_text(str(product.source))}</span>')

    alt = " alt" if index % 2 else ""
    return _h(
        f"""
        <div class="prod{alt}">
        <div class="prod-img">{_thumbnail(attrs)}</div>
        <div class="prod-main">
        <div class="prod-title">{_text(product.title)}</div>
        <div class="prod-tags">{"".join(tags)}</div>
        {desc_html}
        <div class="prod-attrs">{"".join(items)}</div>
        </div>
        <div class="prod-price"><div class="price">{_text(format_money(product.price, product.currency))}</div>
        <small>Cód. {_text(str(product.id))}</small></div>
        </div>
        """
    )


def results_panel(
    page_products: list,
    *,
    count: int,
    loaded: int,
    total_matches: int,
    filtered: bool,
    first_index: int = 0,
) -> str:
    """Cabeçalho da janela + lista rolável com os produtos da página atual."""
    header = (
        f'<div class="win-h"><span>Resultados da busca</span>'
        f'<span class="win-meta">{_plural(count, "produto", "produtos")}</span></div>'
    )
    note = ""
    if total_matches > loaded:
        note = (
            f'<div class="res-note">O catálogo encontrou {total_matches} produtos; '
            f"os {loaded} primeiros foram carregados.</div>"
        )
    if page_products:
        body = "".join(product_row(p, first_index + i) for i, p in enumerate(page_products))
    elif filtered:
        body = (
            '<div class="res-empty">Nenhum produto nessa faixa de preço.<br>'
            "Ajuste ou limpe os filtros ao lado.</div>"
        )
    else:
        body = '<div class="res-empty">Nenhum produto encontrado para essa busca.</div>'
    return _h(f'{header}<div class="res-scroll">{note}{body}</div>')


def page_info(start: int, end: int, count: int) -> str:
    text = "Nenhum produto" if count == 0 else f"Exibindo {start}–{end} de {_plural(count, 'produto', 'produtos')}"
    return f'<div class="page-info">{text}</div>'


def _price_phrase(criteria, currency: str | None) -> str | None:
    lo, hi = getattr(criteria, "min_price", None), getattr(criteria, "max_price", None)
    if lo is None and hi is None:
        return None
    if lo is not None and hi is not None:
        text = f"entre {format_money(lo, currency)} e {format_money(hi, currency)}"
    elif hi is not None:
        prefix = "até" if getattr(criteria, "max_inclusive", True) else "abaixo de"
        text = f"{prefix} {format_money(hi, currency)}"
    else:
        prefix = "a partir de" if getattr(criteria, "min_inclusive", True) else "acima de"
        text = f"{prefix} {format_money(lo, currency)}"
    return text if getattr(criteria, "price_is_hard", True) else f"{text} (flexível)"


def criteria_summary(criteria, unapplied: tuple[str, ...] | list[str], currency: str | None) -> str:
    """O que o agente entendeu do pedido (vem de IntentSearchCriteria)."""
    rows = [
        ("Busca", criteria.query),
        ("Categoria", criteria.category),
        ("Preço", _price_phrase(criteria, currency)),
        ("Marcas", ", ".join(criteria.brands)),
        ("Sem as marcas", ", ".join(criteria.excluded_brands)),
        ("Uso", ", ".join(criteria.use_cases)),
        ("Perfil", ", ".join(criteria.profile_context)),
        ("Exige", ", ".join(criteria.required_features)),
        ("Prefere", ", ".join(criteria.preferred_features)),
        ("Sem", ", ".join(criteria.excluded_features)),
    ]
    body = "".join(
        f'<div class="crit-row"><b>{label}</b>{_text(str(value))}</div>' for label, value in rows if value
    )
    warn = ""
    if unapplied:
        warn = f'<div class="crit-warn"><b>Ainda não aplicado:</b> {_text(", ".join(unapplied))}</div>'
    return _h(f'<div class="crit"><div class="crit-h">Sua busca</div><div class="crit-body">{body}{warn}</div></div>')
