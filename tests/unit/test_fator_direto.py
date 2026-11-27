"""Metodo fator_direto, sem banco. FICTICIO: todos os fatores, GWP e misturas sao inventados.

Caso feito a mao: 1.000 L de diesel comercial (85% fossil e 15% bio), unidade com 50% de
participacao. Fossil: CO2 2,6 kg/L e CH4 0,0004 kg/L. Bio: CO2 biogenico 2,4 kg/L e CH4
0,0003 kg/L. GWP: CO2 1 e CH4 28.
  fossil 850 L: CO2 2.210 kg = 2,21 t x 50% = 1,105 tCO2e
                CH4 0,34 kg = 0,00034 t x 50% = 0,00017 t x 28 = 0,00476 tCO2e
  bio    150 L: CO2 bio 360 kg = 0,36 t x 50% = 0,18 t (fora do escopo)
                CH4 0,045 kg = 0,000045 t x 50% = 0,0000225 t x 28 = 0,00063 tCO2e
  escopo 1 = 1,11039 tCO2e; biogenico = 0,18 t
"""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from hypothesis import given, settings
from hypothesis import strategies as st

from app.modules.calculo import fator_direto as m
from app.modules.calculo.servico import data_de_referencia

D = Decimal
FONTE = UUID("00000000-0000-4000-8000-0000000000f1")
INSTANTE = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)


