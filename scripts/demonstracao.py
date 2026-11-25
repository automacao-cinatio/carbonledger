"""Dados de demonstracao na pilha local (make seed), com referencia FICTICIA.

    uv run python scripts/demonstracao.py [--app http://localhost:8000]

Percorre o app como pessoas de verdade: curadoria importa e aprova a referencia ficticia
(tests/dados_ficticios/demonstracao, codigos demo.* e GWP DEMO_FICTICIO_100), um cliente novo
e criado, a administradora entra pelo provedor de dev, cadastra a organizacao, abre o
inventario, cadastra as fontes e lanca os dados; um gestor convidado aprova (quem lanca nao
aprova); as evidencias passam pelo antivirus e pelo S3; o calculo e conferido, o inventario e
fechado e o relatorio sai assinado com um certificado autoassinado. Uma segunda empresa fica
com a coleta pela metade, para o painel.

Unico atalho fora do app: o inventario da demonstracao passa a usar o conjunto de GWP
DEMO_FICTICIO_100 (o padrao e AR5_100, que fica so para o GWP oficial), direto no banco local.

So para desenvolvimento: recusa rodar se a API estiver em producao. Cada execucao cria um
cliente novo. Ver docs/runbooks/demonstracao.md.
"""

import argparse
import json
import re
import shutil
import subprocess  # nosec B404  # docker compose com lista de argumentos, sem shell
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import UUID as UUIDValido

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

RAIZ = Path(__file__).resolve().parents[1]
PASTA_REFERENCIA = RAIZ / "tests" / "dados_ficticios" / "demonstracao"
MAILPIT = "http://localhost:8025"
ANO = 2025
CNPJ_EMPRESA = "11222333000181"
CNPJ_SEGUNDA = "11444777000161"
SENHA_CERTIFICADO = "demonstracao"
UUID = r"[0-9a-f-]{36}"


class ErroDemonstracao(RuntimeError):
    pass


def _docker(*argumentos: str) -> str:
    docker = shutil.which("docker")
    if docker is None:
        raise ErroDemonstracao("docker nao encontrado; abra o Docker Desktop")
    resultado = subprocess.run(  # noqa: S603  # nosec B603
        [docker, "compose", *argumentos], capture_output=True, cwd=RAIZ
    )
    saida = resultado.stdout.decode("utf-8", "replace")
    if resultado.returncode != 0:
        erro = resultado.stderr.decode("utf-8", "replace").strip().splitlines()[-5:]
        raise ErroDemonstracao(f"docker compose {argumentos[0]}: {saida} {' | '.join(erro)}")
    return saida


# ---------------------------------------------------------------- referencia ficticia


def curadoria() -> None:
    montagem = f"{PASTA_REFERENCIA.resolve().as_posix()}:/app/demonstracao:ro"
    for tipo in ("gwp", "fatores", "misturas"):
        saida = _docker(
            "run", "--rm", "--no-deps", "-v", montagem, "curadoria", "fatores", "importar",
            "--tipo", tipo, "--por", "Demonstracao (curadoria)", f"/app/demonstracao/{tipo}.csv",
        )  # fmt: skip
        lote = re.search(rf"Lote ({UUID}) importado", saida)
        if lote is None:
            print(f"  {tipo}: ja importado antes")
            continue
        _docker(
            "run", "--rm", "--no-deps", "curadoria", "fatores", "aprovar", lote.group(1),
            "--responsavel", "Demonstracao (aprovacao)",
        )  # fmt: skip
        print(f"  {tipo}: lote {lote.group(1)} importado e aprovado")
    # Lote de demonstracao que ficou pendente numa execucao interrompida: aprova agora.
    for linha in _docker("run", "--rm", "--no-deps", "curadoria", "fatores", "lotes").splitlines():
        if "pendente" in linha and "/app/demonstracao/" in linha:
            _docker(
                "run", "--rm", "--no-deps", "curadoria", "fatores", "aprovar", linha.split()[0],
                "--responsavel", "Demonstracao (aprovacao)",
            )  # fmt: skip


