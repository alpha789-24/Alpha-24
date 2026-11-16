# Python Strategy Runtime Sandbox: Architecture & Security Design

> **Document Type:** Architectural Specification & Security Design  
> **Status:** Proposed Design (Pre-Implementation)  
> **Target Audience:** Engineering, Security, System Architecture  
> **Hard Invariant:** **NO CODE IN THIS SPECIFICATION EXECUTES USER-SUPPLIED PYTHON CODE.** Implementation of active execution runtimes remains hard-blocked until formal sign-off.

---

## 1. Executive Summary & Core Security Invariant

The Alpha24 Strategy Studio allows users to formulate algorithmic trading strategies. Phase 7 introduced the **Python Strategy Importer (First Release)**, which restricts all handling of user-supplied `.py` files strictly to in-memory, read-only static AST parsing (`ast.parse`) with zero execution.

This document defines the complete architectural blueprint for **future phases** that require sandboxed execution of user-authored Python code (for backtesting simulation, paper trading, and potential live execution).

```
+-------------------------------------------------------------------------------+
|                             CORE SECURITY INVARIANT                           |
|                                                                               |
|  User-supplied Python code is treated as HOSTILE, UNTRUSTED, and ADVERSARIAL. |
|  It must never share process memory, execution contexts, network namespaces,  |
|  filesystem trees, or credential environments with the backend host process.   |
|                                                                               |
|  The strategy execution runtime CANNOT talk to the broker, CANNOT access the  |
|  internet, CANNOT access the host filesystem, and CANNOT inspect credentials. |
+-------------------------------------------------------------------------------+
```

---

## 2. The Isolation Boundary Architecture

### 2.1 Isolation Paradigm: Container vs. Process Sandboxing

To achieve defense-in-depth, execution of user code cannot rely on Python-level sandboxing (e.g., `RestrictedPython`, monkeypatching `sys.modules`, or `pypy-sandbox`), as Python's dynamic runtime and C-extensions provide countless known sandbox escapes via object model introspection (`__subclasses__()`, ctypes, frame manipulation, etc.).

Real security requires **OS-level containerized sandboxing** backed by Linux kernel primitives:

```
+-------------------------------------------------------------------------------+
| HOST SYSTEM (FastAPI Backend, Broker Adapters, Database, .env Secrets)       |
|                                                                               |
|  +-------------------+          IPC Pipe / Unix Socket (JSON-RPC)             |
|  | Strategy Manager  | <===================================================>  |
|  +-------------------+                      |                                 |
+---------------------------------------------|---------------------------------+
                                              |
+---------------------------------------------v---------------------------------+
| ISOLATION BOUNDARY: Linux Container (cgroups v2 + seccomp-bpf + user namespace)
|                                                                               |
|  - Network: None (lo DOWN, no external interfaces, no sockets)               |
|  - Filesystem: Read-only overlay root, 16MB noexec tmpfs for /tmp            |
|  - Secrets: Zero env vars passed (no IIFL_APP_KEY, no tokens, clean env)      |
|  - User: Non-root UID 10001 (drop all capabilities, cap-drop ALL)            |
|  - Process: Monitored single-worker runner process                            |
|                                                                               |
|    +---------------------------------------------------------------------+    |
|    | Runner Sandbox (Python 3.12, stripped runtime)                      |    |
|    |                                                                     |    |
|    |   +-------------------------------------------------------------+   |    |
|    |   | User Strategy Module (Loaded via restricted AST import)     |   |    |
|    |   | - on_bar(candles, context) -> Signal (BUY / SELL / HOLD)     |   |    |
|    |   +-------------------------------------------------------------+   |    |
|    +---------------------------------------------------------------------+    |
+-------------------------------------------------------------------------------+
```

### 2.2 Boundary Enforcement Rules

1. **Least Privilege Execution:**
   - The worker container runs as an unmapped unprivileged user (`UID 10001:GID 10001`).
   - All Linux kernel capabilities are explicitly dropped: `--cap-drop=ALL`.
   - `no-new-privileges` flag is permanently asserted to prevent setuid/setgid escalation.

