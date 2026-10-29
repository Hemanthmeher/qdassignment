# QuickDrop Rider Payout Dispute Desk (`qdassignment`)

An autonomous, production-grade AI dispute resolution backend designed for QuickDrop's 5,000 delivery riders. It ingests incoming WhatsApp messages, understands unstructured multi-turn Hinglish/English queries, verifies claims against historical trip datasets using a deterministic financial policy engine, executes automated disbursements via PaySwift banking, and provides human-in-the-loop operational review for complex exceptions.

---

## 1. Project Overview

Every day, delivery riders submit payout inquiries over WhatsApp (e.g., missing surge multipliers, daily incentive discrepancies, or duplicate cancellation penalties). Resolving these manually creates a 48-hour backlog.

This project solves the problem with a high-throughput, dual-layer architecture:
- **Zero Financial Hallucination:** The LLM strictly handles intent recognition, entity extraction, and conversational dialogue. It is never allowed to calculate fares or move money on its own.
- **Deterministic Policy Engine:** All fare calculations, daily incentive thresholds (12+ completed trips in IST), duplicate cancellation penalty audits, and 7-day lookback constraints are executed with 100% deterministic mathematical precision.
- **Banking Guardrails:** Discrepancies up to ₹200 are auto-paid through PaySwift with automatic idempotency keys and retry logic. Claims exceeding ₹200, repeat daily payouts, and edge cases (accidents, cross-rider claims) are held in an Ops approval queue.

---

## 2. Architecture & Tech Stack

```
[Rider WhatsApp Message] ──> [FastAPI: POST /messages]
                                    │
            ┌───────────────────────┴───────────────────────┐
            ▼                                               ▼
 [Groq LLM: Intent & Entities]                 [Deterministic Policy Engine]
   (openai/gpt-oss-120b)                        - Fares: Base ₹25 + ₹6/km * Surge
                                                - Incentive: ₹150 for >= 12 trips
                                                - Penalties: ₹10/cancellation refund
                                                            │
                                    ┌───────────────────────┴───────────────────────┐
                                    ▼                                               ▼
                    [Auto-Pay: PaySwift Sandbox]                      [Ops Review Queue]
                     - Live Ledger Reconciliation                      - Disputes > ₹200
                     - Max ₹200 / 1 per rider/day                      - Escalations & Accidents
                     - Idempotent Retry Logic                          - Visual Dashboard (/ops)
```

- **Backend API:** FastAPI (Python 3.12) with asynchronous request pipelines.
- **Database & ORM:** SQLAlchemy 2.0 with SQLite (local development) and PostgreSQL 16 (containerized Docker production).
- **AI / LLM:** Groq API running `openai/gpt-oss-120b` with deterministic financial fallback.
- **Banking Gateway:** PaySwift Payouts API client with exponential backoff and idempotency protection.
- **Frontend / Dashboard:** Tailwind CSS + Vanilla JS Ops Portal with live chat simulator.
- **Containerization & CI:** Docker, Docker Compose, and GitHub Actions CI.

---

## 3. Prerequisites

Before running the project, ensure you have:
- **Python 3.12+** installed on your machine.
- **Git** installed.
- *(Optional)* **Docker & Docker Compose** for containerized one-command startup.

---

## 4. Step-by-Step Installation & Running Guide

### Step 1: Clone the Repository & Setup Environment
```bash
git clone https://github.com/Hemanthmeher/qdassignment.git
cd qdassignment

# Create and activate virtual environment
python -m venv .venv

# On Windows PowerShell:
.venv\Scripts\Activate.ps1

# On Linux/macOS:
source .venv/bin/activate
```

### Step 2: Install Dependencies
```bash
pip install -r requirements.txt
```

### Step 3: Configure Environment Variables
Copy the template configuration file:
```bash
# Windows:
copy .env.example .env

# Linux/macOS:
cp .env.example .env
```
*(The default `.env` is pre-configured with local defaults and a sandbox Groq key).*

---

### Step 4: Run Locally (Fastest - Under 2 Minutes)

Open **two separate terminals**:

#### Terminal 1 — Start the PaySwift Banking Sandbox:
```bash
python run_payswift.py
```
> *PaySwift sandbox will start listening on port `8081`.*

#### Terminal 2 — Start the FastAPI Service:
```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```
> *On startup, SQLite automatically initializes and seeds 40 riders, 3,314 trips, and 3,222 payout lines from `data/`.*

---

### Alternative: Run with Docker Compose (One Command)
If you prefer Docker, you can run the whole stack (PostgreSQL 16, PaySwift, and FastAPI App) with a single command:
```bash
docker compose up --build
```
- App Service: `http://localhost:8000`
- Ops Dashboard: `http://localhost:8000/ops`
- PaySwift Sandbox: `http://localhost:8081`

---

## 5. How to Run Evaluations & Tests

Run the end-to-end evaluation harness against the running service with a single command:

```bash
python evals/run.py http://localhost:8000
```

The harness automatically executes the multi-turn conversational scenarios from `data/conversations.json`, validating:
- Net balance updates in the live PaySwift ledger.
- Correct queueing of disputes > ₹200 and escalations in Ops pending.
- Factually accurate Hinglish/English replies with mandatory entity mentions.
- Response delivery within the strict **10-second** webhook latency budget.

---

## 6. Ops Dashboard & Trace Endpoints

### 📊 Human Operations Dashboard
Visit **[http://localhost:8000/ops](http://localhost:8000/ops)** (or root `http://localhost:8000`) in your browser to access:
- **Pending Review Queue:** Real-time list of disputes requiring human decision, with one-click **Approve** (triggers PaySwift disbursement) and **Reject** buttons.
- **Rider Trace Inspector:** Enter any rider ID (e.g., `R003`, `R016`, `R028`) to inspect the chronological chain of agent reasoning, policy checks, tool calls, and replies.
- **WhatsApp Live Chat Simulator:** Test any custom rider query in real-time and observe the agent's live response.

### 🔌 Key API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Health check verifying service status and PaySwift connectivity. |
| `POST` | `/messages` | Inbound WhatsApp webhook returning agent reply within 10s. |
| `GET` | `/trace/{rider_id}` | Chronological audit trace of agent reasoning and tool executions. |
| `GET` | `/ops/pending` | List of items awaiting human operational review. |
| `POST` | `/ops/{id}/approve` | Approves dispute and executes payment via PaySwift. |
| `POST` | `/ops/{id}/reject` | Rejects dispute and logs reason. |
| `GET` | `/docs` | Interactive Swagger API documentation. |
