"""
SQLAlchemy 2.0 (async) модели для системы лояльности TAKE coffee & more.

Стек: Python 3.11+, SQLAlchemy 2.0 (async ORM), PostgreSQL (asyncpg).
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(AsyncAttrs, DeclarativeBase):
    """Базовый класс для всех моделей."""

    pass


class TransactionType(str, enum.Enum):
    ACCUMULATE = "ACCUMULATE"
    DEDUCT = "DEDUCT"


class EmployeeRole(str, enum.Enum):
    BARISTA = "barista"
    ADMIN = "admin"


class Branch(Base):
    """Филиал сети (напр. 'Tumanyan 38', 'Abovyan City')."""

    __tablename__ = "branches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False
    )

    employees: Mapped[list["Employee"]] = relationship(back_populates="branch")
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="branch")

    def __repr__(self) -> str:
        return f"<Branch id={self.id} name={self.name!r}>"


class User(Base):
    """Клиент — идентифицируется по telegram_id.

    Тир считается от free_coffees_redeemed (см. compute_tier() в app/main.py):
      Member: 0-1, Silver: 2-4, Gold: 5+.
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("points_balance >= 0", name="ck_users_points_balance_non_negative"),
        CheckConstraint(
            "free_coffees_redeemed >= 0", name="ck_users_free_coffees_non_negative"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, nullable=False, index=True
    )
    first_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    points_balance: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    free_coffees_redeemed: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False
    )

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="user")

    def __repr__(self) -> str:
        return f"<User id={self.id} telegram_id={self.telegram_id}>"


class Employee(Base):
    """Сотрудник (бариста / админ), привязан к конкретному филиалу."""

    __tablename__ = "employees"
    __table_args__ = (
        UniqueConstraint("branch_id", "pin_code_hash", name="uq_employee_branch_pin"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"), nullable=False)
    pin_code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[EmployeeRole] = mapped_column(
        Enum(
            EmployeeRole,
            name="employee_role",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        default=EmployeeRole.BARISTA,
        server_default=EmployeeRole.BARISTA.value,
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False
    )

    branch: Mapped["Branch"] = relationship(back_populates="employees")
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="employee")

    def __repr__(self) -> str:
        return f"<Employee id={self.id} name={self.name!r} role={self.role}>"


class EmployeeSession(Base):
    """Сессионный токен, выданный после успешного PIN-логина бариста."""

    __tablename__ = "employee_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False)
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(nullable=False)

    employee: Mapped["Employee"] = relationship()

    def __repr__(self) -> str:
        return f"<EmployeeSession employee_id={self.employee_id}>"


class Transaction(Base):
    """Начисление или списание баллов (за скан чашки или списание награды)."""

    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint(
            "sum_amd IS NULL OR sum_amd >= 0", name="ck_transactions_sum_non_negative"
        ),
        Index("ix_transactions_user_id_timestamp", "user_id", "timestamp"),
        Index("ix_transactions_branch_id_timestamp", "branch_id", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"), nullable=False)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False)

    # Сумма чека в AMD — необязательна: earn по чашкам не завязан на сумму.
    sum_amd: Mapped[int | None] = mapped_column(Integer, nullable=True)
    points_change: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[TransactionType] = mapped_column(
        Enum(TransactionType, name="transaction_type"), nullable=False
    )

    # Защита от повторного проведения одной и той же операции при ретраях
    # со стороны клиента/сети (см. README по идемпотентности).
    idempotency_key: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True
    )

    timestamp: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False, index=True
    )

    user: Mapped["User"] = relationship(back_populates="transactions")
    branch: Mapped["Branch"] = relationship(back_populates="transactions")
    employee: Mapped["Employee"] = relationship(back_populates="transactions")

    def __repr__(self) -> str:
        return (
            f"<Transaction id={self.id} user_id={self.user_id} "
            f"type={self.type} points_change={self.points_change}>"
        )