2. **Network Isolation:**
   - The container is spun up with `--network=none`.
   - Loopback interface (`lo`) is down or prohibited from opening TCP/UDP sockets.
   - Any attempt to call `socket.socket()`, `urllib.request`, `requests`, or `http.client` raises an immediate `OSError: [Errno 101] Network is unreachable` or is blocked by seccomp syscall filters.

3. **Filesystem Isolation:**
   - Root filesystem is mounted strictly **read-only** (`--read-only`).
   - No host directories, volumes, or project workspaces are bind-mounted into the container.
   - Only a scratch in-memory directory `/tmp` is provided as `tmpfs` with `size=16M,noexec,nosuid,nodev`.
   - The user script is passed via standard input (stdin) or as a single read-only file mounted into memory.

4. **Environment Sanitization (Zero Inherited Secrets):**
   - The runner container does **not** inherit host environment variables.
   - Specifically stripped: `IIFL_APP_KEY`, `IIFL_APP_SECRET`, `IIFL_REDIRECT_URL`, session tokens, database connection strings, JWT signing keys, AWS/cloud credentials.
   - The container environment contains only:
     ```
     PYTHONPATH=/sandbox
     PYTHONUNBUFFERED=1
     PYTHONDONTWRITEBYTECODE=1
     LC_ALL=C.UTF-8
     LANG=C.UTF-8
     ```

---

## 3. Resource Limits & Quotas

Each strategy execution unit operates under strict, enforceable cgroups v2 resource quotas. Any strategy exceeding its quota is terminated immediately with an explicit status code and reason reported to the user.

| Resource | Quota Limit | Enforcement Mechanism | Failure Action |
|---|---|---|---|
| **CPU Time** | `0.5 cores` (50% of 1 core, e.g., `cpu.max="50000 100000"`) | cgroups v2 CPU bandwidth controller | CPU throttling; task watchdog flags high latency |
| **Memory (RAM)** | `256 MB` total limit (`memory.max=268435456`) | cgroups v2 memory controller; swap disabled (`memory.swap.max=0`) | OOM Killer terminates worker; reported as `OUT_OF_MEMORY` |
| **Execution Runtime (Per Bar)** | `200 ms` wall-clock timeout per bar / tick | Host IPC watchdog timer (`SIGALRM` / `select()` timeout) | Worker process terminated; reported as `EXECUTION_TIMEOUT` |
| **Total Run Timeout (Backtest)** | `120 seconds` maximum for full backtest suite | Host-managed job deadline | Entire container terminated; reported as `BACKTEST_TIMEOUT` |
| **Process Count (PID Limit)** | Max `16 PIDs` (`pids.max=16`) | cgroups v2 PID controller | Blocks `fork()`, `multiprocessing`, or `subprocess` bombs |
| **Output Size (Stdout/Stderr)** | `64 KB` maximum per run | Host stream pipe buffer with truncate sentinel | Buffer truncated; excess bytes dropped with warning |
| **IPC Message Size** | `1 MB` per frame | Protocol framing parser | Rejects oversized frames; raises `IPC_PAYLOAD_TOO_LARGE` |
| **Concurrency Limit** | Max `2 concurrent strategy sandboxes` per user | Backend worker pool semaphore | Additional requests queued with position ETA or rejected (429) |

---

## 4. Runtime Environment & Strategy Interface Design

### 4.1 Supported Python Version

- **Target Runtime:** `Python 3.12 (CPython)` inside a distroless Linux container (`gcr.io/distroless/python3-debian12`).
- **Standard Library:** Standard library math, statistics, datetime, collections, and dataclasses are available. Network, system, and process modules are removed or blocked at syscall level.

### 4.2 Strategy Interface Paradigms: Signals-Only vs. Restricted Order API

The design evaluates two architectural models for strategy authoring:

#### Paradigm A: Signals-Only Contract (Recommended & Approved)

In this model, the user's Python code is a **pure function** that evaluates market data and emits a declarative signal. The user code has **zero agency** and cannot execute trades, query account balances, or manage order state directly.

