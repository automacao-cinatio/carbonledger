"""Validacao de CNPJ (numerico e alfanumerico), CNAE e UF.

CNPJ alfanumerico (Receita Federal, a partir de julho de 2026): as 12 primeiras posicoes
aceitam 0-9 e A-Z; os 2 digitos verificadores continuam numericos. O calculo e o mesmo do
CNPJ numerico, com cada caractere valendo o codigo ASCII menos 48 (A = 17, B = 18...).
"""

import re

UFS = (
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT", "PA",
    "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO",
)  # fmt: skip

_PESOS_DV1 = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
_PESOS_DV2 = (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
_CNPJ = re.compile(r"^[0-9A-Z]{12}[0-9]{2}$")
_CNAE = re.compile(r"^[0-9]{4}-[0-9]/[0-9]{2}$")


def _digito(base: str, pesos: tuple[int, ...]) -> str:
    resto = sum((ord(c) - 48) * p for c, p in zip(base, pesos, strict=True)) % 11
    return "0" if resto < 2 else str(11 - resto)


def normalizar_cnpj(texto: str) -> str:
    """Tira pontuacao e espacos e passa para maiusculas: 12.abc.345/01de-35 -> 12ABC34501DE35."""
    return re.sub(r"[\s./-]", "", texto).upper()


def cnpj_valido(cnpj: str) -> bool:
    if not _CNPJ.fullmatch(cnpj) or len(set(cnpj)) == 1:
        return False
    dv1 = _digito(cnpj[:12], _PESOS_DV1)
    dv2 = _digito(cnpj[:12] + dv1, _PESOS_DV2)
    return cnpj[12:] == dv1 + dv2


def formatar_cnpj(cnpj: str | None) -> str:
    if not cnpj or len(cnpj) != 14:
        return cnpj or ""
    return f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"


def cnae_no_formato(cnae: str) -> bool:
    """So o formato da subclasse (0000-0/00); a lista oficial entra quando estiver em data/."""
    return bool(_CNAE.fullmatch(cnae))