# ---------------------------------------------------------------- pessoas e portal


class Pessoa:
    """Uma pessoa logada no app pelo provedor de dev, com cookies proprios."""

    def __init__(self, app: str, sujeito: str, email: str) -> None:
        self.app = app.rstrip("/")
        self.sujeito = sujeito
        self.email = email
        self.cliente = httpx.Client(base_url=self.app, follow_redirects=False, timeout=60)

    def entrar(self, inicio: str) -> None:
        ida = self.cliente.get(inicio)
        if ida.status_code != 303:
            raise ErroDemonstracao(f"login nao comecou ({ida.status_code}) em {inicio}")
        claims = {"email": self.email, "email_verified": True, "name": self.sujeito.title()}
        no_provedor = httpx.post(
            ida.headers["location"],
            data={"username": self.sujeito, "claims": json.dumps(claims)},
            follow_redirects=False,
            timeout=30,
        )
        if no_provedor.status_code != 302:
            raise ErroDemonstracao(f"provedor de dev recusou ({no_provedor.status_code})")
        volta = urlsplit(no_provedor.headers["location"])
        retorno = self.cliente.get(f"{volta.path}?{volta.query}")
        if retorno.status_code not in (302, 303):
            raise ErroDemonstracao(f"retorno do login falhou ({retorno.status_code})")

    def _csrf(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.cliente.get("/sessao").json()["csrf"]}

    def post(
        self, caminho: str, dados: dict[str, str] | None = None, **extra: Any
    ) -> httpx.Response:
        resposta = self.cliente.post(caminho, data=dados or {}, headers=self._csrf(), **extra)
        if resposta.status_code >= 400:
            detalhe = resposta.text[:300]
            raise ErroDemonstracao(f"POST {caminho}: {resposta.status_code} {detalhe}")
        return resposta

    def criar(self, caminho: str, dados: dict[str, str]) -> str:
        """POST que redireciona para o que foi criado: devolve o id do fim da URL."""
        return self.post(caminho, dados).headers["hx-redirect"].rsplit("/", 1)[-1]

    def pagina(self, caminho: str) -> str:
        resposta = self.cliente.get(caminho)
        if resposta.status_code != 200:
            raise ErroDemonstracao(f"GET {caminho}: {resposta.status_code}")
        return resposta.text


def _achar(padrao: str, texto: str) -> str:
    achado = re.search(padrao, texto)
    if achado is None:
        raise ErroDemonstracao(f"nao achei {padrao!r} na pagina")
    return achado.group(1)


def _aviso(resposta: httpx.Response) -> str:
    return unquote(resposta.headers.get("x-aviso", ""))


def _link_do_ultimo_email(para: str) -> str:
    mensagens = httpx.get(f"{MAILPIT}/api/v1/search", params={"query": f"to:{para}"}).json()
    if not mensagens.get("messages"):
        raise ErroDemonstracao(f"convite para {para} nao chegou ao Mailpit")
    ultima = mensagens["messages"][0]["ID"]
    texto = httpx.get(f"{MAILPIT}/api/v1/message/{ultima}").json()["Text"]
    achado = re.search(r"https?://[^/\s]+(/convite/\S+/dev)", texto)
    if achado is None:
        raise ErroDemonstracao("link de convite nao encontrado no e-mail")
    return achado.group(1)


# ---------------------------------------------------------------- arquivos


