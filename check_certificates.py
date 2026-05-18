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

# Validering – tilladte certifikatformater
_FORMAT_PATTERNS = {
    "msc": re.compile(r"^MSC-C-\d+$"),
    "asc": re.compile(r"^ASC-C-\d+$"),
    "eco": re.compile(r"^[A-Z]{2}-[A-ZÄÖÜ]{2,6}-\d+$"),
}

# ---------------------------------------------------------------------------
# Validering af suppliers.json
# ---------------------------------------------------------------------------

def validate_suppliers(suppliers: dict) -> list[dict]:
    """Returnerer liste af advarsler om dubletter og forkerte certifikatformater."""
    warnings: list[dict] = []
    seen: dict[str, list[str]] = {}   # cert_id → [leverandørnavn, ...]

    for cert_type in ("msc", "asc"):
        pattern = _FORMAT_PATTERNS[cert_type]
        for s in suppliers.get(cert_type, []):
            for cert_id in s.get("certificate_ids", []):
                seen.setdefault(cert_id, []).append(s["name"])
                if not pattern.match(cert_id):
                    warnings.append({
                        "warning_type": "format",
                        "cert_type": cert_type,
                        "certificate_id": cert_id,
                        "names": [s["name"]],
                        "message": (
                            f"Ugyldigt format '{cert_id}' hos {s['name']} – "
                            f"forventet {cert_type.upper()}-C-XXXXX (kun tal efter bindestreg)"
                        ),
                    })

    pattern = _FORMAT_PATTERNS["eco"]
    for s in suppliers.get("eco", []):
        cert_id = s.get("certificate_id", "")
        seen.setdefault(cert_id, []).append(s["name"])
        if not pattern.match(cert_id):
            warnings.append({
                "warning_type": "format",
                "cert_type": "eco",
                "certificate_id": cert_id,
                "names": [s["name"]],
                "message": (
                    f"Ugyldigt format '{cert_id}' hos {s['name']} – "
                    f"forventet f.eks. IE-ORG-03 eller DE-ÖKO-039"
                ),
            })

    for cert_id, names in seen.items():
        if len(names) > 1:
            warnings.append({
                "warning_type": "duplicate",
                "cert_type": None,
                "certificate_id": cert_id,
                "names": names,
                "message": (
                    f"Duplikat certifikatnummer '{cert_id}' optræder hos: "
                    + " og ".join(f"'{n}'" for n in names)
                ),
            })

    return warnings


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
            if grid_name and grid_name.strip() != name:
                result["msc_cert_holder"] = grid_name.strip()
            results.append(result)

        except requests.RequestException as e:
            results.append(make_result(name, "msc", cert_id, error=f"HTTP-fejl: {e}"))

    return results


