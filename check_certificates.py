#!/usr/bin/env python3
"""
Dagligt certifikat-tjek for MSC, ASC og Øko-certifikater.
Kør manuelt eller sæt op som daglig opgave via Windows Task Scheduler.
"""

import json
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote as _url_quote

import requests

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

# Øko – EU TRACES NT public API (ingen autentificering kræves)
ECO_API_URL = "https://webgate.ec.europa.eu/tracesnt/directory/publication/organic-operator/for/query"

# MSC – Azure API Management
# Sæt API-nøglen enten direkte her eller som miljøvariabel: set MSC_API_KEY=din-nøgle
MSC_API_URL = "https://api-msc-api-prod.azure-api.net/api/chaincustody"
MSC_API_KEY = os.getenv("MSC_API_KEY", "")  # ← Indsæt nøgle her eller sæt MSC_API_KEY som env-variabel

# ASC – officielt REST API
ASC_API_BASE = "https://data.asc-aqua.org/api/status/v1"
ASC_API_KEY  = os.getenv("ASC_API_KEY", "")

# Antal dage før udløb der markeres som "udløber snart"
EXPIRY_WARNING_DAYS = 30

# ---------------------------------------------------------------------------
# Stier
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
SUPPLIERS_FILE = BASE_DIR / "suppliers.json"
RESULTS_FILE = BASE_DIR / "results" / "latest.json"

# ---------------------------------------------------------------------------
# Hjælpefunktioner
# ---------------------------------------------------------------------------

def load_suppliers() -> dict:
    with open(SUPPLIERS_FILE, encoding="utf-8") as f:
        return json.load(f)


def days_until_expiry(date_str: str | None) -> int | None:
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            expiry = datetime.strptime(date_str[:10], fmt[:8]).date()
            return (expiry - datetime.now().date()).days
        except ValueError:
            continue
    return None


def expiry_status(days: int | None) -> str:
    if days is None:
        return "ukendt"
    if days < 0:
        return "udløbet"
    if days <= EXPIRY_WARNING_DAYS:
        return "udløber_snart"
    return "gyldig"


def make_result(name: str, cert_type: str, certificate_id: str,
                valid_until: str | None = None,
                status: str = "ukendt",
                error: str | None = None) -> dict:
    days = days_until_expiry(valid_until)
    return {
        "name": name,
        "type": cert_type,
        "certificate_id": certificate_id,
        "status": status if not error else "ukendt",
        "valid_until": valid_until,
        "days_until_expiry": days,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "error": error,
    }

# ---------------------------------------------------------------------------
# MSC – Azure API (primær) + cert.msc.org scraping (fallback)
# ---------------------------------------------------------------------------

# cert.msc.org supplier directory – bruges når MSC_API_KEY ikke er sat
_MSC_DIR_URL     = "https://cert.msc.org/supplierdirectory/VController.aspx"
_MSC_LIST_PATH   = "02d03d11-054d-44f5-9076-b1bd00a2ebdf"   # public søgeside
_MSC_DETAIL_PATH = "dfb82023-cc58-4550-9918-b1bd00a2f95c"   # certifikatdetailside
_MSC_HEADERS     = {"User-Agent": "Mozilla/5.0 (compatible; certifikat-tjek/1.0)"}


