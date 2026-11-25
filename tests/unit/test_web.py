"""Regras de UI do CLAUDE.md conferidas automaticamente, sem navegador."""

import json
import re
import subprocess  # nosec B404
import sys
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import obter_configuracao
from app.main import criar_app
from app.web.cabecalhos import CSP
from app.web.formatos import NumeroInvalido, formatar_numero, ler_numero
from app.web.renderizacao import MENU, PASTA_ESTATICOS, PASTA_TEMPLATES

RAIZ = Path(__file__).resolve().parents[2]
TEMA = PASTA_ESTATICOS / "css" / "tema.css"
NOSSOS_ESTATICOS = [
    p for p in PASTA_ESTATICOS.rglob("*") if p.is_file() and "vendor" not in p.parts
]
TEMPLATES = list(PASTA_TEMPLATES.rglob("*.html"))
# Modelos de PDF (relatorios/): nunca servidos ao navegador. O Chromium os abre sem
# JavaScript e sem rede, com o tema embutido num bloco de estilo; a CSP nao se aplica.
TEMPLATES_PDF = [p for p in TEMPLATES if "relatorios" in p.relative_to(PASTA_TEMPLATES).parts[:1]]
TEMPLATES_SERVIDOS = [p for p in TEMPLATES if p not in TEMPLATES_PDF]


@pytest.fixture(scope="module")
def cliente() -> Iterator[TestClient]:
    # Com o ciclo de vida: o Redis e o banco so conectam se alguma rota usar.
    with TestClient(criar_app()) as cliente:
        yield cliente


# ---------------------------------------------------------------- numeros pt-BR


@pytest.mark.parametrize(
    ("valor", "casas", "esperado"),
    [
        (Decimal("1234.567"), 2, "1.234,57"),
        (Decimal("0.005"), 2, "0,01"),
        (Decimal("-1234567.1"), 1, "-1.234.567,1"),
        (Decimal("999"), 0, "999"),
        (1000, 2, "1.000,00"),
        (Decimal("2.6012"), 4, "2,6012"),
        (None, 2, ""),
    ],
)
def test_formatar_numero(valor: Decimal | int | None, casas: int, esperado: str) -> None:
    assert formatar_numero(valor, casas) == esperado


def test_float_e_recusado() -> None:
    with pytest.raises(TypeError):
        formatar_numero(1.5)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("1.234,56", "1234.56"),
        ("1234,5", "1234.5"),
        ("1234", "1234"),
        (" 0,1 ", "0.1"),
        ("-2,5", "-2.5"),
    ],
)
def test_ler_numero(texto: str, esperado: str) -> None:
    assert ler_numero(texto) == Decimal(esperado)


@pytest.mark.parametrize("texto", ["1.5", "1,234.56", "abc", "", "1.23,4", "1,,2"])
def test_ler_numero_invalido(texto: str) -> None:
    with pytest.raises(NumeroInvalido):
        ler_numero(texto)


# ---------------------------------------------------------------- regras dos arquivos


def _macros_publicas() -> set[str]:
    nomes: set[str] = set()
    for arquivo in (PASTA_TEMPLATES / "componentes").glob("*.html"):
        nomes |= {
            nome
            for nome in re.findall(r"{%-?\s*macro\s+(\w+)\(", arquivo.read_text(encoding="utf-8"))
            if not nome.startswith("_")
        }
    return nomes


def test_toda_macro_aparece_na_pagina_ui() -> None:
    galeria = (PASTA_TEMPLATES / "paginas" / "ui.html").read_text(encoding="utf-8")
    faltando = sorted(nome for nome in _macros_publicas() if not re.search(rf"\b{nome}\(", galeria))
    assert not faltando, f"componente novo entra primeiro em /ui: {faltando}"


@pytest.mark.parametrize("arquivo", TEMPLATES_SERVIDOS, ids=lambda p: p.name)
def test_template_sem_inline_que_a_csp_bloqueia(arquivo: Path) -> None:
    texto = arquivo.read_text(encoding="utf-8")
    assert not re.search(r"\sstyle\s*=", texto), "estilo inline"
    assert not re.search(r"<style", texto), "bloco de estilo"
    assert not re.search(r"\son[a-z]+\s*=", texto), "handler inline (onclick...)"
    for script in re.findall(r"<script[^>]*>", texto):
        assert "src=" in script or 'type="application/json"' in script, script


