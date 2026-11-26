"""Fictional reference data for the sample code. Nothing here is real customer or policy data."""

RMS = {
    "layla": {"name": "Layla H.", "team": "SME North", "portfolio": {"harbourline", "crescent"}, "clearance": "internal"},
    "omar": {"name": "Omar K.", "team": "SME South", "portfolio": {"gulfstar"}, "clearance": "internal"},
}

CUSTOMERS = {
    "harbourline": {
        "name": "Harbourline Logistics LLC", "crm_id": "4471", "sector": "Freight and logistics",
        "since": 2019, "turnover_aed": 48_000_000, "revenue_yoy_pct": 12, "contact": "Mr Haddad",
        "emirates_id": "784-1985-1234567-1",  # sensitive: must never reach the model
        "facilities": [
            {"id": "F-209", "type": "Working-capital line", "limit": 5_000_000, "used": 4_600_000, "days_above_90": 45},
            {"id": "F-214", "type": "Vehicle finance", "limit": 2_000_000, "used": 1_200_000, "days_above_90": 0},
        ],
        "kyc": {"status": "Valid", "risk": "Medium", "docs": [{"name": "Trade licence", "expires": "2026-12-14"}]},
        "signals": [{"text": "Outbound FX payments up 38% this quarter", "nba": "Discuss FX forwards"}],
    },
    "crescent": {
        "name": "Crescent Foods Trading LLC", "crm_id": "5120", "sector": "Food import",
        "since": 2021, "turnover_aed": 22_000_000, "revenue_yoy_pct": 6, "contact": "Ms Rao",
        "emirates_id": "784-1979-7654321-2",
        "facilities": [{"id": "TF-102", "type": "Import LC line", "limit": 1_500_000, "used": 1_310_000, "days_above_90": 0}],
        "kyc": {"status": "Valid", "risk": "Low", "docs": [{"name": "Trade licence", "expires": "2027-05-20"}]},
        "signals": [{"text": "Salary transfers to another bank each month", "nba": "Offer payroll services"}],
    },
    "gulfstar": {
        "name": "Gulfstar Cement Products LLC", "crm_id": "3308", "sector": "Construction materials",
        "since": 2016, "turnover_aed": 120_000_000, "revenue_yoy_pct": -4, "contact": "Mr Saleh",
        "emirates_id": "784-1970-1111111-3",
        "facilities": [{"id": "F-120", "type": "Working-capital line", "limit": 15_000_000, "used": 11_000_000, "days_above_90": 0}],
        "kyc": {"status": "Valid", "risk": "Medium", "docs": [{"name": "Trade licence", "expires": "2027-08-01"}]},
        "signals": [],
    },
}

# status: current | superseded ; classification: internal | restricted
POLICY_PASSAGES = [
    {"id": "credit-5.3.1", "doc": "SME Credit Policy v4.2", "ref": "§5.3.1", "status": "current", "classification": "internal",
     "text": "Unsecured SME working capital lines require a minimum of 3 years audited trading history and annual turnover of at least AED 5m. Maximum unsecured exposure per obligor is AED 2m."},
    {"id": "credit-5.3.1-v4.1", "doc": "SME Credit Policy v4.1", "ref": "§5.3.1", "status": "superseded", "classification": "internal",
     "text": "Unsecured SME working capital lines require a minimum of 2 years audited trading history."},
    {"id": "credit-5.3.4", "doc": "SME Credit Policy v4.2", "ref": "§5.3.4", "status": "current", "classification": "internal",
     "text": "Businesses with less than 3 years trading history may be offered secured facilities only, including receivables-backed lines, cash-collateralised lines and property-secured facilities."},
    {"id": "doa-14", "doc": "Delegation of Authority matrix", "ref": "row 14", "status": "current", "classification": "internal",
     "text": "Policy exceptions for unsecured SME lending up to AED 1m require approval at Credit Risk level 2. Exceptions above AED 1m require Credit Committee approval."},
    {"id": "covenant-6.1", "doc": "Covenant Monitoring Procedure", "ref": "§6.1", "status": "current", "classification": "internal",
     "text": "Utilisation of a working capital line above 90% for 30 or more consecutive days triggers an RM review, to be completed within 10 working days."},
    {"id": "kyc-3.2", "doc": "KYC Procedure", "ref": "§3.2", "status": "current", "classification": "internal",
     "text": "Periodic KYC review is annual for high-risk customers, every 2 years for medium-risk and every 3 years for low-risk. An expired trade licence blocks new drawdowns."},
    {"id": "acct-2.1", "doc": "SME Account Opening Procedure", "ref": "§2.1", "status": "current", "classification": "internal",
     "text": "Required documents to open an SME account: valid trade licence, memorandum of association, passport and ID copies of all shareholders and signatories, UBO declaration, and proof of business address."},
    {"id": "vehicle-1", "doc": "Vehicle Finance product sheet", "ref": "p.1", "status": "current", "classification": "internal",
     "text": "Commercial vehicle finance covers up to 80% of the vehicle value, for tenors up to 5 years, for businesses with at least 1 year of trading."},
    {"id": "cc-overrides", "doc": "Credit Committee pricing overrides", "ref": "Q3 minutes", "status": "current", "classification": "restricted",
     "text": "Restricted: named customer working capital pricing overrides approved by Credit Committee."},
]
