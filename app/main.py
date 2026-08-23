"""
FastAPI application entrypoint for the TAKE coffee&more Loyalty System.

Route map:
  GET  /app                        Customer WebApp (Jinja2 template)
  GET  /barista                    Barista scanner WebApp (Jinja2 template)

  POST /api/auth                   Telegram WebApp handshake -> user + QR payload
  GET  /api/me/{telegram_id}       Fetch current balance (polling refresh)
  GET  /api/me/{telegram_id}/history  Recent transactions for a customer

  POST /api/barista/login          PIN check -> session token
  POST /api/barista/verify-qr      Decode + validate a scanned customer QR
  POST /api/barista/earn           Record an EARN transaction (+3% cashback)
  POST /api/barista/redeem         Record a REDEEM transaction

  GET  /api/admin/branches         List branches
  GET  /api/admin/transactions     Multi-branch transaction history (admin)
"""
import logging
import math
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    InvalidInitData,
    InvalidQRPayload,
    check_barista_pin,
    issue_barista_session_token,
    sign_qr_payload,
    validate_telegram_init_data,
    verify_barista_session_token,
    verify_qr_payload,
)
from app.config import settings
from app.database import get_db, init_db
from app.models import Branch, Transaction, TransactionType, User
from app.schemas import (
    AuthRequest,
    AuthResponse,
    BaristaLoginRequest,
    BaristaLoginResponse,
    BranchOut,
    EarnRequest,
    QRVerifyRequest,
    QRVerifyResponse,
    RedeemRequest,
    TransactionOut,
    TransactionResult,
    UserOut,
    UserPublic,
)
from app.services.bot import send_transaction_notification

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("take_loyalty.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up: initializing database + seeding default branch...")
    await init_db()
    logger.info("Startup complete.")
    yield
    logger.info("Shutting down.")


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

# Middleware для авто-пропуска страницы предупреждения ngrok
@app.middleware("http")
async def add_ngrok_skip_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["ngrok-skip-browser-warning"] = "true"
    return response

templates = Jinja2Templates(directory="app/templates")

