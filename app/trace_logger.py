from datetime import datetime, timezone
import json
from typing import Any, Optional, List, Dict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TraceLog


def record_trace_step(
    session: Session,
    rider_id: str,
    step_type: str,  # message_in, tool_call, decision, reply, error
    name: str,
    input_data: Any = None,
    output_data: Any = None,
    message_id: Optional[str] = None,
    timestamp_str: Optional[str] = None
) -> TraceLog:
    """Inserts a structured trace step into the trace log."""
    now_iso = timestamp_str or datetime.now(timezone.utc).isoformat()
    
    trace_entry = TraceLog(
        rider_id=rider_id,
        message_id=message_id,
        at=now_iso,
        type=step_type,
        name=name,
        input_json=json.dumps(input_data, default=str) if input_data is not None else None,
        output_json=json.dumps(output_data, default=str) if output_data is not None else None,
    )
    session.add(trace_entry)
    session.commit()
    return trace_entry


def get_rider_traces(session: Session, rider_id: str) -> List[Dict[str, Any]]:
    """Returns all chronological trace steps for a given rider."""
    stmt = (
        select(TraceLog)
        .where(TraceLog.rider_id == rider_id)
        .order_by(TraceLog.id.asc())
    )
    result = session.execute(stmt)
    traces = result.scalars().all()
    return [t.to_dict() for t in traces]
