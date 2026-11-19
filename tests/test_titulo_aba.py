"""A aba do navegador mostra apenas "GestCare" em todas as telas."""
import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"

def test_base_titulo_so_gestcare():
    for base in ("base.html", "base_publico.html"):
        texto = (TEMPLATES / base).read_text(encoding="utf-8")
        assert re.search(r"<title>\s*GestCare\s*</title>", texto), base

def test_nenhum_template_muda_o_titulo_da_aba():
    for arq in TEMPLATES.rglob("*.html"):
        texto = arq.read_text(encoding="utf-8")
        for t in re.findall(r"<title>(.*?)</title>", texto, re.S):
            assert t.strip() == "GestCare", f"{arq.name}: {t}"
