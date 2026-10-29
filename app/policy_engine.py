import math
from datetime import datetime, date, timedelta
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field
from sqlalchemy import select, func, distinct
from sqlalchemy.orm import Session
import zoneinfo

from app.models import Trip, PayoutLine
from app.config import settings

IST = zoneinfo.ZoneInfo("Asia/Kolkata")


def round_half_up(val: float) -> int:
    """0.5 rounds up to nearest integer (policy: 0.5 rounds up)."""
    return math.floor(val + 0.5)


def parse_ist_date(started_at_str: str) -> str:
    """Converts UTC or ISO timestamp to YYYY-MM-DD in Asia/Kolkata timezone."""
    clean_str = started_at_str.replace("Z", "+00:00")
    dt = datetime.fromisoformat(clean_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zoneinfo.ZoneInfo("UTC"))
    return dt.astimezone(IST).strftime("%Y-%m-%d")


def is_within_7_days(dispute_date_str: str, reference_dt: datetime) -> bool:
    """Checks if dispute date is within the last 7 calendar days from reference_dt in IST."""
    ref_ist_date = reference_dt.astimezone(IST).date()
    dispute_d = date.fromisoformat(dispute_date_str)
    delta = (ref_ist_date - dispute_d).days
    return 0 <= delta <= settings.DISPUTE_LOOKBACK_DAYS


@dataclass
class DisputeItem:
    item_type: str  # surge_missing, incentive_missing, duplicate_penalty, unpaid_trip, distance_dispute
    date: str
    trip_id: Optional[str]
    amount_claimed: Optional[int]
    amount_owed: int
    details: str
    requires_ops: bool = False
    escalation_reason: Optional[str] = None


@dataclass
class AuditReport:
    rider_id: str
    items: List[DisputeItem] = field(default_factory=list)
    total_owed: int = 0
    outside_policy_window: bool = False
    outside_window_dates: List[str] = field(default_factory=list)
    has_medical_or_accident: bool = False
    has_distance_dispute: bool = False
    has_unauthorized_claim: bool = False
    has_jailbreak: bool = False
    
    @property
    def has_discrepancy(self) -> bool:
        return self.total_owed > 0


def calculate_trip_fare(trip: Trip, actual_paid: Optional[int] = None) -> int:
    """
    Trip fare calculation according to docs/policy.md:
    ₹25 base + ₹6 for each km after the first 2 km.
    Surge multiplier applies to whole trip fare. Round 0.5 up.
    """
    if trip.status != "completed":
        return 0

    # If already paid base fare, surge is multiplier on the base fare
    if actual_paid is not None and actual_paid > 0 and trip.surge_multiplier > 1.0:
        return round_half_up(actual_paid * trip.surge_multiplier)

    # Standard formula
    billable_distance = max(0.0, float(trip.distance_km) - 2.0)
    base_fare = 25.0 + billable_distance * 6.0
    total_fare = base_fare * float(trip.surge_multiplier)
    return round_half_up(total_fare)


def normalize_rider_id(raw: str) -> str:
    """
    Normalizes rider IDs to standard format (e.g. 'R7' -> 'R007').
    Handles raw CSV data inconsistencies.
    """
    raw = raw.strip()
    import re as _re
    m = _re.match(r'^[Rr](\d+)$', raw)
    if m:
        return f"R{int(m.group(1)):03d}"
    return raw


def audit_specific_trip(
    session: Session,
    rider_id: str,
    trip_id: str,
    reference_dt: datetime
) -> Optional[DisputeItem]:
    """Audits a single trip for a rider."""
    stmt = select(Trip).where(Trip.trip_id == trip_id)
    trip = session.execute(stmt).scalars().first()

    if not trip:
        return DisputeItem(
            item_type="not_found",
            date="",
            trip_id=trip_id,
            amount_claimed=None,
            amount_owed=0,
            details=f"Trip {trip_id} not found in system records.",
            requires_ops=True,
            escalation_reason=f"Trip {trip_id} does not exist in records"
        )

    # Normalize both IDs before comparison (handles CSV quirks like 'R7' vs 'R007')
    trip_rider_normalized = normalize_rider_id(trip.rider_id)
    rider_id_normalized = normalize_rider_id(rider_id)

    # Impersonation / Cross-rider check
    if trip_rider_normalized != rider_id_normalized:
        return DisputeItem(
            item_type="unauthorized",
            date=parse_ist_date(trip.started_at),
            trip_id=trip_id,
            amount_claimed=None,
            amount_owed=0,
            details=f"Trip {trip_id} does not belong to rider {rider_id}.",
            requires_ops=True,
            escalation_reason=f"Rider attempted to query order {trip_id} belonging to another rider {trip.rider_id}"
        )
    trip_date = parse_ist_date(trip.started_at)
    if not is_within_7_days(trip_date, reference_dt):
        return DisputeItem(
            item_type="outside_window",
            date=trip_date,
            trip_id=trip_id,
            amount_claimed=None,
            amount_owed=0,
            details=f"Trip {trip_id} on {trip_date} is older than 7 days.",
            requires_ops=True,
            escalation_reason="Dispute date outside 7-day policy window"
        )

    # Check actual payments in payout_lines
    payout_stmt = select(PayoutLine).where(
        PayoutLine.rider_id == rider_id,
        PayoutLine.trip_id == trip_id,
        PayoutLine.line_type == "trip"
    )
    payout_lines = session.execute(payout_stmt).scalars().all()
    actual_paid = sum(p.amount for p in payout_lines)

    if trip.status == "completed":
        expected_fare = calculate_trip_fare(trip, actual_paid if actual_paid > 0 else None)
        diff = expected_fare - actual_paid
        if diff > 0:
            return DisputeItem(
                item_type="surge_missing" if trip.surge_multiplier > 1.0 else "unpaid_trip",
                date=trip_date,
                trip_id=trip_id,
                amount_claimed=None,
                amount_owed=diff,
                details=f"Order {trip_id} ({trip_date}): expected ₹{expected_fare}, received ₹{actual_paid}. Difference: ₹{diff}."
            )
        else:
            return DisputeItem(
                item_type="correctly_paid",
                date=trip_date,
                trip_id=trip_id,
                amount_claimed=None,
                amount_owed=0,
                details=f"Order {trip_id} ({trip_date}) was correctly paid ₹{actual_paid}."
            )
    elif trip.status == "cancelled_by_customer":
        return DisputeItem(
            item_type="customer_cancelled",
            date=trip_date,
            trip_id=trip_id,
            amount_claimed=None,
            amount_owed=0,
            details=f"Order {trip_id} was cancelled by customer, so no payout is generated."
        )
    elif trip.status == "cancelled_by_rider":
        return DisputeItem(
            item_type="rider_cancelled",
            date=trip_date,
            trip_id=trip_id,
            amount_claimed=None,
            amount_owed=0,
            details=f"Order {trip_id} was cancelled by you (cancellation penalty applies)."
        )

    return None


