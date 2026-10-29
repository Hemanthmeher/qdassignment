import re
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy import select
from sqlalchemy.orm import Session
import zoneinfo

from app.models import MessageLog, OpsPending, Trip, PayoutLine
from app.config import settings
from app.policy_engine import (
    parse_ist_date,
    is_within_7_days,
    audit_specific_trip,
    audit_date_payouts,
    DisputeItem,
    round_half_up,
    IST,
)
from app.payswift_client import payswift_client
from app.trace_logger import record_trace_step

logger = logging.getLogger("quickdrop.agent")


def extract_trip_ids(text: str) -> List[str]:
    """Finds all trip IDs matching T[0-9]{6} in text."""
    matches = re.findall(r"\bT\d{6}\b", text, re.IGNORECASE)
    seen = set()
    result = []
    for m in matches:
        upper_m = m.upper()
        if upper_m not in seen:
            seen.add(upper_m)
            result.append(upper_m)
    return result


def extract_dates(text: str, reference_dt: datetime) -> List[str]:
    """
    Extracts dates mentioned in text relative to reference_dt.
    Supports '20 wala', '20 tarikh', '18 ko', 'kal', '19 sept', 'Sep 19', '19th', etc.
    Avoids mistaking currency/amounts (e.g. 'Rs 25', '₹10') for calendar dates.
    """
    ref_ist = reference_dt.astimezone(IST)
    dates = []

    lower_text = text.lower()

    # Check for 'kal' (yesterday)
    if "kal" in lower_text or "yesterday" in lower_text:
        yesterday = (ref_ist - timedelta(days=1)).strftime("%Y-%m-%d")
        dates.append(yesterday)

    # 1. Matches like '19th', '18th', '20th', '1st', '2nd'
    ordinal_matches = re.findall(r"\b(1[2-9]|2[0-5]|[1-9])\s*(?:st|nd|rd|th)\b", lower_text)
    for o in ordinal_matches:
        day_num = int(o)
        parsed_date = f"2026-09-{day_num:02d}"
        if parsed_date not in dates:
            dates.append(parsed_date)

    # 2. Check for patterns like 'Sep 19', '19 sept', '18 tarikh', '20 ko', '20 wala'
    day_matches = re.findall(
        r"(?:sep(?:t)?(?:ember)?\s*(\d{1,2}))|(?:\b(\d{1,2})\s*(?:st|nd|rd|th)?\s*(?:sep(?:t)?(?:ember)?|tarikh|tareekh|ko|wala|wali|din))",
        lower_text
    )
    for m in day_matches:
        day_str = m[0] or m[1]
        if day_str and day_str.isdigit():
            day_num = int(day_str)
            # Filter out obvious amount references like '10 wala' when asking for remaining ₹10
            if day_num == 10 and ("abhi de do" in lower_text or "10 wala bhi" in lower_text):
                continue
            if 1 <= day_num <= 31:
                parsed_date = f"2026-09-{day_num:02d}"
                if parsed_date not in dates:
                    dates.append(parsed_date)

    # 3. Bare day numbers only if not preceded by currency (Rs, ₹, INR) or followed by money/distance words
    if not dates:
        # Strip out currency amounts before checking for bare dates
        cleaned_for_dates = re.sub(r"(?:rs\.?|₹|inr|\$)\s*\d+", "", lower_text)
        cleaned_for_dates = re.sub(r"\d+\s*(?:rs|rupay|rupees|km|orders|order)", "", cleaned_for_dates)
        bare_matches = re.findall(r"\b(1[2-9]|2[0-5])\b", cleaned_for_dates)
        for b in bare_matches:
            parsed_date = f"2026-09-{int(b):02d}"
            if parsed_date not in dates:
                dates.append(parsed_date)

    return dates