```python
# User Strategy Interface (Signals-Only)
class UserStrategy:
    def initialize(self, params: dict[str, Any]) -> None:
        """Called once on startup with configured user parameters."""
        pass

    def on_bar(self, bar: dict[str, Any], context: dict[str, Any]) -> dict[str, Any] | None:
        """
        Pure function called on every completed bar.
        
        Args:
            bar: {'open': float, 'high': float, 'low': float, 'close': float, 
                  'volume': int, 'time': str, 'indicators': dict[str, float]}
            context: {'position': 'FLAT' | 'LONG' | 'SHORT', 
                      'entryPrice': float | None, 'pnl': float}
                      
        Returns:
            {'action': 'BUY' | 'SELL' | 'CLOSE' | 'HOLD', 
             'quantity': int | None, 'stopLoss': float | None, 
             'target': float | None, 'tag': str | None} or None
        """
        if bar['indicators'].get('RSI') < 30 and context['position'] == 'FLAT':
            return {'action': 'BUY', 'quantity': 1, 'stopLoss': bar['close'] - 50}
        return None
```

**Security & Architectural Advantages of Signals-Only:**
- **Zero Broker Interaction:** The user script never sees broker methods, order state transitions, or credential objects.
- **Deterministic & Auditable:** Every execution input and output can be fully serialized, recorded, and replayed in backtests.
- **Fail-Safe by Default:** If the container crashes or times out, the backend defaults to `HOLD` or safely stops evaluation.
- **Separation of Concerns:** Risk checks, lot-sizing, contract master lookups, market hours validation, slippage filters, and idempotency tracking are completely owned by the verified host backend.

---

#### Paradigm B: Restricted Order API (Evaluated & Rejected for Phase 1)

In this model, the user code is provided a simulated broker client instance with methods like `broker.place_order(symbol, side, qty)`.

```python
# Evaluated Alternative (Rejected for Initial Release)
def on_bar(bar, broker):
    if bar['close'] > 24000:
        broker.place_order(symbol="NIFTY", side="BUY", qty=50) # HIGH RISK
```

**Why Restricted Order API is Rejected for Sandboxed User Code:**
- **Asynchronous State Vulnerabilities:** User code can attempt rapid-fire order placement in loops, leading to race conditions or flood vectors before the host can intervene.
- **API Surface Leakage:** Mocking broker interfaces requires exposing complex state machines inside the untrusted sandbox.
- **Ambiguous Failure Handling:** Network drops or unconfirmed fills require complex recovery logic inside user code that is prone to programmer errors.
- **Decision:** **Adopt Paradigm A (Signals-Only) exclusively.** User Python code emits signals; the host trading engine executes verified orders.

---

## 5. Dependency Management & Library Allowlist

### 5.1 Library Allowlist

To prevent malicious C-extensions, arbitrary memory manipulation, and supply-chain attacks, only pre-compiled, cryptographically verified libraries are installed in the sandbox base image.

```
+-------------------------------------------------------------------------------+
| CATEGORY          | ALLOWED PACKAGES                | STATUS                  |
+-------------------+---------------------------------+-------------------------+
| Math & Statistics | math, statistics, decimal, cmath | Standard Library (OK)   |
| Core Data Types   | datetime, dataclasses, typing,  | Standard Library (OK)   |
|                   | collections, functools, enum    |                         |
| Data Analysis     | numpy==1.26.4, pandas==2.2.2    | Pre-installed in Image  |
| Technical Analysis| scipy==1.13.0, ta==0.11.0       | Pre-installed in Image  |
+-------------------+---------------------------------+-------------------------+
| FORBIDDEN         | requests, urllib3, aiohttp,     | BLOCKED & REMOVED       |
|                   | socket, ctypes, multiprocessing,| (Triggers immediate     |
|                   | os, sys, subprocess, pickle     | import failure)         |
+-------------------------------------------------------------------------------+
```

### 5.2 Dependency Management Protocol

1. **No Runtime Package Installation:** `pip`, `pip install`, `conda`, and internet downloads are completely absent from the sandbox image. No network exists to reach PyPI.
2. **Deterministic Build Pipeline:**
   - Docker image is built from a strict multi-stage Dockerfile using pinned SHA256 base digests.
   - All Python wheels are checked against cryptographic SHA256 hashes in a `poetry.lock` or `requirements.txt --require-hashes` file.
   - Container image is signed using Sigstore/Cosign.
3. **Version Compatibility:**
   - Single standard runtime image (e.g., `alpha24-python-runtime:1.0.0`).
   - Strategy metadata stores the runtime version it was validated against.

---

