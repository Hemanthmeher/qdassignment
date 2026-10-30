from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, Field


class InboundMessage(BaseModel):
    message_id: str
    rider_id: str
    text: str
    received_at: str | datetime


class OutboundReply(BaseModel):
    reply: str


class TraceItem(BaseModel):
    at: str
    type: Literal["message_in", "tool_call", "decision", "reply", "error", "ops_approved", "ops_rejected"]
    name: str
    input: Any = None
    output: Any = None


class OpsPendingItem(BaseModel):
    id: str
    rider_id: str
    type: Literal["approval", "escalation"]
    amount: int | None = None
    reason: str
    created_at: str


class OpsActionRequest(BaseModel):
    notes: str | None = None
