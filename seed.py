"""
TAKE coffee & more — one-off DB seed script.

Creates all tables (if they don't exist yet) and inserts a default branch,
verifying the environment config for the barista PIN.

Usage (from the project root, same place you'd run uvicorn from):
    python seed.py
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Import settings and models from app
from app.config import settings
from app.models import Base, Branch, Employee, EmployeeRole, User

DEFAULT_BRANCH_NAME = "Tumanyan 38"
DEFAULT_BRANCH_ADDRESS = "Tumanyan 38, Yerevan"
DEFAULT_BARISTA_NAME = "Barista"


def hash_pin(pin: str) -> str:
    """Must match hash_pin() in app/main.py — same salt, same algorithm."""
    salt = os.environ.get("PIN_SALT", "take-coffee-static-salt")
    return hashlib.sha256(f"{salt}:{pin}".encode()).hexdigest()


async def seed() -> None:
    database_url = settings.DATABASE_URL
    print(f"Connecting to database: {database_url}")
    
    # Configure kwargs for engine based on SQLite vs Postgres
    is_sqlite = database_url.startswith("sqlite")
    engine_kwargs = {"echo": False}
    if not is_sqlite:
        engine_kwargs["pool_pre_ping"] = True

    engine = create_async_engine(database_url, **engine_kwargs)
    session_local = async_sessionmaker(engine, expire_on_commit=False)

    # Ensure tables exist
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✓ Tables ensured (create_all)")

    async with session_local() as db:
        # Seed default branch
        result = await db.execute(select(Branch).where(Branch.name == DEFAULT_BRANCH_NAME))
        branch = result.scalar_one_or_none()
        if branch is None:
            branch = Branch(
                name=DEFAULT_BRANCH_NAME,
                address=DEFAULT_BRANCH_ADDRESS,
                is_active=True
            )
            db.add(branch)
            await db.commit()
            await db.refresh(branch)
            print(f"✓ Created branch: {branch.name} (id={branch.id})")
        else:
            print(f"– Branch already exists: {branch.name} (id={branch.id})")

        # Seed default barista employee (idempotent on branch_id + pin hash,
        # matching the uq_employee_branch_pin constraint on Employee).
        barista_pin = settings.BARISTA_PIN
        if barista_pin:
            pin_hash = hash_pin(barista_pin)
            result = await db.execute(
                select(Employee).where(
                    Employee.branch_id == branch.id, Employee.pin_code_hash == pin_hash
                )
            )
            employee = result.scalar_one_or_none()
            if employee is None:
                employee = Employee(
                    name=DEFAULT_BARISTA_NAME,
                    branch_id=branch.id,
                    pin_code_hash=pin_hash,
                    role=EmployeeRole.BARISTA,
                    is_active=True,
                )
                db.add(employee)
                await db.commit()
                print(f"✓ Created barista employee: {employee.name} (PIN length: {len(barista_pin)})")
            else:
                print(f"– Barista employee already exists: {employee.name}")
        else:
            print("⚠ BARISTA_PIN is not configured in the environment settings!")

    await engine.dispose()
    print(f"\nDone. Log into barista.html with PIN: {settings.BARISTA_PIN}")


if __name__ == "__main__":
    asyncio.run(seed())