## 6. Data Ingestion Pipeline (Feeding Data to the Sandbox)

To maintain absolute isolation, the untrusted sandbox **never pulls** or requests data from databases, market APIs, or local files. Instead, the backend **pushes** data through a unidirectionally controlled IPC channel.

```
[ Market Data Service ] 
         |
         v
[ Host Strategy Engine ]
         |  1. Serializes clean bar payload (JSON-lines / MsgPack)
         |  2. Sends via anonymous stdin pipe
         v
+========================================================================+
|  CONTAINER SANDBOX (stdin -> Python Worker -> stdout)                  |
|                                                                        |
|  1. Reads single framed message from sys.stdin                         |
|  2. Executes: signal = strategy.on_bar(bar, context)                   |
|  3. Writes JSON result to sys.stdout: {"signal": "BUY", "tag": "r1"}   |
|  4. Flushes stdout                                                     |
+========================================================================+
         |
         v
[ Host Strategy Engine ]
         |  3. Reads stdout within 200ms deadline
         |  4. Validates signal schema
         v
[ Risk & Go-Live Gates Engine ]
```

### 6.1 Data Format Specification (Host to Sandbox)

```json
{
  "type": "BAR_EVENT",
  "symbol": "NIFTY 50",
  "timeframe": "5m",
  "bar": {
    "timestamp": "2026-11-16T10:30:00+05:30",
    "open": 24020.5,
    "high": 24045.0,
    "low": 24015.0,
    "close": 24038.5,
    "volume": 125000,
    "indicators": {
      "RSI_14": 48.2,
      "EMA_20": 24010.0,
      "SUPERTREND": 23980.0
    }
  },
  "context": {
    "position": "FLAT",
    "entryPrice": null,
    "currentPnl": 0.0,
    "barsSinceEntry": 0
  }
}
```

---

## 7. Permission Separation across Lifecycle Phases

Permissions must be physically and logically partitioned across the four operational stages:

```
+---------------------------------------------------------------------------------------+
| CAPABILITY                    | ANALYSIS | BACKTEST | PAPER TRADING | LIVE EXECUTION  |
+-------------------------------+----------+----------+---------------+-----------------+
| Runtime Execution             | NONE     | BATCH    | REAL-TIME     | REAL-TIME       |
| Host Process Access           | None     | None     | None          | None            |
| Network Access                | BLOCKED  | BLOCKED  | BLOCKED       | BLOCKED         |
| Historical Data Feed          | None     | Batched  | None          | None            |
| Live Quote Stream             | None     | None     | Pushed ticks  | Pushed ticks    |
| Broker Order Placement        | BLOCKED  | BLOCKED  | SIMULATED     | GATED (BY HOST) |
| Hard-Blocked without Key/OTP  | Yes      | No       | No            | HARD-BLOCKED    |
+---------------------------------------------------------------------------------------+
```

1. **Static Analysis (Current Phase):** Runs 100% in backend memory via Python `ast.parse`. No code runs.
2. **Backtest Simulation:** Runs inside an ephemeral container. Historical bar data is streamed over stdin. Emitted signals generate virtual paper trades.
3. **Paper Trading:** Long-lived container process receiving streaming live ticks from the backend poller/WS. Signals execute virtual trades against live quotes.
4. **Live Execution:** Hard-blocked by default. When explicitly unlocked, user Python **still emits signals only**. The host broker execution engine independently verifies go-live gates, idempotency keys, lot sizes, margin limits, and sends orders to IIFL.

---

## 8. Explicit Live Authorization & Emergency Kill Switch

### 8.1 Dual-Custody Go-Live Ceremony

Before any Python-derived strategy can be promoted to LIVE mode, the host enforces the Phase 7 live gate protocol:

1. **Cryptographic Version Lock:**
   A SHA256 hash of the strategy's source code, parameter dictionary, and compile graph is computed (`versionHash`). Any modification immediately invalidates live authorization.
2. **Prerequisite Gates:**
   - Completed backtest record matching `versionHash`.
   - Completed paper worker trading session matching `versionHash`.
   - Active, verified IIFL broker session with profile confirmed.
   - Broker capabilities check returning `liveExecutionEnabled: True`.