@pytest.mark.parametrize("arquivo", TEMPLATES + NOSSOS_ESTATICOS, ids=lambda p: p.name)
def test_nada_de_dominio_externo(arquivo: Path) -> None:
    texto = arquivo.read_text(encoding="utf-8")
    assert not re.search(r"(src|href)\s*=\s*[\"']?(https?:)?//", texto)
    assert not re.search(r"url\(\s*[\"']?(https?:)?//", texto)
    assert not re.search(r"@import", texto)


def test_modelo_de_pdf_so_embute_o_tema() -> None:
    assert TEMPLATES_PDF, "esperado ao menos o modelo do relatorio"
    for arquivo in TEMPLATES_PDF:
        texto = arquivo.read_text(encoding="utf-8")
        assert re.findall(r"<style[^>]*>(.*?)</style>", texto, re.S) == ["{{ tema | safe }}"]
        assert not re.search(r"<script|\sstyle\s*=|\son[a-z]+\s*=", texto)


def test_cor_e_fonte_so_no_tema() -> None:
    cor = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(|\boklch\(")
    fonte = re.compile(r"font-family\s*:")
    suspeitos = TEMPLATES + [p for p in NOSSOS_ESTATICOS if p != TEMA]
    suspeitos += list((RAIZ / "app").rglob("*.py"))
    for arquivo in suspeitos:
        texto = arquivo.read_text(encoding="utf-8")
        if arquivo.suffix == ".html":
            texto = re.sub(r"{#.*?#}", "", texto, flags=re.S)
            texto = re.sub(r'href="#[\w-]*"|data-bs-target="#[\w-]*"', "", texto)
            texto = re.sub(r'hx-(target|indicator)="#[\w-]*"', "", texto)
            texto = re.sub(
                r'hx-indicator="#{{[^"]*"|hx-target="#{{[^"]*"|data-bs-target="#{{[^"]*"', "", texto
            )
        assert not cor.search(texto), f"cor fora do tema.css em {arquivo.name}: {cor.search(texto)}"
        assert not fonte.search(texto), f"fonte fora do tema.css em {arquivo.name}"


def _cores_do_tema() -> dict[str, str]:
    return dict(re.findall(r"(--cl-[\w-]+):\s*(#[0-9a-fA-F]{6})", TEMA.read_text(encoding="utf-8")))


def _contraste(a: str, b: str) -> float:
    def luminancia(cor: str) -> float:
        canais = [int(cor[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        lineares = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in canais]
        return 0.2126 * lineares[0] + 0.7152 * lineares[1] + 0.0722 * lineares[2]

    claro, escuro = sorted((luminancia(a), luminancia(b)), reverse=True)
    return (claro + 0.05) / (escuro + 0.05)


@pytest.mark.parametrize(
    ("frente", "fundo", "minimo"),
    [
        ("--cl-branco", "--cl-marinho-900", 4.5),
        ("--cl-branco", "--cl-marinho-700", 4.5),
        ("--cl-branco", "--cl-indigo-700", 4.5),
        ("--cl-indigo-700", "--cl-branco", 4.5),
        ("--cl-indigo-700", "--cl-indigo-50", 4.5),
        ("--cl-texto", "--cl-branco", 4.5),
        ("--cl-texto", "--cl-marinho-100", 4.5),
        ("--cl-texto-suave", "--cl-branco", 4.5),
        ("--cl-texto-suave", "--cl-fundo", 4.5),
        ("--cl-texto-suave", "--cl-marinho-100", 4.5),
        ("--cl-sucesso", "--cl-branco", 4.5),
        ("--cl-alerta", "--cl-branco", 4.5),
        ("--cl-perigo", "--cl-branco", 4.5),
        ("--cl-info", "--cl-branco", 4.5),
        ("--cl-indigo-600", "--cl-branco", 3.0),
        ("--cl-ambar-300", "--cl-marinho-900", 3.0),
        ("--cl-ambar-300", "--cl-marinho-700", 3.0),
        ("--cl-borda", "--cl-branco", 3.0),
    ],
)
def test_contraste_wcag_aa(frente: str, fundo: str, minimo: float) -> None:
    cores = _cores_do_tema()
    assert _contraste(cores[frente], cores[fundo]) >= minimo


def test_vendor_confere_com_o_manifesto() -> None:
    resultado = subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, str(RAIZ / "scripts" / "vendor_manifesto.py"), "--conferir"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert resultado.returncode == 0, resultado.stdout
    manifesto = json.loads(
        (PASTA_ESTATICOS / "vendor" / "MANIFESTO.json").read_text(encoding="utf-8")
    )
    licencas = {p["licenca"] for p in manifesto["pacotes"].values()}
    assert licencas <= {"MIT", "0BSD", "Apache-2.0"}


def test_menu_na_ordem_do_trabalho() -> None:
    assert [item.url for item in MENU] == [
        "/",
        "/organizacao",
        "/coleta",
        "/resultados",
        "/fechamento",
        "/relatorios",
        "/verificacao",
        "/metas",
        "/painel",
    ]


# ---------------------------------------------------------------- respostas


def test_csp_e_cabecalhos_em_toda_resposta(cliente: TestClient) -> None:
    for caminho in ("/ui", "/entrar", "/nao-existe", "/saude-inexistente"):
        resposta = cliente.get(caminho, headers={"accept": "text/html"})
        assert resposta.headers["content-security-policy"] == CSP
        assert resposta.headers["x-content-type-options"] == "nosniff"
        assert resposta.headers["x-frame-options"] == "DENY"
    assert "'unsafe-inline'" not in CSP and "'unsafe-eval'" not in CSP


def test_pagina_ui_renderiza_todos_os_componentes(cliente: TestClient) -> None:
    resposta = cliente.get("/ui")
    assert resposta.status_code == 200
    html = resposta.text
    assert '<html lang="pt-BR">' in html
    assert "1.234,57" in html and "tCO₂e" in html
    assert 'data-confirmar-texto="Empresa Exemplo Ltda"' in html
    assert '"includeIndicatorStyles": false' in html.replace("&#34;", '"')
    assert 'aria-live="polite"' in html


def test_estaticos_com_versao_pelo_conteudo(cliente: TestClient) -> None:
    html = cliente.get("/ui").text
    tema = re.search(r'href="(/static/css/tema\.css\?v=[0-9a-f]{10})"', html)
    assert tema, "tema.css sem versao"
    assert cliente.get(tema.group(1)).status_code == 200


def test_erro_vira_login_no_navegador_e_json_na_api(cliente: TestClient) -> None:
    # Sem sessao (ou com ela expirada), o navegador vai para o login; nada vaza.
    pagina = cliente.get("/nao-existe", headers={"accept": "text/html"}, follow_redirects=False)
    assert pagina.status_code == 303
    assert pagina.headers["location"] == "/entrar"
    api = cliente.get("/api/nao-existe", headers={"accept": "text/html"})
    assert api.json() == {"detalhe": "Não encontrado."}
    htmx = cliente.get("/nao-existe", headers={"accept": "text/html", "HX-Request": "true"})
    assert htmx.json() == {"detalhe": "Não encontrado."}


def test_validacao_na_hora(cliente: TestClient) -> None:
    ruim = cliente.post("/ui/validar/consumo", data={"consumo": "1.5"})
    assert "vírgula" in ruim.text and 'id="erro-consumo"' in ruim.text
    bom = cliente.post("/ui/validar/consumo", data={"consumo": "1.234,5"})
    assert re.search(r'id="erro-consumo"[^>]*></div>', bom.text)
    assert cliente.post("/ui/validar/qualquer", data={}).status_code == 404


def test_envio_com_erro_por_campo_e_aviso(cliente: TestClient) -> None:
    erro = cliente.post("/ui/enviar", data={"responsavel": "a@b.com", "consumo": "-1"})
    assert erro.status_code == 422
    assert erro.json()["erros"][0]["campo"] == "consumo"
    ok = cliente.post("/ui/enviar", data={"responsavel": "a@b.com", "consumo": "10"})
    assert ok.status_code == 204
    assert ok.headers["x-aviso"].startswith("Consumo%20de%20exemplo")


def test_ui_some_em_producao(monkeypatch: pytest.MonkeyPatch) -> None:
    for nome, valor in {
        "AMBIENTE": "producao",
        "PROVEDOR_CHAVES": "kms",
        "KMS_CHAVE_ID": "alias/carbonledger",
        "DATABASE_URL": "postgresql://app_user:x@banco:5432/carbonledger?sslmode=require",
        "EVIDENCIAS_MODO_RETENCAO": "COMPLIANCE",
        "APP_URL_BASE": "https://carbonledger.example",
    }.items():
        monkeypatch.setenv(nome, valor)
    obter_configuracao.cache_clear()
    try:
        resposta = TestClient(criar_app()).get("/ui", headers={"accept": "application/json"})
        assert resposta.status_code == 404
    finally:
        obter_configuracao.cache_clear()


def test_sem_sessao_as_telas_levam_ao_login(cliente: TestClient) -> None:
    for caminho in ("/", "/clientes"):
        resposta = cliente.get(caminho, follow_redirects=False)
        assert resposta.status_code == 303
        assert resposta.headers["location"] == "/entrar"
