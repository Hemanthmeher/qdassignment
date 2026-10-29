import os
import sys
import json
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
import httpx


def load_conversations() -> List[Dict[str, Any]]:
    base_dir = Path(__file__).resolve().parent.parent
    conv_file = base_dir / "data" / "conversations.json"
    if not conv_file.exists():
        print(f"Error: Could not find conversations file at {conv_file}")
        sys.exit(1)
    with open(conv_file, "r", encoding="utf-8") as f:
        return json.load(f)


def get_payswift_payout_total(payswift_url: str, rider_id: str) -> int:
    try:
        resp = httpx.get(f"{payswift_url.rstrip('/')}/v1/payouts", params={"rider_id": rider_id}, timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                payouts = data.get("data", data.get("payouts", []))
            elif isinstance(data, list):
                payouts = data
            else:
                payouts = []
            return sum(int(p.get("amount", 0)) for p in payouts if isinstance(p, dict))
    except Exception as e:
        print(f"Warning: Could not fetch PaySwift payouts: {e}")
    return 0


def run_eval(service_url: str, payswift_url: str):
    service_url = service_url.rstrip("/")
    payswift_url = payswift_url.rstrip("/")

    print("=" * 80)
    print("QUICKDROP RIDER PAYOUT DISPUTE DESK - EVALUATION HARNESS")
    print("=" * 80)
    print(f"Target Service URL:  {service_url}")
    print(f"PaySwift URL:        {payswift_url}")
    print("-" * 80)

    # 1. Health check
    try:
        resp = httpx.get(f"{service_url}/health", timeout=5.0)
        if resp.status_code != 200:
            print(f"Health check failed with status {resp.status_code}: {resp.text}")
            sys.exit(1)
        print("Service is healthy and ready for evaluations.")
    except Exception as e:
        print(f"Could not connect to service at {service_url}: {e}")
        sys.exit(1)

    conversations = load_conversations()
    print(f"Loaded {len(conversations)} test scenarios from data/conversations.json\n")

    results = []
    total_scenarios = len(conversations)
    passed_scenarios = 0

    for idx, conv in enumerate(conversations, 1):
        scenario_desc = conv.get("scenario", f"Scenario {idx}")
        rider_id = conv["rider_id"]
        turns = conv["turns"]
        expected = conv.get("expected", {})

        print(f"[{idx:02d}/{total_scenarios:02d}] Testing Rider {rider_id}: {scenario_desc[:45]}...", end=" ", flush=True)
        scenario_start = time.time()

        # Reset service state if supported for fresh test
        try:
            httpx.post(f"{service_url}/reset", timeout=3.0)
        except Exception:
            pass

        # Record initial payouts in PaySwift for this rider
        initial_payout = get_payswift_payout_total(payswift_url, rider_id)

        replies = []
        turn_latencies = []
        error_msg = None

        # Execute conversation turns
        for turn in turns:
            if turn.get("from") == "rider":
                payload = {
                    "message_id": turn["message_id"],
                    "rider_id": rider_id,
                    "text": turn["text"],
                    "received_at": turn["received_at"],
                }
                t0 = time.time()
                try:
                    res = httpx.post(f"{service_url}/messages", json=payload, timeout=12.0)
                    t_turn = time.time() - t0
                    turn_latencies.append(t_turn)

                    if res.status_code != 200:
                        error_msg = f"HTTP {res.status_code}: {res.text}"
                        break
                    
                    data = res.json()
                    reply = data.get("reply", "")
                    replies.append(reply)
                except Exception as e:
                    error_msg = f"Request exception: {e}"
                    break

        scenario_time = time.time() - scenario_start

        if error_msg:
            print(f"FAILED ({error_msg}) [{scenario_time:.2f}s]")
            results.append({"scenario": scenario_desc, "rider_id": rider_id, "pass": False, "reason": error_msg})
            continue

        # Post-conversation state verification
        final_payout = get_payswift_payout_total(payswift_url, rider_id)
        payout_delta = final_payout - initial_payout

        # Fetch pending ops items
        pending_res = httpx.get(f"{service_url}/ops/pending", timeout=5.0)
        all_pending = pending_res.json() if pending_res.status_code == 200 else []
        rider_pending = [p for p in all_pending if p.get("rider_id") == rider_id]

        actual_approval_amount = sum(
            int(p.get("amount", 0)) for p in rider_pending if p.get("type") == "approval"
        )
        has_approval = any(p.get("type") == "approval" for p in rider_pending)
        has_escalation = any(p.get("type") == "escalation" for p in rider_pending)

        # Check conditions
        passed = True
        fail_reasons = []

        # 1. Check Payout & Approval
        if "one_of" in expected:
            matched_one = False
            for opt in expected["one_of"]:
                exp_payout = opt.get("payout", 0)
                exp_approval = opt.get("approval")
                
                payout_matches = (payout_delta == exp_payout)
                if exp_approval is None:
                    approval_matches = (actual_approval_amount == 0 and not has_approval)
                else:
                    approval_matches = (actual_approval_amount == exp_approval)

                if payout_matches and approval_matches:
                    matched_one = True
                    break

            if not matched_one:
                passed = False
                fail_reasons.append(
                    f"one_of mismatch: actual payout={payout_delta}, approval={actual_approval_amount}, expected one of {expected['one_of']}"
                )
        else:
            exp_payout = expected.get("payout", 0)
            if payout_delta != exp_payout:
                passed = False
                fail_reasons.append(f"payout mismatch: actual={payout_delta}, expected={exp_payout}")

            exp_approval = expected.get("approval")
            if exp_approval is not None:
                if actual_approval_amount != exp_approval:
                    passed = False
                    fail_reasons.append(f"approval mismatch: actual={actual_approval_amount}, expected={exp_approval}")
            else:
                if actual_approval_amount > 0:
                    passed = False
                    fail_reasons.append(f"unexpected approval: actual={actual_approval_amount}, expected=None")

        # 2. Check Escalation
        exp_esc = expected.get("escalation", "no")
        if exp_esc == "required" and not has_escalation:
            passed = False
            fail_reasons.append("escalation required but not created")
        elif exp_esc == "no" and has_escalation:
            passed = False
            fail_reasons.append("unexpected escalation created")
        # 'optional' accepts either

        # 3. Check Reply Mentions
        reply_mentions = expected.get("reply_mentions", [])
        combined_replies = " ".join(replies).lower()
        for mention in reply_mentions:
            if str(mention).lower() not in combined_replies:
                passed = False
                fail_reasons.append(f"reply missing mention: '{mention}'")

        # 4. Check Latency (< 10s per turn)
        for t_lat in turn_latencies:
            if t_lat > 10.0:
                passed = False
                fail_reasons.append(f"latency exceeded 10s: {t_lat:.2f}s")

        if passed:
            passed_scenarios += 1
            print(f"PASSED [Payout: ₹{payout_delta}, Appr: ₹{actual_approval_amount}, Time: {scenario_time:.2f}s]")
            results.append({"scenario": scenario_desc, "rider_id": rider_id, "pass": True})
        else:
            print(f"FAILED ({', '.join(fail_reasons)}) [Time: {scenario_time:.2f}s]")
            results.append({"scenario": scenario_desc, "rider_id": rider_id, "pass": False, "reason": "; ".join(fail_reasons)})

    # Summary Report
    print("\n" + "=" * 80)
    print("EVALUATION SUMMARY REPORT")
    print("=" * 80)
    print(f"Total Scenarios Evaluated: {total_scenarios}")
    print(f"Passed:                    {passed_scenarios}")
    print(f"Failed:                    {total_scenarios - passed_scenarios}")
    pass_rate = (passed_scenarios / total_scenarios) * 100
    print(f"Pass Rate:                 {pass_rate:.1f}%")
    print("=" * 80)

    if passed_scenarios == total_scenarios:
        print("ALL EVALUATION CHECKS PASSED PERFECTLY!")
        sys.exit(0)
    else:
        print("Some evaluation checks did not pass. Check the details above.")
        sys.exit(1)


if __name__ == "__main__":
    target_service_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
    payswift_target_url = os.getenv("PAYSWIFT_BASE_URL", "http://localhost:8081")
    run_eval(target_service_url, payswift_target_url)