3. **Explicit Confirmation Ceremony:**
   The user must physically type:
   `"I UNDERSTAND THE RISK OF REAL CAPITAL LOSS"`
   which is validated on the backend before live flag activation.

### 8.2 Emergency Kill Switch Architecture

The Kill Switch operates completely outside the sandbox runtime:

```
[ USER / RISK OFFICER ]
         |
         | Hits "EMERGENCY KILL SWITCH" (UI or POST /api/broker/kill-switch)
         v
+-----------------------------------------------------------------------+
| BACKEND KILL SWITCH CONTROLLER                                        |
|                                                                       |
| 1. Container Teardown:                                                |
|    - Sends SIGKILL (kill -9) to all active runner sandbox containers  |
|    - Destroys ephemeral container instances                           |
|                                                                       |
| 2. Order Cancellation:                                                |
|    - Queries IIFL adapter for all OPEN, PENDING, PARTIAL orders       |
|    - Issues cancel_order calls to broker                              |
|                                                                       |
| 3. Position Neutralization (Optional Square-Off):                     |
|    - Places market square-off orders for all open broker positions    |
|                                                                       |
| 4. Security Revocation:                                               |
|    - Revokes all live approval records across all strategies          |
|    - Sets liveTradingUnlocked = False globally                        |
+-----------------------------------------------------------------------+
```

---

## 9. Observability, Telemetry & Secret-Free Logging

1. **Stdout/Stderr Redaction Pipeline:**
   All text emitted from user code is streamed through an active regex filter before being persisted to audit logs:
   - Matches and masks any 16+ character alphanumeric tokens: `***REDACTED_SECRET***`.
   - Matches IIFL Client IDs, Auth Codes, API Keys, Passwords.
   - Output truncated to 20 lines / 10 KB per execution tick to prevent log-bloat denial of service.
2. **Structured Audit Events:**
   Every container lifecycle transition is recorded in structured JSON:
   ```json
   {
     "timestamp": "2026-11-16T10:35:12.102Z",
     "event": "SANDBOX_TICK_EXECUTED",
     "strategyId": "strat-py-902",
     "versionHash": "a3f8c92e104b",
     "durationMs": 14.2,
     "cpuTimeMs": 6.8,
     "memoryBytes": 42106880,
     "signalEmitted": "BUY",
     "exitCode": 0
   }
   ```

---

## 10. Threat Model: Top 10 Abuse Cases & Countermeasures

| # | Threat Scenario | Attack Vector | Countermeasure in this Architecture |
|---|---|---|---|
| **1** | **Remote Code Execution & Reverse Shell** | Attacker imports `socket` or `subprocess` to connect to external command-and-control server. | `--network=none` disables network; seccomp blocks `socket` syscall; binary `nc`/`bash` absent in distroless image. |
| **2** | **Credential & Environment Exfiltration** | Attacker reads `os.environ` or searches `/proc/1/environ` to steal broker API keys. | Host environment is sanitized; zero credentials passed to container; `/proc` mounted with `hidepid=2`. |
| **3** | **Host Filesystem Snooping / Tampering** | Attacker attempts `open('/etc/passwd')` or searches for backend `backend/.env`. | Host filesystem is not mounted; rootfs is read-only overlay; attempt raises `FileNotFoundError` or `PermissionDenied`. |
| **4** | **Memory Exhaustion (OOM Bomb)** | Strategy allocates giant arrays: `[0] * 10**9` to crash the studio backend. | cgroups v2 enforces `memory.max=256M`. Kernel OOM killer kills only the sandbox container, leaving host intact. |
| **5** | **CPU Starvation / Infinite Loop DoS** | Strategy runs `while True: pass` to peg host CPU at 100%. | cgroups enforces `cpu.max=0.5`. Host watchdog terminates process after 200ms with `EXECUTION_TIMEOUT`. |
| **6** | **Fork Bomb / Process Exhaustion** | Strategy attempts recursive `multiprocessing` or `os.fork()` to exhaust system PIDs. | cgroups `pids.max=16` blocks process creation; seccomp drops `clone`/`fork` privileges. |
| **7** | **Covert Network Exfiltration** | Attempting DNS tunneling or probing local host ports (e.g., `localhost:8000`). | Container network namespace is empty; loopback interface is down; no route to host exists. |
| **8** | **Deserialization Exploits** | Attacker embeds malicious payload in serialized object (`pickle.loads()`). | `pickle`, `shelve`, and arbitrary object serialization are prohibited; IPC uses strict JSON / typed schemas only. |
| **9** | **Rogue Order Flooding** | Strategy logic glitches or loops, attempting to fire 10,000 orders/second. | Signals-only paradigm prevents direct order calls. Host engine enforces lot size limits, idempotency keys, and 1 order/bar limit. |
| **10** | **Side-Channel & Timing Attacks** | Strategy tries to measure memory cache timings to deduce co-tenant data. | Sandboxes run in discrete container namespaces; no shared memory or memory mapping with host. |

