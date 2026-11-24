import importlib.util
from pathlib import Path

_CAMINHO = Path(__file__).resolve().parents[2] / "scripts" / "verificar_caracteres.py"
_spec = importlib.util.spec_from_file_location("verificar_caracteres", _CAMINHO)
assert _spec is not None and _spec.loader is not None
verificador = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verificador)


def test_detecta_travessoes(tmp_path: Path) -> None:
    ruim = tmp_path / "ruim.md"
    ruim.write_text("a \u2013 b\nc \u2014 d\n", encoding="utf-8")
    problemas = verificador.encontrar_proibidos([ruim])
    assert len(problemas) == 2
    assert "U+2013" in problemas[0]
    assert "U+2014" in problemas[1]


def test_aceita_texto_limpo(tmp_path: Path) -> None:
    bom = tmp_path / "bom.md"
    bom.write_text("a - b: acentuação ok\n", encoding="utf-8")
    assert verificador.encontrar_proibidos([bom]) == []


def test_ignora_binario(tmp_path: Path) -> None:
    binario = tmp_path / "x.bin"
    binario.write_bytes(b"\xff\xfe\x00\x80")
    assert verificador.encontrar_proibidos([binario]) == []


def test_so_as_pastas_vendor_ficam_de_fora(tmp_path: Path) -> None:
    raiz = tmp_path
    assert verificador.de_terceiros(raiz / "app/web/static/vendor/x-1.0/a.js", raiz)
    assert verificador.de_terceiros(raiz / "tests/e2e/vendor/axe/axe.min.js", raiz)
    assert not verificador.de_terceiros(raiz / "app/web/static/js/app.js", raiz)
    assert not verificador.de_terceiros(raiz / "app/web/vendor/a.js", raiz)
    assert not verificador.de_terceiros(raiz / "docs/vendor/nota.md", raiz)