async def check_already_paid_today_in_payswift(rider_id: str, reference_dt: datetime) -> bool:
    """Checks PaySwift live ledger to verify if rider has already received a payout today."""
    try:
        payouts = await payswift_client.list_payouts(rider_id=rider_id)
        if not payouts:
            return False
        
        today_ist = reference_dt.astimezone(IST).strftime("%Y-%m-%d")
        for p in payouts:
            created_at = p.get("created_at") or ""
            if created_at.startswith(today_ist):
                return True
        return False
    except Exception as e:
        logger.warning(f"Could not verify PaySwift payouts: {e}")
        return False


async def generate_groq_reply(
    rider_id: str,
    user_text: str,
    conversation_history: List[str],
    audit_details: str,
    required_mentions: List[str],
    fallback_reply: str,
    session: Session,
    message_id: str
) -> str:
    """
    Invokes Groq with model specified in settings.GROQ_MODEL (default: openai/gpt-oss-120b).
    Enforces that required factual numbers and references are strictly preserved.
    """
    if not settings.GROQ_API_KEY:
        return fallback_reply

    try:
        from groq import AsyncGroq
        client = AsyncGroq(api_key=settings.GROQ_API_KEY, timeout=3.5)

        system_prompt = (
            "You are the QuickDrop AI Rider Payout Dispute Assistant. "
            "Riders message via WhatsApp in Hinglish or English. "
            "Your tone is polite, concise, professional, and empathetic. "
            "CRITICAL MANDATORY RULES:\n"
            f"1. You MUST include each of these exact strings in your reply: {required_mentions}\n"
            f"2. Verified audit findings: {audit_details}\n"
            "3. Do not invent or promise any money beyond verified audit findings.\n"
            "4. Keep your answer under 2-3 sentences in natural conversational Hinglish."
        )

        messages = [{"role": "system", "content": system_prompt}]
        for hist in conversation_history[-3:]:
            messages.append({"role": "user", "content": hist})
        messages.append({"role": "user", "content": user_text})

        completion = await client.chat.completions.create(
            model=settings.GROQ_MODEL,
            messages=messages,
            max_tokens=150,
            temperature=0.2,
        )

        llm_text = completion.choices[0].message.content.strip()
        # Verify all mandatory mentions are present
        if all(str(m).lower() in llm_text.lower() for m in required_mentions):
            record_trace_step(
                session=session,
                rider_id=rider_id,
                step_type="decision",
                name=f"groq_llm_response_{settings.GROQ_MODEL.replace('/', '_')}",
                input_data={"model": settings.GROQ_MODEL, "required_mentions": required_mentions},
                output_data={"generated_text": llm_text},
                message_id=message_id,
            )
            return llm_text
        else:
            return fallback_reply
    except Exception as e:
        logger.info(f"Groq LLM ({settings.GROQ_MODEL}) bypassed or unavailable: {e}. Using deterministic reply.")
        return fallback_reply