# Static assets (favicon, any local JS/CSS overrides not pulled from CDN).
app.mount("/static", StaticFiles(directory="app/static"), name="static")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _get_or_create_user(db: AsyncSession, tg_user: dict) -> User:
    telegram_id = int(tg_user["id"])
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(
            telegram_id=telegram_id,
            first_name=tg_user.get("first_name", "Guest"),
            username=tg_user.get("username"),
            bonus_balance=0,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    return user


async def _get_default_branch(db: AsyncSession, branch_id: int) -> Branch:
    result = await db.execute(select(Branch).where(Branch.id == branch_id))
    branch = result.scalar_one_or_none()
    if branch is None:
        raise HTTPException(status_code=404, detail=f"Branch {branch_id} not found")
    if not branch.is_active:
        raise HTTPException(status_code=400, detail=f"Branch {branch_id} is not active")
    return branch


async def require_barista_session(authorization: Optional[str] = Header(default=None)) -> None:
    """
    FastAPI dependency guarding every barista-only mutation endpoint.
    Expects `Authorization: Bearer <session_token>` issued by /api/barista/login.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing barista session token")

    token = authorization.removeprefix("Bearer ").strip()
    if not verify_barista_session_token(token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired barista session")


# ---------------------------------------------------------------------------
# Frontend routes
# ---------------------------------------------------------------------------
@app.get("/app", response_class=HTMLResponse)
async def customer_app(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="client.html",
        context={"app_name": settings.APP_NAME},
    )


@app.get("/barista", response_class=HTMLResponse)
async def barista_app(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="barista.html",
        context={"app_name": settings.APP_NAME},
    )


@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(
        f"<h1>{settings.APP_NAME}</h1>"
        f"<p>Customer app: <a href='/app'>/app</a> &middot; "
        f"Barista scanner: <a href='/barista'>/barista</a></p>"
    )


# ---------------------------------------------------------------------------
# Customer auth / handshake
# ---------------------------------------------------------------------------
@app.post("/api/auth", response_model=AuthResponse)
async def authenticate(payload: AuthRequest, db: AsyncSession = Depends(get_db)):
    try:
        data = validate_telegram_init_data(payload.init_data)
    except InvalidInitData as exc:
        raise HTTPException(status_code=401, detail=f"Invalid Telegram initData: {exc}") from exc

    user = await _get_or_create_user(db, data["user"])
    branch = await _get_default_branch(db, settings.DEFAULT_BRANCH_ID)
    qr_payload = sign_qr_payload(user.telegram_id)

    return AuthResponse(
        user=UserOut.model_validate(user),
        branch=BranchOut.model_validate(branch),
        qr_payload=qr_payload,
    )


@app.get("/api/me/{telegram_id}", response_model=UserOut)
async def get_me(telegram_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return UserOut.model_validate(user)


@app.get("/api/me/{telegram_id}/history", response_model=list[TransactionOut])
async def get_my_history(telegram_id: int, limit: int = 20, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    tx_result = await db.execute(
        select(Transaction)
        .where(Transaction.user_id == user.id)
        .order_by(Transaction.created_at.desc())
        .limit(limit)
    )
    transactions = tx_result.scalars().all()
    return [TransactionOut.model_validate(t) for t in transactions]


# ---------------------------------------------------------------------------
# Barista: PIN login
# ---------------------------------------------------------------------------
@app.post("/api/barista/login", response_model=BaristaLoginResponse)
async def barista_login(payload: BaristaLoginRequest):
    if not check_barista_pin(payload.pin):
        raise HTTPException(status_code=401, detail="Incorrect PIN")
    token = issue_barista_session_token()
    return BaristaLoginResponse(ok=True, session_token=token)


# ---------------------------------------------------------------------------
# Barista: scan a customer QR code
# ---------------------------------------------------------------------------
@app.post("/api/barista/verify-qr", response_model=QRVerifyResponse, dependencies=[Depends(require_barista_session)])
async def barista_verify_qr(payload: QRVerifyRequest, db: AsyncSession = Depends(get_db)):
    try:
        telegram_id = verify_qr_payload(payload.qr_payload)
    except InvalidQRPayload as exc:
        raise HTTPException(status_code=400, detail=f"Invalid QR code: {exc}") from exc

    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="No customer found for this QR code")

    return QRVerifyResponse(user=UserPublic.model_validate(user))


# ---------------------------------------------------------------------------
# Barista: EARN transaction (+3% cashback)
# ---------------------------------------------------------------------------
@app.post("/api/barista/earn", response_model=TransactionResult, dependencies=[Depends(require_barista_session)])
async def earn_bonus(payload: EarnRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.telegram_id == payload.telegram_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="Customer not found")

    branch = await _get_default_branch(db, payload.branch_id)

    bonus_earned = math.floor(payload.bill_amount * settings.CASHBACK_RATE)

    transaction = Transaction(
        user_id=user.id,
        branch_id=branch.id,
        bill_amount=payload.bill_amount,
        bonus_change=bonus_earned,
        type=TransactionType.EARN,
    )
    user.bonus_balance += bonus_earned

    db.add(transaction)
    await db.commit()
    await db.refresh(transaction)
    await db.refresh(user)

    await send_transaction_notification(
        telegram_id=user.telegram_id,
        transaction_type=TransactionType.EARN,
        bonus_change=bonus_earned,
        branch_name=branch.name,
        new_balance=user.bonus_balance,
    )

    return TransactionResult(
        transaction=TransactionOut.model_validate(transaction),
        new_balance=user.bonus_balance,
        message=f"Earned +{bonus_earned} ֏. New balance: {user.bonus_balance} ֏.",
    )


# ---------------------------------------------------------------------------
# Barista: REDEEM transaction
# ---------------------------------------------------------------------------
@app.post("/api/barista/redeem", response_model=TransactionResult, dependencies=[Depends(require_barista_session)])
async def redeem_bonus(payload: RedeemRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.telegram_id == payload.telegram_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="Customer not found")

    branch = await _get_default_branch(db, payload.branch_id)

    max_redeemable = min(user.bonus_balance, payload.bill_amount)
    if payload.redeem_amount > max_redeemable:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot redeem {payload.redeem_amount} ֏. Maximum redeemable is "
                f"{max_redeemable} ֏ (lesser of balance {user.bonus_balance} ֏ "
                f"and bill amount {payload.bill_amount} ֏)."
            ),
        )
    if payload.redeem_amount <= 0:
        raise HTTPException(status_code=400, detail="Redeem amount must be positive")

    transaction = Transaction(
        user_id=user.id,
        branch_id=branch.id,
        bill_amount=payload.bill_amount,
        bonus_change=-payload.redeem_amount,
        type=TransactionType.REDEEM,
    )
    user.bonus_balance -= payload.redeem_amount

    db.add(transaction)
    await db.commit()
    await db.refresh(transaction)
    await db.refresh(user)

    await send_transaction_notification(
        telegram_id=user.telegram_id,
        transaction_type=TransactionType.REDEEM,
        bonus_change=-payload.redeem_amount,
        branch_name=branch.name,
        new_balance=user.bonus_balance,
    )

    return TransactionResult(
        transaction=TransactionOut.model_validate(transaction),
        new_balance=user.bonus_balance,
        message=f"Redeemed {payload.redeem_amount} ֏. New balance: {user.bonus_balance} ֏.",
    )


# ---------------------------------------------------------------------------
# Admin: branches + multi-branch transaction history
# ---------------------------------------------------------------------------
@app.get("/api/admin/branches", response_model=list[BranchOut])
async def list_branches(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Branch).order_by(Branch.id))
    branches = result.scalars().all()
    return [BranchOut.model_validate(b) for b in branches]


@app.get("/api/admin/transactions", response_model=list[TransactionOut])
async def list_transactions(
    branch_id: Optional[int] = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    query = select(Transaction).order_by(Transaction.created_at.desc()).limit(limit)
    if branch_id is not None:
        query = query.where(Transaction.branch_id == branch_id)
    result = await db.execute(query)
    transactions = result.scalars().all()
    return [TransactionOut.model_validate(t) for t in transactions]


@app.get("/health")
async def health_check():
    return {"status": "ok", "app": settings.APP_NAME}