"""Referencia de fatores no banco: papeis, so insercao, lotes, decisao e consulta bitemporal.

Os valores vem de tests/dados_ficticios (inventados). Cada teste usa codigos proprios para
nao depender da ordem dos outros. O instante de conhecimento vem sempre do relogio do banco.
"""

from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from app.modules.fatores import repositorio
from app.modules.fatores.arquivos import ErroArquivo
from tests.isolation.conftest import Banco
from tests.isolation.tabelas import TABELAS_DE_REFERENCIA

pytestmark = pytest.mark.integration

FICTICIOS = Path(__file__).resolve().parents[1] / "dados_ficticios"
NEGADO = asyncpg.exceptions.InsufficientPrivilegeError
SO_INSERCAO = "CL003"
CABECALHO_FATORES = (
    "codigo;descricao;categoria;gas;biogenico;valor;unidade;"
    "vigencia_inicio;vigencia_fim;fonte;referencia\n"
)


@pytest.fixture
async def curadoria(banco: Banco) -> AsyncIterator[asyncpg.Connection]:
    conexao = await asyncpg.connect(banco.dsn_curadoria)
    try:
        yield conexao
    finally:
        await conexao.close()


def _fatores(codigo: str, valor: str) -> bytes:
    linha = f"{codigo};Ficticio;escopo1.teste;CO2;nao;{valor};kg/L;01/01/2020;;FICTICIO;\n"
    return (CABECALHO_FATORES + linha).encode()


async def _vigentes(
    conexao: asyncpg.Connection, codigo: str, data: date, conhecido_em: datetime | None = None
) -> list[repositorio.FatorVigente]:
    instante = conhecido_em or await repositorio.instante_do_banco(conexao)
    return await repositorio.fatores_da_atividade(conexao, codigo, data, instante)


# ---------------------------------------------------------------- papeis e imutabilidade


@pytest.mark.parametrize("tabela", TABELAS_DE_REFERENCIA)
async def test_privilegios_da_referencia(conexao_admin: asyncpg.Connection, tabela: str) -> None:
    esperado = {
        "app_user": {"SELECT"},
        "app_curadoria": {"SELECT", "INSERT"},
        "app_verificador": set(),
    }
    for papel, privilegios in esperado.items():
        tem = {
            p
            for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
            if await conexao_admin.fetchval(
                "SELECT has_table_privilege($1, $2::regclass, $3)", papel, tabela, p
            )
        }
        assert tem == privilegios, f"{papel} em {tabela}: {tem}"


async def test_app_user_nao_planta_fator(conexao_app: asyncpg.Connection) -> None:
    with pytest.raises(NEGADO):
        await conexao_app.execute(
            "INSERT INTO lote_importacao (lote_id, tipo, arquivo, sha256, linhas, importado_por)"
            " VALUES ($1, 'fatores', 'x', $2, 0, 'intruso')",
            uuid4(),
            "0" * 64,
        )


async def test_ninguem_altera_nem_apaga(
    curadoria: asyncpg.Connection, conexao_admin: asyncpg.Connection
) -> None:
    conjunto = f"IMUTAVEL_{uuid4().hex[:8].upper()}"
    conteudo = ("conjunto;gas;valor;fonte\n" + f"{conjunto};CO2;1;FICTICIO\n").encode()
    resultado = await repositorio.importar(curadoria, "gwp", "imutavel.csv", conteudo, "Teste")
    with pytest.raises(NEGADO):
        await curadoria.execute(
            "UPDATE gwp_versao SET valor = 2 WHERE lote_id = $1", resultado.lote_id
        )
    for comando in (
        "UPDATE gwp_versao SET valor = 2",
        "DELETE FROM gwp_versao",
        "TRUNCATE gwp_versao",
    ):
        async with conexao_admin.transaction():
            with pytest.raises(asyncpg.PostgresError) as erro:
                await conexao_admin.execute(comando)
        assert erro.value.sqlstate == SO_INSERCAO


# ---------------------------------------------------------------- lote e decisao


async def test_lote_pendente_nao_vale_e_aprovado_vale(
    curadoria: asyncpg.Connection, conexao_app: asyncpg.Connection
) -> None:
    codigo = f"teste.pendente_{uuid4().hex[:8]}"
    resultado = await repositorio.importar(
        curadoria, "fatores", "x.csv", _fatores(codigo, "1,5"), "Teste"
    )
    assert (resultado.inseridas, resultado.ja_conhecidas) == (1, 0)
    assert await _vigentes(conexao_app, codigo, date(2026, 6, 1)) == []

    instante = await repositorio.decidir(
        curadoria, resultado.lote_id, aprovar=True, responsavel="Curadora"
    )
    fatores = await _vigentes(conexao_app, codigo, date(2026, 6, 1))
    assert [(f.gas, f.valor) for f in fatores] == [("CO2", Decimal("1.5"))]
    assert fatores[0].aprovado_em == instante
    assert len(fatores[0].hash_conteudo) == 64

    with pytest.raises(repositorio.DecisaoInvalida, match="ja decidido"):
        await repositorio.decidir(
            curadoria, resultado.lote_id, aprovar=False, responsavel="X", justificativa="y"
        )


