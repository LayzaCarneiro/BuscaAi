"""CSS global do BuscaAI (identidade Web 1.0).

Tudo que é visual fica aqui: paleta, tipografia, caixas, balões do chat e os
overrides necessários para o Streamlit não parecer um app moderno.

Layout "tela cheia": header e footer têm altura fixa e somam `--chrome`. O corpo
de cada tela ocupa `100vh - --chrome`, e o footer é empurrado para a base.
"""

GLOBAL_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Press+Start+2P&display=swap');

:root {
  --bg: #E8E6DA;
  --navy: #00008B;
  --blue: #0000CC;
  --note: #F6F7BE;
  --ok: #1B8A1B;
  --warn: #D40000;
  --shadow: #8C8C80;
  --pixel: 'Press Start 2P', 'Courier New', monospace;
  --body: Tahoma, Verdana, Geneva, sans-serif;
  /* title bar 36 + header 102 + footer 132 */
  --chrome: 270px;
}

/* ---------- Streamlit: remove o "chrome" moderno ---------- */
[data-testid="stHeader"], [data-testid="stToolbar"], [data-testid="stDecoration"],
[data-testid="stStatusWidget"], [data-testid="stSidebar"], [data-testid="collapsedControl"],
[data-testid="stSidebarCollapsedControl"], #MainMenu, footer {
  display: none !important;
}
div[data-testid="stElementContainer"]:has(> style),
div.element-container:has(> style) { display: none !important; }

html { color-scheme: light; }
html, body, .stApp, [data-testid="stAppViewContainer"] {
  background: var(--bg) !important;
  font-family: var(--body);
  color: #000;
}
[data-testid="stMain"], section.main { overflow-x: hidden; }
[data-testid="stMainBlockContainer"], .block-container {
  max-width: 1240px !important;
  padding: 0 24px 0 !important;
}
[data-testid="stVerticalBlock"] { gap: 0.5rem; }

/* O Streamlit aplica margem negativa embaixo dos blocos de markdown, o que faz
   um bloco sobrepor o seguinte. Zeramos para o layout bater com as contas. */
[data-testid="stElementContainer"], div.element-container,
[data-testid="stMarkdown"], [data-testid="stMarkdownContainer"] {
  margin-bottom: 0 !important;
}
.st-key-janela_assistente > [data-testid="stElementContainer"],
.st-key-janela_assistente > div.element-container { flex: none; }

/* Bloco principal da página: ocupa a altura toda, sem espaçamento entre
   header / corpo / footer, e o footer é empurrado para a base. */
[data-testid="stVerticalBlock"]:has(> [data-testid="stElementContainer"] .footer-block),
[data-testid="stVerticalBlock"]:has(> div.element-container .footer-block) {
  min-height: 100vh;
  gap: 0;
}
[data-testid="stElementContainer"]:has(.footer-block),
div.element-container:has(.footer-block) { margin-top: auto; }