---

## 11. Local Machine Limitations: Why Real Isolation on Windows is Hard

The current development environment is **Windows (NT Kernel)** running a native Python process (`python.exe`). Native Windows environments present severe security challenges for sandboxing untrusted code:

### 11.1 Native Windows Sandbox Deficiencies

1. **No Native cgroups or Namespaces:**
   Windows lacks Linux kernel cgroups and namespaces. Process separation via Windows `Job Objects` is complex, easily misconfigured, and does not provide network namespace isolation.
2. **Network Isolation Gaps:**
   Windows processes running under the same user account have full access to loopback (`127.0.0.1`), allowing an untrusted script to probe the FastAPI backend, MongoDB, or local services.
3. **Leaky Filesystem ACLs:**
   A standard Windows process runs with the privileges of the logged-in user (`C:\Users\HP`), giving any executed Python script full read access to user documents, SSH keys, `.env` files, and browser data.
4. **Token & Named Pipe Impersonation:**
   Windows processes can probe and interact with other local IPC handles, COM objects, and named pipes.

### 11.2 What We Need for Real Isolation

To run user Python safely on this machine, one of the following foundations is **mandatory**:

```
+-------------------------------------------------------------------------------+
| OPTION A: Docker Desktop with WSL2 Engine (Recommended for Local Dev)         |
|                                                                               |
| - Runs a real Linux kernel (WSL2) with full cgroups v2 and seccomp-bpf.       |
| - Docker daemon manages lightweight runner containers.                        |
| - Fast, battle-tested, standard production parity.                            |
+-------------------------------------------------------------------------------+
                                      OR
+-------------------------------------------------------------------------------+
| OPTION B: gVisor (runsc) or Kata Containers (Cloud / Production Standard)     |
|                                                                               |
| - gVisor implements a user-space Linux kernel that intercepts all syscalls.   |
| - Provides hardware-level virtualization boundary (microVM).                  |
| - Eliminates container escape vulnerabilities entirely.                       |
+-------------------------------------------------------------------------------+
```

---

## 12. Decisions Required Before Any Implementation

Before writing any code that spawns processes or runs user Python, the following architectural decisions must be formally approved:

1. **Platform Prerequisite Choice:**
   - **Decision:** Do we require **Docker Desktop / WSL2** as a prerequisite for local Python strategy execution, or do we defer all code execution to a remote sandboxed cloud cluster?
   - *Recommendation:* Require Docker Desktop locally for backtesting/paper runs; reject native `subprocess` on Windows.
2. **Strategy Interface Approval:**
   - **Decision:** Confirm approval of **Paradigm A (Signals-Only)** as the sole strategy interface.
   - *Recommendation:* Approve Paradigm A. Reject direct broker order APIs from user code.
3. **Execution Modes Scope:**
   - **Decision:** Should Python strategies ever be permitted in **LIVE execution**, or should they remain permanently restricted to Backtest and Paper Trading?
   - *Recommendation:* Keep LIVE execution permanently restricted to converted Visual Rule Builder strategies for Phase 1. Revisit Python live execution only after audited sandbox deployment.
4. **Resource Quotas Baseline:**
   - **Decision:** Agree on 256 MB RAM and 200 ms per-bar execution budget as platform standard.
5. **Storage of User Python Code:**
   - **Decision:** When saved to My Strategies, should user source code be stored with secrets permanently masked with `***MASKED***` (non-reversible), or encrypted at rest with a user key?
   - *Recommendation:* Store masked version only in the studio database; never persist raw credentials.

---

*End of Design Specification — Alpha24 Strategy Studio Python Runtime Sandbox.*