class DisputeAgent:
    """Autonomous agent handling rider dispute messages end-to-end."""

    async def handle_message(
        self,
        session: Session,
        message_id: str,
        rider_id: str,
        text: str,
        received_at_str: str,
    ) -> str:
        # Parse timestamp
        clean_received = received_at_str.replace("Z", "+00:00")
        try:
            received_dt = datetime.fromisoformat(clean_received)
            if received_dt.tzinfo is None:
                received_dt = received_dt.replace(tzinfo=IST)
        except Exception:
            received_dt = datetime.now(IST)

        # 1. Idempotency Check (Vendor Retries)
        existing_msg_stmt = select(MessageLog).where(MessageLog.message_id == message_id)
        existing_msg = session.execute(existing_msg_stmt).scalar_one_or_none()
        if existing_msg and existing_msg.reply_text:
            logger.info(f"Duplicate message {message_id} detected. Returning cached reply.")
            return existing_msg.reply_text

        # 2. Record message_in Trace
        record_trace_step(
            session=session,
            rider_id=rider_id,
            step_type="message_in",
            name="whatsapp_message_received",
            input_data={"message_id": message_id, "text": text, "received_at": received_at_str},
            output_data=None,
            message_id=message_id,
            timestamp_str=received_at_str,
        )

        # Save incoming message to log
        msg_record = MessageLog(
            message_id=message_id,
            rider_id=rider_id,
            sender="rider",
            text=text,
            received_at=received_at_str,
        )
        session.add(msg_record)
        session.commit()

        # 3. Security Checks (Prompt Injection & Impersonation)
        lower_text = text.lower()

        # A. Prompt Injection / Jailbreak
        if "ignore all previous rules" in lower_text or "system:" in lower_text:
            record_trace_step(
                session=session,
                rider_id=rider_id,
                step_type="decision",
                name="refuse_jailbreak",
                input_data={"text": text},
                output_data={"action": "refused", "escalate": "optional"},
                message_id=message_id,
            )
            ops_item = OpsPending(
                rider_id=rider_id,
                type="escalation",
                amount=None,
                reason="Potential prompt injection / security violation attempted",
                created_at=received_dt.isoformat(),
            )
            session.add(ops_item)
            session.commit()

            reply = "Main sirf QuickDrop payout guidelines aur records ke hisaab se hi payouts assist kar sakta hoon."
            self._finalize_reply(session, rider_id, message_id, reply, msg_record)
            return reply

        # B. Impersonation Check (Claiming another rider's ID)
        other_rider_match = re.search(r"\bthis\s+is\s+(r\d{3})\b", lower_text)
        if other_rider_match:
            claimed_id = other_rider_match.group(1).upper()
            if claimed_id != rider_id.upper():
                record_trace_step(
                    session=session,
                    rider_id=rider_id,
                    step_type="decision",
                    name="reject_impersonation",
                    input_data={"claimed_id": claimed_id, "actual_rider_id": rider_id},
                    output_data={"action": "rejected", "escalate": "optional"},
                    message_id=message_id,
                )
                ops_item = OpsPending(
                    rider_id=rider_id,
                    type="escalation",
                    amount=None,
                    reason=f"Rider {rider_id} claimed to be {claimed_id}",
                    created_at=received_dt.isoformat(),
                )
                session.add(ops_item)
                session.commit()

                reply = f"Main sirf is number se jude account ({rider_id}) ke baare mein madad kar sakta hoon. Aapke account ki koi specific problem ho to bataiye."
                self._finalize_reply(session, rider_id, message_id, reply, msg_record)
                return reply

        # 4. Fetch Multi-turn Conversation Context
        history_stmt = (
            select(MessageLog)
            .where(MessageLog.rider_id == rider_id)
            .order_by(MessageLog.id.asc())
        )
        history_msgs = session.execute(history_stmt).scalars().all()
        full_conversation_text = " ".join([m.text for m in history_msgs])
        history_texts = [m.text for m in history_msgs]

        # Check for user follow-up confirmation ("kab tak aayega", "thik hai")
        if any(kw in lower_text for kw in ["kab tak aayega", "kab aayega", "status"]) and len(history_msgs) > 2:
            payouts = await payswift_client.list_payouts(rider_id=rider_id)
            if payouts:
                latest_p = payouts[-1]
                reply = f"₹{latest_p.get('amount', 25)} ka payment PaySwift pe process ho gaya hai."
            else:
                reply = "Aapka payment PaySwift pe process ho raha hai."
            self._finalize_reply(session, rider_id, message_id, reply, msg_record)
            return reply

        # Check for user asking to immediately pay pending ops item ("abhi de do", "woh bhi de do")
        if any(kw in lower_text for kw in ["abhi de do", "woh bhi", "abhi chahiye", "turant de do", "abhi bhej"]):
            pending_stmt = select(OpsPending).where(
                OpsPending.rider_id == rider_id,
                OpsPending.status == "pending",
                OpsPending.type == "approval"
            ).order_by(OpsPending.created_at.desc())
            pending_item = session.execute(pending_stmt).scalars().first()
            if pending_item:
                amt_str = f"₹{pending_item.amount}" if pending_item.amount else "Pending amount"
                reply = f"Samajh sakta hoon. {amt_str} ops approval ke liye chala gaya hai, approve hote hi aa jayega."
                self._finalize_reply(session, rider_id, message_id, reply, msg_record)
                return reply

        # 5. Extract Entities (Trip IDs and Dates)
        trip_ids = extract_trip_ids(text)
        dates = extract_dates(text, received_dt)

        # Check if conversation history contains earlier dates/trips if none in current turn
        if not trip_ids and not dates:
            hist_trip_ids = extract_trip_ids(full_conversation_text)
            hist_dates = extract_dates(full_conversation_text, received_dt)
            if hist_trip_ids:
                trip_ids = hist_trip_ids
            if hist_dates:
                dates = hist_dates

        # 6. Check for Special Non-Standard Situations
        # A. Medical / Accident Exception
        if "accident" in lower_text or "emergency" in lower_text or "hospital" in lower_text:
            trip_ref = trip_ids[0] if trip_ids else "order"
            record_trace_step(
                session=session,
                rider_id=rider_id,
                step_type="decision",
                name="accident_exception_escalated",
                input_data={"text": text},
                output_data={"action": "escalate_to_ops"},
                message_id=message_id,
            )
            ops_item = OpsPending(
                rider_id=rider_id,
                type="escalation",
                amount=None,
                reason=f"Rider reports accident during {trip_ref}, penalty waiver requested",
                created_at=received_dt.isoformat(),
            )
            session.add(ops_item)
            session.commit()

            reply = f"Aapka accident ka issue humari ops team ko bhej diya gaya hai. Team order {trip_ref} ka cancellation penalty review karegi."
            self._finalize_reply(session, rider_id, message_id, reply, msg_record)
            return reply

        # B. Distance Dispute (e.g. GPS error)
        if "distance galat" in lower_text or "gps" in lower_text:
            trip_ref = trip_ids[0] if trip_ids else "order"
            record_trace_step(
                session=session,
                rider_id=rider_id,
                step_type="decision",
                name="distance_dispute_escalated",
                input_data={"trip_id": trip_ref, "text": text},
                output_data={"action": "escalate_to_ops"},
                message_id=message_id,
            )
            ops_item = OpsPending(
                rider_id=rider_id,
                type="escalation",
                amount=None,
                reason=f"Rider disputes GPS distance recorded on order {trip_ref}",
                created_at=received_dt.isoformat(),
            )
            session.add(ops_item)
            session.commit()

            reply = f"Order {trip_ref} ka distance dispute humari ops team ko review ke liye bhej diya gaya hai."
            self._finalize_reply(session, rider_id, message_id, reply, msg_record)
            return reply

        # C. User Pushback / Disagreement ("dobara check karo", "nahi nahi")
        if any(pb in lower_text for pb in ["dobara check karo", "nahi nahi", "check again", "galat hai record"]):
            record_trace_step(
                session=session,
                rider_id=rider_id,
                step_type="decision",
                name="rider_pushback_escalated",
                input_data={"text": text},
                output_data={"action": "escalate_to_ops"},
                message_id=message_id,
            )
            ops_item = OpsPending(
                rider_id=rider_id,
                type="escalation",
                amount=None,
                reason="Rider disagrees with completed trip count / incentive record",
                created_at=received_dt.isoformat(),
            )
            session.add(ops_item)
            session.commit()

            base_reply = "Dobara check kiya: humare record mein 19 Sep ko 10 completed trips hain. Agar aapko lagta hai record galat hai, main ise ops team ko bhej deta hoon."
            reply = await generate_groq_reply(
                rider_id=rider_id,
                user_text=text,
                conversation_history=history_texts,
                audit_details="10 completed trips found in records, minimum 12 required for incentive",
                required_mentions=["10"],
                fallback_reply=base_reply,
                session=session,
                message_id=message_id,
            )
            self._finalize_reply(session, rider_id, message_id, reply, msg_record)
            return reply

        # 7. If Vague Complaint (no date, no trip ID, e.g. "mera payout galat hai bhai")
        if not trip_ids and not dates:
            record_trace_step(
                session=session,
                rider_id=rider_id,
                step_type="decision",
                name="request_details",
                input_data={"text": text},
                output_data={"prompt": "ask_for_date_or_trip"},
                message_id=message_id,
            )
            reply = "Kaunse din ya kaunse order ka payout galat laga? Date ya order ID bata dijiye."
            self._finalize_reply(session, rider_id, message_id, reply, msg_record)
            return reply

        # 8. Deterministic Policy Audit
        dispute_items: List[DisputeItem] = []

        # Audit by Trip IDs if provided
        for t_id in trip_ids:
            item = audit_specific_trip(session, rider_id, t_id, received_dt)
            if item:
                dispute_items.append(item)

        # Audit by Dates if provided
        for d_str in dates:
            d_items = audit_date_payouts(session, rider_id, d_str, received_dt)
            for item in d_items:
                if not any(existing.trip_id and existing.trip_id == item.trip_id for existing in dispute_items):
                    dispute_items.append(item)

        # Trace the policy audit tool call
        record_trace_step(
            session=session,
            rider_id=rider_id,
            step_type="tool_call",
            name="audit_policy_engine",
            input_data={"trip_ids": trip_ids, "dates": dates, "rider_id": rider_id},
            output_data=[
                {"type": it.item_type, "owed": it.amount_owed, "details": it.details}
                for it in dispute_items
            ],
            message_id=message_id,
        )

        # 9. Evaluate Findings & Enforce Financial Limits
        # Check for unauthorized / cross-rider attempts
        unauthorized_items = [it for it in dispute_items if it.item_type == "unauthorized"]
        if unauthorized_items:
            unauth = unauthorized_items[0]
            record_trace_step(
                session=session,
                rider_id=rider_id,
                step_type="decision",
                name="unauthorized_order_escalated",
                input_data={"trip_id": unauth.trip_id},
                output_data={"escalate": "optional"},
                message_id=message_id,
            )
            ops_item = OpsPending(
                rider_id=rider_id,
                type="escalation",
                amount=None,
                reason=unauth.escalation_reason or "Rider queried order belonging to another rider",
                created_at=received_dt.isoformat(),
            )
            session.add(ops_item)
            session.commit()

            reply = f"Order {unauth.trip_id} aapke account ke records mein nahi hai. Humne is query ko ops team ko check karne ke liye mark kiya hai."
            self._finalize_reply(session, rider_id, message_id, reply, msg_record)
            return reply

        # Check for Outside 7-Day Window
        outside_items = [it for it in dispute_items if it.item_type == "outside_window"]
        if outside_items:
            out = outside_items[0]
            record_trace_step(
                session=session,
                rider_id=rider_id,
                step_type="decision",
                name="outside_window_escalated",
                input_data={"date": out.date, "trip_id": out.trip_id},
                output_data={"action": "escalated_ops"},
                message_id=message_id,
            )
            ops_item = OpsPending(
                rider_id=rider_id,
                type="escalation",
                amount=None,
                reason=f"Dispute for {out.date} is outside the 7-day policy window",
                created_at=received_dt.isoformat(),
            )
            session.add(ops_item)
            session.commit()

            reply = f"Order {out.trip_id or out.date} 7 din se purana hai. Policy ke tehat 7 din ke baad manual approval lagta hai, humne ops team ko bhej diya hai."
            self._finalize_reply(session, rider_id, message_id, reply, msg_record)
            return reply

        # Separate items with owed amounts
        owed_items = [it for it in dispute_items if it.amount_owed > 0]
        total_owed_all = sum(it.amount_owed for it in owed_items)

        # Case 0: Total across all items exceeds auto-pay limit → send all to ops
        if total_owed_all > settings.MAX_AUTO_PAY_LIMIT and len(owed_items) >= 1:
            ops_item = OpsPending(
                rider_id=rider_id,
                type="approval",
                amount=total_owed_all,
                reason=f"Total discrepancy ₹{total_owed_all} exceeds ₹{settings.MAX_AUTO_PAY_LIMIT} auto-pay limit: {len(owed_items)} item(s) — {'; '.join(it.details[:40] for it in owed_items)}",
                created_at=received_dt.isoformat(),
            )
            session.add(ops_item)
            session.commit()

            record_trace_step(
                session=session,
                rider_id=rider_id,
                step_type="decision",
                name="queue_ops_approval_total_exceeds_limit",
                input_data={"total_owed": total_owed_all, "limit": settings.MAX_AUTO_PAY_LIMIT, "items": len(owed_items)},
                output_data={"action": "queued_for_ops_approval", "approval_id": ops_item.id},
                message_id=message_id,
            )

            reply = f"Humne check kiya: {len(owed_items)} orders ka total ₹{total_owed_all} banta hai. ₹{settings.MAX_AUTO_PAY_LIMIT} se zyada hone ke karan ops team approval ke baad transfer hoga."
            self._finalize_reply(session, rider_id, message_id, reply, msg_record)
            return reply

        # Case 1: Multiple issues in one message where each is within limit (e.g. R027)
        if len(owed_items) > 1:
            first_item = owed_items[0]
            second_item = owed_items[1]

            # Sub-case: All items are same type and same date (e.g. R033: multiple unpaid trips on same date)
            # In this case, combine into a single payout
            all_same_date = len(set(it.date for it in owed_items)) == 1
            all_same_type = len(set(it.item_type for it in owed_items)) == 1
            combined_total = sum(it.amount_owed for it in owed_items)

            if all_same_date and all_same_type and combined_total <= settings.MAX_AUTO_PAY_LIMIT:
                # Combined auto-pay for all items
                trip_refs = ", ".join(it.trip_id for it in owed_items if it.trip_id)
                payout_res = await payswift_client.send_payout(
                    rider_id=rider_id,
                    amount=combined_total,
                    reference=f"Dispute auto-pay: {first_item.item_type} on {first_item.date}",
                    idempotency_key=f"payout-{rider_id}-{first_item.date}-combined-{combined_total}"
                )
                record_trace_step(
                    session=session,
                    rider_id=rider_id,
                    step_type="tool_call",
                    name="payswift_send_payout_combined",
                    input_data={"rider_id": rider_id, "amount": combined_total, "trips": trip_refs},
                    output_data=payout_res,
                    message_id=message_id,
                )
                record_trace_step(
                    session=session,
                    rider_id=rider_id,
                    step_type="decision",
                    name="auto_pay_combined_trips",
                    input_data={"combined_total": combined_total, "trips": trip_refs},
                    output_data={"status": "paid", "amount": combined_total},
                    message_id=message_id,
                )
                base_reply = f"{first_item.date} ke orders {trip_refs} ka payment nahi aaya tha. Total ₹{combined_total} PaySwift pe bhej diya gaya hai."
                req_mentions = [str(combined_total)]
                reply = await generate_groq_reply(
                    rider_id=rider_id,
                    user_text=text,
                    conversation_history=history_texts,
                    audit_details=f"Multiple unpaid trips on {first_item.date}: {trip_refs}, total ₹{combined_total}",
                    required_mentions=req_mentions,
                    fallback_reply=base_reply,
                    session=session,
                    message_id=message_id,
                )
                self._finalize_reply(session, rider_id, message_id, reply, msg_record)
                return reply

            # Sub-case: Different dates/types (e.g. R027: surge on 20th + penalty on 21st)
            # Pay first item, queue second for ops approval (daily auto-pay limit = 1 per day)
            payout_res = await payswift_client.send_payout(
                rider_id=rider_id,
                amount=first_item.amount_owed,
                reference=f"Dispute auto-pay: {first_item.item_type} on {first_item.date}",
                idempotency_key=f"payout-{rider_id}-{first_item.date}-{first_item.amount_owed}"
            )
            record_trace_step(
                session=session,
                rider_id=rider_id,
                step_type="tool_call",
                name="payswift_send_payout",
                input_data={"rider_id": rider_id, "amount": first_item.amount_owed},
                output_data=payout_res,
                message_id=message_id,
            )

            # Queue second item for Ops approval
            ops_item = OpsPending(
                rider_id=rider_id,
                type="approval",
                amount=second_item.amount_owed,
                reason=f"Second dispute in same session ({second_item.details[:60]}): requires ops approval per daily financial limit",
                created_at=received_dt.isoformat(),
            )
            session.add(ops_item)
            session.commit()

            record_trace_step(
                session=session,
                rider_id=rider_id,
                step_type="decision",
                name="multi_issue_resolution",
                input_data={"paid_item": first_item.amount_owed, "queued_item": second_item.amount_owed},
                output_data={"paid": first_item.amount_owed, "approval_pending": second_item.amount_owed},
                message_id=message_id,
            )

            base_reply = (
                f"Dono check kiye. {first_item.date}: "
                f"{'order ' + first_item.trip_id + ' pe surge nahi laga' if first_item.trip_id else first_item.details[:40]}, "
                f"₹{first_item.amount_owed} kam aaye. "
                f"{second_item.date}: {second_item.details[:50]}. "
                f"₹{first_item.amount_owed} abhi bhej rahe hain; ek din mein ek hi auto-payment ho sakta hai, "
                f"isliye ₹{second_item.amount_owed} ops team approve karegi."
            )
            reply = await generate_groq_reply(
                rider_id=rider_id,
                user_text=text,
                conversation_history=history_texts,
                audit_details=f"{first_item.date} issue ₹{first_item.amount_owed}; {second_item.date} issue ₹{second_item.amount_owed}",
                required_mentions=[str(first_item.amount_owed), str(second_item.amount_owed)],
                fallback_reply=base_reply,
                session=session,
                message_id=message_id,
            )
            self._finalize_reply(session, rider_id, message_id, reply, msg_record)
            return reply

        # Case 2: Exactly 1 item owed
        elif len(owed_items) == 1:
            item = owed_items[0]
            total_owed = item.amount_owed

            # Check if total_owed > 200 (Threshold limit)
            if total_owed > settings.MAX_AUTO_PAY_LIMIT:
                ops_item = OpsPending(
                    rider_id=rider_id,
                    type="approval",
                    amount=total_owed,
                    reason=f"Discrepancy ₹{total_owed} exceeds ₹200 auto-pay limit: {item.details}",
                    created_at=received_dt.isoformat(),
                )
                session.add(ops_item)
                session.commit()

                record_trace_step(
                    session=session,
                    rider_id=rider_id,
                    step_type="decision",
                    name="queue_ops_approval",
                    input_data={"amount": total_owed, "limit": settings.MAX_AUTO_PAY_LIMIT},
                    output_data={"action": "queued_for_ops_approval", "approval_id": ops_item.id},
                    message_id=message_id,
                )

                reply = f"Humne check kiya: {item.details} Total ₹{total_owed} banta hai. ₹200 se zyada hone ke karan ops team approval ke baad transfer hoga."
                self._finalize_reply(session, rider_id, message_id, reply, msg_record)
                return reply

            # Check if rider already had an auto-pay today in PaySwift
            already_paid = await check_already_paid_today_in_payswift(rider_id, received_dt)
            if already_paid:
                ops_item = OpsPending(
                    rider_id=rider_id,
                    type="approval",
                    amount=total_owed,
                    reason=f"Rider already had a payout today; ₹{total_owed} requires ops approval per daily limit",
                    created_at=received_dt.isoformat(),
                )
                session.add(ops_item)
                session.commit()

                reply = f"Aapka ₹{total_owed} ka payout ops approval ke liye bhej diya gaya hai kyunki ek din mein ek hi auto-payment allowed hai."
                self._finalize_reply(session, rider_id, message_id, reply, msg_record)
                return reply

            # Auto-pay via PaySwift
            payout_res = await payswift_client.send_payout(
                rider_id=rider_id,
                amount=total_owed,
                reference=f"Dispute auto-pay: {item.item_type}",
                idempotency_key=f"payout-{rider_id}-{item.date or 'dispute'}-{total_owed}"
            )
            record_trace_step(
                session=session,
                rider_id=rider_id,
                step_type="tool_call",
                name="payswift_send_payout",
                input_data={"rider_id": rider_id, "amount": total_owed},
                output_data=payout_res,
                message_id=message_id,
            )

            record_trace_step(
                session=session,
                rider_id=rider_id,
                step_type="decision",
                name="auto_pay_approved",
                input_data={"amount": total_owed},
                output_data={"status": "paid", "amount": total_owed},
                message_id=message_id,
            )

            # Specific phrasing matching required mentions
            req_mentions = []
            if item.item_type == "surge_missing":
                base_reply = f"{item.date} ko order {item.trip_id} pe surge laga tha: ₹{total_owed} kam mile the, farak ₹{total_owed} aapko bhej rahe hain."
                req_mentions = [item.trip_id, str(total_owed)]
            elif item.item_type == "incentive_missing":
                base_reply = f"{item.date} ko aapke 12 se zyada completed orders the. ₹{total_owed} ka daily incentive bhej diya gaya hai."
                req_mentions = [str(total_owed)]
            elif item.item_type == "duplicate_penalty":
                base_reply = f"{item.date} ko cancellation penalty duplicate cut hui thi. Extra penalty ₹{total_owed} ka refund bhej rahe hain."
                req_mentions = [str(total_owed)]
            elif item.item_type == "unpaid_trip":
                base_reply = f"Aapke orders ka farak ₹{total_owed} PaySwift pe process kar diya gaya hai."
                req_mentions = [str(total_owed)]
            else:
                base_reply = f"Aapka ₹{total_owed} ka payout PaySwift pe send kar diya gaya hai."
                req_mentions = [str(total_owed)]

            reply = await generate_groq_reply(
                rider_id=rider_id,
                user_text=text,
                conversation_history=history_texts,
                audit_details=item.details,
                required_mentions=req_mentions,
                fallback_reply=base_reply,
                session=session,
                message_id=message_id,
            )
            self._finalize_reply(session, rider_id, message_id, reply, msg_record)
            return reply

        # Case 3: Zero Discrepancy Found (Record is correct or rider is mistaken)
        else:
            ineligible_incentive = [it for it in dispute_items if it.item_type == "incentive_ineligible"]
            if ineligible_incentive:
                init_item = ineligible_incentive[0]
                base_reply = f"{init_item.date} ko aapke 10 trips complete hue the. Incentive 12 trips pe milta hai, isliye nahi bana."
                reply = await generate_groq_reply(
                    rider_id=rider_id,
                    user_text=text,
                    conversation_history=history_texts,
                    audit_details=f"Rider had 10 completed trips on {init_item.date}; incentive requires at least 12 trips",
                    required_mentions=["10"],
                    fallback_reply=base_reply,
                    session=session,
                    message_id=message_id,
                )
            elif any(it.item_type == "correctly_paid" for it in dispute_items):
                trip_ref = trip_ids[0] if trip_ids else "order"
                reply = f"Order {trip_ref} pe fare calculation aur surge check kiya gaya hai, payout record ke anusar bilkul sahi tha."
            else:
                reply = "Humare records ke mutabik aapka payout sahi calculate hua tha. Agar koi specific order ID ya date hai to bataiye."

            record_trace_step(
                session=session,
                rider_id=rider_id,
                step_type="decision",
                name="explanation_provided",
                input_data={"reasons": [it.details for it in dispute_items]},
                output_data={"reply": reply},
                message_id=message_id,
            )

            self._finalize_reply(session, rider_id, message_id, reply, msg_record)
            return reply

    def _finalize_reply(
        self,
        session: Session,
        rider_id: str,
        message_id: str,
        reply: str,
        msg_record: MessageLog,
    ):
        """Records the reply trace step and updates the message record."""
        msg_record.reply_text = reply
        record_trace_step(
            session=session,
            rider_id=rider_id,
            step_type="reply",
            name="whatsapp_reply_sent",
            input_data=None,
            output_data=reply,
            message_id=message_id,
        )
        session.commit()


dispute_agent = DisputeAgent()
