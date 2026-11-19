"""Gera o ícone da aba do navegador (favicon) e os ícones do app (PWA).

Desenho: o mesmo coração com linha de batimento do logo da barra lateral
(Tabler "heartbeat"), em azul-marinho sobre um quadrado branco de cantos
arredondados. O traço é mais fino que o dos ícones da tela (1,5 em vez de 2)
para não ficar pesado no tamanho pequeno da aba.

Só precisa rodar de novo se quiser trocar a cor ou o desenho:
    pip install cairosvg pillow
    python gerar_icones.py
Os arquivos gerados já estão no repositório; o servidor não precisa disso.
"""
import io
import os

import cairosvg
from PIL import Image

OUT_DIR = os.path.join(os.path.dirname(__file__), 'static', 'icons')
os.makedirs(OUT_DIR, exist_ok=True)

COR_TRACO = '#16293d'   # azul-marinho da barra lateral (direção A)
COR_FUNDO = '#ffffff'
ESPESSURA = 1.5         # em unidades do desenho de 24x24 (o padrão do Tabler é 2)

# Coração com pulso (Tabler Icons 3.31.0, MIT, (c) Pawel Kuna), 24x24.
CAMINHOS = (
    'M19.5 13.572l-7.5 7.428l-2.896 -2.868m-6.117 -8.104a5 5 0 0 1 9.013 -3.022'
    'a5 5 0 1 1 7.5 6.572',
    'M3 13h2l2 3l2 -6l1 3h3',
)


def svg(tamanho_desenho, raio, fundo_inteiro=False, espessura=ESPESSURA):
    """Monta o SVG num quadro de 32x32. tamanho_desenho = largura ocupada pelo
    coração (em unidades do quadro); raio = arredondamento do quadrado."""
    escala = tamanho_desenho / 24
    deslocamento = (32 - tamanho_desenho) / 2
    # O desenho do Tabler fica um pouco abaixo do centro: sobe meia unidade.
    ajuste_y = -0.5 * escala
    rx = 0 if fundo_inteiro else raio
    caminhos = ''.join(f'<path d="{d}"/>' for d in CAMINHOS)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        f'<rect width="32" height="32" rx="{rx}" fill="{COR_FUNDO}"/>'
        f'<g transform="translate({deslocamento:.3f} {deslocamento + ajuste_y:.3f}) scale({escala:.4f})" '
        f'fill="none" stroke="{COR_TRACO}" stroke-width="{espessura}" '
        'stroke-linecap="round" stroke-linejoin="round">'
        f'{caminhos}</g></svg>'
    )


def png(conteudo_svg, tamanho):
    dados = cairosvg.svg2png(bytestring=conteudo_svg.encode(), output_width=tamanho,
                             output_height=tamanho)
    return Image.open(io.BytesIO(dados)).convert('RGBA')


def salvar(nome, imagem):
    imagem.save(os.path.join(OUT_DIR, nome))
    print(f'Gerado: {nome}')


# Aba do navegador: coração ocupa 3/4 do quadrado para ler bem em 16 px.
SVG_ABA = svg(tamanho_desenho=24, raio=7)
with open(os.path.join(OUT_DIR, 'favicon.svg'), 'w', encoding='utf-8') as f:
    f.write(SVG_ABA + '\n')
print('Gerado: favicon.svg')
salvar('favicon-32.png', png(SVG_ABA, 32))
salvar('favicon-16.png', png(svg(tamanho_desenho=25, raio=7, espessura=1.75), 16))
# .ico com 16, 32 e 48 px (navegadores antigos e o atalho do Windows)
png(SVG_ABA, 48).save(os.path.join(OUT_DIR, 'favicon.ico'), sizes=[(16, 16), (32, 32), (48, 48)])
print('Gerado: favicon.ico')

# Ícones do app instalado (PWA): mesmo desenho, com um pouco mais de respiro.
SVG_APP = svg(tamanho_desenho=20, raio=6)
salvar('icon-192.png', png(SVG_APP, 192))
salvar('icon-512.png', png(SVG_APP, 512))

# "maskable": fundo até a borda e desenho menor (o celular recorta em círculo).
SVG_MASK = svg(tamanho_desenho=16, raio=0, fundo_inteiro=True)
salvar('icon-maskable-192.png', png(SVG_MASK, 192))
salvar('icon-maskable-512.png', png(SVG_MASK, 512))

# iPhone: o próprio sistema arredonda os cantos, então o fundo cobre tudo.
salvar('apple-touch-icon.png', png(svg(tamanho_desenho=19, raio=0, fundo_inteiro=True), 180))

print('Pronto! Ícones salvos em static/icons/')
