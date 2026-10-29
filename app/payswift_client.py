import asyncio
import logging
import uuid
import httpx
from typing import Optional, Dict, Any, List

from app.config import settings

logger = logging.getLogger("quickdrop.payswift")


class PaySwiftClient:
    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or settings.PAYSWIFT_BASE_URL).rstrip("/")
        self._client: Optional[httpx.AsyncClient] = None

    def get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=5.0)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def check_health(self) -> bool:
        try:
            client = self.get_client()
            resp = await client.get(f"{self.base_url}/health", timeout=1.0)
            return resp.status_code == 200
        except Exception as e:
            logger.warning(f"PaySwift health check failed: {e}")
            return False

    async def list_payouts(self, rider_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch payouts from PaySwift ledger (reconcile against PaySwift)."""
        url = f"{self.base_url}/v1/payouts"
        params = {"rider_id": rider_id} if rider_id else {}
        client = self.get_client()
        
        for attempt in range(3):
            try:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict):
                        return data.get("data", data.get("payouts", []))
                    elif isinstance(data, list):
                        return data
                    return []
                elif resp.status_code in (429, 500, 502, 503, 504):
                    await asyncio.sleep(0.3 * (2 ** attempt))
                    continue
                else:
                    logger.error(f"PaySwift list_payouts error: {resp.status_code} {resp.text}")
                    break
            except Exception as e:
                logger.warning(f"PaySwift list_payouts attempt {attempt + 1} failed: {e}")
                await asyncio.sleep(0.3 * (2 ** attempt))
        return []

    async def send_payout(
        self,
        rider_id: str,
        amount: int,
        reference: str,
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Sends payout to PaySwift with automatic idempotency key and exponential backoff retry.
        Handles PaySwift sandbox chaos (429, 5xx).
        """
        if not idempotency_key:
            idempotency_key = f"ps-{rider_id}-{amount}-{uuid.uuid4().hex[:8]}"

        url = f"{self.base_url}/v1/payouts"
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key
        }
        payload = {
            "rider_id": rider_id,
            "amount": int(amount),
            "reference": reference
        }
        client = self.get_client()

        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code in (200, 201):
                    logger.info(f"PaySwift payout succeeded for {rider_id}: ₹{amount}")
                    return resp.json()
                elif resp.status_code == 409:
                    logger.info(f"PaySwift duplicate/409 idempotent response: {resp.text}")
                    return resp.json() if resp.text else {"status": "success", "amount": amount}
                elif resp.status_code in (429, 500, 502, 503, 504):
                    logger.warning(f"PaySwift returned {resp.status_code}, retrying (attempt {attempt + 1}/{max_retries})...")
                    await asyncio.sleep(0.3 * (1.5 ** attempt))
                    continue
                else:
                    logger.error(f"PaySwift payout rejected {resp.status_code}: {resp.text}")
                    return {"status": "failed", "error": resp.text, "status_code": resp.status_code}
            except Exception as e:
                logger.warning(f"PaySwift connection error attempt {attempt + 1}: {e}")
                await asyncio.sleep(0.3 * (1.5 ** attempt))

        return {"status": "failed", "error": "PaySwift max retries exhausted"}


payswift_client = PaySwiftClient()
