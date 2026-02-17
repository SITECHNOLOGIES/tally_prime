import os
import time
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from test import TallyDataExtractor

# ============================================================================
# CONFIG
# ============================================================================

TALLY_URL = os.getenv("TALLY_URL", "http://localhost:9000")
TALLY_COMPANY = os.getenv("TALLY_COMPANY", "Nimona")
TALLY_ODBC_DSN = os.getenv("TALLY_ODBC_DSN", "TallyODBC_9000")
TALLY_FY_START = os.getenv("TALLY_FY_START", "20250401")
TALLY_FY_END = os.getenv("TALLY_FY_END", "20260331")

logger = logging.getLogger("TallyAPI")

# ============================================================================
# RESPONSE MODEL
# ============================================================================

class APIResponse(BaseModel):
    success: bool
    data: Any = None
    error: Optional[str] = None
    count: Optional[int] = None
    extraction_method: Optional[str] = None
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())

# ============================================================================
# EXTRACTOR SINGLETON
# ============================================================================

_extractor: Optional[TallyDataExtractor] = None

def get_extractor() -> TallyDataExtractor:
    global _extractor
    if _extractor is None:
        _extractor = TallyDataExtractor(
            url=TALLY_URL, company_name=TALLY_COMPANY,
            odbc_dsn=TALLY_ODBC_DSN,
            financial_year_start=TALLY_FY_START, financial_year_end=TALLY_FY_END,
        )
    return _extractor

def reset_extractor(company_name=None, url=None, force_odbc=False):
    global _extractor
    _extractor = TallyDataExtractor(
        url=url or TALLY_URL, company_name=company_name or TALLY_COMPANY,
        odbc_dsn=TALLY_ODBC_DSN,
        financial_year_start=TALLY_FY_START, financial_year_end=TALLY_FY_END,
        force_odbc=force_odbc,
    )
    return _extractor

# ============================================================================
# APP
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting TruGenie Tally API...")
    ext = get_extractor()
    conn = ext.test_connection()
    method = conn.get("active_method", "none")
    logger.info("Startup connection: %s", method)
    yield
    logger.info("Shutting down.")