def _pdf_de_evidencia(titulo: str) -> bytes:
    texto = f"Evidencia FICTICIA da demonstracao: {titulo}".encode("latin-1")
    conteudo = b"BT /F1 12 Tf 50 780 Td (" + texto + b") Tj ET"
    objetos = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842]"
        b" /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(conteudo)).encode() + b" >>\nstream\n" + conteudo
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]  # fmt: skip
    saida = b"%PDF-1.4\n"
    posicoes = []
    for numero, objeto in enumerate(objetos, start=1):
        posicoes.append(len(saida))
        saida += f"{numero} 0 obj\n".encode() + objeto + b"\nendobj\n"
    xref = len(saida)
    saida += f"xref\n0 {len(objetos) + 1}\n0000000000 65535 f \n".encode()
    saida += b"".join(f"{p:010d} 00000 n \n".encode() for p in posicoes)
    saida += f"trailer\n<< /Size {len(objetos) + 1} /Root 1 0 R >>\n".encode()
    return saida + f"startxref\n{xref}\n%%EOF\n".encode()


def _certificado(nome: str, cnpj: str) -> bytes:
    """A1 autoassinado com o CNPJ no campo do e-CNPJ ICP-Brasil (so dev aceita sem cadeia)."""
    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sujeito = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{nome} (FICTICIO)")])
    cnpj_der = bytes([0x13, len(cnpj)]) + cnpj.encode("ascii")
    inicio = datetime.now(UTC) - timedelta(days=1)
    certificado = (
        x509.CertificateBuilder()
        .subject_name(sujeito)
        .issuer_name(sujeito)
        .public_key(chave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(inicio)
        .not_valid_after(inicio + timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.OtherName(x509.ObjectIdentifier("2.16.76.1.3.3"), cnpj_der)]
            ),
            critical=False,
        )
        .sign(chave, hashes.SHA256())
    )
    return pkcs12.serialize_key_and_certificates(
        b"demonstracao",
        chave,
        certificado,
        None,
        serialization.BestAvailableEncryption(SENHA_CERTIFICADO.encode()),
    )


# ---------------------------------------------------------------- roteiro

DIESEL = [1180, 1105, 1250, 1320, 1290, 1410, 1385, 1440, 1300, 1270, 1195, 1220]
ENERGIA = ["84,2", "80,5", "88,1", "86,9", "90,4", "92,8", "95,1", "97,3", "91,6", "89,0",
           "85,7", "83,9"]  # fmt: skip
VIAGENS_KM = "48.300"


def _lancar(pessoa: Pessoa, fonte: str, periodos: list[int], valores: list[Any]) -> None:
    for periodo, valor in zip(periodos, valores, strict=True):
        dados = {"quantidade": str(valor)}
        if periodo:
            dados["data_compra"] = f"{ANO}-{periodo:02d}-10"
        pessoa.post(f"/coleta/fontes/{fonte}/periodos/{periodo}/lancamentos", dados)


def _aprovar_tudo(pessoa: Pessoa, fonte: str, periodos: list[int]) -> None:
    for periodo in periodos:
        pagina = pessoa.pagina(f"/coleta/fontes/{fonte}/periodos/{periodo}")
        for lancamento in set(re.findall(rf"/coleta/lancamentos/({UUID})/aprovar", pagina)):
            pessoa.post(f"/coleta/lancamentos/{lancamento}/aprovar")


def _usar_gwp_ficticio(inventario: str) -> None:
    inventario = str(UUIDValido(inventario))  # so um uuid entra no SQL abaixo
    sql = f"UPDATE inventario SET gwp_conjunto = 'DEMO_FICTICIO_100' WHERE inventario_id = '{inventario}'"  # noqa: S608, E501  # nosec B608
    _docker(
        "exec", "-T", "postgres", "sh", "-c",
        f'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "{sql}"',
    )  # fmt: skip


