"""Cliente grande na pilha local, para medir o desempenho com volume (so desenvolvimento).

    uv run python scripts/volume.py gerar [--empresas 12] [--fontes 30] [--anos 2]
    uv run python scripts/volume.py medir --email <e-mail que o gerar imprimiu>

gerar: como a demonstracao (scripts/demonstracao.py), percorre o app pelo HTTP, com a mesma
referencia FICTICIA: cliente novo, grupo, empresas com tres unidades, um inventario por ano,
fontes mensais e anuais, dados pela importacao da planilha (um envio por inventario) e
aprovacao de cada dado por um gestor, em paralelo. O ano mais antigo de cada empresa sai
calculado, conferido e fechado, com uma evidencia por fonte; o mais recente fica calculado e
aberto. Atalhos fora do app, so de leitura ou de ambiente: o GWP ficticio no inventario (como
na demonstracao) e a lista dos dados pendentes lida do banco, para o gestor nao abrir pagina a
pagina.

medir: entra como a administradora do cliente grande e mede cada tela (mediana de varias
leituras) e as acoes pesadas (calcular, importar planilha, verificar a trilha). Ver
docs/runbooks/desempenho.md.
"""

import argparse
import csv
import io
import re
import statistics
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any
from uuid import UUID as UUIDValido

import httpx
from demonstracao import (
    ANO,
    UUID,
    ErroDemonstracao,
    Pessoa,
    _achar,
    _docker,
    _link_do_ultimo_email,
    _pdf_de_evidencia,
    _usar_gwp_ficticio,
    curadoria,
)

# Atividades da referencia ficticia: (categoria, atividade, unidade, periodicidade, faixa).
TIPOS_DE_FONTE = (
    ("escopo1.combustao_estacionaria", "demo.diesel_comercial", "L", "mensal", (300, 2500)),
    ("escopo2.energia_eletrica", "demo.energia_rede", "MWh", "mensal", (20, 140)),
    ("escopo1.combustao_estacionaria", "demo.diesel_fossil", "L", "mensal", (100, 900)),
    ("escopo3.cat06_viagens", "demo.voo", "km", "anual", (5000, 90000)),
)
UNIDADES = (("Fabrica", "Campinas"), ("Escritorio", "Sao Paulo"), ("Deposito", "Jundiai"))


def cnpj_valido(base: int) -> str:
    """CNPJ com digitos verificadores certos, a partir de um numero de 8 digitos."""
    numeros = [int(d) for d in f"{base:08d}0001"]
    for pesos in ((5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2), (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)):
        resto = sum(n * p for n, p in zip(numeros, pesos, strict=True)) % 11
        numeros.append(0 if resto < 2 else 11 - resto)
    return "".join(str(n) for n in numeros)


def quantidade(semente: int, faixa: tuple[int, int]) -> str:
    """Valor deterministico na faixa, com vírgula decimal (formato da planilha)."""
    menor, maior = faixa
    valor = menor + (semente * 7919) % (maior - menor)
    return f"{valor},{semente % 10}"


class PessoaRapida(Pessoa):
    """Pessoa que guarda o token CSRF (a demonstracao busca um por envio)."""

    _token: dict[str, str] | None = None

    def _csrf(self) -> dict[str, str]:
        if self._token is None:
            self._token = super()._csrf()
        return self._token


def _psql(sql: str) -> list[str]:
    saida = _docker(
        "exec", "-T", "postgres", "sh", "-c",
        f'psql -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "{sql}"',
    )  # fmt: skip
    return [linha for linha in saida.splitlines() if linha.strip()]


SQL_PENDENTES = (
    "SELECT l.lancamento_id FROM lancamento l JOIN fonte_emissao f"
    " ON f.tenant_id = l.tenant_id AND f.fonte_id = l.fonte_id"
    " WHERE f.inventario_id = :inventario AND NOT EXISTS (SELECT 1 FROM lancamento_decisao d"
    " WHERE d.tenant_id = l.tenant_id AND d.lancamento_id = l.lancamento_id)"
)


def _pendentes(inventario: str) -> list[str]:
    inventario = str(UUIDValido(inventario))  # so um uuid entra no SQL abaixo
    return _psql(SQL_PENDENTES.replace(":inventario", f"'{inventario}'"))