app = FastAPI(
    title="TruGenie - Tally Prime Integration API",
    description="REST API for Tally Prime data extraction. Vouchers use Export Data format.",
    version="1.2.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

def api_response(data, count=None):
    ext = get_extractor()
    return APIResponse(
        success=True, data=data,
        count=count if count is not None else (len(data) if isinstance(data, list) else None),
        extraction_method=ext.get_extraction_method(),
    )

def handle_error(exc, context):
    logger.exception("Error in %s: %s", context, exc)
    return JSONResponse(status_code=500, content=APIResponse(success=False, error=f"{context}: {str(exc)}").model_dump())

# ============================================================================
# HEALTH & CONFIG
# ============================================================================

@app.get("/", tags=["Health"])
async def root():
    return {"api": "TruGenie Tally Integration", "version": "1.2.0", "docs": "/docs"}

@app.get("/health", tags=["Health"])
async def health_check():
    ext = get_extractor()
    conn = ext.test_connection()
    return {
        "status": "healthy" if conn["active_method"] else "unhealthy",
        "tally_url": TALLY_URL, "company": TALLY_COMPANY,
        "xml_api": conn["xml_api"], "odbc": conn["odbc"],
        "active_method": conn["active_method"],
    }

@app.post("/config/switch-company", tags=["Config"])
async def switch_company(
    company_name: str = Query(...), tally_url: Optional[str] = Query(None),
    force_odbc: bool = Query(False),
):
    ext = reset_extractor(company_name=company_name, url=tally_url, force_odbc=force_odbc)
    conn = ext.test_connection()
    return {"success": True, "company": company_name, "connected": bool(conn["active_method"])}

# ============================================================================
# COMPANY
# ============================================================================

@app.get("/companies", tags=["Company"])
async def get_companies():
    try:
        companies = get_extractor().get_company_list()
        return api_response(companies, count=len(companies))
    except Exception as exc:
        return handle_error(exc, "get_companies")

@app.get("/company/info", tags=["Company"])
async def get_company_info():
    try:
        return api_response(get_extractor().get_company_info())
    except Exception as exc:
        return handle_error(exc, "get_company_info")

# ============================================================================
# LEDGERS
# ============================================================================

@app.get("/ledgers", tags=["Ledgers"])
async def get_all_ledgers(refresh: bool = Query(False)):
    try:
        return api_response(get_extractor().get_all_ledgers(force_refresh=refresh))
    except Exception as exc:
        return handle_error(exc, "get_all_ledgers")

@app.get("/ledgers/search", tags=["Ledgers"])
async def search_ledger(name: str = Query(...)):
    try:
        ledger = get_extractor().get_ledger_by_name(name)
        if not ledger:
            raise HTTPException(status_code=404, detail=f"Ledger '{name}' not found")
        return api_response(ledger)
    except HTTPException:
        raise
    except Exception as exc:
        return handle_error(exc, "search_ledger")

@app.get("/ledgers/group/{group_name}", tags=["Ledgers"])
async def get_ledgers_by_group(group_name: str):
    try:
        return api_response(get_extractor().get_ledgers_by_group(group_name))
    except Exception as exc:
        return handle_error(exc, "get_ledgers_by_group")

@app.get("/ledgers/bank-accounts", tags=["Ledgers"])
async def get_bank_accounts():
    try: return api_response(get_extractor().get_bank_accounts())
    except Exception as exc: return handle_error(exc, "get_bank_accounts")

@app.get("/ledgers/cash-accounts", tags=["Ledgers"])
async def get_cash_accounts():
    try: return api_response(get_extractor().get_cash_accounts())
    except Exception as exc: return handle_error(exc, "get_cash_accounts")

@app.get("/ledgers/fixed-assets", tags=["Ledgers"])
async def get_fixed_assets():
    try: return api_response(get_extractor().get_fixed_assets())
    except Exception as exc: return handle_error(exc, "get_fixed_assets")

@app.get("/ledgers/loans", tags=["Ledgers"])
async def get_loans():
    try: return api_response(get_extractor().get_loans())
    except Exception as exc: return handle_error(exc, "get_loans")

# ============================================================================
# DEBTORS & CREDITORS
# ============================================================================

@app.get("/debtors", tags=["Debtors & Creditors"])
async def get_debtors():
    try:
        debtors = get_extractor().get_debtors()
        total = sum(d.get("closing_balance", 0) for d in debtors)
        return api_response({"debtors": debtors, "total_receivables": total, "count": len(debtors)})
    except Exception as exc:
        return handle_error(exc, "get_debtors")

@app.get("/debtors/top", tags=["Debtors & Creditors"])
async def get_top_debtors(limit: int = Query(10, ge=1, le=100)):
    try: return api_response(get_extractor().get_top_debtors(limit))
    except Exception as exc: return handle_error(exc, "get_top_debtors")

@app.get("/creditors", tags=["Debtors & Creditors"])
async def get_creditors():
    try:
        creditors = get_extractor().get_creditors()
        total = sum(c.get("closing_balance", 0) for c in creditors)
        return api_response({"creditors": creditors, "total_payables": total, "count": len(creditors)})
    except Exception as exc:
        return handle_error(exc, "get_creditors")

@app.get("/creditors/top", tags=["Debtors & Creditors"])
async def get_top_creditors(limit: int = Query(10, ge=1, le=100)):
    try: return api_response(get_extractor().get_top_creditors(limit))
    except Exception as exc: return handle_error(exc, "get_top_creditors")

# ============================================================================
# VOUCHERS - Now uses Export Data format (returns real amounts!)
# ============================================================================

@app.get("/vouchers", tags=["Vouchers"])
async def get_vouchers(
    voucher_type: Optional[str] = Query(None, description="Sales, Purchase, Receipt, Payment, Journal, Contra"),
    from_date: Optional[str] = Query(None, description="YYYYMMDD"),
    to_date: Optional[str] = Query(None, description="YYYYMMDD"),
    limit: int = Query(500, ge=1, le=10000),
):
    """Get vouchers with amount, party name, narration. Uses Tally Export Data format."""
    try:
        vouchers = get_extractor().get_vouchers(
            voucher_type=voucher_type, from_date=from_date, to_date=to_date, limit=limit,
        )
        return api_response(vouchers)
    except Exception as exc:
        return handle_error(exc, "get_vouchers")

@app.get("/vouchers/details", tags=["Vouchers"])
async def get_voucher_details(
    voucher_type: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, description="YYYYMMDD"),
    to_date: Optional[str] = Query(None, description="YYYYMMDD"),
    limit: int = Query(200, ge=1, le=5000),
):
    """Get vouchers WITH line-item Dr/Cr ledger entries."""
    try:
        vouchers = get_extractor().get_vouchers_with_entries(
            voucher_type=voucher_type, from_date=from_date, to_date=to_date, limit=limit,
        )
        return api_response(vouchers)
    except Exception as exc:
        return handle_error(exc, "get_voucher_details")