def roteiro(app: str) -> None:
    saude = httpx.get(f"{app}/saude", timeout=10)
    if saude.status_code != 200:
        raise ErroDemonstracao(f"API fora do ar em {app}: suba a pilha com make up")
    if httpx.get(f"{app}/ui", headers={"accept": "application/json"}).status_code == 404:
        raise ErroDemonstracao("a API parece estar em producao (/ui some): demonstracao recusada")

    print("1. Referencia ficticia (curadoria importa e aprova)")
    curadoria()

    print("2. Cliente novo e administradora")
    carimbo = datetime.now(UTC).strftime("%d/%m %H:%M")
    saida = _docker(
        "run", "--rm", "--no-deps", "api", "python", "-m", "app.cli", "criar-tenant",
        "--nome", f"Demonstracao FICTICIA {carimbo}", "--email", "dona@demonstracao.example",
    )  # fmt: skip
    convite = re.search(r"(/convite/\S+/dev)", saida)
    if convite is None:
        raise ErroDemonstracao("o convite de dev nao saiu: confira OIDC_DEV_EMISSOR no .env")
    dona = Pessoa(app, "dona-demonstracao", "dona@demonstracao.example")
    dona.entrar(convite.group(1))

    print("3. Organizacao")
    dona.post("/organizacao/grupos", {"nome": "Grupo Demonstracao"})
    grupo = _achar(rf"/organizacao/grupos/({UUID})/editar", dona.pagina("/organizacao"))
    empresa = dona.criar("/organizacao/empresas", {
        "grupo_id": grupo, "cnpj": CNPJ_EMPRESA, "nome": "Metalurgica Exemplo S.A.",
        "uf": "SP", "municipio": "Campinas"})  # fmt: skip
    for nome, municipio, participacao in (
        ("Fabrica Campinas", "Campinas", "100"),
        ("Centro de distribuicao", "Jundiai", "100"),
    ):
        dona.post(f"/organizacao/empresas/{empresa}/unidades", {
            "nome": nome, "uf": "SP", "municipio": municipio,
            "participacao_societaria": participacao, "controle_operacional": "sim"})  # fmt: skip
    unidade = _achar(
        rf"/organizacao/unidades/({UUID})/editar", dona.pagina(f"/organizacao/empresas/{empresa}")
    )
    segunda = dona.criar("/organizacao/empresas", {
        "grupo_id": grupo, "cnpj": CNPJ_SEGUNDA, "nome": "Transportes Exemplo Ltda.",
        "uf": "MG", "municipio": "Contagem"})  # fmt: skip
    dona.post(f"/organizacao/empresas/{segunda}/unidades", {
        "nome": "Garagem Contagem", "uf": "MG", "municipio": "Contagem",
        "participacao_societaria": "100", "controle_operacional": "sim"})  # fmt: skip
    unidade_segunda = _achar(
        rf"/organizacao/unidades/({UUID})/editar", dona.pagina(f"/organizacao/empresas/{segunda}")
    )

    print(f"4. Inventario {ANO}, fontes e dados")
    inventario = dona.criar(f"/coleta/empresas/{empresa}/inventarios",
                            {"ano": str(ANO), "abordagem": "controle_operacional"})  # fmt: skip
    _usar_gwp_ficticio(inventario)
    base = {"unidade_id": unidade, "material": "sim"}
    caldeira = dona.criar(f"/coleta/inventarios/{inventario}/fontes", {
        **base, "categoria": "escopo1.combustao_estacionaria", "descricao": "Caldeira a diesel",
        "unidade_medida": "L", "periodicidade": "mensal",
        "atividade": "demo.diesel_comercial"})  # fmt: skip
    energia = dona.criar(f"/coleta/inventarios/{inventario}/fontes", {
        **base, "categoria": "escopo2.energia_eletrica", "descricao": "Energia da fabrica",
        "unidade_medida": "MWh", "periodicidade": "mensal",
        "atividade": "demo.energia_rede"})  # fmt: skip
    viagens = dona.criar(f"/coleta/inventarios/{inventario}/fontes", {
        **base, "categoria": "escopo3.cat06_viagens", "descricao": "Viagens aereas da equipe",
        "unidade_medida": "km", "periodicidade": "anual", "atividade": "demo.voo"})  # fmt: skip
    meses = list(range(1, 13))
    _lancar(dona, caldeira, meses, DIESEL)
    _lancar(dona, energia, meses, ENERGIA)
    _lancar(dona, viagens, [0], [VIAGENS_KM])
    for fonte, titulo in ((caldeira, "notas de diesel"), (energia, "contas de energia"),
                          (viagens, "relatorio de viagens")):  # fmt: skip
        dona.post(f"/coleta/fontes/{fonte}/evidencias", {"periodo": ""},
                  files={"arquivo": (f"{titulo}.pdf", _pdf_de_evidencia(titulo),
                                     "application/pdf")})  # fmt: skip

    print("5. Gestor convidado aprova os dados (quem lanca nao aprova)")
    dona.post("/usuarios/convites", {"email": "gestor@demonstracao.example", "perfil": "gestor"})
    gestor = Pessoa(app, "gestor-demonstracao", "gestor@demonstracao.example")
    gestor.entrar(_link_do_ultimo_email("gestor@demonstracao.example"))
    _aprovar_tudo(gestor, caldeira, meses)
    _aprovar_tudo(gestor, energia, meses)
    _aprovar_tudo(gestor, viagens, [0])

    print("6. Calculo, conferencia e fechamento")
    dona.post(f"/resultados/inventarios/{inventario}/calcular")
    calculo = _achar(rf"/resultados/calculos/({UUID})", dona.pagina("/resultados"))
    dona.post(f"/resultados/calculos/{calculo}/conferir")
    resultado = dona.post(f"/fechamento/inventarios/{inventario}/fechar",
                          {"confirmacao": "Metalurgica Exemplo S.A."})  # fmt: skip
    print(f"   {_aviso(resultado) or 'inventario fechado'}")

    print("7. Certificado da empresa e relatorio assinado")
    dona.post(f"/relatorios/empresas/{empresa}/certificado", {"senha": SENHA_CERTIFICADO},
              files={"arquivo": ("metalurgica.pfx", _certificado("METALURGICA EXEMPLO SA",
                                                                 CNPJ_EMPRESA),
                                 "application/x-pkcs12")})  # fmt: skip
    dona.post(f"/relatorios/inventarios/{inventario}/gerar")

    print("8. Segunda empresa com a coleta pela metade")
    inventario_2 = dona.criar(f"/coleta/empresas/{segunda}/inventarios",
                              {"ano": str(ANO), "abordagem": "controle_operacional"})  # fmt: skip
    _usar_gwp_ficticio(inventario_2)
    frota = dona.criar(f"/coleta/inventarios/{inventario_2}/fontes", {
        "unidade_id": unidade_segunda, "material": "sim",
        "categoria": "escopo1.combustao_estacionaria", "descricao": "Gerador da garagem",
        "unidade_medida": "L", "periodicidade": "mensal",
        "atividade": "demo.diesel_comercial"})  # fmt: skip
    _lancar(dona, frota, list(range(1, 7)), [410, 395, 430, 450, 470, 455])
    _aprovar_tudo(gestor, frota, list(range(1, 4)))

    print()
    print(f"Pronto. Abra {app} e entre pelo provedor de desenvolvimento como:")
    print("  administradora: usuario 'dona-demonstracao', e-mail dona@demonstracao.example")
    print("  gestor:         usuario 'gestor-demonstracao', e-mail gestor@demonstracao.example")
    print("Todos os fatores, GWP e misturas da demonstracao sao FICTICIOS.")


def main(argumentos: list[str] | None = None) -> int:
    analisador = argparse.ArgumentParser(prog="scripts/demonstracao.py")
    analisador.add_argument("--app", default="http://localhost:8000")
    args = analisador.parse_args(argumentos)
    try:
        roteiro(args.app)
    except ErroDemonstracao as erro:
        print(f"Erro: {erro}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