def check_msc_all(msc_suppliers: list[dict]) -> list[dict]:
    # (name, cert_id, species_codes)
    checks = [
        (s["name"], cert_id, s.get("species_codes", []))
        for s in msc_suppliers
        for cert_id in s["certificate_ids"]
    ]

    if not checks:
        return []

    if not MSC_API_KEY:
        return _check_msc_cert_dir([(n, c) for n, c, _ in checks])

    api_checks      = [(n, c, sc) for n, c, sc in checks if sc]
    scraping_checks = [(n, c)     for n, c, sc in checks if not sc]

    # Certifikater API bekræfter gyldige – skal suppleres med scraping for udløbsdato
    api_valid:    list[tuple[str, str]] = []
    # Certifikater API markerer som ikke-fundet
    api_notfound: list[tuple[str, str]] = []

    if api_checks:
        payload = [
            {"certificateCode": cert_id, "speciesCode": species}
            for _, cert_id, species_codes in api_checks
            for species in species_codes
        ]
        try:
            resp = requests.post(
                MSC_API_URL,
                json=payload,
                headers={
                    "Ocp-Apim-Subscription-Key": MSC_API_KEY,
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
            resp.raise_for_status()
            items = resp.json() if isinstance(resp.json(), list) else [resp.json()]

            by_cert: dict[str, list[dict]] = {}
            for item in items:
                code = item.get("certificateCode", "")
                if code:
                    by_cert.setdefault(code, []).append(item)

            for name, cert_id, _ in api_checks:
                cert_items = by_cert.get(cert_id, [])
                valid_hit  = next((i for i in cert_items if i.get("result") == "Valid"), None)
                notfound   = next((i for i in cert_items if i.get("result") == "Not found"), None)

                if valid_hit:
                    api_valid.append((name, cert_id))
                elif notfound:
                    api_notfound.append((name, cert_id))
                else:
                    scraping_checks.append((name, cert_id))

        except (requests.HTTPError, requests.RequestException):
            scraping_checks.extend([(n, c) for n, c, _ in api_checks])

    # Scraping for: certifikater uden art-koder + "species not in scope" fejl
    scraping_results: dict[str, dict] = {}
    if scraping_checks or api_valid:
        # Kør scraping for alle der mangler udløbsdato (no-species + api_valid supplement)
        all_scraping = scraping_checks + api_valid
        for r in _check_msc_cert_dir(all_scraping):
            scraping_results[r["certificate_id"]] = r

    results: list[dict] = []

    # API-gyldige: brug scraped udløbsdato hvis tilgængelig, ellers behold API-status
    for name, cert_id in api_valid:
        scraped = scraping_results.get(cert_id)
        if scraped and not scraped.get("error"):
            results.append(scraped)
        else:
            results.append(make_result(name, "msc", cert_id, status="gyldig"))

    # API-ikke-fundet
    for name, cert_id in api_notfound:
        results.append(make_result(name, "msc", cert_id,
                                   error="Certifikat ikke fundet i MSC API"))

    # Rene scraping-resultater (ingen art-koder / API fejlede)
    for name, cert_id in scraping_checks:
        if cert_id in scraping_results:
            results.append(scraping_results[cert_id])

    return results

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
_SMTP_SERVER       = "smtp.gmail.com"
_SMTP_PORT         = 587
_NOTIFICATION_FROM = "fwypakkeri@gmail.com"
_NOTIFICATION_TO   = ["quality@foodwithyou.com", "info@foodwithyou.com"]
_TYPE_LABELS       = {"msc": "MSC", "asc": "ASC", "eco": "Øko"}
_DASHBOARD_URL     = "https://mct-fwy.github.io/certifikat-tjek/"


def _cert_url(result: dict) -> str:
    """Bygger certifikatlink – spejl af certUrl() i dashboard/index.html."""
    cert_type = result.get("type", "")
    cert_id   = result.get("certificate_id", "")
    if cert_type == "msc":
        return f"https://cert.msc.org/supplierdirectory/Default.aspx?certno={_url_quote(cert_id)}"
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
    smtp.ehlo()  # Re-identify after STARTTLS – required by Office365
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


def send_test_email(scenario: str = "warning") -> None:
    """Sender en test-e-mail for at verificere SMTP-konfigurationen.

    scenario: 'warning' (udløber snart) eller 'expired' (udløbet).
    """
    password = os.getenv("EMAIL_PASSWORD", "")
    if not password:
        print("EMAIL_PASSWORD ikke sat – kan ikke sende test-mail.")
        sys.exit(1)

    is_expired = scenario == "expired"
    fake_result = {
        "name": "TEST – Leverandør A/S",
        "type": "msc",
        "certificate_id": "MSC-C-TEST-001",
        "status": "udløbet" if is_expired else "udløber_snart",
        "valid_until": "2026-04-01" if is_expired else "2026-05-27",
        "days_until_expiry": -42 if is_expired else 14,
        "error": None,
    }
    emoji   = "🔴" if is_expired else "⚠️"
    subject = f"✅ Test ({emoji}): Food with You certifikat-notifikation"
    html    = _build_email_html(fake_result, is_expired=is_expired)

    print(f"Sender test-email (scenarie: {scenario}) via {_SMTP_SERVER}:{_SMTP_PORT} ...")
    try:
        with _smtp_connect(password) as smtp:
            for to_addr in _NOTIFICATION_TO:
                smtp.send_message(_make_mime(to_addr, subject, html))
                print(f"  Mail sendt til {to_addr}")
        print("Test-email afsendt.")
    except smtplib.SMTPException as exc:
        print(f"SMTP-fejl: {exc}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Hovedfunktion
# ---------------------------------------------------------------------------

_HOLDER_FIELDS = ("msc_cert_holder", "asc_cert_holder", "traces_operator", "traces_reference")


def _load_previous_results() -> dict[tuple, dict]:
    """Indlæser tidligere resultater fra results/latest.json som opslag (type, cert_id) → post."""
    if not RESULTS_FILE.exists():
        return {}
    try:
        with open(RESULTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {
            (r["type"], r["certificate_id"]): r
            for r in data.get("results", [])
            if r.get("valid_until")  # gem kun poster med kendte udløbsdatoer
        }
    except (json.JSONDecodeError, KeyError, OSError):
        return {}


def _apply_previous_fallback(results: list[dict], prev: dict[tuple, dict]) -> None:
    """
    Udfylder manglende valid_until/holder-felter fra forrige kørsel for poster
    der ikke har fejl og ikke fik en udløbsdato fra det aktuelle tjek.
    Opdaterer listen in-place.
    """
    if not prev:
        return
    for r in results:
        if r.get("valid_until") or r.get("error"):
            continue
        key = (r["type"], r["certificate_id"])
        p = prev.get(key)
        if not p:
            continue
        r["valid_until"]       = p["valid_until"]
        r["days_until_expiry"] = days_until_expiry(p["valid_until"])
        r["status"]            = expiry_status(r["days_until_expiry"])
        for field in _HOLDER_FIELDS:
            if p.get(field):
                r[field] = p[field]
        print(f"  [fallback] {r['certificate_id']}: genbrug udløbsdato {p['valid_until']} fra forrige kørsel")


def run_checks() -> dict:
    suppliers = load_suppliers()

    eco_list = suppliers.get("eco", [])
    msc_list = suppliers.get("msc", [])
    asc_list = suppliers.get("asc", [])

    n_msc = sum(len(s["certificate_ids"]) for s in msc_list)
    n_asc = sum(len(s["certificate_ids"]) for s in asc_list)

    # Gem tidligere resultater til fallback (bruges hvis scraping fejler)
    prev_results = _load_previous_results()

    # Valider suppliers.json før tjek
    warnings = validate_suppliers(suppliers)
    if warnings:
        print(f"\nAdvarsler ({len(warnings)}):")
        for w in warnings:
            print(f"  {'DUPLIKAT' if w['warning_type'] == 'duplicate' else 'FORMAT  '} {w['message']}")
    else:
        print("\nValidering: ingen advarsler.")

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

    # Fallback: genbruger udløbsdato/holder fra forrige kørsel for poster uden data
    _apply_previous_fallback(all_results, prev_results)

    output = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": {
            "total":          len(all_results),
            "gyldig":         sum(1 for r in all_results if r["status"] == "gyldig"),
            "udloeber_snart": sum(1 for r in all_results if r["status"] == "udløber_snart"),
            "udloebet":       sum(1 for r in all_results if r["status"] == "udløbet"),
            "ukendt":         sum(1 for r in all_results if r["status"] == "ukendt"),
            "advarsler":      len(warnings),
        },
        "warnings": warnings,
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
        _scenario = "expired" if "--scenario" in sys.argv and sys.argv[sys.argv.index("--scenario") + 1] == "expired" else "warning"
        send_test_email(_scenario)
    else:
        print(f"Certifikat-tjek startet: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        run_checks()