def _id(n: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{n:012d}")


def _fator(
    n: int, gas: str, valor: str, *, bio: bool = False, unidade: str = "kg/L"
) -> m.FatorUsado:
    return m.FatorUsado(_id(n), gas, bio, D(valor), unidade, f"hash-{n}")


FOSSIL = m.Componente(
    "teste.diesel_fossil",
    D(85),
    _id(90),
    "mist-1",
    (_fator(1, "CO2", "2.6"), _fator(2, "CH4", "0.0004")),
)
BIO = m.Componente(
    "teste.biodiesel",
    D(15),
    _id(91),
    "mist-2",
    (_fator(3, "CO2", "2.4", bio=True), _fator(4, "CH4", "0.0003")),
)
GWPS = {
    "CO2": m.GwpUsado(_id(50), "CO2", D(1), "g-co2"),
    "CH4": m.GwpUsado(_id(51), "CH4", D(28), "g-ch4"),
}


def _item(
    quantidade: str = "1000",
    unidade: str = "L",
    *,
    lancamento: int = 100,
    periodo: int = 1,
    componentes: tuple[m.Componente, ...] = (FOSSIL, BIO),
) -> m.Item:
    return m.Item(
        fonte_id=FONTE,
        lancamento_id=_id(lancamento),
        periodo=periodo,
        categoria="escopo1.combustao_movel",
        atividade="teste.diesel_comercial",
        quantidade=D(quantidade),
        unidade=unidade,
        data_referencia=date(2025, 1, 15),
        percentual_consolidado=D(50),
        componentes=componentes,
    )


def _entrada(*itens: m.Item, gwps: dict[str, m.GwpUsado] | None = None) -> m.Entrada:
    return m.Entrada("TESTE_100", INSTANTE, tuple(itens), GWPS if gwps is None else gwps)


def test_caso_feito_a_mao() -> None:
    resultado = m.calcular(_entrada(_item()))
    assert resultado.escopo1_tco2e == D("1.11039")
    assert resultado.biogenico_tco2 == D("0.18")
    assert resultado.pendencias == ()
    por_gas = m.somar_por(resultado.linhas, "gas")
    assert por_gas == {"CO2": D("1.105"), "CH4": D("0.00539")}
    biogenica = next(linha for linha in resultado.linhas if linha.biogenico)
    assert biogenica.gwp is None and biogenica.tco2e is None and biogenica.massa_t == D("0.18")
    assert {linha.componente for linha in resultado.linhas} == {
        "teste.diesel_fossil",
        "teste.biodiesel",
    }


def test_unidade_convertida_pelo_pint() -> None:
    em_litros = m.calcular(_entrada(_item("1000", "L")))
    em_metros = m.calcular(_entrada(_item("1", "m**3")))
    assert em_metros.escopo1_tco2e == em_litros.escopo1_tco2e
    assert em_metros.linhas[0].quantidade_convertida == D(850)


def test_grandeza_diferente_vira_pendencia_e_nao_zero() -> None:
    resultado = m.calcular(_entrada(_item("1000", "kg")))
    assert resultado.linhas == ()
    assert {p.motivo for p in resultado.pendencias} == {"unidade_incompativel"}
    assert resultado.escopo1_tco2e == 0


def test_gas_sem_gwp_vira_pendencia() -> None:
    resultado = m.calcular(_entrada(_item(), gwps={"CO2": GWPS["CO2"]}))
    assert [p.motivo for p in resultado.pendencias] == ["sem_gwp", "sem_gwp"]
    assert all(linha.gas != "CH4" for linha in resultado.linhas)


def test_sem_mistura_o_combustivel_entra_inteiro() -> None:
    puro = m.Componente(
        "teste.gas_natural", None, None, None, (_fator(5, "CO2", "2", unidade="kg/m**3"),)
    )
    resultado = m.calcular(_entrada(_item("100", "m**3", componentes=(puro,))))
    # 100 m3 x 2 kg/m3 = 200 kg = 0,2 t x 50% = 0,1 tCO2e
    assert resultado.escopo1_tco2e == D("0.1")


def test_mesma_entrada_mesmo_hash_em_qualquer_ordem() -> None:
    a, b = _item(lancamento=100, periodo=1), _item("500", lancamento=101, periodo=2)
    primeiro = m.calcular(_entrada(a, b))
    segundo = m.calcular(_entrada(b, a))
    assert primeiro.hash_entrada == segundo.hash_entrada
    assert primeiro.hash_resultado == segundo.hash_resultado
    # 2,60 e 2,6 sao o mesmo numero: o hash nao muda com a escala.
    com_zero = replace(FOSSIL, fatores=(_fator(1, "CO2", "2.60"), _fator(2, "CH4", "0.00040")))
    assert (
        m.calcular(_entrada(_item(componentes=(com_zero, BIO)))).hash_entrada
        == m.calcular(_entrada(_item())).hash_entrada
    )


def test_qualquer_mudanca_muda_o_hash() -> None:
    base = m.calcular(_entrada(_item()))
    outro_fator = replace(FOSSIL, fatores=(_fator(1, "CO2", "2.7"), FOSSIL.fatores[1]))
    assert (
        m.calcular(_entrada(_item(componentes=(outro_fator, BIO)))).hash_resultado
        != base.hash_resultado
    )
    assert m.calcular(_entrada(_item("1001"))).hash_entrada != base.hash_entrada
    outro_instante = replace(_entrada(_item()), conhecido_em=datetime(2026, 2, 1, tzinfo=UTC))
    assert m.calcular(outro_instante).hash_entrada != base.hash_entrada


def test_entrada_guardada_refaz_o_mesmo_resultado() -> None:
    entrada = replace(
        _entrada(_item(), _item("1", "m**3", lancamento=101, periodo=2)),
        pendencias=(m.Pendencia(FONTE, 3, "falta_dado", "sem dado"),),
    )
    original = m.calcular(entrada)
    refeito = m.calcular(m.entrada_de_json(m.entrada_para_json(entrada)))
    assert refeito.hash_entrada == original.hash_entrada
    assert refeito.hash_resultado == original.hash_resultado
    assert refeito.escopo1_tco2e == original.escopo1_tco2e


@settings(deadline=None)  # a primeira chamada monta o registro de unidades do pint
@given(st.decimals(min_value=D(0), max_value=D("1e9"), places=6, allow_nan=False))
def test_emissao_proporcional_a_quantidade(quantidade: Decimal) -> None:
    """Dobrar a quantidade dobra a emissao, sem perda de precisao (Decimal, nunca float)."""
    uma = m.calcular(_entrada(_item(str(quantidade)))).escopo1_tco2e
    duas = m.calcular(_entrada(_item(str(quantidade * 2)))).escopo1_tco2e
    assert duas == uma * 2
    assert isinstance(uma, Decimal)


def test_data_de_referencia() -> None:
    assert data_de_referencia(2025, 2, None) == date(2025, 2, 28)
    assert data_de_referencia(2024, 2, None) == date(2024, 2, 29)
    assert data_de_referencia(2025, 0, None) == date(2025, 12, 31)
    assert data_de_referencia(2025, 5, date(2025, 5, 3)) == date(2025, 5, 3)


def test_escopo_2_usa_o_fim_do_mes_e_nao_a_data_da_compra() -> None:
    from app.modules.calculo.montagem import data_de_referencia as referencia

    compra = date(2025, 5, 3)
    assert referencia(2025, 2, compra, "escopo2.energia_eletrica") == date(2025, 2, 28)
    assert referencia(2025, 2, compra, "escopo1.combustao_movel") == compra


def test_escopo_2_tem_total_proprio() -> None:
    sin = m.Componente("teste.sin", None, None, None, (_fator(6, "CO2", "0.1", unidade="t/MWh"),))
    eletrica = replace(
        _item("200", "MWh", lancamento=200, componentes=(sin,)),
        categoria="escopo2.energia_eletrica",
        percentual_consolidado=D(100),
    )
    resultado = m.calcular(_entrada(_item(), eletrica))
    assert resultado.escopo1_tco2e == D("1.11039")
    assert resultado.escopo2_tco2e == D(20)


def test_versao_1_0_0_continua_refeita_com_o_mesmo_hash() -> None:
    """Execucoes gravadas na 1.0.0 (so escopo 1) sao conferidas pelo codigo novo."""
    entrada = _entrada(_item())
    antiga = m.calcular(entrada, "1.0.0")
    esperado = m.hash_canonico(
        m.METODO, "1.0.0", antiga.linhas, antiga.pendencias, antiga.escopo1_tco2e,
        antiga.biogenico_tco2,
    )  # fmt: skip
    assert antiga.hash_resultado == esperado
    assert antiga.versao == "1.0.0"
    assert m.calcular(entrada).hash_resultado != antiga.hash_resultado


# ---------------------------------------------------------------- metas (P18)


def _meta(base: str = "100", reducao: str = "50", escopos: str = "1+2") -> object:
    from app.modules.metas.servico import Meta

    return Meta(
        meta_id=_id(1),
        empresa_id=_id(2),
        nome="Meta",
        escopos=escopos,
        inventario_base_id=_id(3),
        calculo_base_id=_id(4),
        ano_base=2025,
        valor_base_tco2e=D(base),
        ano_alvo=2030,
        reducao_percentual=D(reducao),
        ativa=True,
    )


def test_trajetoria_linear_da_meta() -> None:
    meta = _meta()
    assert meta.valor_alvo_tco2e == D(50)  # type: ignore[attr-defined]
    esperados = [meta.esperado(ano) for ano in range(2024, 2032)]  # type: ignore[attr-defined]
    assert esperados == [D(100), D(100), D(90), D(80), D(70), D(60), D(50), D(50)]


def test_total_dos_escopos_e_situacao_do_ano() -> None:
    from app.modules.metas.servico import Ponto, total_dos_escopos

    assert total_dos_escopos("1", D(3), D(4)) == D(3)
    assert total_dos_escopos("2", D(3), D(4)) == D(4)
    assert total_dos_escopos("1+2", D(3), D(4)) == D(7)
    assert Ponto(2026, D(90), D(85), False).no_caminho is True
    assert Ponto(2026, D(90), D(95), True).no_caminho is False
    assert Ponto(2026, D(90), None, False).no_caminho is None


# ---------------------------------------------------------------- escopo 3 (P17)


def test_escopo_3_tem_total_proprio_e_aceita_valor_gasto() -> None:
    gasto = m.Componente(
        "teste.servicos", None, None, None, (_fator(7, "CO2", "0.5", unidade="kg/BRL"),)
    )
    compras = replace(
        _item("10000", "BRL", lancamento=300, componentes=(gasto,)),
        categoria="escopo3.cat01_bens_servicos",
        percentual_consolidado=D(100),
    )
    resultado = m.calcular(_entrada(_item(), compras))
    # 10.000 BRL x 0,5 kg/BRL = 5.000 kg = 5 tCO2e
    assert resultado.escopo3_tco2e == D(5)
    assert resultado.escopo1_tco2e == D("1.11039")
    assert resultado.escopo2_tco2e == 0


def test_versao_1_1_0_continua_refeita_com_o_mesmo_hash() -> None:
    entrada = _entrada(_item())
    antiga = m.calcular(entrada, "1.1.0")
    esperado = m.hash_canonico(
        m.METODO, "1.1.0", antiga.linhas, antiga.pendencias, antiga.escopo1_tco2e,
        antiga.escopo2_tco2e, antiga.biogenico_tco2,
    )  # fmt: skip
    assert antiga.hash_resultado == esperado
    assert m.calcular(entrada).hash_resultado != antiga.hash_resultado


def test_moeda_e_uma_grandeza_propria() -> None:
    from app.modules.calculo.unidades import mesma_dimensao, validar_unidade_de_atividade

    assert str(validar_unidade_de_atividade("BRL")) == "BRL"
    assert not mesma_dimensao("BRL", "kg")
    assert mesma_dimensao("t*km", "kg*km")


# ---------------------------------------------------------------- hash fixo e hash da entrada


def _entrada_de_referencia() -> m.Entrada:
    return replace(
        _entrada(
            _item("1234.5678901234567890123", lancamento=100, periodo=1),
            _item("0.000001", "m**3", lancamento=101, periodo=2),
            _item("1000", lancamento=102, periodo=3),
        ),
        pendencias=(m.Pendencia(FONTE, 4, "falta_dado", "sem dado"),),
    )


def test_hashes_fixos_nao_mudam_com_otimizacao() -> None:
    """Valores tirados do codigo anterior aos atalhos do hash canonico (desempenho, 2026-11-27).
    Se mudarem, calculos gravados deixam de conferir: nunca ajuste o valor para passar."""
    resultado = m.calcular(_entrada_de_referencia())
    assert resultado.hash_entrada.hex() == (
        "82555f8017a9064175a2b5b729d164a09c9f58c1cdab8c5dc066e3e8bf55fa4d"
    )
    assert resultado.hash_resultado.hex() == (
        "977b57ed40e6f1063fef8e7cbec4f8577a464c8f0d8aad769e178d24932787e1"
    )


@settings(deadline=None)
@given(
    st.lists(
        st.decimals(min_value=D(0), max_value=D("1e12"), places=12, allow_nan=False),
        min_size=1,
        max_size=6,
    ),
    st.sampled_from(sorted(m.VERSOES_COMPATIVEIS)),
)
def test_hash_da_entrada_e_o_mesmo_do_calculo(quantidades: list[Decimal], versao: str) -> None:
    itens = [
        _item(str(q), lancamento=100 + i, periodo=1 + i % 12) for i, q in enumerate(quantidades)
    ]
    entrada = _entrada(*reversed(itens))
    assert m.hash_da_entrada(entrada, versao) == m.calcular(entrada, versao).hash_entrada