def _preencher_planilha(modelo: str, semente: int) -> bytes:
    leitor = csv.DictReader(io.StringIO(modelo.lstrip("﻿")), delimiter=";")
    faixas = {t[2]: t[4] for t in TIPOS_DE_FONTE}
    saida = io.StringIO()
    escritor = csv.DictWriter(saida, fieldnames=leitor.fieldnames or [], delimiter=";")
    escritor.writeheader()
    for numero, linha in enumerate(leitor):
        faixa = faixas.get(linha["unidade_medida"], (1, 1000))
        linha["quantidade"] = quantidade(semente + numero, faixa)
        escritor.writerow(linha)
    return saida.getvalue().encode("utf-8")


def _aprovar(gestores: list[PessoaRapida], lancamentos: list[str]) -> None:
    """Cada gestor (uma sessao propria) aprova uma fatia dos dados, em paralelo."""
    paralelo = len(gestores)

    def lote(indice: int) -> None:
        for lancamento in lancamentos[indice::paralelo]:
            gestores[indice].post(f"/coleta/lancamentos/{lancamento}/aprovar")

    with ThreadPoolExecutor(paralelo) as executor:
        list(executor.map(lote, range(paralelo)))


def gerar(app: str, empresas: int, fontes: int, anos: int, paralelo: int) -> None:
    if httpx.get(f"{app}/ui", headers={"accept": "application/json"}).status_code == 404:
        raise ErroDemonstracao("a API parece estar em producao (/ui some): recusado")
    inicio = time.monotonic()
    print("1. Referencia ficticia")
    curadoria()

    carimbo = datetime.now(UTC).strftime("%m%d%H%M")
    email = f"dona-{carimbo}@volume.example"
    email_gestor = f"gestor-{carimbo}@volume.example"
    print(f"2. Cliente novo ({email})")
    saida = _docker(
        "run", "--rm", "--no-deps", "api", "python", "-m", "app.cli", "criar-tenant",
        "--nome", f"Volume FICTICIO {carimbo}", "--email", email,
    )  # fmt: skip
    convite = re.search(r"(/convite/\S+/dev)", saida)
    if convite is None:
        raise ErroDemonstracao("o convite de dev nao saiu: confira OIDC_DEV_EMISSOR no .env")
    dona = PessoaRapida(app, f"dona-{carimbo}", email)
    dona.entrar(convite.group(1))
    dona.post("/usuarios/convites", {"email": email_gestor, "perfil": "gestor"})
    gestor = PessoaRapida(app, f"gestor-{carimbo}", email_gestor)
    gestor.entrar(_link_do_ultimo_email(email_gestor))
    # Sessoes do gestor abertas uma vez so: o app limita os inicios de login por IP.
    gestores = [gestor]
    for _ in range(paralelo - 1):
        outro = PessoaRapida(app, f"gestor-{carimbo}", email_gestor)
        outro.entrar("/entrar/dev")
        gestores.append(outro)

    dona.post("/organizacao/grupos", {"nome": f"Grupo Volume {carimbo}"})
    grupo = _achar(rf"/organizacao/grupos/({UUID})/editar", dona.pagina("/organizacao"))
    base_cnpj = int(carimbo) * 100 % 90_000_000 + 10_000_000
    anos_inventario = [ANO - anos + 1 + i for i in range(anos)]
    total = 0
    for e in range(empresas):
        nome = f"Empresa Volume {e + 1:02d} {carimbo}"
        empresa = dona.criar("/organizacao/empresas", {
            "grupo_id": grupo, "cnpj": cnpj_valido(base_cnpj + e), "nome": nome,
            "uf": "SP", "municipio": "Campinas"})  # fmt: skip
        for unidade_nome, municipio in UNIDADES:
            dona.post(f"/organizacao/empresas/{empresa}/unidades", {
                "nome": unidade_nome, "uf": "SP", "municipio": municipio,
                "participacao_societaria": "100", "controle_operacional": "sim"})  # fmt: skip
        unidades = re.findall(rf"/organizacao/unidades/({UUID})/editar",
                              dona.pagina(f"/organizacao/empresas/{empresa}"))  # fmt: skip
        for ano in anos_inventario:
            inventario = dona.criar(
                f"/coleta/empresas/{empresa}/inventarios",
                {"ano": str(ano), "abordagem": "controle_operacional"},
            )
            _usar_gwp_ficticio(inventario)
            ids_fontes = []
            for f in range(fontes):
                categoria, atividade, medida, periodicidade, _faixa = TIPOS_DE_FONTE[
                    f % len(TIPOS_DE_FONTE)
                ]
                dados_fonte = {
                    "unidade_id": unidades[f % len(unidades)], "material": "sim",
                    "categoria": categoria, "descricao": f"Fonte {f + 1:02d} ({atividade})",
                    "unidade_medida": medida, "periodicidade": periodicidade,
                    "atividade": atividade}  # fmt: skip
                ids_fontes.append(
                    dona.criar(f"/coleta/inventarios/{inventario}/fontes", dados_fonte)
                )
            modelo = dona.cliente.get(f"/coleta/inventarios/{inventario}/planilha.csv").text
            planilha = _preencher_planilha(modelo.replace("\r\n", "\n"), e * 1000 + ano)
            planilha = _datas_de_compra(planilha, ano)
            dona.post(f"/coleta/inventarios/{inventario}/planilha",
                      files={"arquivo": ("coleta.csv", planilha, "text/csv")})  # fmt: skip
            pendentes = _pendentes(inventario)
            total += len(pendentes)
            _aprovar(gestores, pendentes)
            fechar = ano == anos_inventario[0] and anos > 1
            if fechar:
                for fonte in ids_fontes:
                    dona.post(f"/coleta/fontes/{fonte}/evidencias", {"periodo": ""},
                              files={"arquivo": ("evidencia.pdf", _pdf_de_evidencia(nome),
                                                 "application/pdf")})  # fmt: skip
            calculo = dona.criar(f"/resultados/inventarios/{inventario}/calcular", {})
            if fechar:
                dona.post(f"/resultados/calculos/{calculo}/conferir")
                dona.post(f"/fechamento/inventarios/{inventario}/fechar", {"confirmacao": nome})
            situacao = ", fechado" if fechar else ""
            decorrido = time.monotonic() - inicio
            print(
                f"   {nome} {ano}: {len(pendentes)} dados aprovados{situacao} ({decorrido:.0f} s)"
            )
    print()
    print(f"Pronto: {empresas} empresas, {total} dados, {time.monotonic() - inicio:.0f} s.")
    print(f"Para medir: uv run python scripts/volume.py medir --email {email}")