async def test_mesmo_arquivo_nao_entra_duas_vezes(curadoria: asyncpg.Connection) -> None:
    conteudo = _fatores(f"teste.repetido_{uuid4().hex[:8]}", "2")
    await repositorio.importar(curadoria, "fatores", "a.csv", conteudo, "Teste")
    with pytest.raises(repositorio.LoteRepetido):
        await repositorio.importar(curadoria, "fatores", "b.csv", conteudo, "Teste")


async def test_rejeicao_exige_justificativa_e_nao_vale(
    curadoria: asyncpg.Connection, conexao_app: asyncpg.Connection
) -> None:
    codigo = f"teste.rejeitado_{uuid4().hex[:8]}"
    importado = await repositorio.importar(
        curadoria, "fatores", "r.csv", _fatores(codigo, "3"), "Teste"
    )
    with pytest.raises(repositorio.DecisaoInvalida, match="justificativa"):
        await repositorio.decidir(curadoria, importado.lote_id, aprovar=False, responsavel="C")
    await repositorio.decidir(
        curadoria, importado.lote_id, aprovar=False, responsavel="C", justificativa="fonte errada"
    )
    assert await _vigentes(conexao_app, codigo, date(2026, 1, 1)) == []


async def test_linha_ja_aprovada_nao_e_gravada_de_novo(curadoria: asyncpg.Connection) -> None:
    codigo = f"teste.igual_{uuid4().hex[:8]}"
    primeiro = await repositorio.importar(
        curadoria, "fatores", "1.csv", _fatores(codigo, "1,50"), "T"
    )
    await repositorio.decidir(curadoria, primeiro.lote_id, aprovar=True, responsavel="C")
    # Outro arquivo (comentario e escala decimal diferentes), mesma linha: nada novo.
    conteudo = b"# outro arquivo\n" + _fatores(codigo, "1,5")
    segundo = await repositorio.importar(curadoria, "fatores", "2.csv", conteudo, "T")
    assert (segundo.inseridas, segundo.ja_conhecidas) == (0, 1)


async def test_arquivo_invalido_nao_grava_nada(
    curadoria: asyncpg.Connection, conexao_admin: asyncpg.Connection
) -> None:
    antes = await conexao_admin.fetchval("SELECT count(*) FROM lote_importacao")
    with pytest.raises(ErroArquivo):
        await repositorio.importar(
            curadoria, "fatores", "ruim.csv", _fatores("Codigo Ruim", "x"), "T"
        )
    assert await conexao_admin.fetchval("SELECT count(*) FROM lote_importacao") == antes


# ---------------------------------------------------------------- consulta bitemporal


async def test_versao_nova_vale_so_depois_de_aprovada(
    curadoria: asyncpg.Connection, conexao_app: asyncpg.Connection
) -> None:
    """O inventario calculado antes da correcao refaz a mesma conta (conhecido_em)."""
    codigo = f"teste.corrigido_{uuid4().hex[:8]}"
    data = date(2026, 3, 15)
    v1 = await repositorio.importar(curadoria, "fatores", "v1.csv", _fatores(codigo, "1,0"), "T")
    aprovado_v1 = await repositorio.decidir(curadoria, v1.lote_id, aprovar=True, responsavel="C")
    calculado_em = await repositorio.instante_do_banco(conexao_app)

    v2 = await repositorio.importar(curadoria, "fatores", "v2.csv", _fatores(codigo, "1,1"), "T")
    aprovado_v2 = await repositorio.decidir(curadoria, v2.lote_id, aprovar=True, responsavel="C")
    assert aprovado_v2 > aprovado_v1

    antes = await _vigentes(conexao_app, codigo, data, calculado_em)
    depois = await _vigentes(conexao_app, codigo, data)
    assert [f.valor for f in antes] == [Decimal("1.0")]
    assert [f.valor for f in depois] == [Decimal("1.1")]
    assert antes[0].hash_conteudo != depois[0].hash_conteudo
    um_segundo_antes = aprovado_v1 - timedelta(seconds=1)
    assert await _vigentes(conexao_app, codigo, data, um_segundo_antes) == []


