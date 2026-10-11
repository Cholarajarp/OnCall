"""OnCall target service — a real payments-style API.

Deploys, requests, and failures are all real. A faulty deploy genuinely
breaks `/checkout` (payment-gateway timeout); rolling it back genuinely
restores it. Every request is recorded; every 5xx has a real error detail.
"""

from __future__ import annotations

import sys
from pathlib import Path
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from target_service.store import fault_active, init_db, log_error, log_request
from config import settings

if settings.mode != 'sandbox':
    raise RuntimeError('The fault-injection target may only run in sandbox mode')

SERVICE = "payments"
app = FastAPI(title="shopcite-payments", version="1.1.0")
init_db()


@app.middleware("http")
async def record_requests(request: Request, call_next):
    response = await call_next(request)
    log_request(request.url.path, response.status_code)
    return response


@app.get("/health")
async def health():
    return {"service": SERVICE, "status": "ok"}


@app.post("/checkout")
async def checkout():
    if fault_active(SERVICE):
        detail = (
            "TimeoutError: payment gateway deadline (5000ms) exceeded — "
            "tax-calc.compute() did not return after the timeout change"
        )
        log_error("/checkout", detail)
        return JSONResponse(
            status_code=500,
            content={"error": "payment_gateway_timeout", "detail": detail},
        )
    return {"status": "charged", "order_id": f"ord-{id(object()) % 10_000_000:07d}"}


if __name__ == "__main__":
    uvicorn.run(app, host=settings.host, port=settings.target_port, log_level="warning")
