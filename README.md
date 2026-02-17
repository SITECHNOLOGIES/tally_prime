# Tally Prime Integration API

A Python REST API that extracts real-time accounting data from **Tally Prime** over its XML API. Built for real estate companies using Tally as their primary accounting software.

> **Built with:** Python 3.10+ · FastAPI · Tally Prime XML API · Optional ODBC Fallback

---

## Table of Contents

- [What This Does](#what-this-does)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Step 1 — Install & Configure Tally Prime](#step-1--install--configure-tally-prime)
- [Step 2 — Set Up the Python Project](#step-2--set-up-the-python-project)
- [Step 3 — Configure & Run](#step-3--configure--run)
- [API Endpoints](#api-endpoints)
- [Usage Examples](#usage-examples)
- [How It Works — Tally XML API Deep Dive](#how-it-works--tally-xml-api-deep-dive)
- [Project Structure](#project-structure)
- [Troubleshooting](#troubleshooting)
- [ODBC Setup (Optional Fallback)](#odbc-setup-optional-fallback)
- [Contributing](#contributing)

---

## What This Does

This API connects to a running instance of Tally Prime and exposes all accounting data through clean REST endpoints:

- **Ledgers** — all accounts with opening/closing balances, Dr/Cr indicators
- **Vouchers** — sales, purchases, receipts, payments, journals with full amounts and party names
- **Debtors & Creditors** — outstanding receivables and payables
- **Financial Summary** — total assets, liabilities, bank balance, loans
- **Trial Balance** — full trial balance derived from ledger data
- **Day Book** — all vouchers for any given date
- **Groups & Cost Centres** — chart of accounts structure

Data is extracted in real-time from Tally — no database sync or manual export needed.

---

## Architecture

```
┌──────────────┐      HTTP POST       ┌──────────────────┐
│              │  (XML over port 9000) │                  │
│  Tally Prime │◄─────────────────────►│  new_utils.py    │
│  (Windows)   │                       │  TallyDataExtractor│
│              │                       │                  │
└──────────────┘                       └────────┬─────────┘
                                                │
                                       ┌────────▼─────────┐
                                       │                  │
                                       │  app.py          │
                                       │  FastAPI Server   │
                                       │  (port 8001)     │
                                       │                  │
                                       └────────┬─────────┘
                                                │
                                    ┌───────────▼──────────┐
                                    │  REST API Consumers  │
                                    │  Frontend / Mobile   │
                                    │  Other Services      │
                                    └──────────────────────┘
```

**Two extraction methods:**

| Data Type | Method Used | Why |
|---|---|---|
| Ledgers, Groups, Cost Centres, Company Info | TDL Custom Reports | Clean flat XML, fast response |
| Vouchers (with amounts, party, narration) | Collection Export + NATIVEMETHOD | Only method that returns complete voucher data reliably |

---

## Prerequisites

Before you begin, make sure you have:

| Requirement | Version | Download |
|---|---|---|
| Tally Prime | 4.0 or later | [tallysolutions.com/download](https://tallysolutions.com/download/) |
| Python | 3.10+ | [python.org/downloads](https://www.python.org/downloads/) |
| pip | Latest | Comes with Python |
| Windows | 10/11 | Tally runs only on Windows |

> **Note:** Tally Prime must be running on the same machine (or accessible via network) for the API to connect.

---

## Step 1 — Install & Configure Tally Prime

### 1.1 Install Tally

If you don't have Tally Prime installed:

1. Download from [tallysolutions.com/download](https://tallysolutions.com/download/)
2. Run the installer and follow the setup wizard
3. Activate with your license or use **Educational Mode** for testing

### 1.2 Enable XML API Server (Required)

Tally's HTTP server must be running for this API to connect. Here's how to enable it:

1. Open **Tally Prime**
2. Press **F12** to open Configuration
3. Go to **Connectivity** (or **Advanced Configuration** in older versions)
4. Set these options:

```
Tally Prime Server?              : Yes
Port (for XML):                  : 9000
Allow Remote Access?             : Yes     (if API runs on different machine)
```

5. Press **Ctrl+A** to save

### 1.3 Verify Tally is Listening

Open a browser and go to:

```
http://localhost:9000
```

If Tally's HTTP server is running, you'll see Tally respond (might show a blank page or XML — that's normal). If you get "connection refused", Tally's server isn't enabled — go back to step 1.2.

### 1.4 Create or Open a Company

Make sure you have a company loaded in Tally. Note the **exact company name** — it's case-sensitive. You'll need it in the configuration.

For this project, the default company name is `Nimona`. Change it in the config if yours is different.

---

## Step 2 — Set Up the Python Project

### 2.1 Clone the Repository

```bash
git clone https://github.com/SITECHNOLOGIES/tally_prime.git
cd tally_prime
```

### 2.2 Create Virtual Environment (Recommended)

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/Mac (if running API remotely)
source venv/bin/activate
```

### 2.3 Install Dependencies

```bash
pip install fastapi uvicorn requests pydantic
```

If you plan to use the ODBC fallback (optional):

```bash
pip install pyodbc
```

### 2.4 Verify Installation

```bash
python -c "import fastapi, uvicorn, requests; print('All dependencies installed ✓')"
```

---

## Step 3 — Configure & Run

### 3.1 Configuration

Edit the environment variables at the top of `app.py`, or set them as system environment variables:

| Variable | Default | Description |
|---|---|---|
| `TALLY_URL` | `http://localhost:9000` | Tally HTTP server URL |
| `TALLY_COMPANY` | `Nimona` | Company name in Tally (case-sensitive) |
| `TALLY_FY_START` | `20250401` | Financial year start (YYYYMMDD) |
| `TALLY_FY_END` | `20260331` | Financial year end (YYYYMMDD) |
| `TALLY_ODBC_DSN` | `TallyODBC_9000` | ODBC DSN name (optional) |

**Quick way — set via environment:**

```bash
# Windows PowerShell
$env:TALLY_COMPANY = "Your Company Name"
$env:TALLY_URL = "http://localhost:9000"

# Windows CMD
set TALLY_COMPANY=Your Company Name
set TALLY_URL=http://localhost:9000
```

Or just edit the defaults directly in `app.py`:

```python
TALLY_URL = os.getenv("TALLY_URL", "http://localhost:9000")
TALLY_COMPANY = os.getenv("TALLY_COMPANY", "Your Company Name")  # ← change this
```

### 3.2 Test the Extractor Directly

Before running the API server, test that the extractor can talk to Tally:

```bash
python new_utils.py
```

You should see output like:

```
1. Testing connection...
  XML API: ✓
  ODBC:    ✗

2. Company info...
  {"company_name": "Nimona", ...}

3. Ledgers...
  Total: 132

4. Day Book for 1-Apr-25...
  Date         Particulars                                    Vch Type     Vch No.     Debit Amount
  2025/04/01   UltraTech Cement Ltd                           Payment           14    2,850,000.00
  ...
```

If you see `Cannot connect`, check that Tally is running and the HTTP server is enabled (Step 1.2).

### 3.3 Start the API Server

```bash
python app.py
```

Or with uvicorn directly:

```bash
uvicorn app:app --host 0.0.0.0 --port 8001 --reload
```

The API will start on `http://localhost:8001`.

### 3.4 Open the API Docs

Navigate to:

```
http://localhost:8001/docs
```

This opens the interactive **Swagger UI** where you can test all endpoints directly from your browser.

---

## API Endpoints

### Health & Config

| Method | Endpoint | Description |
|---|---|---|
| GET | `/` | API info and version |
| GET | `/health` | Connection status (XML API + ODBC) |
| POST | `/config/switch-company` | Switch to a different Tally company |

### Company

| Method | Endpoint | Description |
|---|---|---|
| GET | `/companies` | List all companies in Tally |
| GET | `/company/info` | Current company details (name, GSTIN, PAN, etc.) |

### Ledgers

| Method | Endpoint | Description |
|---|---|---|
| GET | `/ledgers` | All ledgers with balances (cached 5 min) |
| GET | `/ledgers?refresh=true` | Force refresh bypassing cache |
| GET | `/ledgers/search?name=HDFC` | Find ledger by name |
| GET | `/ledgers/group/{group_name}` | Ledgers by parent group |
| GET | `/ledgers/bank-accounts` | All bank accounts |
| GET | `/ledgers/cash-accounts` | Cash-in-hand accounts |
| GET | `/ledgers/fixed-assets` | Fixed asset ledgers |
| GET | `/ledgers/loans` | Secured + unsecured loans |

### Debtors & Creditors

| Method | Endpoint | Description |
|---|---|---|
| GET | `/debtors` | All sundry debtors with total receivables |
| GET | `/debtors/top?limit=10` | Top debtors by outstanding amount |
| GET | `/creditors` | All sundry creditors with total payables |
| GET | `/creditors/top?limit=10` | Top creditors by outstanding amount |

### Vouchers

| Method | Endpoint | Description |
|---|---|---|
| GET | `/vouchers` | All vouchers with amounts and party names |
| GET | `/vouchers?voucher_type=Sales` | Filter by type |
| GET | `/vouchers?from_date=20250401&to_date=20250430` | Filter by date range |
| GET | `/vouchers/details` | Vouchers WITH line-item Dr/Cr entries |
| GET | `/vouchers/sales` | Sales vouchers only |
| GET | `/vouchers/purchases` | Purchase vouchers only |
| GET | `/vouchers/receipts` | Receipt vouchers only |
| GET | `/vouchers/payments` | Payment vouchers only |
| GET | `/vouchers/journals` | Journal vouchers only |
| GET | `/vouchers/daybook?date=20250401` | All vouchers for a specific date |

### Reports

| Method | Endpoint | Description |
|---|---|---|
| GET | `/reports/financial-summary` | Total assets, liabilities, bank balance, etc. |
| GET | `/reports/group-summary` | Ledger summary by parent group |
| GET | `/reports/trial-balance` | Full trial balance |

### Masters

| Method | Endpoint | Description |
|---|---|---|
| GET | `/groups` | All account groups |
| GET | `/cost-centres` | All cost centres |

### Export & Debug

| Method | Endpoint | Description |
|---|---|---|
| GET | `/export/all` | Full data export (takes 30-60s) |
| GET | `/debug/raw-voucher-xml` | Raw XML from Tally (for debugging) |

---

## Usage Examples

### Python (requests)

```python
import requests

BASE = "http://localhost:8001"

# Get all bank accounts
banks = requests.get(f"{BASE}/ledgers/bank-accounts").json()
for b in banks["data"]:
    print(f"{b['ledger_name']}: ₹{b['closing_balance']:,.2f}")

# Get sales vouchers for April 2025
sales = requests.get(f"{BASE}/vouchers/sales", params={
    "from_date": "20250401",
    "to_date": "20250430"
}).json()
print(f"Total sales vouchers: {sales['count']}")

# Get top 5 debtors
top = requests.get(f"{BASE}/debtors/top", params={"limit": 5}).json()
for d in top["data"]:
    print(f"{d['ledger_name']}: ₹{d['closing_balance']:,.2f}")
```

### cURL

```bash
# Health check
curl http://localhost:8001/health

# Get all vouchers
curl "http://localhost:8001/vouchers?limit=10"

# Get day book for 1 April 2025
curl "http://localhost:8001/vouchers/daybook?date=20250401"

# Switch company
curl -X POST "http://localhost:8001/config/switch-company?company_name=MyCompany"
```

### JavaScript (fetch)

```javascript
// Get financial summary
const response = await fetch('http://localhost:8001/reports/financial-summary');
const data = await response.json();
console.log(`Total Assets: ₹${data.data.total_assets.toLocaleString()}`);
console.log(`Total Receivables: ₹${data.data.total_receivables.toLocaleString()}`);
```

---

## How It Works — Tally XML API Deep Dive

Tally Prime exposes an HTTP server (default port 9000) that accepts XML POST requests and returns XML responses. There are multiple ways to query it, but not all work for every data type.

### Method 1 — TDL Custom Reports (for Ledgers, Groups, Company)

Send a TDL (Tally Definition Language) report definition. Tally executes it and returns flat XML.

```xml
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>LedgerReport</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVCURRENTCOMPANY>Nimona</SVCURRENTCOMPANY>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <!-- Report/Form/Part/Line/Field definitions -->
                    <COLLECTION NAME="LedgerCollection">
                        <TYPE>Ledger</TYPE>
                    </COLLECTION>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>
```

Works great for ledgers. Returns clean, small XML.

**Does NOT work for voucher amounts** — `$Amount`, `$PartyLedgerName`, `$Narration` return empty/0 in this context because Tally stores voucher amounts at the ledger-entry level, not the voucher header.

### Method 2 — Collection Export with NATIVEMETHOD (for Vouchers)

This is the method that works for complete voucher data:

```xml
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>VchCollection</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVCURRENTCOMPANY>Nimona</SVCURRENTCOMPANY>
                <SVFROMDATE>20250401</SVFROMDATE>
                <SVTODATE>20250401</SVTODATE>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="VchCollection">
                        <TYPE>Voucher</TYPE>
                        <NATIVEMETHOD>VoucherNumber, VoucherTypeName, Date,
                                      Amount, PartyLedgerName, Narration</NATIVEMETHOD>
                        <NATIVEMETHOD>AllLedgerEntries</NATIVEMETHOD>
                    </COLLECTION>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>
```

Returns full `<VOUCHER>` objects with nested `<ALLLEDGERENTRIES.LIST>` containing each debit/credit line.

> **Important:** Using `NATIVEMETHOD=*` dumps ALL fields (1.3MB+) including ones with invalid XML characters that break parsers. Always specify the exact fields you need.

### Tally Amount Conventions

| Context | Sign Convention |
|---|---|
| Ledger Opening/Closing Balance | Positive = Debit, Negative = Credit |
| Voucher `ALLLEDGERENTRIES.LIST` AMOUNT | Negative = Debit entry, Positive = Credit entry |
| `ISDEEMEDPOSITIVE=Yes` | Confirms it's a debit-side entry |

---

## Project Structure

```
tally-integration-api/
├── new_utils.py          # Core extractor — TallyDataExtractor class
│                         #   - TDL reports for ledgers/groups
│                         #   - Collection export for vouchers  
│                         #   - ODBC fallback
│                         #   - Caching, retry logic, error handling
│
├── app.py                # FastAPI server — REST endpoints
│                         #   - 30+ endpoints covering all Tally data
│                         #   - Swagger docs at /docs
│                         #   - CORS enabled
│
├── tally_voucher_debug.py # Diagnostic tool — tests 9 XML formats
│                         #   against Tally to find which ones work
│
├── logs/                 # Auto-created log directory
│   └── tally_extraction_YYYYMMDD.log
│
├── requirements.txt      # Python dependencies
└── README.md             # This file
```

### Key Classes

**`TallyDataExtractor`** (in `new_utils.py`)

The main class. Initialize with Tally URL and company name, then call methods:

```python
from new_utils import TallyDataExtractor

extractor = TallyDataExtractor(
    url="http://localhost:9000",
    company_name="Nimona",
    financial_year_start="20250401",
    financial_year_end="20260331",
)

# Use any method
ledgers = extractor.get_all_ledgers()          # Cached for 5 min
vouchers = extractor.get_vouchers(limit=100)   # With amounts!
banks = extractor.get_bank_accounts()
summary = extractor.get_financial_summary()
daybook = extractor.get_day_book("20250401")
```

---

## Troubleshooting

### "Connection refused" or "Cannot connect to Tally"

1. Make sure Tally Prime is running
2. Check HTTP server is enabled: **F12 → Connectivity → Tally Prime Server = Yes**
3. Verify port: open `http://localhost:9000` in a browser
4. If running API on a different machine, enable **Allow Remote Access** in Tally

### Vouchers return empty `data: []`

This was a known issue. The fix requires using **Collection Export** (not Export Data or TDL Reports). Make sure you have the latest `new_utils.py` (v4+).

To debug, run:

```bash
python tally_voucher_debug.py
```

Look for which test returns `<VOUCHER> elements found: X` where X > 0.

### Company info returns all empty strings

The TDL report needs a `<COLLECTION>` definition with `<TYPE>Company</TYPE>`. Without it, the REPEAT line has nothing to iterate. Fixed in v4+.

### "XML parse error: invalid character number"

Tally sometimes includes control characters in XML responses. The extractor has built-in cleaning:

```python
# Automatic cleaning in _execute_request():
re.sub(r'&#([0-8]|1[0-9]|2[0-9]|3[01]);', '', xml_string)

# Extra aggressive cleaning for vouchers:
re.sub(r'&#x[0-9a-fA-F]+;', '', xml_resp)
```

If you still get parse errors, avoid `NATIVEMETHOD=*` — always specify exact fields.

### Amounts show as 0.0

If using an older version of the code that uses TDL Reports or Export Data for vouchers, upgrade to v4+ which uses Collection Export with NATIVEMETHOD.

### Ledger cache returns stale data

The extractor caches ledger data for 5 minutes. To force a fresh fetch:

```bash
curl "http://localhost:8001/ledgers?refresh=true"
```

Or in Python:

```python
extractor.get_all_ledgers(force_refresh=True)
```

### Different Financial Year

Change the FY dates in config:

```bash
# For FY 2024-25
$env:TALLY_FY_START = "20240401"
$env:TALLY_FY_END = "20250331"
```

### Multiple Companies

Switch at runtime without restarting:

```bash
curl -X POST "http://localhost:8001/config/switch-company?company_name=OtherCompany"
```

---

## ODBC Setup (Optional Fallback)

The ODBC pathway is a fallback if the XML API is unavailable. It requires the Tally ODBC driver.

### Install Tally ODBC Driver

1. Run the Tally Prime installer again
2. Select **Modify** and check **ODBC Driver**
3. Or download separately from Tally's website

### Configure Windows ODBC

1. Open **ODBC Data Sources (64-bit)** from Windows search
2. Go to **System DSN** tab
3. Click **Add** → Select **Tally ODBC Driver**
4. Configure:
   - **DSN Name:** `TallyODBC_9000`
   - **Server:** `localhost`
   - **Port:** `9000`
5. Click OK

### Enable ODBC in Tally

1. Open Tally Prime
2. Press **F12** → **Advanced Configuration**
3. Set **Enable ODBC Server** = Yes

### Test ODBC

```bash
python new_utils.py --odbc
```

### Force ODBC Mode (skip XML API)

```python
extractor = TallyDataExtractor(force_odbc=True)
```

Or via API:

```bash
curl -X POST "http://localhost:8001/config/switch-company?company_name=Nimona&force_odbc=true"
```

---

## API Response Format

All endpoints return a consistent JSON structure:

```json
{
    "success": true,
    "data": [ ... ],
    "count": 132,
    "extraction_method": "xml_api",
    "timestamp": "2026-02-17T18:30:00.000000"
}
```

On error:

```json
{
    "success": false,
    "error": "get_vouchers: Connection refused",
    "timestamp": "2026-02-17T18:30:00.000000"
}
```

---

## Requirements

Create a `requirements.txt`:

```
fastapi>=0.100.0
uvicorn>=0.23.0
requests>=2.28.0
pydantic>=2.0.0
pyodbc>=4.0.0    # Optional — only for ODBC fallback
```

---

## License

MIT

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Commit your changes: `git commit -m 'Add your feature'`
4. Push to the branch: `git push origin feature/your-feature`
5. Open a Pull Request

When contributing, please run the debug script (`tally_voucher_debug.py`) against your Tally version and include the output if you're fixing XML-related issues — different Tally versions respond differently to the same XML requests.
