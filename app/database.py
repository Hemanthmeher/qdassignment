import csv
import logging
from pathlib import Path
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker, Session

from app.config import settings
from app.models import Base, Rider, Trip, PayoutLine

logger = logging.getLogger("quickdrop.db")

connect_args = {"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(
    settings.DATABASE_URL,
    connect_args=connect_args,
    echo=False,
    future=True
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False
)


def init_db():
    Base.metadata.create_all(bind=engine)
    seed_data_if_needed()


def seed_data_if_needed():
    """Seeds the DB from data/*.csv if tables are empty."""
    base_dir = Path(__file__).resolve().parent.parent
    data_dir = base_dir / "data"
    
    if not data_dir.exists():
        logger.warning(f"Data directory {data_dir} does not exist, skipping seed.")
        return

    with SessionLocal() as session:
        # Check if riders table is populated
        rider_count = session.execute(select(func.count(Rider.rider_id))).scalar_one()
        if rider_count > 0:
            logger.info("Database already seeded.")
            return

        logger.info("Seeding database from CSV files...")

        # 1. Seed riders.csv
        riders_file = data_dir / "riders.csv"
        if riders_file.exists():
            with open(riders_file, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rider_dicts = [
                    {
                        "rider_id": row["rider_id"],
                        "name": row["name"],
                        "city": row["city"],
                        "joined_on": row["joined_on"],
                    }
                    for row in reader
                ]
                if rider_dicts:
                    session.execute(Rider.__table__.insert(), rider_dicts)

        # 2. Seed trips.csv
        trips_file = data_dir / "trips.csv"
        if trips_file.exists():
            with open(trips_file, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                trip_dicts = [
                    {
                        "trip_id": row["trip_id"],
                        "rider_id": row["rider_id"],
                        "started_at": row["started_at"],
                        "status": row["status"],
                        "distance_km": float(row["distance_km"]),
                        "surge_multiplier": float(row["surge_multiplier"]),
                    }
                    for row in reader
                ]
                if trip_dicts:
                    session.execute(Trip.__table__.insert(), trip_dicts)

        # 3. Seed payout_lines.csv
        payouts_file = data_dir / "payout_lines.csv"
        if payouts_file.exists():
            with open(payouts_file, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                payout_dicts = [
                    {
                        "line_id": row["line_id"],
                        "payout_date": row["payout_date"],
                        "rider_id": row["rider_id"],
                        "line_type": row["line_type"],
                        "trip_id": row["trip_id"] if row["trip_id"] else None,
                        "amount": int(row["amount"]),
                    }
                    for row in reader
                ]
                if payout_dicts:
                    session.execute(PayoutLine.__table__.insert(), payout_dicts)

        session.commit()
        logger.info("Database successfully seeded.")


def get_db():
    with SessionLocal() as session:
        yield session