def _msc_parse_expire(date_str: str | None) -> str | None:
    """Konverterer 'DD Month YYYY' → 'YYYY-MM-DD'."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.strip(), "%d %B %Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _check_msc_cert_dir(checks: list[tuple]) -> list[dict]:
    """
    Fallback: slår certifikater op via cert.msc.org offentlige directory.

    Trin 1: Henter alle ~22.000 certifikatposter fra iggrid JSON-endpoint
            (ét POST-kald ~19 MB) og bygger et opslag {Nr1 → PK_Person}.
    Trin 2: Henter certifikatdetailsiden per certifikat for udløbsdato.
    """
    session = requests.Session()
    iggrid_url = (
        f"{_MSC_DIR_URL}?Path={_MSC_LIST_PATH}&xf=1&iggrid=grdSupplier"
    )

    # Etablér session
    try:
        session.get(
            f"{_MSC_DIR_URL}?Path={_MSC_LIST_PATH}&xf=1",
            headers=_MSC_HEADERS, timeout=30,
        )
    except requests.RequestException as e:
        return [make_result(n, "msc", c, error=f"Kunne ikke åbne cert.msc.org: {e}")
                for n, c in checks]

    # Hent alle poster i ét kald
    try:
        grid_resp = session.post(
            iggrid_url,
            json={"Page": 0, "PageSize": 25000},
            headers={**_MSC_HEADERS, "Content-Type": "application/json",
                     "X-Requested-With": "XMLHttpRequest"},
            timeout=120,
        )
        grid_resp.raise_for_status()
        records = grid_resp.json().get("Records", [])
    except requests.RequestException as e:
        return [make_result(n, "msc", c, error=f"Fejl ved hentning af MSC-certifikatliste: {e}")
                for n, c in checks]

    # Byg opslag: certifikatnummer → (PK_Person, firmanavn fra grid)
    cert_info: dict[str, tuple[str, str]] = {
        r["Nr1"]: (r["PK_Person"], r.get("Name", ""))
        for r in records
        if r.get("Nr1") and r.get("PK_Person")
    }

    results = []
    for name, cert_id in checks:
        info = cert_info.get(cert_id)
        if info is None:
            results.append(make_result(name, "msc", cert_id,
                                       error="Certifikat ikke fundet i MSC-databasen"))
            continue

        pk, grid_name = info
        try:
            detail_resp = session.get(
                f"{_MSC_DIR_URL}?Path={_MSC_DETAIL_PATH}&pk={pk}&PCIdx=0&dtstrg=0",
                headers=_MSC_HEADERS,
                timeout=30,
            )
            detail_resp.raise_for_status()
            html = detail_resp.text

            # Annullerede/udløbne certifikater viser en separat besked
            if "txtSuspendedCertInfoText" in html:
                result = make_result(name, "msc", cert_id, status="udløbet")
                results.append(result)
                continue

            expire_m = re.search(r'txtExpire">([^<]+)', html)

            valid_until = _msc_parse_expire(expire_m.group(1) if expire_m else None)
            status = expiry_status(days_until_expiry(valid_until))

            result = make_result(name, "msc", cert_id,
                                 valid_until=valid_until, status=status)
            # Vis firmanavn fra grid, når det afviger fra vores leverandørnavn
            if grid_name and grid_name.strip() != name:
                result["msc_cert_holder"] = grid_name.strip()
            results.append(result)

        except requests.RequestException as e:
            results.append(make_result(name, "msc", cert_id, error=f"HTTP-fejl: {e}"))

    return results


def check_msc_all(msc_suppliers: list[dict]) -> list[dict]:
    checks = [
        (s["name"], cert_id)
        for s in msc_suppliers
        for cert_id in s["certificate_ids"]
    ]

    if not checks:
        return []

    # ── Primær sti: Azure API (kræver API-nøgle) ──────────────────────────────
    if MSC_API_KEY:
        payload = [{"certificateCode": cert_id, "speciesCode": ""} for _, cert_id in checks]
        try:
            response = requests.post(
                MSC_API_URL,
                json=payload,
                headers={
                    "Ocp-Apim-Subscription-Key": MSC_API_KEY,
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
            response.raise_for_status()
            api_data = response.json()

            # TODO: Bekræft feltnavne fra det faktiske API-svar
            by_code: dict[str, dict] = {}
            for item in (api_data if isinstance(api_data, list) else [api_data]):
                code = item.get("certificateCode") or item.get("certificate_code") or ""
                if code:
                    by_code[code] = item

            results = []
            for name, cert_id in checks:
                item = by_code.get(cert_id)
                if item is None:
                    results.append(make_result(name, "msc", cert_id,
                                               error="Certifikat ikke fundet i API-svar"))
                    continue
                valid_until = (
                    item.get("expiryDate") or item.get("expiry_date")
                    or item.get("validTo") or item.get("valid_to")
                    or item.get("endDate")
                )
                is_valid = item.get("isValid") if "isValid" in item else None
                if is_valid is False:
                    status = "udløbet"
                else:
                    status = expiry_status(days_until_expiry(valid_until))
                results.append(make_result(name, "msc", cert_id,
                                           valid_until=valid_until, status=status))
            return results

        except requests.HTTPError as e:
            error_msg = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            return [make_result(n, "msc", c, error=error_msg) for n, c in checks]
        except requests.RequestException as e:
            return [make_result(n, "msc", c, error=f"Forbindelsesfejl: {e}") for n, c in checks]

    # ── Fallback: cert.msc.org scraping ──────────────────────────────────────
    return _check_msc_cert_dir(checks)

# ---------------------------------------------------------------------------
# ASC – officielt REST API (data.asc-aqua.org)
# GET /api/status/v1/<API-KEY>/certcode/<CERT-CODE>
# Felter: Certificate_status, Expiry_date, Certificate_holder
# ---------------------------------------------------------------------------

_ASC_INVALID_STATUSES = {"Cancelled", "Suspended", "Withdrawn", "Certification not awarded"}


def check_asc_all(asc_suppliers: list[dict]) -> list[dict]:
    checks = [
        (s["name"], cert_id)
        for s in asc_suppliers
        for cert_id in s["certificate_ids"]
    ]

    if not checks:
        return []

    if not ASC_API_KEY:
        return [make_result(n, "asc", c, error="ASC_API_KEY ikke sat")
                for n, c in checks]

    session = requests.Session()
    headers = {"User-Agent": "Mozilla/5.0 (compatible; certifikat-tjek/1.0)"}
    results = []

    for name, cert_id in checks:
        try:
            resp = session.get(
                f"{ASC_API_BASE}/{ASC_API_KEY}/certcode/{cert_id}",
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            docs = resp.json().get("documents", [])

            if not docs:
                results.append(make_result(name, "asc", cert_id,
                                           error="Certifikat ikke fundet i ASC API"))
                continue

            doc         = docs[0]
            cert_status = doc.get("Certificate_status", "")
            expiry_date = doc.get("Expiry_date")
            cert_holder = doc.get("Certificate_holder", name)

            if cert_status in _ASC_INVALID_STATUSES:
                # Udløbsdato kan være i fremtiden selvom certifikatet er trukket tilbage
                status = "udløbet"
                result = make_result(name, "asc", cert_id, valid_until=None, status=status)
            else:
                status = expiry_status(days_until_expiry(expiry_date))
                result = make_result(name, "asc", cert_id, valid_until=expiry_date, status=status)
            if cert_holder and cert_holder != name:
                result["asc_cert_holder"] = cert_holder
            result["asc_cert_status"] = cert_status
            results.append(result)

        except requests.RequestException as e:
            results.append(make_result(name, "asc", cert_id, error=f"ASC API fejl: {e}"))

    return results

# ---------------------------------------------------------------------------
# Øko – EU TRACES NT API (offentlig, ingen autentificering)
# GET https://webgate.ec.europa.eu/tracesnt/directory/publication/organic-operator/for/query
# Parametre: query (fritekst), max (antal resultater)
# Returnerer JSON-array med certifikat-records.
# ---------------------------------------------------------------------------

# TRACES-statusser der betragtes som ugyldige
_ECO_INVALID_STATUSES = {"EXPIRED", "WITHDRAWN", "REISSUED"}

def _eco_traces_status(traces_status_id: str, expires_on: str | None) -> str:
    """Oversætter TRACES-status og udløbsdato til vores interne status."""
    if traces_status_id in _ECO_INVALID_STATUSES:
        return "udløbet"
    if traces_status_id == "ISSUED":
        return expiry_status(days_until_expiry(expires_on))
    return "ukendt"


def check_eco_all(eco_suppliers: list[dict]) -> list[dict]:
    results = []
    headers = {
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0 (compatible; certifikat-tjek/1.0)",
    }

    for s in eco_suppliers:
        name    = s["name"]
        cert_id = s["certificate_id"]          # f.eks. "IE-ORG-03"
        query   = s.get("search_query", name)  # overstyring hvis operatørnavn afviger

        try:
            response = requests.get(
                ECO_API_URL,
                params={"query": query, "max": 50},
                headers=headers,
                timeout=15,
            )
            response.raise_for_status()
            records: list[dict] = response.json()

            # Find records der matcher vores certifikat-udsteder (f.eks. IE-ORG-03)
            matching = [
                r for r in records
                if r.get("issuingBody", {}).get("code") == cert_id
            ]

            if not matching:
                results.append(make_result(
                    name, "eco", cert_id,
                    error=f"Ingen aktiv post fundet i TRACES for '{query}' med udsteder {cert_id}"
                ))
                continue

            # Foretrækker ISSUED; falder tilbage på første match
            issued = [r for r in matching if r.get("status", {}).get("id") == "ISSUED"]
            record = issued[0] if issued else matching[0]

            expires_on    = record.get("expiresOn")                    # "YYYY-MM-DD"
            traces_status = record.get("status", {}).get("id", "")
            operator_name = record.get("operator", {}).get("name", name)
            reference     = record.get("reference", "")

            status = _eco_traces_status(traces_status, expires_on)
            result = make_result(name, "eco", cert_id, valid_until=expires_on, status=status)
            result["traces_reference"] = reference
            result["traces_operator"]  = operator_name
            results.append(result)

        except requests.RequestException as e:
            results.append(make_result(name, "eco", cert_id, error=f"HTTP-fejl mod TRACES: {e}"))

    return results

# ---------------------------------------------------------------------------
# E-mail notifikationer via Microsoft 365 SMTP
# ---------------------------------------------------------------------------

NOTIFICATION_THRESHOLD_DAYS = 14
_SMTP_SERVER       = "smtp.office365.com"
_SMTP_PORT         = 587
_NOTIFICATION_FROM = "mt@foodwithyou.com"
_NOTIFICATION_TO   = ["quality@foodwithyou.com", "info@foodwithyou.com"]
_TYPE_LABELS       = {"msc": "MSC", "asc": "ASC", "eco": "Øko"}
_DASHBOARD_URL     = "https://mct-fwy.github.io/certifikat-tjek/"


def _cert_url(result: dict) -> str:
    """Bygger certifikatlink – spejl af certUrl() i dashboard/index.html."""
    cert_type = result.get("type", "")
    cert_id   = result.get("certificate_id", "")
    if cert_type == "msc":
        return f"https://fisheries.msc.org/en/fisheries/search-a-fishery/?q={_url_quote(cert_id)}"
    if cert_type == "asc":
        return f"https://asc-aqua.org/find-a-supplier/{_url_quote(cert_id)}/"
    ref = result.get("traces_reference") or cert_id
    return (
        "https://webgate.ec.europa.eu/tracesnt/directory/publication/"
        f"organic-operator/index#!?query={_url_quote(ref)}&sort=-issuedOn&states=ISSUED"
    )


def _build_email_html(result: dict, is_expired: bool) -> str:
    name        = result.get("name", "")
    cert_id     = result.get("certificate_id", "")
    cert_type   = result.get("type", "")
    valid_until = result.get("valid_until") or "Ukendt"
    days        = result.get("days_until_expiry")
    type_label  = _TYPE_LABELS.get(cert_type, cert_type.upper())
    url         = _cert_url(result)

    if is_expired:
        header_color = "#dc2626"
        heading      = "&#128308;&nbsp; Certifikat udl&oslash;bet"
        days_row     = ""
    else:
        header_color = "#d97706"
        heading      = "&#9888;&#65039;&nbsp; Certifikat udl&oslash;ber snart"
        days_row     = f"""
        <tr>
          <td style="padding:8px 0;color:#64748b;border-top:1px solid #f1f5f9">Dage tilbage</td>
          <td style="border-top:1px solid #f1f5f9;color:#d97706;font-weight:700">{days} dage</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="da">
