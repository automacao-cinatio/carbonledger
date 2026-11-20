"""Tabelas-base do isolamento: tenant e empresa. O cadastro completo entra no P06."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Tenant(Base):
    """Cliente da plataforma. O proprio tenant_id e a chave, para o RLS ser uniforme."""

    __tablename__ = "tenant"

    tenant_id: Mapped[UUID] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(Text)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Empresa(Base):
    """Empresa inventariada. A chave composta impede apontar para empresa de outro tenant."""

    __tablename__ = "empresa"

    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenant.tenant_id"), primary_key=True)
    empresa_id: Mapped[UUID] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(Text)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
