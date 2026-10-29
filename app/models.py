from datetime import datetime, timezone
import json
import uuid
from typing import Any
from sqlalchemy import (
    Column,
    String,
    Integer,
    Float,
    DateTime,
    Text,
    Index,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Rider(Base):
    __tablename__ = "riders"

    rider_id = Column(String(50), primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    city = Column(String(100), nullable=False)
    joined_on = Column(String(50), nullable=False)


class Trip(Base):
    __tablename__ = "trips"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trip_id = Column(String(50), index=True, nullable=False)
    rider_id = Column(String(50), index=True, nullable=False)
    started_at = Column(String(50), nullable=False)
    status = Column(String(50), nullable=False)  # completed, cancelled_by_customer, cancelled_by_rider
    distance_km = Column(Float, nullable=False)
    surge_multiplier = Column(Float, nullable=False, default=1.0)


class PayoutLine(Base):
    __tablename__ = "payout_lines"

    line_id = Column(String(50), primary_key=True, index=True)
    payout_date = Column(String(50), index=True, nullable=False)
    rider_id = Column(String(50), index=True, nullable=False)
    line_type = Column(String(50), nullable=False)  # trip, daily_incentive, cancellation_penalty
    trip_id = Column(String(50), nullable=True, index=True)
    amount = Column(Integer, nullable=False)


class MessageLog(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(String(100), unique=True, index=True, nullable=False)
    rider_id = Column(String(50), index=True, nullable=False)
    sender = Column(String(20), nullable=False)  # rider, agent
    text = Column(Text, nullable=False)
    received_at = Column(String(50), nullable=False)
    reply_text = Column(Text, nullable=True)  # stored if sender is rider and agent replied
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class TraceLog(Base):
    __tablename__ = "traces"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rider_id = Column(String(50), index=True, nullable=False)
    message_id = Column(String(100), nullable=True, index=True)
    at = Column(String(50), nullable=False)
    type = Column(String(50), nullable=False)  # message_in, tool_call, decision, reply, error
    name = Column(String(100), nullable=False)
    input_json = Column(Text, nullable=True)
    output_json = Column(Text, nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "type": self.type,
            "name": self.name,
            "input": json.loads(self.input_json) if self.input_json is not None else None,
            "output": json.loads(self.output_json) if self.output_json is not None else None,
        }


class OpsPending(Base):
    __tablename__ = "ops_pending"

    id = Column(String(50), primary_key=True, default=lambda: f"OP-{uuid.uuid4().hex[:8].upper()}")
    rider_id = Column(String(50), index=True, nullable=False)
    type = Column(String(50), nullable=False)  # approval, escalation
    amount = Column(Integer, nullable=True)
    reason = Column(Text, nullable=False)
    status = Column(String(50), default="pending", nullable=False)  # pending, approved, rejected
    created_at = Column(String(50), nullable=False)
    resolved_at = Column(String(50), nullable=True)
    notes = Column(Text, nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "rider_id": self.rider_id,
            "type": self.type,
            "amount": self.amount,
            "reason": self.reason,
            "created_at": self.created_at,
        }