def _datas_de_compra(planilha: bytes, ano: int) -> bytes:
    """Combustivel pede a data da compra (mistura pela data): dia 10 do mes do periodo."""
    leitor = csv.DictReader(io.StringIO(planilha.decode("utf-8")), delimiter=";")
    saida = io.StringIO()
    escritor = csv.DictWriter(saida, fieldnames=leitor.fieldnames or [], delimiter=";")
    escritor.writeheader()
    for linha in leitor:
        if linha["unidade_medida"] == "L":
            mes = 6 if linha["periodo"] == "ano" else int(linha["periodo"])
            linha["data_compra"] = f"10/{mes:02d}/{ano}"
        escritor.writerow(linha)
    return saida.getvalue().encode("utf-8")


# ---------------------------------------------------------------- medicao


def _cronometrar(acao: Callable[[], Any], vezes: int) -> tuple[float, float]:
    tempos = []
    for _ in range(vezes):
        inicio = time.perf_counter()
        acao()
        tempos.append((time.perf_counter() - inicio) * 1000)
    return statistics.median(tempos), max(tempos)


def medir(app: str, email: str, vezes: int) -> None:
    sujeito = email.split("@", 1)[0]
    dona = PessoaRapida(app, sujeito, email)
    dona.entrar("/entrar/dev")
    coleta = dona.pagina("/coleta")
    inventario = _achar(rf"/coleta/inventarios/({UUID})", coleta)
    inventario_pagina = dona.pagina(f"/coleta/inventarios/{inventario}")
    empresa = _achar(rf"/organizacao/empresas/({UUID})", dona.pagina("/organizacao"))
    fonte = _achar(rf"/coleta/fontes/({UUID})", inventario_pagina)
    calculo = _achar(rf"/resultados/calculos/({UUID})", dona.pagina("/resultados"))

    def get(caminho: str) -> Callable[[], Any]:
        def acao() -> None:
            resposta = dona.cliente.get(caminho)
            if resposta.status_code != 200:
                raise ErroDemonstracao(f"GET {caminho}: {resposta.status_code}")

        return acao

    telas = {
        "Painel": "/painel",
        "Organizacao": "/organizacao",
        "Empresa": f"/organizacao/empresas/{empresa}",
        "Coleta": "/coleta",
        "Inventario (coleta)": f"/coleta/inventarios/{inventario}",
        "Planilha modelo": f"/coleta/inventarios/{inventario}/planilha.csv",
        "Fonte": f"/coleta/fontes/{fonte}",
        "Periodo da fonte": f"/coleta/fontes/{fonte}/periodos/1",
        "Resultados": "/resultados",
        "Calculo": f"/resultados/calculos/{calculo}",
        "Memoria de calculo (CSV)": f"/resultados/calculos/{calculo}/memoria.csv",
        "Fechamento": "/fechamento",
        "Relatorios": "/relatorios",
        "Metas": "/metas",
        "Integridade": "/integridade",
        "Verificacao": "/verificacao",
        "Usuarios": "/usuarios",
    }
    print(f"{'Tela':<28}{'mediana ms':>12}{'maximo ms':>12}")
    for nome, caminho in telas.items():
        mediana, maximo = _cronometrar(get(caminho), vezes)
        print(f"{nome:<28}{mediana:>12.0f}{maximo:>12.0f}")

    print()
    print(f"{'Acao':<28}{'mediana ms':>12}{'maximo ms':>12}")
    abertos = re.findall(rf"/resultados/inventarios/({UUID})/calcular", dona.pagina("/resultados"))
    cliques: list[float] = []

    def verificar_ate_o_fim() -> None:
        """Clique em Verificar agora e espera o worker concluir (a tela deixa de mostrar)."""
        inicio = time.perf_counter()
        dona.post("/integridade/verificar")
        cliques.append((time.perf_counter() - inicio) * 1000)
        while "Verificação em andamento" in dona.pagina("/integridade"):
            time.sleep(0.5)

    acoes: dict[str, Callable[[], Any]] = {"Verificacao no worker": verificar_ate_o_fim}
    if abertos:
        acoes["Calcular inventario"] = lambda: dona.post(
            f"/resultados/inventarios/{abertos[0]}/calcular"
        )
    modelo = dona.cliente.get(f"/coleta/inventarios/{inventario}/planilha.csv").content
    acoes["Importar planilha igual"] = lambda: dona.post(
        f"/coleta/inventarios/{inventario}/planilha",
        files={"arquivo": ("coleta.csv", modelo, "text/csv")},
    )
    for nome, acao in acoes.items():
        mediana, maximo = _cronometrar(acao, max(1, vezes // 2))
        print(f"{nome:<28}{mediana:>12.0f}{maximo:>12.0f}")
    nome_clique = "Pedir verificacao (clique)"
    print(f"{nome_clique:<28}{statistics.median(cliques):>12.0f}{max(cliques):>12.0f}")


def main(argumentos: list[str] | None = None) -> int:
    analisador = argparse.ArgumentParser(prog="scripts/volume.py")
    analisador.add_argument("--app", default="http://127.0.0.1:8000")
    comandos = analisador.add_subparsers(dest="comando", required=True)
    g = comandos.add_parser("gerar")
    g.add_argument("--empresas", type=int, default=12)
    g.add_argument("--fontes", type=int, default=30)
    g.add_argument("--anos", type=int, default=2)
    g.add_argument("--paralelo", type=int, default=4)
    m = comandos.add_parser("medir")
    m.add_argument("--email", required=True)
    m.add_argument("--vezes", type=int, default=5)
    args = analisador.parse_args(argumentos)
    try:
        if args.comando == "gerar":
            gerar(args.app, args.empresas, args.fontes, args.anos, args.paralelo)
        else:
            medir(args.app, args.email, args.vezes)
    except ErroDemonstracao as erro:
        print(f"Erro: {erro}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