/* ---------- Estrutura da página ---------- */
.bleed { width: 100vw; margin-left: calc(50% - 50vw); }
.rule { border-top: 2px solid #000; margin: 0; }

.titlebar {
  background: var(--navy); color: #fff; font-family: var(--pixel); font-size: 12px;
  height: 36px; box-sizing: border-box;
  padding: 0 16px; display: flex; align-items: center; gap: 10px;
  border-bottom: 2px solid #6b6bd0;
}

.site-head {
  display: flex; align-items: center; justify-content: space-between;
  gap: 16px; height: 100px; box-sizing: border-box;
}
.brand { display: flex; align-items: center; gap: 10px; }
.brand-name { font-family: var(--pixel); color: var(--blue); font-size: 22px; line-height: 1.1; }
.brand-tag { font-size: 14px; margin-top: 4px; }
.site-nav { display: flex; gap: 30px; margin: 0 auto; align-self: flex-end; padding-bottom: 12px; font-size: 15px; flex-wrap: wrap; }
.seal {
  border: 1px solid #000; background: var(--bg); padding: 8px 14px;
  display: flex; align-items: center; gap: 10px; font-size: 12px;
}
.seal b { color: var(--ok); font-size: 14px; display: block; }

.bw a, .bw a:visited { color: var(--blue) !important; text-decoration: underline !important; }

/* ---------- Tela home: grade de 3 colunas que preenche a altura ---------- */
.home-grid {
  display: grid; grid-template-columns: 1.05fr 2.9fr 1.05fr; gap: 24px;
  min-height: calc(100vh - var(--chrome)); box-sizing: border-box; padding: 18px 0;
}
.col-left, .col-right { display: flex; flex-direction: column; gap: 18px; }
.stage {
  display: flex; flex-direction: column; justify-content: center; align-items: stretch;
  gap: clamp(20px, 5vh, 56px);
}

/* ---------- Caixas laterais ---------- */
.box { border: 1px solid #000; background: var(--bg); }
.box-h {
  background: var(--navy); color: #fff; font-weight: 700; text-align: center;
  padding: 7px 8px; font-size: clamp(15px, 2vh, 19px);
}
.box ul { list-style: none; margin: 0; padding: 14px 14px; }
.box li { display: flex; gap: 10px; align-items: center; font-size: clamp(15px, 2.1vh, 19px); line-height: 1.3; }
.box li .ico { width: 28px; text-align: center; font-size: clamp(20px, 3vh, 28px); flex: none; }
.box li .tick { color: var(--ok); font-size: 17px; flex: none; }
.box.fill { flex: 1; display: flex; flex-direction: column; }
.box.fill ul { flex: 1; display: flex; flex-direction: column; justify-content: space-evenly; }
.box.benefits ul { display: flex; flex-direction: column; gap: clamp(12px, 2.4vh, 24px); }

.burst-wrap { flex: 1; display: flex; justify-content: center; align-items: center; }
.burst {
  width: min(100%, 250px); aspect-ratio: 1.6; background: #E58A00;
  display: flex; align-items: center; justify-content: center;
}
.burst-in {
  width: calc(100% - 8px); height: calc(100% - 8px); background: #FFD91A;
  display: flex; align-items: center; justify-content: center; text-align: center;
  color: var(--warn); font-weight: 700; font-size: clamp(14px, 1.8vw, 19px); line-height: 1.25; padding: 0 22px;
}

/* ---------- Centro da home ---------- */
.hero { text-align: center; }
.hero-title {
  font-family: var(--pixel); color: var(--blue); font-size: clamp(40px, min(5.6vw, 11vh), 80px); line-height: 1;
  text-shadow: 4px 4px 0 #B9BDF0; display: flex; justify-content: center; align-items: flex-end; gap: 12px;
}
.hero-title svg { width: clamp(48px, 5vw, 76px); height: auto; }
.hero-sub { font-weight: 700; font-size: clamp(17px, 3vh, 26px); letter-spacing: .6px; line-height: 1.5; margin-top: clamp(14px, 3vh, 30px); }

.cta-wrap { display: flex; justify-content: center; }
.bw a.btn, .bw a.btn:visited {
  display: inline-block; background: #0000A8; color: #fff !important; text-decoration: none !important;
  font-weight: 700; font-size: clamp(17px, 2.6vh, 22px); padding: 14px 40px;
  border: 2px solid; border-color: #8A8AFF #000040 #000040 #8A8AFF;
}
.bw a.btn:hover { background: #0000C8; }
.bw a.btn:active { border-color: #000040 #8A8AFF #8A8AFF #000040; }

.quick { display: flex; justify-content: center; gap: 30px; flex-wrap: wrap; font-size: clamp(16px, 2.3vh, 20px); }
.quick .ico { font-size: 22px; margin-right: 4px; }

.strip {
  position: relative; background: var(--note); border: 1px solid #9A9A70;
  padding: 14px 16px 14px 78px; font-size: 15px;
}
.novo {
  position: absolute; left: 12px; top: 50%; width: 58px; height: 40px;
  transform: translateY(-50%) rotate(-12deg); background: #E3262E; color: #fff;
  display: flex; align-items: center; justify-content: center;
  font-weight: 700; font-style: italic; font-size: 13px;
}

/* ---------- Tela de busca: janela do assistente ---------- */
.st-key-janela_assistente {
  border: 1px solid #000; background: var(--bg); box-shadow: 4px 4px 0 var(--shadow);
  padding: 0 !important; gap: 0 !important; margin: 16px auto; width: 100%; max-width: 1000px;
}
.win-h {
  background: var(--navy); color: #fff; font-weight: 700; padding: 0 12px; font-size: 15px;
  height: 36px; box-sizing: border-box; display: flex; align-items: center;
}

/* O chat ocupa toda a altura que sobra entre o header e o footer */
.chat-scroll {
  display: flex; flex-direction: column-reverse; overflow-y: auto;
  height: calc(100vh - var(--chrome) - 156px); min-height: 220px;
  background: #fff; border-bottom: 1px solid #000;
}
.chat-inner { display: flex; flex-direction: column; gap: 10px; padding: 14px; }
.chat-empty { margin: auto; max-width: 420px; text-align: center; font-size: 15px; line-height: 1.6; color: #555; padding: 0 16px; }
.msg { display: flex; }
.msg.user { justify-content: flex-end; }
.msg.agent { justify-content: flex-start; }
.bubble { max-width: 72%; padding: 7px 11px; font-size: 14px; line-height: 1.45; border: 1px solid; overflow-wrap: anywhere; }
.msg.user .bubble { background: #D9DDFF; border-color: var(--navy); }
.msg.agent .bubble { background: var(--note); border-color: #8A8A5A; }
.who { display: block; font-weight: 700; font-size: 11px; margin-bottom: 2px; color: var(--navy); }
.dots i {
  display: inline-block; width: 5px; height: 5px; margin-left: 4px; background: var(--navy);
  animation: blink 1.2s steps(1) infinite;
}
.dots i:nth-child(2) { animation-delay: .4s; }
.dots i:nth-child(3) { animation-delay: .8s; }
@keyframes blink { 0% { opacity: 1; } 33% { opacity: .2; } 66% { opacity: 1; } }
@media (prefers-reduced-motion: reduce) { .dots i { animation: none; } }

.st-key-janela_assistente [data-testid="stForm"] {
  border: 0 !important; background: transparent;
  height: 84px; box-sizing: border-box; padding: 0 14px !important;
  display: flex; flex-direction: column; justify-content: center;
}
[data-testid="InputInstructions"] { display: none !important; }

/* Campo de texto: borda "afundada". O visual do Streamlit está no elemento raiz
   (stTextInputRootElement); os elementos internos ficam transparentes. */
.st-key-janela_assistente [data-testid="stTextInputRootElement"] {
  background: #fff !important; border-radius: 0 !important; border: 2px solid !important;
  border-color: #6F6F66 #fff #fff #6F6F66 !important; box-shadow: none !important;
}
.st-key-janela_assistente [data-testid="stTextInputRootElement"]:focus-within {
  outline: 2px solid var(--blue); outline-offset: 1px;
}
.st-key-janela_assistente [data-baseweb="input"],
.st-key-janela_assistente [data-baseweb="base-input"] {
  background: transparent !important; border: 0 !important; border-radius: 0 !important;
}
.st-key-janela_assistente input {
  font-family: var(--body) !important; font-size: 15px !important;
  color: #000 !important; -webkit-text-fill-color: #000 !important; caret-color: #000;
  background: transparent !important; min-height: 42px; padding: 8px 12px !important;
}
.st-key-janela_assistente input:disabled { -webkit-text-fill-color: #555 !important; opacity: 1; }
.st-key-janela_assistente input::placeholder { color: #6B6B60 !important; -webkit-text-fill-color: #6B6B60 !important; opacity: 1; }

/* Botões: azul chapado com relevo */
[data-testid="stButton"] { display: flex; justify-content: center; }
.stButton > button, [data-testid="stFormSubmitButton"] button {
  background: #0000A8 !important; border-radius: 0 !important; border: 2px solid !important;
  border-color: #8A8AFF #000040 #000040 #8A8AFF !important; box-shadow: none !important;
  min-height: 46px; font-family: var(--body); font-weight: 700; font-size: 15px;
}
.stButton > button *, [data-testid="stFormSubmitButton"] button * { color: #fff !important; }
.stButton > button:hover, [data-testid="stFormSubmitButton"] button:hover { background: #0000C8 !important; }
.stButton > button:active, [data-testid="stFormSubmitButton"] button:active {
  border-color: #000040 #8A8AFF #8A8AFF #000040 !important;
}
.stButton > button:disabled, [data-testid="stFormSubmitButton"] button:disabled {
  background: #A8A8A0 !important; border-color: #D0D0C8 #6F6F66 #6F6F66 #D0D0C8 !important; cursor: not-allowed;
}
.stButton > button:disabled *, [data-testid="stFormSubmitButton"] button:disabled * { color: #E8E6DA !important; }

/* ---------- Placeholder da tela de resultados ---------- */
.page-fill {
  display: flex; align-items: center; justify-content: center;
  min-height: calc(100vh - var(--chrome) - 70px);
}

/* ---------- Rodapé ---------- */
.badges {
  display: grid; grid-template-columns: repeat(4, auto); gap: 18px; justify-content: center;
  align-items: center; height: 90px; box-sizing: border-box;
}
.badge { border: 1px solid #000; padding: 8px 14px; display: flex; gap: 10px; align-items: center; font-size: 12px; font-weight: 700; }
.badge small { display: block; font-weight: 400; font-size: 11px; }
.badge.cards { border-color: var(--navy); flex-direction: column; gap: 4px; align-items: center; }
.chip { display: inline-block; padding: 2px 8px; font-size: 12px; font-weight: 700; border: 1px solid #999; background: #fff; margin: 0 2px; }
.chip.visa { color: #1A1F71; font-style: italic; }
.chip.mc { color: #EB001B; }
.chip.amex { color: #fff; background: #2E77BC; border-color: #2E77BC; }
.footbar {
  background: var(--navy); color: #fff; height: 40px; box-sizing: border-box;
  display: flex; align-items: center; justify-content: center; font-size: 13px;
}
.footbar .sep { margin: 0 14px; }

@media (max-width: 900px) {
  .home-grid { grid-template-columns: 1fr; min-height: 0; }
  .site-head { height: auto; padding: 12px 0; flex-wrap: wrap; }
  .site-nav { margin: 0; padding-bottom: 0; }
  .badges { grid-template-columns: repeat(2, 1fr); height: auto; padding: 14px 0; }
  .footbar { height: auto; padding: 10px 12px; text-align: center; flex-wrap: wrap; }
  .chat-scroll { height: 60vh; }
}
"""
