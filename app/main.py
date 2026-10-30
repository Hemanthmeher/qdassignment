from contextlib import asynccontextmanager
import sys
import asyncio
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime, timezone
import logging

# Fix Windows asyncio event loop policy for uvicorn + httpx compatibility
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import select, delete

from app.config import settings
from app.database import init_db, get_db
from app.models import OpsPending, MessageLog, TraceLog
from app.schemas import InboundMessage, OutboundReply, TraceItem, OpsPendingItem, OpsActionRequest
from app.agent import dispute_agent
from app.trace_logger import get_rider_traces, record_trace_step
from app.payswift_client import payswift_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("quickdrop.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize DB and seed from CSV files
    logger.info("Initializing QuickDrop database...")
    init_db()
    logger.info("Database ready.")
    yield
    # Shutdown: Close HTTP clients
    await payswift_client.close()


app = FastAPI(
    title="QuickDrop Rider Payout Dispute Desk",
    version="1.0.0",
    description="Autonomous Agent & Ops Workflow for Rider Payout Disputes",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
async def root():
    """Redirects root visitors directly to the visual Ops Dashboard."""
    return RedirectResponse(url="/ops")


@app.get("/health", status_code=status.HTTP_200_OK)
async def health_check():
    """Health check endpoint specified in SUBMISSION.md."""
    payswift_alive = await payswift_client.check_health()
    return {
        "status": "ok",
        "service": "quickdrop-dispute-desk",
        "payswift_status": "connected" if payswift_alive else "unreachable",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/messages", response_model=OutboundReply, status_code=status.HTTP_200_OK)
async def receive_rider_message(
    payload: InboundMessage,
    db: Session = Depends(get_db)
):
    """
    Ingests WhatsApp messages from riders one by one.
    Returns agent's reply within 10 seconds.
    """
    received_at_str = (
        payload.received_at.isoformat()
        if isinstance(payload.received_at, datetime)
        else str(payload.received_at)
    )

    try:
        reply_text = await dispute_agent.handle_message(
            session=db,
            message_id=payload.message_id,
            rider_id=payload.rider_id,
            text=payload.text,
            received_at_str=received_at_str,
        )
        return OutboundReply(reply=reply_text)
    except Exception as e:
        logger.error(f"Error handling rider message {payload.message_id}: {e}", exc_info=True)
        # Record error in trace
        record_trace_step(
            session=db,
            rider_id=payload.rider_id,
            step_type="error",
            name="message_processing_failed",
            input_data={"error": str(e)},
            output_data=None,
            message_id=payload.message_id,
        )
        return OutboundReply(
            reply="Aapke message ko process karne mein takleef hui. Humari ops team jald check karegi."
        )


@app.get("/trace/{rider_id}", response_model=List[TraceItem])
async def get_trace(
    rider_id: str,
    db: Session = Depends(get_db)
):
    """
    Returns full chronological trace of agent actions, tool calls, and decisions for rider_id.
    Matches exact schema in SUBMISSION.md.
    """
    traces = get_rider_traces(db, rider_id)
    return traces


@app.get("/ops/pending", response_model=List[OpsPendingItem])
async def get_ops_pending(
    db: Session = Depends(get_db)
):
    """
    Returns list of items waiting for ops approval or escalation.
    Matches exact schema in SUBMISSION.md.
    """
    stmt = (
        select(OpsPending)
        .where(OpsPending.status == "pending")
        .order_by(OpsPending.created_at.asc())
    )
    result = db.execute(stmt)
    pending_items = result.scalars().all()
    return [p.to_dict() for p in pending_items]


@app.post("/ops/{pending_id}/approve")
async def approve_pending_item(
    pending_id: str,
    payload: OpsActionRequest = OpsActionRequest(),
    db: Session = Depends(get_db)
):
    """Human ops approval: if type is approval with amount, pays out via PaySwift."""
    stmt = select(OpsPending).where(OpsPending.id == pending_id)
    item = db.execute(stmt).scalar_one_or_none()

    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    if item.status != "pending":
        raise HTTPException(status_code=400, detail=f"Item is already {item.status}")

    payout_result = None
    if item.type == "approval" and item.amount and item.amount > 0:
        payout_result = await payswift_client.send_payout(
            rider_id=item.rider_id,
            amount=item.amount,
            reference=f"Ops approved: {pending_id} - {item.reason[:50]}",
            idempotency_key=f"ops-approve-{pending_id}"
        )
        record_trace_step(
            session=db,
            rider_id=item.rider_id,
            step_type="tool_call",
            name="payswift_ops_approved_payout",
            input_data={"pending_id": pending_id, "amount": item.amount},
            output_data=payout_result,
        )

    item.status = "approved"
    resolved_time = datetime.now(timezone.utc).isoformat()
    item.resolved_at = resolved_time
    item.notes = payload.notes
    db.commit()

    # Append explicit trace log entry for Ops Approval to rider audit trail
    record_trace_step(
        session=db,
        rider_id=item.rider_id,
        step_type="ops_approved",
        name="ops_item_approved",
        input_data={
            "pending_id": pending_id,
            "type": item.type,
            "amount": item.amount,
            "reason": item.reason,
            "notes": payload.notes,
        },
        output_data={
            "status": "approved",
            "payout": payout_result,
        },
        timestamp_str=resolved_time,
    )

    return {
        "status": "approved",
        "id": pending_id,
        "rider_id": item.rider_id,
        "payout": payout_result,
    }


@app.post("/ops/{pending_id}/reject")
async def reject_pending_item(
    pending_id: str,
    payload: OpsActionRequest = OpsActionRequest(),
    db: Session = Depends(get_db)
):
    """Human ops rejection: marks pending item as rejected."""
    stmt = select(OpsPending).where(OpsPending.id == pending_id)
    item = db.execute(stmt).scalar_one_or_none()

    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    if item.status != "pending":
        raise HTTPException(status_code=400, detail=f"Item is already {item.status}")

    item.status = "rejected"
    resolved_time = datetime.now(timezone.utc).isoformat()
    item.resolved_at = resolved_time
    item.notes = payload.notes
    db.commit()

    # Append explicit trace log entry for Ops Rejection to rider audit trail
    record_trace_step(
        session=db,
        rider_id=item.rider_id,
        step_type="ops_rejected",
        name="ops_item_rejected",
        input_data={
            "pending_id": pending_id,
            "type": item.type,
            "amount": item.amount,
            "reason": item.reason,
            "notes": payload.notes,
        },
        output_data={"status": "rejected"},
        timestamp_str=resolved_time,
    )

    return {"status": "rejected", "id": pending_id, "rider_id": item.rider_id}


@app.get("/ops", response_class=HTMLResponse)
async def ops_dashboard_ui():
    """Renders the human Ops Dashboard UI."""
    template_path = Path(__file__).parent / "templates" / "ops_dashboard.html"
    if template_path.exists():
        return HTMLResponse(content=template_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h3>Ops dashboard template not found.</h3>")


@app.post("/reset")
async def reset_runtime_state(db: Session = Depends(get_db)):
    """Utility endpoint to clear messages, traces, and pending items for clean evaluation runs."""
    db.execute(delete(MessageLog))
    db.execute(delete(TraceLog))
    db.execute(delete(OpsPending))
    db.commit()
    return {"status": "reset_successful"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=True)
