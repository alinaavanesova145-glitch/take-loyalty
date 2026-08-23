"""
Pydantic v2 schemas — request/response contracts for the API.

Keeping these separate from the ORM models (app/models.py) means the
database layer can evolve independently of the wire format, and lets us
validate/constrain inbound data (e.g. bill_amount must be positive) before
it ever touches business logic.
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models import TransactionType


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------
class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    telegram_id: int
    first_name: str
    username: Optional[str] = None
    bonus_balance: int
    created_at: datetime


class UserPublic(BaseModel):
    """Minimal user payload returned to the barista scanner after a QR scan."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    telegram_id: int
    first_name: str
    username: Optional[str] = None
    bonus_balance: int


# ---------------------------------------------------------------------------
# Branch
# ---------------------------------------------------------------------------
class BranchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    address: str
    is_active: bool


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------
class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    branch_id: int
    bill_amount: int
    bonus_change: int
    type: TransactionType
    created_at: datetime


# ---------------------------------------------------------------------------
# Auth / WebApp handshake
# ---------------------------------------------------------------------------
class AuthRequest(BaseModel):
    """Raw `Telegram.WebApp.initData` string sent by the client on load."""

    init_data: str = Field(..., min_length=1)


class AuthResponse(BaseModel):
    user: UserOut
    branch: BranchOut
    qr_payload: str  # signed token to render as the customer's QR code


# ---------------------------------------------------------------------------
# QR verification (barista scans a customer's code)
# ---------------------------------------------------------------------------
class QRVerifyRequest(BaseModel):
    qr_payload: str = Field(..., min_length=1)


class QRVerifyResponse(BaseModel):
    user: UserPublic


# ---------------------------------------------------------------------------
# Barista PIN login
# ---------------------------------------------------------------------------
class BaristaLoginRequest(BaseModel):
    pin: str = Field(..., min_length=1, max_length=32)


class BaristaLoginResponse(BaseModel):
    ok: bool
    session_token: Optional[str] = None


# ---------------------------------------------------------------------------
# Transaction creation (Earn / Redeem) initiated by the barista
# ---------------------------------------------------------------------------
class EarnRequest(BaseModel):
    telegram_id: int
    bill_amount: int = Field(..., gt=0, description="Total bill amount in AMD, must be positive")
    branch_id: int = 1


class RedeemRequest(BaseModel):
    telegram_id: int
    bill_amount: int = Field(..., gt=0, description="Total bill amount in AMD, must be positive")
    redeem_amount: int = Field(..., gt=0, description="Bonus AMD the customer wants to redeem")
    branch_id: int = 1


class TransactionResult(BaseModel):
    transaction: TransactionOut
    new_balance: int
    message: str
