"""Centralised StrikerAPI client.

Handles auth, HTTP, JSON serialisation, errors, retries (5xx/429), timeouts,
documented rate limits, and local credit accounting.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Optional

import requests

from ingestion.config import Settings

log = logging.getLogger("strikerapi.client")


class StrikerAPIError(Exception):
    """Base error with HTTP status code."""

    def __init__(self, message: str, status_code: Optional[int] = None,
                 tool: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.tool = tool


class CreditsExhaustedError(StrikerAPIError):
    """HTTP 402: insufficient credits. Ingestion must stop."""


class InvalidRequestError(StrikerAPIError):
    """HTTP 400/401: not worth retrying."""


class RateLimitedError(StrikerAPIError):
    """HTTP 429."""


class ServerError(StrikerAPIError):
    """HTTP 5xx: retryable with idempotency key."""


class StrikerClient:
    """Thread-safe minimal client respecting documented limits."""

    def __init__(self, settings: Settings):
        self.s = settings
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "x-api-key": self.s.api_key,
        })
        self._lock = threading.Lock()
        self._last_request_at = 0.0
        # Local credit ledger tracking (real balance endpoint not documented).
        self.local_spent = 0          # cumulativo en esta ejecucion/sesion
        self.last_response_headers = None
        self.on_spend = None          # callback(tool, credits) si se dispone de DB
        self.on_raw = None            # callback(tool, raw_key, path, size)

    def set_on_spend(self, cb: Any) -> None:
        self.on_spend = cb

    def set_on_raw(self, cb: Any) -> None:
        self.on_raw = cb

    def use_session(self, session: Any) -> None:
        """Inyectar una sesion propia (tests / adaptadores)."""
        self._session = session  # type: ignore[assignment]

    # ------------------------------------------------------------------ rate
    def _throttle(self) -> None:
        """Respect documented 60 req/min (>=100 credits) or 10 req/min (<100)."""
        rate = self.s.rate_per_minute
        if self.local_spent >= self.s.low_balance_threshold:
            # ``local_spent`` declarado por precaucion: bajo saldo => lento.
            rate = self.s.low_balance_rate_per_minute
        min_interval = 60.0 / rate
        with self._lock:
            now = time.monotonic()
            wait = min_interval - (now - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    # ------------------------------------------------------------- request
    def _post(self, tool: str, params: dict, idempotency_key: str,
              timeout: float) -> requests.Response:
        url = f"{self.s.base_url}/v1/{tool}"
        body = dict(params)
        if self.s.competition and tool not in self._competition_agnostic():
            # league-specific tools accept ``league`` param (laliga/ucl).
            body.setdefault("league", self.s.competition)
        headers = {"x-idempotency-key": idempotency_key}
        self._throttle()
        return self._session.post(url, json=body, headers=headers, timeout=timeout)

    @staticmethod
    def _competition_agnostic() -> set:
        # list_teams/search_players ignore league; kept explicit for safety.
        return {"list_teams", "search_players"}

    def call(self, tool: str, params: Optional[dict] = None, raw_dir=None,
             raw_key: Optional[str] = None) -> dict:
        """Perform an API call with retries, refunds semantics and credit accounting.

        Returns the parsed payload (unwrapped from ``data`` when present).
        Stores the raw JSON to disk if ``raw_dir`` is given.
        """
        params = params or {}
        cost = self.s.cost_of(tool)
        idem = str(uuid.uuid4())
        last_exc: Optional[StrikerAPIError] = None

        for attempt in range(self.s.max_retries + 1):
            try:
                resp = self._post(tool, params, idem, self.s.timeout_seconds)
                self.last_response_headers = resp.headers
            except requests.RequestException as e:
                last_exc = ServerError(f"request exception: {e}", tool=tool)
                if attempt >= self.s.max_retries:
                    self._bump(cost, tool)
                    raise
                self._backoff(attempt)
                continue

            if resp.status_code == 402:
                self._bump(cost, tool)
                raise CreditsExhaustedError(_msg(resp), 402, tool)
            if resp.status_code in (400, 401):
                self._bump(cost, tool)
                raise InvalidRequestError(_msg(resp), resp.status_code, tool)
            if resp.status_code == 429:
                if attempt >= self.s.max_retries:
                    self._bump(cost, tool)
                    raise RateLimitedError(_msg(resp), 429, tool)
                self._backoff(attempt)
                continue
            if 500 <= resp.status_code < 600:
                if attempt >= self.s.max_retries:
                    self._bump(cost, tool)
                    raise ServerError(_msg(resp), resp.status_code, tool)
                self._backoff(attempt)   # idempotency key: retry is duplicate-safe
                continue
            if resp.status_code != 200:
                self._bump(cost, tool)
                raise InvalidRequestError(
                    f"unexpected HTTP {resp.status_code}: {_msg(resp)}",
                    resp.status_code, tool)

            # ---- success ---- (empty answers are refunded per docs)
            try:
                payload = resp.json()
            except ValueError:
                self._bump(cost, tool)
                raise StrikerAPIError(f"invalid JSON from {tool}", tool=tool)
            if raw_dir is not None and resp.ok:
                self._save_raw(tool, raw_dir, raw_key, resp, cost)
            # empty answer is refunded: do not count credits
            if self._is_empty(payload):
                log.info("empty response for %s (refunded, 0 credits)", tool)
                return payload
            self._bump(cost, tool)
            return self._unwrap(payload)

        raise last_exc or StrikerAPIError("unreachable", tool=tool)

    # ------------------------------------------------------------- helpers
    def _bump(self, credits: int, tool: Optional[str] = None) -> None:
        with self._lock:
            self.local_spent += credits
        if self.on_spend is not None:
            self.on_spend(tool, credits)
        log.debug("credits spent now %s (total session %s)", credits, self.local_spent)

    @staticmethod
    def _is_empty(payload: dict) -> bool:
        # A list that exists and is empty + count==0 => refunded.
        for key, val in payload.items():
            if isinstance(val, list) and len(val) == 0:
                count = payload.get("count")
                if count is None or count == 0:
                    return True
        return False

    @staticmethod
    def _unwrap(payload: dict) -> dict:
        if isinstance(payload, dict) and "data" in payload:
            inner = payload["data"]
            if isinstance(inner, dict):
                return inner
        return payload

    def _save_raw(self, tool, raw_dir, raw_key, resp, credits) -> str:
        # raw_dir like raw/<competition>/<tool>/<key>.json
        import json
        from pathlib import Path
        comp = self.s.competition
        base = Path(raw_dir) / comp / tool
        base.mkdir(parents=True, exist_ok=True)
        key = raw_key or f"{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        path = base / f"{key}.json"
        data = resp.json()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        if self.on_raw is not None:
            size = path.stat().st_size
            self.on_raw(tool, key, str(path), size)
        log.debug("raw saved: %s", path)
        return str(path)

    def _backoff(self, attempt: int) -> None:
        time.sleep(self.s.retry_backoff_base ** attempt)


def _msg(resp) -> str:
    try:
        j = resp.json()
        if isinstance(j, dict) and ("error" in j or "message" in j):
            return f"{j.get('error', '')} {j.get('message', '')}".strip()
        return str(j)[:300]
    except Exception:
        return resp.text[:300]