<head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f0f2f5;padding:40px 0;margin:0">
  <div style="max-width:540px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,.08)">
    <div style="background:{header_color};padding:22px 32px">
      <p style="color:#fff;font-size:1.05rem;font-weight:700;margin:0">{heading}</p>
    </div>
    <div style="padding:28px 32px">
      <table style="width:100%;border-collapse:collapse;font-size:0.9rem">
        <tr>
          <td style="padding:8px 0;color:#64748b;width:140px">Leverand&oslash;r</td>
          <td style="font-weight:600">{name}</td>
        </tr>
        <tr>
          <td style="padding:8px 0;color:#64748b;border-top:1px solid #f1f5f9">Certifikat-ID</td>
          <td style="font-family:monospace;border-top:1px solid #f1f5f9">{cert_id}</td>
        </tr>
        <tr>
          <td style="padding:8px 0;color:#64748b;border-top:1px solid #f1f5f9">Type</td>
          <td style="border-top:1px solid #f1f5f9">{type_label}</td>
        </tr>
        <tr>
          <td style="padding:8px 0;color:#64748b;border-top:1px solid #f1f5f9">Udl&oslash;bsdato</td>
          <td style="border-top:1px solid #f1f5f9">{valid_until}</td>
        </tr>
        {days_row}
      </table>
      <a href="{url}" style="display:inline-block;margin-top:22px;padding:11px 22px;background:#1a1a2e;color:#fff;border-radius:7px;text-decoration:none;font-weight:700;font-size:0.9rem">
        &Aring;bn certifikat &rarr;
      </a>
      <a href="{_DASHBOARD_URL}" style="display:inline-block;margin-top:10px;margin-left:12px;font-size:0.82rem;color:#64748b;text-decoration:none">
        &Aring;bn dashboard
      </a>
    </div>
    <div style="padding:14px 32px;background:#f8fafc;border-top:1px solid #e2e8f0">
      <p style="font-size:0.74rem;color:#94a3b8;margin:0">
        Food with You Certifikat Dashboard &middot; Automatisk dagligt tjek
      </p>
    </div>
  </div>
