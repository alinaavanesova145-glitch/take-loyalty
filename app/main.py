"""
TAKE coffee & more — FastAPI backend.

Запуск (из корня проекта, где лежит models.py):
    uvicorn app.main:app --reload

Переменные окружения:
    DATABASE_URL      — напр. postgresql+asyncpg://user:pass@host/dbname
    CORS_ORIGINS       — через запятую, напр. "https://t.me,https://your-webapp.example"
"""

from __future__ import annotations

import hashlib
import os
import secrets
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base, Branch, Employee, EmployeeSession, Transaction, TransactionType, User

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

APP_NAME = os.environ.get("APP_NAME", "TAKE coffee&more Loyalty")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/take_loyalty"
)
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",")]

POINTS_PER_CUP = 100
REWARD_THRESHOLD = 800  # points needed for one free coffee (8 cups)
SESSION_TTL_HOURS = 12

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


app = FastAPI(title="TAKE coffee & more API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.on_event("startup")
async def on_startup() -> None:
    # Convenient for local dev / first boot. In production, prefer Alembic
    # migrations instead of create_all so schema changes are tracked.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# --------------------------------------------------------------------------
# Frontend routes
# --------------------------------------------------------------------------


@app.get("/app", response_class=HTMLResponse)
async def customer_app(request: Request):
    return templates.TemplateResponse(
        request=request, name="client.html", context={"app_name": APP_NAME}
    )


@app.get("/barista", response_class=HTMLResponse)
async def barista_app(request: Request):
    return templates.TemplateResponse(
        request=request, name="barista.html", context={"app_name": APP_NAME}
    )


@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(
        f"<h1>{APP_NAME}</h1>"
        f"<p>Customer app: <a href='/app'>/app</a> &middot; "
        f"Barista scanner: <a href='/barista'>/barista</a></p>"
    )


# --------------------------------------------------------------------------
# Loyalty tier logic
# --------------------------------------------------------------------------


def compute_tier(free_coffees_redeemed: int) -> str:
    """Member: 0-1, Silver: 2-4, Gold: 5+ free coffees redeemed."""
    if free_coffees_redeemed >= 5:
        return "Gold"
    if free_coffees_redeemed >= 2:
        return "Silver"
    return "Member"


def user_state(user: User) -> "UserStateOut":
    return UserStateOut(
        telegram_id=user.telegram_id,
        first_name=user.first_name,
        points_balance=user.points_balance,
        free_coffees_redeemed=user.free_coffees_redeemed,
        tier=compute_tier(user.free_coffees_redeemed),
        cups_count=user.points_balance // POINTS_PER_CUP,
        points_to_next_reward=max(REWARD_THRESHOLD - user.points_balance, 0),
    )


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------


class UserStateOut(BaseModel):
    telegram_id: int
    first_name: str | None
    points_balance: int
    free_coffees_redeemed: int
    tier: str
    cups_count: int
    points_to_next_reward: int


class BaristaLoginIn(BaseModel):
    pin: str = Field(min_length=4, max_length=8)


class BaristaLoginOut(BaseModel):
    ok: bool
    session_token: str
    employee_name: str
    branch_name: str


class VerifyQrIn(BaseModel):
    qr_payload: str


class VerifyQrOut(BaseModel):
    user: UserStateOut


class AddPointsIn(BaseModel):
    telegram_id: int
    cups: int = Field(default=1, ge=1, le=20)
    idempotency_key: str | None = None


class RedeemIn(BaseModel):
    telegram_id: int
    idempotency_key: str | None = None


class TransactionOut(BaseModel):
    message: str
    user: UserStateOut


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def hash_pin(pin: str) -> str:
    """Simple salted hash for staff PINs. Employees are seeded out-of-band
    (admin tooling / DB seed script) using this same function."""
    salt = os.environ.get("PIN_SALT", "take-coffee-static-salt")
    return hashlib.sha256(f"{salt}:{pin}".encode()).hexdigest()


async def get_or_create_user(
    db: AsyncSession, telegram_id: int, first_name: str | None = None
) -> User:
    """Zero-start logic: brand-new users begin at 0 points / 0 redemptions."""
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(
            telegram_id=telegram_id,
            first_name=first_name,
            points_balance=0,
            free_coffees_redeemed=0,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    elif first_name and user.first_name != first_name:
        user.first_name = first_name
        await db.commit()
        await db.refresh(user)
    return user


async def get_current_employee(
    authorization: str | None = Header(default=None), db: AsyncSession = Depends(get_db)
) -> Employee:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing barista session")
    token = authorization.removeprefix("Bearer ").strip()

    result = await db.execute(select(EmployeeSession).where(EmployeeSession.token == token))
    session = result.scalar_one_or_none()
    if not session or session.expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
        raise HTTPException(status_code=401, detail="Session expired, please log in again")

    employee = await db.get(Employee, session.employee_id)
    if not employee or not employee.is_active:
        raise HTTPException(status_code=401, detail="Employee inactive")
    return employee


# --------------------------------------------------------------------------
# Customer-facing endpoints
# --------------------------------------------------------------------------


@app.get("/api/user/{telegram_id}", response_model=UserStateOut)
async def get_user(
    telegram_id: int, first_name: str | None = None, db: AsyncSession = Depends(get_db)
):
    user = await get_or_create_user(db, telegram_id, first_name)
    return user_state(user)


# --------------------------------------------------------------------------
# Barista endpoints
# --------------------------------------------------------------------------


@app.post("/api/barista/login", response_model=BaristaLoginOut)
async def barista_login(payload: BaristaLoginIn, db: AsyncSession = Depends(get_db)):
    pin_hash = hash_pin(payload.pin)
    result = await db.execute(
        select(Employee).where(Employee.pin_code_hash == pin_hash, Employee.is_active.is_(True))
    )
    employee = result.scalar_one_or_none()
    if not employee:
        raise HTTPException(status_code=401, detail="Incorrect PIN")

    branch = await db.get(Branch, employee.branch_id)

    token = secrets.token_urlsafe(32)
    session = EmployeeSession(
        employee_id=employee.id,
        token=token,
        expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
        + timedelta(hours=SESSION_TTL_HOURS),
    )
    db.add(session)
    await db.commit()

    return BaristaLoginOut(
        ok=True,
        session_token=token,
        employee_name=employee.name,
        branch_name=branch.name if branch else "—",
    )


@app.post("/api/barista/verify-qr", response_model=VerifyQrOut)
async def verify_qr(
    payload: VerifyQrIn,
    db: AsyncSession = Depends(get_db),
    employee: Employee = Depends(get_current_employee),
):
    # Expected payload format: "TAKE:{telegram_id}"
    raw = payload.qr_payload.strip()
    if not raw.startswith("TAKE:"):
        raise HTTPException(status_code=400, detail="Unrecognized QR code")
    try:
        telegram_id = int(raw.removeprefix("TAKE:"))
    except ValueError:
        raise HTTPException(status_code=400, detail="Unrecognized QR code")

    user = await get_or_create_user(db, telegram_id)
    return VerifyQrOut(user=user_state(user))


@app.post("/api/barista/add-points", response_model=TransactionOut)
async def add_points(
    payload: AddPointsIn,
    db: AsyncSession = Depends(get_db),
    employee: Employee = Depends(get_current_employee),
):
    if payload.idempotency_key:
        existing = await db.execute(
            select(Transaction).where(Transaction.idempotency_key == payload.idempotency_key)
        )
        if existing.scalar_one_or_none():
            user = await get_or_create_user(db, payload.telegram_id)
            return TransactionOut(message="Already recorded", user=user_state(user))

    user = await get_or_create_user(db, payload.telegram_id)
    points_earned = POINTS_PER_CUP * payload.cups
    user.points_balance += points_earned

    db.add(
        Transaction(
            user_id=user.id,
            branch_id=employee.branch_id,
            employee_id=employee.id,
            sum_amd=None,
            points_change=points_earned,
            type=TransactionType.ACCUMULATE,
            idempotency_key=payload.idempotency_key,
        )
    )
    await db.commit()
    await db.refresh(user)

    cup_word = "cup" if payload.cups == 1 else "cups"
    return TransactionOut(
        message=f"+{points_earned} pts for {payload.cups} {cup_word}",
        user=user_state(user),
    )


@app.post("/api/barista/redeem", response_model=TransactionOut)
async def redeem(
    payload: RedeemIn,
    db: AsyncSession = Depends(get_db),
    employee: Employee = Depends(get_current_employee),
):
    if payload.idempotency_key:
        existing = await db.execute(
            select(Transaction).where(Transaction.idempotency_key == payload.idempotency_key)
        )
        if existing.scalar_one_or_none():
            user = await get_or_create_user(db, payload.telegram_id)
            return TransactionOut(message="Already recorded", user=user_state(user))

    user = await get_or_create_user(db, payload.telegram_id)
    if user.points_balance < REWARD_THRESHOLD:
        raise HTTPException(
            status_code=400,
            detail=f"Not enough points — needs {REWARD_THRESHOLD}, has {user.points_balance}",
        )

    user.points_balance -= REWARD_THRESHOLD
    user.free_coffees_redeemed += 1

    db.add(
        Transaction(
            user_id=user.id,
            branch_id=employee.branch_id,
            employee_id=employee.id,
            sum_amd=None,
            points_change=-REWARD_THRESHOLD,
            type=TransactionType.DEDUCT,
            idempotency_key=payload.idempotency_key,
        )
    )
    await db.commit()
    await db.refresh(user)

    return TransactionOut(message="Free coffee redeemed!", user=user_state(user))