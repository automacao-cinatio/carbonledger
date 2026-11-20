from uuid import UUID

import pytest

from app.core.tenancy import ContextoInvalido, ContextoTenant, Escopo, aplicar_contexto

T = UUID("11111111-1111-4111-8111-111111111111")
E1 = UUID("22222222-2222-4222-8222-222222222222")
E2 = UUID("33333333-3333-4333-8333-333333333333")
U = UUID("44444444-4444-4444-8444-444444444444")


def test_configuracoes_do_escopo_tenant() -> None:
    contexto = ContextoTenant(tenant_id=T, escopo=Escopo.TENANT, usuario_id=U)
    assert contexto.como_configuracoes() == {
        "app.tenant_id": str(T),
        "app.escopo": "tenant",
        "app.empresas": "{}",
        "app.usuario_id": str(U),
    }


def test_empresas_em_ordem_estavel() -> None:
    contexto = ContextoTenant(tenant_id=T, escopo=Escopo.EMPRESAS, empresas=frozenset({E2, E1}))
    assert contexto.como_configuracoes()["app.empresas"] == "{" + f"{E1},{E2}" + "}"
    assert contexto.como_configuracoes()["app.usuario_id"] == ""


def test_escopo_tenant_nao_aceita_lista_de_empresas() -> None:
    with pytest.raises(ContextoInvalido):
        ContextoTenant(tenant_id=T, escopo=Escopo.TENANT, empresas=frozenset({E1}))


@pytest.mark.parametrize(
    "argumentos",
    [
        {"tenant_id": str(T), "escopo": Escopo.TENANT},
        {"tenant_id": T, "escopo": "tenant"},
        {"tenant_id": T, "escopo": Escopo.EMPRESAS, "empresas": frozenset({str(E1)})},
    ],
)
def test_tipos_errados_sao_recusados(argumentos: dict[str, object]) -> None:
    with pytest.raises(ContextoInvalido):
        ContextoTenant(**argumentos)  # type: ignore[arg-type]


class _ConexaoFalsa:
    def __init__(self, em_transacao: bool) -> None:
        self.em_transacao = em_transacao
        self.chamadas: list[tuple[object, ...]] = []

    def is_in_transaction(self) -> bool:
        return self.em_transacao

    async def execute(self, *argumentos: object) -> str:
        self.chamadas.append(argumentos)
        return "SELECT 1"


async def test_aplicar_contexto_usa_set_config_local() -> None:
    conexao = _ConexaoFalsa(em_transacao=True)
    await aplicar_contexto(conexao, ContextoTenant(tenant_id=T, escopo=Escopo.TENANT))
    ((sql, *valores),) = conexao.chamadas
    assert isinstance(sql, str)
    assert sql.count(", true)") == 4
    assert "SET " not in sql.upper().replace("SET_CONFIG", "")
    assert valores == [str(T), "tenant", "{}", ""]


async def test_aplicar_contexto_fora_de_transacao_e_recusado() -> None:
    conexao = _ConexaoFalsa(em_transacao=False)
    with pytest.raises(ContextoInvalido):
        await aplicar_contexto(conexao, ContextoTenant(tenant_id=T, escopo=Escopo.TENANT))
    assert conexao.chamadas == []