</body>
</html>"""


def _smtp_connect(password: str) -> smtplib.SMTP:
    smtp = smtplib.SMTP(_SMTP_SERVER, _SMTP_PORT, timeout=30)
    smtp.ehlo()
    smtp.starttls()
    smtp.login(_NOTIFICATION_FROM, password)
    return smtp


def _make_mime(to_addr: str, subject: str, html: str) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = _NOTIFICATION_FROM
    msg["To"]      = to_addr
    msg.attach(MIMEText(html, "html", "utf-8"))
    return msg


def send_notifications(results: list[dict]) -> None:
    """Sender e-mail via Microsoft 365 SMTP for certifikater der udløber inden for
    NOTIFICATION_THRESHOLD_DAYS dage eller allerede er udløbet."""
    password = os.getenv("EMAIL_PASSWORD", "")
    if not password:
        print("\nSMTP: EMAIL_PASSWORD ikke sat – springer notifikationer over.")
        return

    to_send = []
    for result in results:
        if result.get("error"):
            continue
        status = result.get("status", "")
        days   = result.get("days_until_expiry")
        is_expired = (status == "udløbet") or (days is not None and days < 0)
        is_warning = (
            not is_expired
            and days is not None
            and 0 <= days <= NOTIFICATION_THRESHOLD_DAYS
        )
        if is_expired or is_warning:
            to_send.append((result, is_expired))

    if not to_send:
        print("\nSMTP: ingen notifikationer nødvendige i dag.")
        return

    try:
        with _smtp_connect(password) as smtp:
            sent = 0
            for result, is_expired in to_send:
                name  = result.get("name", "")
                days  = result.get("days_until_expiry")
                subject = (
                    f"\U0001f534 Certifikat udløbet: {name}"
                    if is_expired else
                    f"⚠️ Certifikat udløber snart: {name} ({days} dage)"
                )
                html = _build_email_html(result, is_expired)
                for to_addr in _NOTIFICATION_TO:
                    smtp.send_message(_make_mime(to_addr, subject, html))
                    sent += 1
                    print(f"  Mail sendt til {to_addr}: {subject}")
        print(f"\nSMTP: {sent} e-mail(s) sendt.")
    except smtplib.SMTPException as exc:
        print(f"\nSMTP-fejl: {exc}")


def send_test_email() -> None:
    """Sender en test-e-mail for at verificere SMTP-konfigurationen."""
    password = os.getenv("EMAIL_PASSWORD", "")
    if not password:
        print("EMAIL_PASSWORD ikke sat – kan ikke sende test-mail.")
        sys.exit(1)

    fake_result = {
        "name": "TEST – Leverandør A/S",
        "type": "msc",
        "certificate_id": "MSC-C-TEST-001",
        "status": "udloeber_snart",
        "valid_until": "2026-05-15",
        "days_until_expiry": 12,
        "error": None,
    }
    subject = "✅ Test: Food with You certifikat-notifikation"
    html    = _build_email_html(fake_result, is_expired=False)

    try:
        with _smtp_connect(password) as smtp:
            for to_addr in _NOTIFICATION_TO:
                smtp.send_message(_make_mime(to_addr, subject, html))
                print(f"  Test-mail sendt til {to_addr}")
        print("Test-email afsendt.")
    except smtplib.SMTPException as exc:
        print(f"SMTP-fejl: {exc}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Hovedfunktion
# ---------------------------------------------------------------------------

def run_checks() -> dict:
    suppliers = load_suppliers()

    eco_list = suppliers.get("eco", [])
    msc_list = suppliers.get("msc", [])
    asc_list = suppliers.get("asc", [])

    n_msc = sum(len(s["certificate_ids"]) for s in msc_list)
    n_asc = sum(len(s["certificate_ids"]) for s in asc_list)

    print(f"\nØko ({len(eco_list)} leverandører)")
    eco_results = check_eco_all(eco_list)
    for r in eco_results:
        _print_result(r)

    print(f"\nMSC ({len(msc_list)} leverandører, {n_msc} certifikater)")
    msc_results = check_msc_all(msc_list)
    for r in msc_results:
        _print_result(r)

    print(f"\nASC ({len(asc_list)} leverandører, {n_asc} certifikater)")
    asc_results = check_asc_all(asc_list)
    for r in asc_results:
        _print_result(r)

    all_results = eco_results + msc_results + asc_results

    output = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": {
            "total":          len(all_results),
            "gyldig":         sum(1 for r in all_results if r["status"] == "gyldig"),
            "udloeber_snart": sum(1 for r in all_results if r["status"] == "udløber_snart"),
            "udloebet":       sum(1 for r in all_results if r["status"] == "udløbet"),
            "ukendt":         sum(1 for r in all_results if r["status"] == "ukendt"),
        },
        "results": all_results,
    }

    RESULTS_FILE.parent.mkdir(exist_ok=True)
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nResultater gemt: {RESULTS_FILE}")
    s = output["summary"]
    print(f"  Gyldige: {s['gyldig']}  |  Udløber snart: {s['udloeber_snart']}  "
          f"|  Udløbet: {s['udloebet']}  |  Ukendt: {s['ukendt']}")

    send_notifications(all_results)
    return output


def _print_result(r: dict) -> None:
    flag = "OK" if r["status"] == "gyldig" else ("!!" if r["status"] in ("udløbet", "udløber_snart") else "??")
    err = f"  ({r['error']})" if r["error"] else ""
    print(f"  {flag} {r['name']:35s} [{r['certificate_id']}]  =>  {r['status']}{err}")


if __name__ == "__main__":
    if "--test-email" in sys.argv:
        send_test_email()
    else:
        print(f"Certifikat-tjek startet: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        run_checks()
