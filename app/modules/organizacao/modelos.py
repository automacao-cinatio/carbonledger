"""Tabelas da estrutura: tenant, grupo, empresa e unidade (espelho das migracoes 0001 e 0004)."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Numeric, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Tenant(Base):
    """Cliente da plataforma. O proprio tenant_id e a chave, para o RLS ser uniforme."""

    __tablename__ = "tenant"

    tenant_id: Mapped[UUID] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(Text)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Grupo(Base):
    __tablename__ = "grupo"

    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenant.tenant_id"), primary_key=True)
    grupo_id: Mapped[UUID] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(Text)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Empresa(Base):
    """Empresa inventariada (CNPJ da matriz). Chaves compostas impedem cruzar tenants."""

    __tablename__ = "empresa"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "grupo_id"], ["grupo.tenant_id", "grupo.grupo_id"]),
    )

    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenant.tenant_id"), primary_key=True)
    empresa_id: Mapped[UUID] = mapped_column(primary_key=True)
    grupo_id: Mapped[UUID]
    nome: Mapped[str] = mapped_column(Text)
    nome_fantasia: Mapped[str | None] = mapped_column(Text)
    cnpj: Mapped[str | None] = mapped_column(Text)
    cnae: Mapped[str | None] = mapped_column(Text)
    uf: Mapped[str | None] = mapped_column(Text)
    municipio: Mapped[str | None] = mapped_column(Text)
    ativa: Mapped[bool] = mapped_column(server_default="true")
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Unidade(Base):
    """Estabelecimento ou instalacao, com os fatos dos limites organizacionais."""

    __tablename__ = "unidade"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "empresa_id"], ["empresa.tenant_id", "empresa.empresa_id"]
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(primary_key=True)
    unidade_id: Mapped[UUID] = mapped_column(primary_key=True)
    empresa_id: Mapped[UUID]
    nome: Mapped[str] = mapped_column(Text)
    cnpj: Mapped[str | None] = mapped_column(Text)
    uf: Mapped[str] = mapped_column(Text)
    municipio: Mapped[str] = mapped_column(Text)
    participacao_societaria: Mapped[Decimal] = mapped_column(Numeric(7, 4), server_default="100")
    controle_operacional: Mapped[bool]
    controle_financeiro: Mapped[bool]
    ativa: Mapped[bool] = mapped_column(server_default="true")
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
