from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.core.config import Configuracao, obter_configuracao
from app.modules.saude.servico import verificar_saude

router = APIRouter(tags=["saude"])


@router.get("/vivo")
async def vivo() -> dict[str, str]:
    """O processo responde. Sem consultar dependencia: e a checagem do conteiner no ECS, que
    mata a tarefa quando falha. Com o /saude ali, uma troca de no do banco ou do Redis
    derrubaria todas as tarefas da API ao mesmo tempo."""
    return {"status": "vivo"}


@router.get("/saude")
async def saude(
    resposta: Response,
    configuracao: Annotated[Configuracao, Depends(obter_configuracao)],
) -> dict[str, object]:
    componentes = await verificar_saude(configuracao)
    tudo_ok = all(componentes.values())
    if not tudo_ok:
        resposta.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ok" if tudo_ok else "degradado",
        "componentes": {nome: "ok" if ok else "falha" for nome, ok in componentes.items()},
    }
