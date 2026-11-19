"""Tela de entrada: só marca + formulário, com abertura e transição de entrada."""
from pathlib import Path

import app as gc

RAIZ = Path(__file__).resolve().parent.parent


def test_login_sem_frases_explicativas():
    html = gc.app.test_client().get('/login').get_data(as_text=True)
    for frase in ('Cuidar de quem um dia', 'Agenda de medicação', 'Portal da família',
                  'Acesso da equipe e das famílias'):
        assert frase not in html
    assert 'id="form-entrar"' in html and 'name="csrf_token"' in html
    assert 'Criar conta da família' in html
    assert 'entrada-pulso' in html and 'entrada-cortina' in html


def test_base_publica_sem_slogan():
    texto = (RAIZ / 'templates' / 'base_publico.html').read_text(encoding='utf-8')
    assert 'Cuidar de quem um dia' not in texto and 'publico-marca-lista' not in texto


def test_painel_recebe_a_chegada():
    for base in ('base.html', 'base_publico.html'):
        assert "partials/_chegada.html" in (RAIZ / 'templates' / base).read_text(encoding='utf-8')


def test_movimento_respeita_preferencia_de_menos_movimento():
    css = (RAIZ / 'static' / 'css' / 'entrada.css').read_text(encoding='utf-8')
    # toda animação mora dentro de blocos "no-preference"
    fora = css.split('@media (prefers-reduced-motion: no-preference)')[0]
    assert 'animation:' not in fora
    js = (RAIZ / 'templates' / 'login.html').read_text(encoding='utf-8')
    assert "prefers-reduced-motion: reduce" in js