def audit_date_payouts(
    session: Session,
    rider_id: str,
    target_date_str: str,
    reference_dt: datetime
) -> List[DisputeItem]:
    """Audits all trips, daily incentive, and cancellation penalties for a rider on a target date."""
    items: List[DisputeItem] = []

    if not is_within_7_days(target_date_str, reference_dt):
        items.append(DisputeItem(
            item_type="outside_window",
            date=target_date_str,
            trip_id=None,
            amount_claimed=None,
            amount_owed=0,
            details=f"Date {target_date_str} is older than 7 days.",
            requires_ops=True,
            escalation_reason=f"Date {target_date_str} outside 7-day policy window"
        ))
        return items

    # Fetch all trips for this rider (handle CSV rider_id inconsistencies like 'R7' vs 'R007')
    rider_id_normalized = normalize_rider_id(rider_id)
    all_trips_stmt = select(Trip)
    all_trips_raw = session.execute(all_trips_stmt).scalars().all()
    all_trips = [t for t in all_trips_raw if normalize_rider_id(t.rider_id) == rider_id_normalized]

    # Filter by target date in IST
    date_trips = [t for t in all_trips if parse_ist_date(t.started_at) == target_date_str]

    # Fetch payout lines for that date
    payouts_stmt = select(PayoutLine).where(
        PayoutLine.rider_id == rider_id,
        PayoutLine.payout_date == target_date_str
    )
    payout_lines = session.execute(payouts_stmt).scalars().all()

    # 1. Check Unpaid or Underpaid Trips
    paid_trip_ids = {p.trip_id: p.amount for p in payout_lines if p.line_type == "trip" and p.trip_id}
    
    # Distinct trips map
    unique_completed_trips = {}
    for t in date_trips:
        if t.status == "completed":
            unique_completed_trips[t.trip_id] = t

    for t_id, t in unique_completed_trips.items():
        actual_paid = paid_trip_ids.get(t_id, 0)
        expected_fare = calculate_trip_fare(t, actual_paid if actual_paid > 0 else None)
        diff = expected_fare - actual_paid
        if diff > 0:
            item_type = "surge_missing" if t.surge_multiplier > 1.0 else "unpaid_trip"
            items.append(DisputeItem(
                item_type=item_type,
                date=target_date_str,
                trip_id=t_id,
                amount_claimed=None,
                amount_owed=diff,
                details=f"Order {t_id}: expected ₹{expected_fare}, received ₹{actual_paid} (diff ₹{diff})."
            ))

    # 2. Check Daily Incentive (>= 12 completed trips in IST)
    completed_count = len(unique_completed_trips)
    incentive_paid = any(p.line_type == "daily_incentive" and p.amount >= 150 for p in payout_lines)

    if completed_count >= 12 and not incentive_paid:
        items.append(DisputeItem(
            item_type="incentive_missing",
            date=target_date_str,
            trip_id=None,
            amount_claimed=150,
            amount_owed=150,
            details=f"Completed {completed_count} trips on {target_date_str}. ₹150 daily incentive was missing."
        ))
    elif completed_count < 12 and not incentive_paid:
        items.append(DisputeItem(
            item_type="incentive_ineligible",
            date=target_date_str,
            trip_id=None,
            amount_claimed=None,
            amount_owed=0,
            details=f"{completed_count} completed trips on {target_date_str}. Minimum 12 trips required for incentive."
        ))

    # 3. Check Cancellation Penalties
    actual_rider_cancellations = sum(1 for t in date_trips if t.status == "cancelled_by_rider")
    penalty_lines = [p for p in payout_lines if p.line_type == "cancellation_penalty"]
    penalties_charged_count = len(penalty_lines)
    
    if penalties_charged_count > actual_rider_cancellations:
        excess_penalties = penalties_charged_count - actual_rider_cancellations
        refund_amount = excess_penalties * 10
        items.append(DisputeItem(
            item_type="duplicate_penalty",
            date=target_date_str,
            trip_id=None,
            amount_claimed=refund_amount,
            amount_owed=refund_amount,
            details=f"Charged {penalties_charged_count} cancellation penalties on {target_date_str} for {actual_rider_cancellations} cancelled trips. Extra penalty refund: ₹{refund_amount}."
        ))

    return items