async def test_vigencia_escolhe_o_fator_da_data(
    curadoria: asyncpg.Connection, conexao_app: asyncpg.Connection
) -> None:
    lote = await repositorio.importar(
        curadoria, "fatores", "fatores.csv", (FICTICIOS / "fatores.csv").read_bytes(), "T"
    )
    await repositorio.decidir(curadoria, lote.lote_id, aprovar=True, responsavel="C")
    codigo = "teste.combustivel_x"
    em_2025 = {
        (f.gas, f.biogenico): f.valor
        for f in await _vigentes(conexao_app, codigo, date(2025, 7, 1))
    }
    em_2026 = {
        (f.gas, f.biogenico): f.valor
        for f in await _vigentes(conexao_app, codigo, date(2026, 7, 1))
    }
    assert em_2025 == {
        ("CO2", False): Decimal("1.1111"),
        ("CH4", False): Decimal("0.00015"),
        ("CO2", True): Decimal("0.3333"),
    }
    assert em_2026[("CO2", False)] == Decimal("1.2222")
    assert await _vigentes(conexao_app, codigo, date(2019, 12, 31)) == []


async def test_gwp_e_mistura_pela_data_da_compra(
    curadoria: asyncpg.Connection, conexao_app: asyncpg.Connection
) -> None:
    for tipo, arquivo in (("gwp", "gwp.csv"), ("misturas", "misturas.csv")):
        lote = await repositorio.importar(
            curadoria, tipo, arquivo, (FICTICIOS / arquivo).read_bytes(), "T"
        )
        await repositorio.decidir(curadoria, lote.lote_id, aprovar=True, responsavel="C")
    agora = await repositorio.instante_do_banco(conexao_app)
    ch4 = await repositorio.gwp_do_gas(conexao_app, "TESTE_100", "CH4", agora)
    assert ch4 is not None and ch4.valor == Decimal("11.5")
    assert await repositorio.gwp_do_gas(conexao_app, "TESTE_100", "SF6", agora) is None

    async def componentes(data: date) -> dict[str, Decimal]:
        lista = await repositorio.mistura_na_data(
            conexao_app, "teste.diesel_comercial", data, agora
        )
        return {c.componente: c.percentual for c in lista}

    assert await componentes(date(2025, 12, 31)) == {
        "teste.biodiesel": Decimal(10),
        "teste.diesel_fossil": Decimal(90),
    }
    assert await componentes(date(2026, 1, 1)) == {
        "teste.biodiesel": Decimal(15),
        "teste.diesel_fossil": Decimal(85),
    }


# ---------------------------------------------------------------- linha de comando


def test_linha_de_comando_da_curadoria(
    banco: Banco,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import re

    from app.cli import main
    from app.core.config import obter_configuracao

    monkeypatch.setenv("CURADORIA_DATABASE_URL", banco.dsn_curadoria)
    obter_configuracao.cache_clear()
    try:
        ruim = tmp_path / "ruim.csv"
        ruim.write_text("conjunto;gas;valor;fonte\nTESTE;CO2;abc;f\n", encoding="utf-8")
        assert main(["fatores", "importar", "--tipo", "gwp", "--por", "Teste", str(ruim)]) == 1
        assert "linha 2, coluna valor" in capsys.readouterr().out

        bom = tmp_path / "bom.csv"
        conjunto = f"CLI_{uuid4().hex[:8].upper()}"
        bom.write_text(f"conjunto;gas;valor;fonte\n{conjunto};CO2;1;FICTICIO\n", encoding="utf-8")
        assert main(["fatores", "importar", "--tipo", "gwp", "--por", "Teste", str(bom)]) == 0
        saida = capsys.readouterr().out
        lote = re.search(r"Lote ([0-9a-f-]{36}) importado", saida)
        assert lote, saida
        assert main(["fatores", "importar", "--tipo", "gwp", "--por", "Teste", str(bom)]) == 0
        assert "Nada a fazer" in capsys.readouterr().out

        assert (
            main(["fatores", "rejeitar", lote.group(1), "--responsavel", "C", "--motivo", " "]) == 1
        )
        assert "justificativa" in capsys.readouterr().out
        assert main(["fatores", "aprovar", lote.group(1), "--responsavel", "Curadora"]) == 0
        assert "aprovado em" in capsys.readouterr().out
        assert main(["fatores", "lotes"]) == 0
        assert f"{lote.group(1)}  gwp      aprovado" in capsys.readouterr().out
    finally:
        obter_configuracao.cache_clear()