@app.get("/vouchers/sales", tags=["Vouchers"])
async def get_sales_vouchers(from_date: Optional[str] = Query(None), to_date: Optional[str] = Query(None)):
    try: return api_response(get_extractor().get_sales_vouchers(from_date, to_date))
    except Exception as exc: return handle_error(exc, "get_sales_vouchers")

@app.get("/vouchers/purchases", tags=["Vouchers"])
async def get_purchase_vouchers(from_date: Optional[str] = Query(None), to_date: Optional[str] = Query(None)):
    try: return api_response(get_extractor().get_purchase_vouchers(from_date, to_date))
    except Exception as exc: return handle_error(exc, "get_purchase_vouchers")

@app.get("/vouchers/receipts", tags=["Vouchers"])
async def get_receipt_vouchers(from_date: Optional[str] = Query(None), to_date: Optional[str] = Query(None)):
    try: return api_response(get_extractor().get_receipt_vouchers(from_date, to_date))
    except Exception as exc: return handle_error(exc, "get_receipt_vouchers")

@app.get("/vouchers/payments", tags=["Vouchers"])
async def get_payment_vouchers(from_date: Optional[str] = Query(None), to_date: Optional[str] = Query(None)):
    try: return api_response(get_extractor().get_payment_vouchers(from_date, to_date))
    except Exception as exc: return handle_error(exc, "get_payment_vouchers")

@app.get("/vouchers/journals", tags=["Vouchers"])
async def get_journal_vouchers(from_date: Optional[str] = Query(None), to_date: Optional[str] = Query(None)):
    try: return api_response(get_extractor().get_journal_vouchers(from_date, to_date))
    except Exception as exc: return handle_error(exc, "get_journal_vouchers")

@app.get("/vouchers/daybook", tags=["Vouchers"])
async def get_day_book(date: Optional[str] = Query(None, description="YYYYMMDD, defaults to today")):
    """
    Get all vouchers for a specific date (Day Book).
    Returns vouchers with 'particulars' field matching Tally Day Book column.
    """
    try:
        ext = get_extractor()
        vouchers = ext.get_day_book(date)
        
        # Calculate summary by type
        from collections import defaultdict
        by_type = defaultdict(lambda: {"count": 0, "total": 0.0})
        for v in vouchers:
            t = v.get("voucher_type", "Other")
            by_type[t]["count"] += 1
            by_type[t]["total"] += v.get("amount", 0)
        
        return api_response({
            "date": date or "today",
            "vouchers": vouchers,
            "total_vouchers": len(vouchers),
            "summary_by_type": dict(by_type),
        })
    except Exception as exc:
        return handle_error(exc, "get_day_book")

