"""
Tally Prime Data Extraction Functions for API Development
==========================================================

Primary: XML API over HTTP  |  Fallback: ODBC (Tally ODBC Driver)

CRITICAL FIX (Feb 16, 2026 - v4):
- Voucher extraction: 
  * Export Data (TALLYREQUEST=Export Data) → returns import dialog prompt (BROKEN)
  * Collection + NATIVEMETHOD=* → 1.3MB with invalid XML chars (BROKEN)  
  * Collection + NATIVEMETHOD=specific fields → 169KB, works perfectly! (USED)
  FIX: Use TYPE=Collection with NATIVEMETHOD listing specific fields.
  
- Company info: Added COLLECTION binding (was missing, caused empty response)

Author: Nimona Sarraf
Date: February 13, 2026
Updated: February 17, 2026 v4 (Collection Export for vouchers)
Purpose: POC - Nimona Integration (Real Estate Client)
"""

import requests
import xml.etree.ElementTree as ET
import re
import logging
import json
import os
import time
from typing import Dict, List, Optional, Union, Tuple
from datetime import datetime
from enum import Enum

# ============================================================================
# LOGGING SETUP
# ============================================================================

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler(
            os.path.join(LOG_DIR, f"tally_extraction_{datetime.now():%Y%m%d}.log"),
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("TallyExtractor")


# ============================================================================
# ENUMS
# ============================================================================

class VoucherType(str, Enum):
    SALES = "Sales"
    PURCHASE = "Purchase"
    RECEIPT = "Receipt"
    PAYMENT = "Payment"
    JOURNAL = "Journal"
    CONTRA = "Contra"
    CREDIT_NOTE = "Credit Note"
    DEBIT_NOTE = "Debit Note"


class ExtractionMethod(str, Enum):
    XML_API = "xml_api"
    ODBC = "odbc"


# ============================================================================
# MAIN EXTRACTOR CLASS
# ============================================================================

class TallyDataExtractor:
    """
    Comprehensive Tally Prime data extractor.
    Primary: XML API over HTTP  |  Fallback: ODBC via pyodbc
    
    KEY INSIGHT on Tally XML API:
    
    There are THREE ways to get data from Tally via XML:
    
    1. TDL Reports (custom reports via <TDL><TDLMESSAGE><REPORT>):
       - Works great for: Ledgers, Groups, Cost Centres, Company list
       - FAILS for voucher amounts because $Amount is not directly 
         accessible on Voucher collection in flat TDL report context.
    
    2. Export Data (<TALLYREQUEST>Export Data</TALLYREQUEST>):
       - BROKEN: Returns an import dialog prompt on this Tally version
       - May work on other Tally versions/configurations
    
    3. Collection Export (<TYPE>Collection</TYPE> + <NATIVEMETHOD>):
       - WORKS for vouchers! Returns VOUCHER objects with all nested data.
       - Key: use specific NATIVEMETHOD fields (not *) to avoid
         1MB+ responses with invalid XML character references.
       - Returns: VoucherNumber, VoucherTypeName, Date, Amount,
         PartyLedgerName, Narration, AllLedgerEntries
    
    This module uses approach #1 for ledgers/groups and #3 for vouchers.
    """

    def __init__(
        self,
        url: str = "http://localhost:9000",
        company_name: str = "Nimona",
        odbc_dsn: str = "TallyODBC_9000",
        financial_year_start: str = "20250401",
        financial_year_end: str = "20260331",
        force_odbc: bool = False,
        max_retries: int = 3,
    ):
        self.url = url
        self.company_name = company_name
        self.odbc_dsn = odbc_dsn
        self.fy_start = financial_year_start
        self.fy_end = financial_year_end
        self.force_odbc = force_odbc
        self.max_retries = max_retries
        self._method = ExtractionMethod.ODBC if force_odbc else ExtractionMethod.XML_API

        # Cache
        self._ledger_cache: Optional[List[Dict]] = None
        self._cache_time: Optional[float] = None
        self._cache_ttl = 300  # 5 minutes

        logger.info(
            "TallyDataExtractor init | company=%s | url=%s | FY=%s-%s | force_odbc=%s",
            company_name, url, financial_year_start, financial_year_end, force_odbc,
        )

    # ========================================================================
    # INTERNAL HELPERS
    # ========================================================================

    @staticmethod
    def clean_xml(xml_string: str) -> str:
        """Remove invalid XML character references (control chars)."""
        return re.sub(r'&#([0-8]|1[0-9]|2[0-9]|3[01]);', '', xml_string)

    @staticmethod
    def parse_amount(amount_str: str) -> Tuple[float, str]:
        """
        Parse Tally amount string to (abs_amount, 'Dr'|'Cr').
        Tally: Positive = Debit, Negative = Credit
        """
        if not amount_str:
            return 0.0, "Dr"

        cleaned = amount_str.replace(",", "").replace(" ", "").strip()

        if cleaned.upper().endswith("DR"):
            cleaned = cleaned[:-2].strip()
            forced_dr = True
        elif cleaned.upper().endswith("CR"):
            cleaned = cleaned[:-2].strip()
            forced_dr = False
        else:
            forced_dr = None

        try:
            val = float(cleaned)
        except ValueError:
            return 0.0, "Dr"

        if forced_dr is not None:
            return abs(val), "Dr" if forced_dr else "Cr"

        if val < 0:
            return abs(val), "Cr"
        return val, "Dr"

    @staticmethod
    def parse_tally_date(date_str: str) -> str:
        """
        Parse Tally date to YYYY-MM-DD.
        Handles: YYYYMMDD, d-MMM-YY, d-MMM-YYYY, DD-MM-YYYY, etc.
        """
        if not date_str:
            return ""
        date_str = date_str.strip()

        if re.match(r'^\d{8}$', date_str):
            return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

        for fmt in ("%d-%b-%Y", "%d-%b-%y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue

        return date_str

    @staticmethod
    def _get_xml_text(element, tag: str, default: str = "") -> str:
        """Safely get text from an XML child element."""
        child = element.find(tag)
        if child is not None and child.text:
            return child.text.strip()
        return default

    def _execute_request(self, xml_request: str, timeout: int = 30) -> Optional[str]:
        """Execute XML request to Tally with retry logic."""
        if self.force_odbc:
            return None

        headers = {"Content-Type": "application/xml"}

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.post(
                    self.url, data=xml_request, headers=headers, timeout=timeout
                )
                if resp.status_code == 200:
                    self._method = ExtractionMethod.XML_API
                    logger.debug("XML API OK (%d bytes) attempt=%d", len(resp.text), attempt)
                    return self.clean_xml(resp.text)
                else:
                    logger.warning("Tally HTTP %d (attempt %d/%d)", resp.status_code, attempt, self.max_retries)
            except requests.exceptions.ConnectionError:
                logger.warning("Connection failed to %s (attempt %d/%d)", self.url, attempt, self.max_retries)
            except requests.exceptions.Timeout:
                logger.warning("Timeout after %ds (attempt %d/%d)", timeout, attempt, self.max_retries)
            except Exception as exc:
                logger.exception("Unexpected error (attempt %d/%d): %s", attempt, self.max_retries, exc)

            if attempt < self.max_retries:
                time.sleep(attempt * 2)

        logger.error("All %d XML API attempts failed", self.max_retries)
        return None

    # ========================================================================
    # ODBC FALLBACK
    # ========================================================================

    def _get_odbc_connection(self):
        try:
            import pyodbc
        except ImportError:
            logger.error("pyodbc not installed.")
            return None
        try:
            conn_str = f"DSN={self.odbc_dsn};Company={self.company_name};Port={self.url.split(':')[-1]};"
            logger.info("ODBC connecting: DSN=%s, Company=%s", self.odbc_dsn, self.company_name)
            conn = pyodbc.connect(conn_str, timeout=30)
            self._method = ExtractionMethod.ODBC
            return conn
        except Exception as exc:
            logger.error("ODBC connection failed: %s", exc)
            return None

    def _odbc_query(self, sql: str) -> Optional[List[Dict]]:
        conn = self._get_odbc_connection()
        if not conn:
            return None
        try:
            cursor = conn.cursor()
            cursor.execute(sql)
            columns = [col[0] for col in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            conn.close()
            return rows
        except Exception as exc:
            logger.error("ODBC query failed: %s | SQL: %s", exc, sql[:200])
            try:
                conn.close()
            except Exception:
                pass
            return None

    def _odbc_get_ledgers(self) -> Optional[List[Dict]]:
        sql = ("SELECT $Name, $Parent, $OpeningBalance, $ClosingBalance, "
               "$Address, $PartyGSTIN, $IncomeTaxNumber, $Email, $Phone, "
               "$LedStateName, $Pincode FROM Ledger")
        rows = self._odbc_query(sql)
        if not rows:
            return None
        ledgers = []
        for row in rows:
            o_raw, c_raw = row.get("$OpeningBalance", 0), row.get("$ClosingBalance", 0)
            if isinstance(o_raw, (int, float)):
                o_amt, o_dc = abs(o_raw), "Cr" if o_raw < 0 else "Dr"
            else:
                o_amt, o_dc = self.parse_amount(str(o_raw))
            if isinstance(c_raw, (int, float)):
                c_amt, c_dc = abs(c_raw), "Cr" if c_raw < 0 else "Dr"
            else:
                c_amt, c_dc = self.parse_amount(str(c_raw))
            o_sign = o_amt if o_dc == "Dr" else -o_amt
            c_sign = c_amt if c_dc == "Dr" else -c_amt
            ledgers.append({
                "ledger_name": row.get("$Name", ""), "company": self.company_name,
                "parent_group": row.get("$Parent", ""),
                "opening_balance": o_amt, "opening_dr_cr": o_dc,
                "closing_balance": c_amt, "closing_dr_cr": c_dc,
                "address": row.get("$Address", ""), "gstin": row.get("$PartyGSTIN", ""),
                "pan": row.get("$IncomeTaxNumber", ""), "email": row.get("$Email", ""),
                "phone": row.get("$Phone", ""), "state": row.get("$LedStateName", ""),
                "pincode": row.get("$Pincode", ""),
                "net_movement": round(c_sign - o_sign, 2),
            })
        return ledgers

    def _odbc_get_company_list(self) -> Optional[List[str]]:
        rows = self._odbc_query("SELECT $Name FROM Company")
        if rows:
            return [r.get("$Name", "") for r in rows if r.get("$Name")]
        return None

    # ========================================================================
    # CONNECTION TEST
    # ========================================================================

    def test_connection(self) -> Dict:
        result = {
            "xml_api": {"connected": False, "companies": [], "error": None},
            "odbc": {"connected": False, "companies": [], "error": None},
            "active_method": None,
        }
        if not self.force_odbc:
            try:
                companies = self._xml_get_company_list()
                if companies:
                    result["xml_api"]["connected"] = True
                    result["xml_api"]["companies"] = companies
                    result["active_method"] = "xml_api"
                    logger.info("XML API: OK (%d companies)", len(companies))
                else:
                    result["xml_api"]["error"] = "No companies returned"
            except Exception as exc:
                result["xml_api"]["error"] = str(exc)
        try:
            companies = self._odbc_get_company_list()
            if companies:
                result["odbc"]["connected"] = True
                result["odbc"]["companies"] = companies
                if not result["active_method"]:
                    result["active_method"] = "odbc"
            else:
                result["odbc"]["error"] = "No companies returned or pyodbc not installed"
        except Exception as exc:
            result["odbc"]["error"] = str(exc)
        return result

    # ========================================================================
    # COMPANY INFORMATION
    # ========================================================================

    def _xml_get_company_list(self) -> List[str]:
        xml_request = """
        <ENVELOPE>
            <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
            <TYPE>Data</TYPE><ID>List of Companies</ID></HEADER>
            <BODY><DESC>
                <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES>
                <TDL><TDLMESSAGE>
                    <REPORT NAME="List of Companies"><FORMS>CompanyForm</FORMS></REPORT>
                    <FORM NAME="CompanyForm"><PARTS>CompanyPart</PARTS></FORM>
                    <PART NAME="CompanyPart">
                        <LINES>CompanyLine</LINES>
                        <REPEAT>CompanyLine : Company</REPEAT>
                        <SCROLLED>Vertical</SCROLLED>
                    </PART>
                    <LINE NAME="CompanyLine"><FIELDS>FldCompanyName</FIELDS></LINE>
                    <FIELD NAME="FldCompanyName"><SET>$Name</SET></FIELD>
                </TDLMESSAGE></TDL>
            </DESC></BODY>
        </ENVELOPE>"""
        xml_resp = self._execute_request(xml_request)
        if not xml_resp:
            return []
        try:
            root = ET.fromstring(xml_resp)
            return [e.text.strip() for e in root.iter() if e.tag == "FLDCOMPANYNAME" and e.text]
        except ET.ParseError as exc:
            logger.error("XML parse error (company list): %s", exc)
            return []

    def get_company_list(self) -> List[str]:
        if not self.force_odbc:
            result = self._xml_get_company_list()
            if result:
                return result
        return self._odbc_get_company_list() or []

    def get_company_info(self) -> Dict:
        """
        Get current company information.
        FIX: Added <COLLECTION NAME="CmpColl"><TYPE>Company</TYPE></COLLECTION>
        Without this, the REPEAT had nothing to iterate over → empty fields.
        """
        xml_request = f"""
        <ENVELOPE>
            <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
            <TYPE>Data</TYPE><ID>CompanyInfoReport</ID></HEADER>
            <BODY><DESC>
                <STATICVARIABLES>
                    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                    <SVCURRENTCOMPANY>{self.company_name}</SVCURRENTCOMPANY>
                </STATICVARIABLES>
                <TDL><TDLMESSAGE>
                    <REPORT NAME="CompanyInfoReport"><FORMS>CmpForm</FORMS></REPORT>
                    <FORM NAME="CmpForm"><PARTS>CmpPart</PARTS></FORM>
                    <PART NAME="CmpPart">
                        <LINES>CmpLine</LINES>
                        <REPEAT>CmpLine : CmpColl</REPEAT>
                        <SCROLLED>Vertical</SCROLLED>
                    </PART>
                    <LINE NAME="CmpLine">
                        <FIELDS>FldCmpName, FldCmpAddr, FldCmpState, FldCmpPin,
                                FldCmpPhone, FldCmpEmail, FldCmpGSTIN, FldCmpPAN,
                                FldCmpBooksFrom</FIELDS>
                    </LINE>
                    <FIELD NAME="FldCmpName"><SET>$Name</SET></FIELD>
                    <FIELD NAME="FldCmpAddr"><SET>$Address</SET></FIELD>
                    <FIELD NAME="FldCmpState"><SET>$State</SET></FIELD>
                    <FIELD NAME="FldCmpPin"><SET>$Pincode</SET></FIELD>
                    <FIELD NAME="FldCmpPhone"><SET>$PhoneNumber</SET></FIELD>
                    <FIELD NAME="FldCmpEmail"><SET>$Email</SET></FIELD>
                    <FIELD NAME="FldCmpGSTIN"><SET>$GSTIN</SET></FIELD>
                    <FIELD NAME="FldCmpPAN"><SET>$IncomeTaxNumber</SET></FIELD>
                    <FIELD NAME="FldCmpBooksFrom"><SET>$BooksFrom</SET></FIELD>
                    <COLLECTION NAME="CmpColl"><TYPE>Company</TYPE></COLLECTION>
                </TDLMESSAGE></TDL>
            </DESC></BODY>
        </ENVELOPE>"""

        xml_resp = self._execute_request(xml_request)
        info = {
            "company_name": "", "address": "", "state": "", "pincode": "",
            "phone": "", "email": "", "gstin": "", "pan": "", "books_from": "",
        }
        if not xml_resp:
            info["company_name"] = self.company_name
            return info
        try:
            root = ET.fromstring(xml_resp)
            tag_map = {
                "FLDCMPNAME": "company_name", "FLDCMPADDR": "address",
                "FLDCMPSTATE": "state", "FLDCMPPIN": "pincode",
                "FLDCMPPHONE": "phone", "FLDCMPEMAIL": "email",
                "FLDCMPGSTIN": "gstin", "FLDCMPPAN": "pan",
                "FLDCMPBOOKSFROM": "books_from",
            }
            for elem in root.iter():
                key = tag_map.get(elem.tag)
                if key and elem.text:
                    info[key] = elem.text.strip()
            if not info["company_name"]:
                info["company_name"] = self.company_name
            return info
        except ET.ParseError as exc:
            logger.error("XML parse error (company info): %s", exc)
            info["company_name"] = self.company_name
            return info

    # ========================================================================
    # LEDGER FUNCTIONS (TDL approach - works perfectly for ledgers)
    # ========================================================================

    def _invalidate_cache(self):
        self._ledger_cache = None
        self._cache_time = None

    def get_all_ledgers(self, force_refresh: bool = False) -> List[Dict]:
        if (not force_refresh and self._ledger_cache is not None
                and self._cache_time and (time.time() - self._cache_time) < self._cache_ttl):
            return self._ledger_cache

        ledgers = self._xml_get_ledgers()
        if ledgers is None:
            logger.info("XML API failed for ledgers, trying ODBC...")
            ledgers = self._odbc_get_ledgers()
        if ledgers is None:
            logger.error("Both XML API and ODBC failed for ledgers")
            return []

        self._ledger_cache = ledgers
        self._cache_time = time.time()
        logger.info("Fetched %d ledgers via %s", len(ledgers), self._method.value)
        return ledgers

    def _xml_get_ledgers(self) -> Optional[List[Dict]]:
        xml_request = f"""
        <ENVELOPE>
            <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
            <TYPE>Data</TYPE><ID>MyReportLedgerTable</ID></HEADER>
            <BODY><DESC>
                <STATICVARIABLES>
                    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                    <SVCURRENTCOMPANY>{self.company_name}</SVCURRENTCOMPANY>
                </STATICVARIABLES>
                <TDL><TDLMESSAGE>
                    <REPORT NAME="MyReportLedgerTable"><FORMS>MyFormLedgerTable</FORMS></REPORT>
                    <FORM NAME="MyFormLedgerTable"><PARTS>MyPartLedgerTable</PARTS></FORM>
                    <PART NAME="MyPartLedgerTable">
                        <LINES>MyLineLedgerTable</LINES>
                        <REPEAT>MyLineLedgerTable : LedgerCollection</REPEAT>
                        <SCROLLED>Vertical</SCROLLED>
                    </PART>
                    <LINE NAME="MyLineLedgerTable">
                        <FIELDS>FldName, FldParent, FldOpeningBalance, FldClosingBalance,
                                FldAddress, FldGSTIN, FldPAN, FldEmail, FldPhone,
                                FldState, FldPincode, FldCreditPeriod</FIELDS>
                    </LINE>
                    <FIELD NAME="FldName"><SET>$Name</SET></FIELD>
                    <FIELD NAME="FldParent"><SET>$Parent</SET></FIELD>
                    <FIELD NAME="FldOpeningBalance"><SET>$OpeningBalance</SET></FIELD>
                    <FIELD NAME="FldClosingBalance"><SET>$ClosingBalance</SET></FIELD>
                    <FIELD NAME="FldAddress"><SET>$Address</SET></FIELD>
                    <FIELD NAME="FldGSTIN"><SET>$PartyGSTIN</SET></FIELD>
                    <FIELD NAME="FldPAN"><SET>$IncomeTaxNumber</SET></FIELD>
                    <FIELD NAME="FldEmail"><SET>$Email</SET></FIELD>
                    <FIELD NAME="FldPhone"><SET>$Phone</SET></FIELD>
                    <FIELD NAME="FldState"><SET>$LedStateName</SET></FIELD>
                    <FIELD NAME="FldPincode"><SET>$Pincode</SET></FIELD>
                    <FIELD NAME="FldCreditPeriod"><SET>$CreditPeriod</SET></FIELD>
                    <COLLECTION NAME="LedgerCollection"><TYPE>Ledger</TYPE></COLLECTION>
                </TDLMESSAGE></TDL>
            </DESC></BODY>
        </ENVELOPE>"""

        xml_resp = self._execute_request(xml_request)
        if not xml_resp:
            return None
        try:
            root = ET.fromstring(xml_resp)
        except ET.ParseError as exc:
            logger.error("XML parse error (ledgers): %s", exc)
            return None

        ledgers, current = [], {}
        for elem in root:
            tag = elem.tag
            value = elem.text.strip() if elem.text else ""
            if tag == "FLDNAME":
                if current and "ledger_name" in current:
                    ledgers.append(current)
                current = {"ledger_name": value, "company": self.company_name}
            elif tag == "FLDPARENT":
                current["parent_group"] = value
            elif tag == "FLDOPENINGBALANCE":
                amt, dc = self.parse_amount(value)
                current["opening_balance"] = amt
                current["opening_dr_cr"] = dc
            elif tag == "FLDCLOSINGBALANCE":
                amt, dc = self.parse_amount(value)
                current["closing_balance"] = amt
                current["closing_dr_cr"] = dc
            elif tag == "FLDADDRESS": current["address"] = value
            elif tag == "FLDGSTIN": current["gstin"] = value
            elif tag == "FLDPAN": current["pan"] = value
            elif tag == "FLDEMAIL": current["email"] = value
            elif tag == "FLDPHONE": current["phone"] = value
            elif tag == "FLDSTATE": current["state"] = value
            elif tag == "FLDPINCODE": current["pincode"] = value
            elif tag == "FLDCREDITPERIOD": current["credit_period"] = value

        if current and "ledger_name" in current:
            ledgers.append(current)

        for led in ledgers:
            o = led.get("opening_balance", 0)
            c = led.get("closing_balance", 0)
            o_sign = o if led.get("opening_dr_cr") == "Dr" else -o
            c_sign = c if led.get("closing_dr_cr") == "Dr" else -c
            led["net_movement"] = round(c_sign - o_sign, 2)

        return ledgers

    def get_ledger_by_name(self, ledger_name: str) -> Optional[Dict]:
        for led in self.get_all_ledgers():
            if led.get("ledger_name", "").lower() == ledger_name.lower():
                return led
        return None

    def get_ledgers_by_group(self, group_name: str) -> List[Dict]:
        return [l for l in self.get_all_ledgers()
                if l.get("parent_group", "").lower() == group_name.lower()]

    def get_bank_accounts(self): return self.get_ledgers_by_group("Bank Accounts")
    def get_cash_accounts(self): return self.get_ledgers_by_group("Cash-in-Hand")
    def get_debtors(self): return self.get_ledgers_by_group("Sundry Debtors")
    def get_creditors(self): return self.get_ledgers_by_group("Sundry Creditors")
    def get_fixed_assets(self): return self.get_ledgers_by_group("Fixed Assets")
    def get_loans(self):
        return self.get_ledgers_by_group("Secured Loans") + self.get_ledgers_by_group("Unsecured Loans")

    # ========================================================================
    # ACCOUNT GROUPS (TDL approach - works fine)
    # ========================================================================

    def get_all_groups(self) -> List[Dict]:
        xml_request = f"""
        <ENVELOPE>
            <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
            <TYPE>Data</TYPE><ID>GroupReport</ID></HEADER>
            <BODY><DESC>
                <STATICVARIABLES>
                    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                    <SVCURRENTCOMPANY>{self.company_name}</SVCURRENTCOMPANY>
                </STATICVARIABLES>
                <TDL><TDLMESSAGE>
                    <REPORT NAME="GroupReport"><FORMS>GroupForm</FORMS></REPORT>
                    <FORM NAME="GroupForm"><PARTS>GroupPart</PARTS></FORM>
                    <PART NAME="GroupPart">
                        <LINES>GroupLine</LINES>
                        <REPEAT>GroupLine : GroupCollection</REPEAT>
                        <SCROLLED>Vertical</SCROLLED>
                    </PART>
                    <LINE NAME="GroupLine"><FIELDS>FldGrpName, FldGrpParent, FldGrpPrimary</FIELDS></LINE>
                    <FIELD NAME="FldGrpName"><SET>$Name</SET></FIELD>
                    <FIELD NAME="FldGrpParent"><SET>$Parent</SET></FIELD>
                    <FIELD NAME="FldGrpPrimary"><SET>$IsPrimary</SET></FIELD>
                    <COLLECTION NAME="GroupCollection"><TYPE>Group</TYPE></COLLECTION>
                </TDLMESSAGE></TDL>
            </DESC></BODY>
        </ENVELOPE>"""
        xml_resp = self._execute_request(xml_request)
        if not xml_resp:
            return []
        try:
            root = ET.fromstring(xml_resp)
            groups, cur = [], {}
            for elem in root:
                tag, val = elem.tag, (elem.text or "").strip()
                if tag == "FLDGRPNAME":
                    if cur: groups.append(cur)
                    cur = {"group_name": val}
                elif tag == "FLDGRPPARENT": cur["parent"] = val
                elif tag == "FLDGRPPRIMARY": cur["is_primary"] = val.lower() in ("yes", "true", "1")
            if cur: groups.append(cur)
            return groups
        except ET.ParseError:
            return []

    # ========================================================================
    # COST CENTRES (TDL approach - works fine)
    # ========================================================================

    def get_cost_centres(self) -> List[Dict]:
        xml_request = f"""
        <ENVELOPE>
            <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
            <TYPE>Data</TYPE><ID>CostCentreReport</ID></HEADER>
            <BODY><DESC>
                <STATICVARIABLES>
                    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                    <SVCURRENTCOMPANY>{self.company_name}</SVCURRENTCOMPANY>
                </STATICVARIABLES>
                <TDL><TDLMESSAGE>
                    <REPORT NAME="CostCentreReport"><FORMS>CCForm</FORMS></REPORT>
                    <FORM NAME="CCForm"><PARTS>CCPart</PARTS></FORM>
                    <PART NAME="CCPart">
                        <LINES>CCLine</LINES>
                        <REPEAT>CCLine : CCColl</REPEAT>
                        <SCROLLED>Vertical</SCROLLED>
                    </PART>
                    <LINE NAME="CCLine"><FIELDS>FldCCName, FldCCParent</FIELDS></LINE>
                    <FIELD NAME="FldCCName"><SET>$Name</SET></FIELD>
                    <FIELD NAME="FldCCParent"><SET>$Parent</SET></FIELD>
                    <COLLECTION NAME="CCColl"><TYPE>Cost Centre</TYPE></COLLECTION>
                </TDLMESSAGE></TDL>
            </DESC></BODY>
        </ENVELOPE>"""
        xml_resp = self._execute_request(xml_request)
        if not xml_resp:
            return []
        try:
            root = ET.fromstring(xml_resp)
            centres, cur = [], {}
            for elem in root:
                tag, val = elem.tag, (elem.text or "").strip()
                if tag == "FLDCCNAME":
                    if cur: centres.append(cur)
                    cur = {"cost_centre": val}
                elif tag == "FLDCCPARENT": cur["parent"] = val
            if cur: centres.append(cur)
            return centres
        except ET.ParseError:
            return []

    # ========================================================================
    # VOUCHER FUNCTIONS - Uses Collection Export (TYPE=Collection)
    # ========================================================================
    # 
    # WHY NOT TDL Reports: $Amount, $PartyLedgerName return empty/0
    # WHY NOT Export Data: Returns import dialog prompt (broken on this Tally)
    # WHY NOT NATIVEMETHOD=*: Returns 1.3MB with invalid XML char refs
    #
    # SOLUTION: Collection Export with specific NATIVEMETHOD fields:
    #   <TYPE>Collection</TYPE>
    #   <NATIVEMETHOD>VoucherNumber, VoucherTypeName, Date, 
    #                 Amount, PartyLedgerName, Narration</NATIVEMETHOD>
    #   <NATIVEMETHOD>AllLedgerEntries</NATIVEMETHOD>
    #
    # Returns 169KB with 59+ clean VOUCHER elements including:
    #   VOUCHERNUMBER, VOUCHERTYPENAME, DATE, AMOUNT, 
    #   PARTYLEDGERNAME, NARRATION, ALLLEDGERENTRIES.LIST
    # ========================================================================

    def _parse_voucher_element(self, vch_elem) -> Dict:
        """
        Parse a single <VOUCHER> XML element into a dict.
        
        Tally Export Data XML structure:
        <VOUCHER VCHTYPE="Sales" REMOTEID="...">
            <DATE>20250401</DATE>
            <VOUCHERNUMBER>19</VOUCHERNUMBER>
            <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
            <PARTYLEDGERNAME>Godrej Properties Limited</PARTYLEDGERNAME>
            <NARRATION>PMC charges...</NARRATION>
            <ALLLEDGERENTRIES.LIST>
                <LEDGERNAME>Godrej Properties Limited</LEDGERNAME>
                <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                <AMOUNT>-5500000.00</AMOUNT>   ← negative = debit entry
            </ALLLEDGERENTRIES.LIST>
            <ALLLEDGERENTRIES.LIST>
                <LEDGERNAME>PMC Service Income</LEDGERNAME>
                <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
                <AMOUNT>5500000.00</AMOUNT>    ← positive = credit entry
            </ALLLEDGERENTRIES.LIST>
        </VOUCHER>
        
        AMOUNT sign convention in Tally Export:
          Negative amount = Debit entry  (ISDEEMEDPOSITIVE=Yes)
          Positive amount = Credit entry (ISDEEMEDPOSITIVE=No)
        
        Day Book "Particulars" mapping:
          Payment:  first ledger entry (party/expense being debited)
          Receipt:  first ledger entry (bank being debited)
          Sales:    party ledger name (debtor)
          Purchase: party ledger name (creditor)
          Journal:  first ledger entry (expense being debited)
        """
        vch = {
            "voucher_number": "",
            "company": self.company_name,
            "voucher_type": vch_elem.get("VCHTYPE", ""),
            "date": "",
            "party_name": "",
            "particulars": "",  # Matches Tally Day Book "Particulars" column
            "narration": "",
            "amount": 0.0,
            "ledger_entries": [],
        }

        for child in vch_elem:
            tag = child.tag
            val = (child.text or "").strip()

            if tag == "VOUCHERNUMBER":
                vch["voucher_number"] = val
            elif tag == "DATE":
                vch["date"] = self.parse_tally_date(val)
            elif tag in ("PARTYLEDGERNAME", "PARTYNAME"):
                if val:
                    vch["party_name"] = val
            elif tag == "NARRATION":
                vch["narration"] = val
            elif tag == "VOUCHERTYPENAME":
                vch["voucher_type"] = val
            elif tag == "ALLLEDGERENTRIES.LIST":
                entry = {}
                for e_child in child:
                    e_tag = e_child.tag
                    e_val = (e_child.text or "").strip()
                    if e_tag == "LEDGERNAME":
                        entry["ledger_name"] = e_val
                    elif e_tag == "AMOUNT":
                        # Tally Export: negative = debit, positive = credit
                        raw_amt = 0.0
                        try:
                            raw_amt = float(e_val.replace(",", ""))
                        except (ValueError, AttributeError):
                            pass
                        entry["raw_amount"] = raw_amt
                        entry["amount"] = abs(raw_amt)
                        # Negative = Debit entry, Positive = Credit entry
                        entry["dr_cr"] = "Dr" if raw_amt < 0 else "Cr"
                    elif e_tag == "ISDEEMEDPOSITIVE":
                        entry["is_debit"] = e_val.lower() in ("yes", "true")
                if entry:
                    vch["ledger_entries"].append(entry)

        # ----------------------------------------------------------------
        # Calculate voucher amount:
        # Sum of all DEBIT entries (negative amounts in Tally = debit)
        # This matches the "Debit Amount" column in Tally Day Book
        # ----------------------------------------------------------------
        debit_entries = [e for e in vch["ledger_entries"] if e.get("raw_amount", 0) < 0]
        credit_entries = [e for e in vch["ledger_entries"] if e.get("raw_amount", 0) > 0]

        # Voucher total = sum of debit side (absolute values)
        debit_total = sum(abs(e.get("raw_amount", 0)) for e in debit_entries)
        credit_total = sum(abs(e.get("raw_amount", 0)) for e in credit_entries)
        vch["amount"] = debit_total or credit_total

        # ----------------------------------------------------------------
        # Particulars: matches Tally Day Book "Particulars" column
        # Day Book shows the FIRST ledger entry's name
        # ----------------------------------------------------------------
        if vch["ledger_entries"]:
            vch["particulars"] = vch["ledger_entries"][0].get("ledger_name", "")

        # If party_name is empty, use particulars
        if not vch["party_name"] and vch["particulars"]:
            vch["party_name"] = vch["particulars"]

        # Clean up raw_amount from entries before returning
        for entry in vch["ledger_entries"]:
            entry.pop("raw_amount", None)

        return vch

    def get_vouchers(
        self,
        voucher_type: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        limit: int = 500,
        include_entries: bool = False,
    ) -> List[Dict]:
        """
        Get vouchers using Collection Export with NATIVEMETHOD.
        
        IMPORTANT: We use TYPE=Collection with NATIVEMETHOD (not Export Data).
        
        Tested approaches and results:
        - Export Data + REPORTNAME=Vouchers → returns import dialog (FAILS)
        - Export Data + REPORTNAME=Day Book → 2MB response with invalid XML chars (FAILS)
        - Collection + NATIVEMETHOD=* → 1.3MB, invalid XML chars (FAILS)
        - Collection + NATIVEMETHOD=specific fields → 169KB, 59 VOUCHER elements (WORKS!)
        
        The working format returns:
          <VOUCHER VCHTYPE="Sales">
            <VOUCHERNUMBER>19</VOUCHERNUMBER>
            <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
            <PARTYLEDGERNAME TYPE="String">Godrej Properties Limited</PARTYLEDGERNAME>
            <NARRATION TYPE="String">PMC charges...</NARRATION>
            <AMOUNT TYPE="Amount">-5500000.00</AMOUNT>
            <ALLLEDGERENTRIES.LIST>...</ALLLEDGERENTRIES.LIST>
          </VOUCHER>
        """
        fd = from_date or self.fy_start
        td = to_date or self.fy_end

        xml_request = f"""
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
                        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                        <SVCURRENTCOMPANY>{self.company_name}</SVCURRENTCOMPANY>
                        <SVFROMDATE>{fd}</SVFROMDATE>
                        <SVTODATE>{td}</SVTODATE>
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
        </ENVELOPE>"""

        xml_resp = self._execute_request(xml_request, timeout=120)
        if not xml_resp:
            logger.warning("Voucher collection export failed")
            return []

        try:
            root = ET.fromstring(xml_resp)
        except ET.ParseError as exc:
            logger.error("XML parse error (vouchers): %s", exc)
            # The response may contain invalid XML character references
            # Try cleaning more aggressively
            try:
                cleaned = re.sub(r'&#x[0-9a-fA-F]+;', '', xml_resp)
                cleaned = re.sub(r'&#\d+;', '', cleaned)
                root = ET.fromstring(cleaned)
                logger.info("Parsed vouchers after aggressive XML cleaning")
            except ET.ParseError as exc2:
                logger.error("XML parse still failed after cleaning: %s", exc2)
                return []

        vouchers = []
        for vch_elem in root.iter("VOUCHER"):
            vch = self._parse_voucher_element(vch_elem)

            if not vch["voucher_number"]:
                continue

            # Filter by voucher type if specified
            if voucher_type and vch.get("voucher_type", "").lower() != voucher_type.lower():
                continue

            # Remove ledger_entries from response unless requested
            if not include_entries:
                vch.pop("ledger_entries", None)

            vouchers.append(vch)

            if len(vouchers) >= limit:
                break

        logger.info(
            "Extracted %d vouchers (type=%s, %s to %s) via %s",
            len(vouchers), voucher_type or "ALL", fd, td, self._method.value,
        )
        return vouchers

    def get_vouchers_with_entries(
        self,
        voucher_type: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict]:
        """Get vouchers WITH full ledger entry details (Dr/Cr per ledger)."""
        return self.get_vouchers(
            voucher_type=voucher_type,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            include_entries=True,
        )

    # Convenience shortcuts
    def get_sales_vouchers(self, from_date=None, to_date=None):
        return self.get_vouchers("Sales", from_date, to_date)

    def get_purchase_vouchers(self, from_date=None, to_date=None):
        return self.get_vouchers("Purchase", from_date, to_date)

    def get_receipt_vouchers(self, from_date=None, to_date=None):
        return self.get_vouchers("Receipt", from_date, to_date)

    def get_payment_vouchers(self, from_date=None, to_date=None):
        return self.get_vouchers("Payment", from_date, to_date)

    def get_journal_vouchers(self, from_date=None, to_date=None):
        return self.get_vouchers("Journal", from_date, to_date)

    def get_contra_vouchers(self, from_date=None, to_date=None):
        return self.get_vouchers("Contra", from_date, to_date)

    def get_credit_notes(self, from_date=None, to_date=None):
        return self.get_vouchers("Credit Note", from_date, to_date)

    def get_debit_notes(self, from_date=None, to_date=None):
        return self.get_vouchers("Debit Note", from_date, to_date)

    def get_day_book(self, date: Optional[str] = None) -> List[Dict]:
        """Get all vouchers for a date."""
        if not date:
            date = datetime.now().strftime("%Y%m%d")
        return self.get_vouchers(from_date=date, to_date=date)

    # ========================================================================
    # TRIAL BALANCE
    # ========================================================================

    def get_trial_balance(self, from_date=None, to_date=None) -> List[Dict]:
        ledgers = self.get_all_ledgers()
        tb = []
        for led in ledgers:
            c = led.get("closing_balance", 0)
            dc = led.get("closing_dr_cr", "Dr")
            if c > 0 or led.get("opening_balance", 0) > 0:
                tb.append({
                    "ledger_name": led.get("ledger_name", ""),
                    "parent_group": led.get("parent_group", ""),
                    "debit": c if dc == "Dr" else 0,
                    "credit": c if dc == "Cr" else 0,
                    "closing_balance": c,
                    "closing_dr_cr": dc,
                })
        return tb

    # ========================================================================
    # SUMMARY & ANALYTICS
    # ========================================================================

    def get_ledger_summary_by_group(self) -> Dict:
        summary: Dict[str, Dict] = {}
        for led in self.get_all_ledgers():
            grp = led.get("parent_group", "Unknown")
            if grp not in summary:
                summary[grp] = {"count": 0, "total_opening_balance": 0.0,
                                "total_closing_balance": 0.0, "total_net_movement": 0.0}
            s = summary[grp]
            s["count"] += 1
            o = led.get("opening_balance", 0)
            c = led.get("closing_balance", 0)
            s["total_opening_balance"] += o if led.get("opening_dr_cr") == "Dr" else -o
            s["total_closing_balance"] += c if led.get("closing_dr_cr") == "Dr" else -c
            s["total_net_movement"] += led.get("net_movement", 0)
        return summary

    def get_financial_summary(self) -> Dict:
        ledgers = self.get_all_ledgers()
        asset_groups = {"Bank Accounts", "Cash-in-Hand", "Fixed Assets", "Sundry Debtors",
                        "Deposits (Asset)", "Loans & Advances (Asset)", "Investments"}
        liability_groups = {"Sundry Creditors", "Secured Loans", "Unsecured Loans",
                            "Capital Account", "Reserves & Surplus", "Duties & Taxes",
                            "Current Liabilities", "Provisions"}

        summary = {"total_ledgers": len(ledgers), "total_assets": 0.0, "total_liabilities": 0.0,
                    "total_receivables": 0.0, "total_payables": 0.0, "total_bank_balance": 0.0,
                    "total_cash_balance": 0.0, "total_loans": 0.0, "total_fixed_assets": 0.0,
                    "extraction_method": self._method.value}

        for led in ledgers:
            grp = led.get("parent_group", "")
            closing = led.get("closing_balance", 0)
            dc = led.get("closing_dr_cr", "Dr")
            signed = closing if dc == "Dr" else -closing

            if grp in asset_groups: summary["total_assets"] += signed
            if grp in liability_groups: summary["total_liabilities"] += abs(signed)
            if grp == "Sundry Debtors": summary["total_receivables"] += closing
            if grp == "Sundry Creditors": summary["total_payables"] += closing
            if grp == "Bank Accounts": summary["total_bank_balance"] += closing
            if grp == "Cash-in-Hand": summary["total_cash_balance"] += closing
            if grp in ("Secured Loans", "Unsecured Loans"): summary["total_loans"] += closing
            if grp == "Fixed Assets": summary["total_fixed_assets"] += closing
        return summary

    def get_top_debtors(self, limit=10):
        return sorted(self.get_debtors(), key=lambda x: x.get("closing_balance", 0), reverse=True)[:limit]

    def get_top_creditors(self, limit=10):
        return sorted(self.get_creditors(), key=lambda x: x.get("closing_balance", 0), reverse=True)[:limit]

    # ========================================================================
    # UTILITY
    # ========================================================================

    def to_json(self, data) -> str:
        return json.dumps(data, indent=2, ensure_ascii=False, default=str)

    def get_extraction_method(self) -> str:
        return self._method.value

    def export_all(self) -> Dict:
        logger.info("Starting full export: %s", self.company_name)
        result = {
            "company_info": self.get_company_info(),
            "groups": self.get_all_groups(),
            "ledgers": self.get_all_ledgers(),
            "cost_centres": self.get_cost_centres(),
            "vouchers": self.get_vouchers(limit=10000),
            "trial_balance": self.get_trial_balance(),
            "financial_summary": self.get_financial_summary(),
            "extraction_timestamp": datetime.now().isoformat(),
            "extraction_method": self._method.value,
        }
        logger.info("Full export complete")
        return result


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

def example_usage():
    print("=" * 110)
    print("TALLY DATA EXTRACTOR - COMPREHENSIVE TEST")
    print("=" * 110)
    extractor = TallyDataExtractor(url="http://localhost:9000", company_name="Nimona")

    # Connection
    print("\n1. Testing connection...")
    conn = extractor.test_connection()
    print(f"  XML API: {'✓' if conn['xml_api']['connected'] else '✗'}")
    print(f"  ODBC:    {'✓' if conn['odbc']['connected'] else '✗'}")

    if not conn["xml_api"]["connected"] and not conn["odbc"]["connected"]:
        print("  ❌ Cannot connect. Exiting.")
        return

    # Company info
    print("\n2. Company info...")
    print(extractor.to_json(extractor.get_company_info()))

    # Ledgers
    print("\n3. Ledgers...")
    ledgers = extractor.get_all_ledgers()
    print(f"  Total: {len(ledgers)}")

    # Day Book format - matches Tally screenshot
    print("\n4. Day Book for 1-Apr-25 (matching Tally screenshot)...")
    print("-" * 110)
    print(f"{'Date':<12} {'Particulars':<45} {'Vch Type':<12} {'Vch No.':>8} {'Debit Amount':>16}")
    print("-" * 110)
    
    daybook = extractor.get_day_book("20250401")
    
    # Sort by type order: Payment, Receipt, Journal, Sales, Purchase (matching Tally)
    type_order = {"Payment": 0, "Receipt": 1, "Journal": 2, "Sales": 3, "Purchase": 4}
    daybook.sort(key=lambda v: (type_order.get(v.get("voucher_type", ""), 9), 
                                 int(v.get("voucher_number", "0") or "0")))
    
    for v in daybook:
        date_str = v.get("date", "").replace("-", "/") if v.get("date") else ""
        # Use Indian number format like Tally
        amt = v.get("amount", 0)
        if amt >= 10000000:  # 1 crore+
            amt_str = f"{amt/10000000:,.2f} Cr"
        else:
            amt_str = f"{amt:>14,.2f}"
        
        print(f"{date_str:<12} {v.get('particulars', v.get('party_name', '')):<45} "
              f"{v.get('voucher_type', ''):<12} {v.get('voucher_number', ''):>8} {amt_str:>16}")
    
    print("-" * 110)
    print(f"Total vouchers on 1-Apr-25: {len(daybook)}")
    
    # Summary by type
    from collections import defaultdict
    by_type = defaultdict(lambda: {"count": 0, "total": 0})
    for v in daybook:
        t = v.get("voucher_type", "Other")
        by_type[t]["count"] += 1
        by_type[t]["total"] += v.get("amount", 0)
    
    print("\nSummary:")
    for t in ["Payment", "Receipt", "Journal", "Sales", "Purchase"]:
        if t in by_type:
            print(f"  {t:<12}: {by_type[t]['count']:>3} vouchers = ₹{by_type[t]['total']:>14,.2f}")

    # Expected values from screenshot
    print("\n5. Verification against Tally Day Book screenshot...")
    expected = {
        "Payment": {"count": 12, "total": 30750000},
        "Receipt": {"count": 8, "total": 48600000},
        "Journal": {"count": 3, "total": 10500000},
    }
    
    all_ok = True
    for vtype, exp in expected.items():
        actual = by_type.get(vtype, {"count": 0, "total": 0})
        count_ok = actual["count"] == exp["count"]
        total_ok = abs(actual["total"] - exp["total"]) < 1
        status = "✅" if (count_ok and total_ok) else "❌"
        print(f"  {status} {vtype}: count={actual['count']}/{exp['count']} "
              f"total=₹{actual['total']:,.0f}/₹{exp['total']:,.0f}")
        if not (count_ok and total_ok):
            all_ok = False
    
    if all_ok:
        print("\n  ✅ ALL AMOUNTS MATCH TALLY DAY BOOK!")
    else:
        print("\n  ⚠️  Some mismatches - check voucher details")

    # All vouchers
    print("\n6. All vouchers (full FY)...")
    all_vch = extractor.get_vouchers(limit=200)
    print(f"  Total: {len(all_vch)}")

    # Sales detail
    print("\n7. Sales vouchers...")
    sales = extractor.get_sales_vouchers()
    print(f"  Count: {len(sales)}, Total: ₹{sum(v['amount'] for v in sales):,.2f}")
    for v in sales[:5]:
        print(f"  Vch#{v['voucher_number']:>4} | {v['date']} | ₹{v['amount']:>14,.2f} | {v['party_name']}")

    # Payment detail  
    print("\n8. Payment vouchers...")
    payments = extractor.get_payment_vouchers()
    print(f"  Count: {len(payments)}, Total: ₹{sum(v['amount'] for v in payments):,.2f}")
    for v in payments[:5]:
        print(f"  Vch#{v['voucher_number']:>4} | {v['date']} | ₹{v['amount']:>14,.2f} | {v['particulars']}")

    # Voucher with entries
    print("\n9. Detailed voucher (first Sales with entries)...")
    detailed = extractor.get_vouchers_with_entries(voucher_type="Sales", limit=1)
    if detailed:
        v = detailed[0]
        print(f"  Vch#{v['voucher_number']} | {v['voucher_type']} | {v['date']} | ₹{v['amount']:,.2f}")
        print(f"  Party: {v['party_name']}")
        print(f"  Narration: {v['narration']}")
        print(f"  Ledger entries:")
        for e in v.get("ledger_entries", []):
            side = "Dr" if e.get("is_debit") else "Cr"
            print(f"    {side}: {e.get('ledger_name', ''):<40} ₹{e.get('amount', 0):>14,.2f}")

    print(f"\n10. Extraction method: {extractor.get_extraction_method()}")
    print("=" * 110)
    print("DONE!")



if __name__ == "__main__":
    example_usage()