# ============================================================================
# GROUPS & COST CENTRES
# ============================================================================

@app.get("/groups", tags=["Masters"])
async def get_all_groups():
    try: return api_response(get_extractor().get_all_groups())
    except Exception as exc: return handle_error(exc, "get_all_groups")

@app.get("/cost-centres", tags=["Masters"])
async def get_cost_centres():
    try: return api_response(get_extractor().get_cost_centres())
    except Exception as exc: return handle_error(exc, "get_cost_centres")

# ============================================================================
# REPORTS
# ============================================================================

@app.get("/reports/financial-summary", tags=["Reports"])
async def get_financial_summary():
    try: return api_response(get_extractor().get_financial_summary())
    except Exception as exc: return handle_error(exc, "get_financial_summary")

@app.get("/reports/group-summary", tags=["Reports"])
async def get_group_summary():
    try: return api_response(get_extractor().get_ledger_summary_by_group())
    except Exception as exc: return handle_error(exc, "get_group_summary")

@app.get("/reports/trial-balance", tags=["Reports"])
async def get_trial_balance(from_date: Optional[str] = Query(None), to_date: Optional[str] = Query(None)):
    try:
        tb = get_extractor().get_trial_balance(from_date, to_date)
        total_dr = sum(e.get("debit", 0) for e in tb)
        total_cr = sum(e.get("credit", 0) for e in tb)
        return api_response({
            "entries": tb, "total_debit": total_dr, "total_credit": total_cr,
            "difference": round(total_dr - total_cr, 2), "is_balanced": abs(total_dr - total_cr) < 1,
        })
    except Exception as exc:
        return handle_error(exc, "get_trial_balance")

# ============================================================================
# EXPORT
# ============================================================================

@app.get("/export/all", tags=["Export"])
async def export_all():
    try:
        start = time.time()
        data = get_extractor().export_all()
        data["export_duration_seconds"] = round(time.time() - start, 2)
        return api_response(data)
    except Exception as exc:
        return handle_error(exc, "export_all")

# ============================================================================
# DEBUG
# ============================================================================

@app.get("/debug/raw-voucher-xml", tags=["Debug"])
async def debug_raw_voucher_xml(
    from_date: str = Query("20250401"), to_date: str = Query("20250401"),
):
    """See raw XML that Tally returns for voucher Collection export."""
    ext = get_extractor()
    xml_req = f"""
    <ENVELOPE>
        <HEADER>
            <VERSION>1</VERSION>
            <TALLYREQUEST>Export</TALLYREQUEST>
            <TYPE>Collection</TYPE>
            <ID>DebugVchColl</ID>
        </HEADER>
        <BODY>
            <DESC>
                <STATICVARIABLES>
                    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                    <SVCURRENTCOMPANY>{ext.company_name}</SVCURRENTCOMPANY>
                    <SVFROMDATE>{from_date}</SVFROMDATE>
                    <SVTODATE>{to_date}</SVTODATE>
                </STATICVARIABLES>
                <TDL>
                    <TDLMESSAGE>
                        <COLLECTION NAME="DebugVchColl">
                            <TYPE>Voucher</TYPE>
                            <NATIVEMETHOD>VoucherNumber, VoucherTypeName, Date, 
                                          Amount, PartyLedgerName, Narration</NATIVEMETHOD>
                            <NATIVEMETHOD>AllLedgerEntries</NATIVEMETHOD>
                        </COLLECTION>
                    </TDLMESSAGE>
                </TDL>
            </DESC>
        </BODY>
    </ENVELOPE>"""
    raw = ext._execute_request(xml_req, timeout=60)
    if raw:
        return PlainTextResponse(raw[:20000], media_type="application/xml")
    return PlainTextResponse("No response from Tally", status_code=502)



if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8001, reload=True, log_level="info")
