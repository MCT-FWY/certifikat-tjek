#!/usr/bin/env python3
"""
Starter certifikat-dashboardet på kontorets mini-pc: en lille web-server, der viser
dashboardet, gemmer ændringer i leverandørlisten og kører certifikat-tjekket hver dag.

    python server.py
    python server.py --ingen-browser     # fra Opgavestyring ved logon

Serveren bruger kun standardbiblioteket. Selve tjekket (check_certificates.py) køres som
en selvstændig proces – præcis som GitHub-workflowet gjorde.
"""

import argparse
import datetime as dt
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).parent
DASHBOARD_DIR = BASE_DIR / "dashboard"
CONFIG_FILE = BASE_DIR / "config.json"
SECRETS_FILE = BASE_DIR / "hemmeligheder.json"
SUPPLIERS_FILE = BASE_DIR / "suppliers.json"
RESULTS_FILE = BASE_DIR / "results" / "latest.json"
CHECK_SCRIPT = BASE_DIR / "check_certificates.py"
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "koersler.json"

DEFAULT_CONFIG = {
    "host": "127.0.0.1",        # sæt til "0.0.0.0" på mini-pc'en, så kollegerne kan åbne dashboardet
    "port": 8080,
    "dagligt_tjek": "08:00",    # null = intet automatisk tjek
    "dashboard_url": None,      # link i advarselsmails – standard er http://<pc-navn>:<port>/
}

# Som GitHub-workflowet: tre forsøg med 15 sekunders pause, højst 10 minutter pr. forsøg
CHECK_ATTEMPTS = 3
CHECK_RETRY_PAUSE = 15
CHECK_TIMEOUT = 10 * 60
KEEP_RUNS = 60                  # antal kørsler i loggen

TYPES = ("eco", "msc", "asc")
TYPE_LABELS = {"eco": "Øko", "msc": "MSC", "asc": "ASC"}
CERT_EXAMPLE = {"msc": "MSC-C-52525", "asc": "ASC-C-00438"}
SECRET_KEYS = ("MSC_API_KEY", "ASC_API_KEY", "EMAIL_PASSWORD")
RUN_ID = re.compile(r"\d{4}-\d{2}-\d{2}_\d{6}")


# ---------------------------------------------------------------------------
# Filer
# ---------------------------------------------------------------------------

def read_json(path: Path):
    """Læser en JSON-fil. Rammer vi filen, mens tjekket skriver den, prøver vi igen."""
    for attempt in range(5):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, PermissionError):
            if attempt == 4:
                raise
            time.sleep(0.2)


def write_json(path: Path, data) -> None:
    """Skriver via en midlertidig fil, så en afbrudt skrivning aldrig efterlader en halv fil."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:         # Windows: filen læses lige nu af en anden proces
            if attempt == 4:
                raise
            time.sleep(0.2)


def warning_key(w: dict) -> tuple:
    """Samme nøgle som _warning_key() i check_certificates.py – navnenes rækkefølge er ligegyldig."""
    return (w.get("warning_type"), w.get("certificate_id"), tuple(sorted(w.get("names") or [])))


def from_office_network(address: str) -> bool:
    """Kun denne pc og pc'er på det lokale netværk får svar. Porten skal ikke åbnes mod
    internettet, men sker det alligevel, afviser serveren selv fremmede adresser."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


# ---------------------------------------------------------------------------
# Leverandørlisten – samme regler, som dashboardet brugte mod GitHub
# ---------------------------------------------------------------------------

def _cert_type(value) -> str:
    if value not in TYPES:
        raise ValueError(f"Ukendt certifikattype: {value!r}")
    return value


def _supplier_fields(d: dict, cert_type: str) -> dict:
    name = str(d.get("name") or "").strip()
    cert_id = str(d.get("certificate_id") or "").strip()
    if not name:
        raise ValueError("Indtast leverandørnavn.")
    if not cert_id:
        raise ValueError("Indtast certifikatnummer.")
    if cert_type in CERT_EXAMPLE and not cert_id.upper().startswith(cert_type.upper() + "-"):
        raise ValueError(f"{cert_type.upper()}-certifikatnummer skal starte med {cert_type.upper()}- "
                         f"(f.eks. {CERT_EXAMPLE[cert_type]}).")
    fields = {"name": name, "certificate_id": cert_id}
    if cert_type == "eco":
        country = str(d.get("country") or "").strip().upper()
        if not country:
            raise ValueError("Indtast landekode for ØKO-certifikat.")
        fields["country"] = country
        fields["search_query"] = str(d.get("search_query") or "").strip() or name
    return fields


def _not_found() -> ValueError:
    return ValueError("Leverandøren findes ikke længere i listen – genindlæs siden.")


def add_supplier(suppliers: dict, d: dict) -> str:
    cert_type = _cert_type(d.get("type"))
    v = _supplier_fields(d, cert_type)
    if cert_type == "eco":
        suppliers.setdefault("eco", []).append(v)
    else:
        entries = suppliers.setdefault(cert_type, [])
        existing = next((s for s in entries if s["name"] == v["name"]), None)
        if existing and v["certificate_id"] in existing["certificate_ids"]:
            raise ValueError(f"{v['name']} har allerede {v['certificate_id']}.")
        if existing:
            existing["certificate_ids"].append(v["certificate_id"])
        else:
            entries.append({"name": v["name"], "certificate_ids": [v["certificate_id"]]})
    return f"{v['name']} ({v['certificate_id']}) er tilføjet."


def edit_supplier(suppliers: dict, d: dict) -> str:
    """For MSC/ASC hører navnet til leverandøren, så hele posten (alle dens certifikater)
    omdøbes; findes det nye navn allerede, flettes de to poster sammen."""
    old = d.get("fra") or {}
    cert_type = _cert_type(old.get("type"))
    old_name, old_cert = old.get("name"), old.get("certificate_id")
    v = _supplier_fields(d.get("til") or {}, cert_type)

    if cert_type == "eco":
        s = next((s for s in suppliers.get("eco", [])
                  if s["certificate_id"] == old_cert and s["name"] == old_name), None)
        if not s:
            raise _not_found()
        s.update(v)
    else:
        entries = suppliers.get(cert_type, [])
        s = next((s for s in entries if s["name"] == old_name and old_cert in s["certificate_ids"]), None)
        if not s:
            raise _not_found()
        s["certificate_ids"] = list(dict.fromkeys(
            v["certificate_id"] if c == old_cert else c for c in s["certificate_ids"]))
        if v["name"] != old_name:
            target = next((x for x in entries if x is not s and x["name"] == v["name"]), None)
            if target:
                target["certificate_ids"] = list(dict.fromkeys(target["certificate_ids"] + s["certificate_ids"]))
                if s.get("species_codes") or target.get("species_codes"):
                    target["species_codes"] = sorted(set(target.get("species_codes", []) + s.get("species_codes", [])))
                entries.remove(s)
            else:
                s["name"] = v["name"]

    if v["name"] != old_name:           # godkendte advarsler følger med navneskiftet
        for a in suppliers.get("approved_warnings", []):
            a["names"] = sorted(v["name"] if n == old_name else n for n in a["names"])

    message = "Ændringerne er gemt."
    if v["certificate_id"] != old_cert:
        message += " Det nye certifikatnummer tjekkes ved næste kørsel."
    return message


def delete_supplier(suppliers: dict, d: dict) -> str:
    cert_type = _cert_type(d.get("type"))
    name, cert_id = d.get("name"), d.get("certificate_id")
    if cert_type == "eco":
        # Øko-nummeret er kontrolorganets kode og deles af flere leverandører – match også navnet
        before = len(suppliers.get("eco", []))
        suppliers["eco"] = [s for s in suppliers.get("eco", [])
                            if not (s["certificate_id"] == cert_id and s["name"] == name)]
        if len(suppliers["eco"]) == before:
            raise _not_found()
    else:
        entries = suppliers.get(cert_type, [])
        s = next((s for s in entries if s["name"] == name and cert_id in s["certificate_ids"]), None)
        if not s:
            raise _not_found()
        s["certificate_ids"].remove(cert_id)
        if not s["certificate_ids"]:
            entries.remove(s)
    return f"{name} ({cert_id}) er fjernet."


def approve_warning(suppliers: dict, d: dict) -> str:
    if d.get("warning_type") not in ("duplicate", "format"):
        raise ValueError("Kun duplikat- og formatadvarsler kan godkendes.")
    names = sorted(str(n) for n in d.get("names") or [])
    if not d.get("certificate_id") or not names:
        raise ValueError("Advarslen mangler certifikatnummer eller leverandører.")
    approved = suppliers.setdefault("approved_warnings", [])
    if not any(warning_key(a) == warning_key(d) for a in approved):
        approved.append({
            "warning_type": d["warning_type"],
            "certificate_id": d["certificate_id"],
            "names": names,
            "approved_on": dt.date.today().isoformat(),
        })
    return f"Advarslen for {d['certificate_id']} er godkendt."


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def _placeholder(cert_type: str, name: str, cert_id: str) -> dict:
    """Kort for en leverandør, der er tilføjet siden sidste tjek."""
    return {"name": name, "type": cert_type, "certificate_id": cert_id, "status": "ukendt",
            "valid_until": None, "days_until_expiry": None, "checked_at": None, "error": None, "_new": True}


class Busy(Exception):
    """Der kører allerede et tjek."""


def _when(t: dt.datetime) -> str:
    days = (dt.date.today() - t.date()).days
    day = "i dag" if days == 0 else "i går" if days == 1 else f"{t.day}/{t.month}"
    return f"{day} {t:%H:%M}"


class App:
    """Én instans deles af alle forespørgsler og af den daglige planlægger."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.suppliers_lock = threading.Lock()      # én ændring af suppliers.json ad gangen
        self.run_lock = threading.Lock()            # højst ét tjek ad gangen
        self.log_lock = threading.Lock()
        self.current_run: dict | None = None
        with self.log_lock:                         # et tjek, der kørte, da serveren stoppede
            runs = self.runs()
            for r in runs:
                if r["status"] == "koerer":
                    r.update(status="afbrudt", besked="Serveren blev stoppet under tjekket")
            self._save_runs(runs)

    def dashboard_url(self) -> str:
        return self.cfg.get("dashboard_url") or f"http://{socket.gethostname()}:{self.cfg['port']}/"

    # -- Resultater -----------------------------------------------------------------

    def suppliers(self) -> dict:
        return read_json(SUPPLIERS_FILE)

    def last_checked(self, data: dict | None = None) -> dt.datetime | None:
        if data is None:
            data = read_json(RESULTS_FILE) if RESULTS_FILE.exists() else {}
        stamp = data.get("checked_at")
        return dt.datetime.fromisoformat(stamp).astimezone() if stamp else None

    def results(self) -> dict:
        """Sidste tjek, tilpasset den nuværende leverandørliste: slettede leverandører er væk,
        nye står som ikke tjekket, og godkendte advarsler er sorteret fra."""
        data = read_json(RESULTS_FILE) if RESULTS_FILE.exists() else {"checked_at": None}
        suppliers = self.suppliers()
        old = data.get("results") or []
        exact = {(r["type"], r["name"], r["certificate_id"]): r for r in old}
        # MSC/ASC-status hører til certifikatet, ikke navnet – så en omdøbt leverandør (eller en
        # anden med samme nummer) beholder resultatet. Øko-numre er kontrolorganets kode og deles.
        by_cert = {(r["type"], r["certificate_id"]): r for r in old if r["type"] != "eco"}

        results = []
        for s in suppliers.get("eco", []):
            key = ("eco", s["name"], s["certificate_id"])
            results.append(exact.get(key) or _placeholder(*key))
        for cert_type in ("msc", "asc"):
            for s in suppliers.get(cert_type, []):
                for cert_id in s["certificate_ids"]:
                    r = exact.get((cert_type, s["name"], cert_id))
                    if r is None and (cert_type, cert_id) in by_cert:
                        r = dict(by_cert[(cert_type, cert_id)], name=s["name"])
                    results.append(r or _placeholder(cert_type, s["name"], cert_id))

        approved = {warning_key(a) for a in suppliers.get("approved_warnings", [])}
        warnings = [w for w in data.get("warnings") or [] if warning_key(w) not in approved]
        count = lambda status: sum(1 for r in results if r["status"] == status)
        data.update(results=results, warnings=warnings, summary={
            "total": len(results),
            "gyldig": count("gyldig"),
            "udloeber_snart": count("udløber_snart"),
            "udloebet": count("udløbet"),
            "ukendt": count("ukendt"),
            "advarsler": len(warnings),
        })
        return data

    def fwy_status(self) -> dict:
        """Én linje til FWY-forsiden, fx '2 udløber snart · tjekket i dag 08:02'."""
        data = self.results()
        results, n_warnings = data["results"], len(data["warnings"])
        counts = [
            (sum(1 for r in results if r["status"] == "udløbet"), "udløbet"),
            (sum(1 for r in results if r["status"] == "udløber_snart"), "udløber snart"),
            (sum(1 for r in results if r["status"] == "ukendt" and not r.get("_new")), "ukendt"),
            (n_warnings, "advarsel" if n_warnings == 1 else "advarsler"),
        ]
        parts = [f"{n} {label}" for n, label in counts if n]
        action = sum(n for n, _ in counts)
        checked = self.last_checked(data)
        if checked is None:
            parts = ["ikke tjekket endnu"]
            action += 1
        else:
            parts = (parts or ["alle gyldige"]) + [f"tjekket {_when(checked)}"]

        latest = (self.runs() or [None])[0]
        if self.current_run:
            parts.append("tjekker nu")
        elif latest and latest["status"] in ("fejl", "afbrudt"):
            parts.append("sidste tjek fejlede")
            action += 1
        text = " · ".join(parts)
        return {"tekst": text[0].upper() + text[1:], "advarsler": action}

    # -- Leverandørlisten -------------------------------------------------------------

    def change_suppliers(self, change, data: dict) -> dict:
        with self.suppliers_lock:
            suppliers = self.suppliers()
            message = change(suppliers, data)
            write_json(SUPPLIERS_FILE, suppliers)
        print(f"[{dt.datetime.now():%H:%M}] {message}")
        return {"besked": message}

    # -- Tjek og kørselslog -----------------------------------------------------------

    def runs(self) -> list[dict]:
        return read_json(LOG_FILE) if LOG_FILE.exists() else []

    def _save_runs(self, runs: list[dict]) -> None:
        for old in runs[KEEP_RUNS:]:
            (LOG_DIR / f"{old['id']}.txt").unlink(missing_ok=True)
        write_json(LOG_FILE, runs[:KEEP_RUNS])

    def _log_run(self, run: dict) -> None:
        with self.log_lock:
            runs = [r for r in self.runs() if r["id"] != run["id"]]
            self._save_runs([dict(run)] + runs)

    def run_status(self) -> dict:
        run = self.current_run
        if run:
            started = dt.datetime.fromisoformat(run["startet"])
            run = dict(run, sekunder=int((dt.datetime.now() - started).total_seconds()))
        finished = next((r for r in self.runs() if r["status"] != "koerer"), None)
        return {"koerer": run, "sidste": finished}

    def start_check(self, trigger: str) -> dict:
        """Starter et tjek i baggrunden. trigger er 'manuel' eller 'automatisk'."""
        with self.run_lock:
            if self.current_run:
                raise Busy("Der kører allerede et tjek.")
            now = dt.datetime.now()
            run = {"id": f"{now:%Y-%m-%d_%H%M%S}", "startet": now.isoformat(timespec="seconds"),
                   "slut": None, "status": "koerer", "udloest_af": trigger, "besked": None}
            self.current_run = run
        self._log_run(run)
        print(f"[{now:%H:%M}] Certifikat-tjek startet ({trigger})")
        threading.Thread(target=self._run_check, args=(run,), daemon=True).start()
        return run

    def _run_check(self, run: dict) -> None:
        # UTF-8, ellers kan Windows ikke skrive emoji fra mail-emnerne, og tjekket fejler
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", DASHBOARD_URL=self.dashboard_url())
        output, ok = [], False
        try:
            for attempt in range(1, CHECK_ATTEMPTS + 1):
                output.append(f"--- Forsøg {attempt}/{CHECK_ATTEMPTS} ({dt.datetime.now():%H:%M:%S}) ---\n")
                try:
                    proc = subprocess.run([sys.executable, str(CHECK_SCRIPT)], cwd=BASE_DIR, env=env,
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          encoding="utf-8", errors="replace", timeout=CHECK_TIMEOUT)
                    output.append(proc.stdout)
                    if proc.returncode == 0:
                        ok = True
                        break
                    output.append(f"\nAfsluttede med fejlkode {proc.returncode}\n")
                except subprocess.TimeoutExpired:
                    output.append(f"\nStoppet efter {CHECK_TIMEOUT // 60} minutter\n")
                if attempt < CHECK_ATTEMPTS:
                    time.sleep(CHECK_RETRY_PAUSE)
        except Exception:
            output.append(traceback.format_exc())
        try:
            text = "".join(output)
            LOG_DIR.mkdir(exist_ok=True)
            (LOG_DIR / f"{run['id']}.txt").write_text(text, encoding="utf-8")
            # Tjekkets egen opsummering, fx "Gyldige: 44 | Udløber snart: 3 ..." og "SMTP: 2 e-mail(s) sendt."
            summary = [line.strip() for line in text.splitlines()
                       if line.strip().startswith(("Gyldige:", "SMTP"))]
            run.update(slut=dt.datetime.now().isoformat(timespec="seconds"), status="ok" if ok else "fejl",
                       besked=" · ".join(summary) or (None if ok else "Alle forsøg fejlede – se loggen"))
            self._log_run(run)
            print(f"[{dt.datetime.now():%H:%M}] Certifikat-tjek {'færdigt' if ok else 'FEJLEDE'}"
                  + (f": {run['besked']}" if run["besked"] else ""))
        finally:
            self.current_run = None

    def run_output(self, run_id: str) -> str:
        if not RUN_ID.fullmatch(run_id or ""):
            raise ValueError("Ugyldigt kørsels-id")
        path = LOG_DIR / f"{run_id}.txt"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def check_due(self) -> bool:
        """Dagens tjek mangler: klokken er over det planlagte tidspunkt, og der er ikke tjekket
        i dag. Fejler et automatisk tjek, prøves igen en time senere."""
        hour, minute = self.cfg["_tjek_tid"]
        now = dt.datetime.now()
        if self.current_run or now < now.replace(hour=hour, minute=minute, second=0, microsecond=0):
            return False
        checked = self.last_checked()
        if checked and checked.date() == now.date():
            return False
        return not any(r["udloest_af"] == "automatisk"
                       and now - dt.datetime.fromisoformat(r["startet"]) < dt.timedelta(hours=1)
                       for r in self.runs())

    def scheduler_loop(self) -> None:
        while True:
            try:
                if self.check_due():
                    self.start_check("automatisk")
            except Exception:
                traceback.print_exc()
            time.sleep(60)


# ---------------------------------------------------------------------------
# Web-server
# ---------------------------------------------------------------------------

POST_ROUTES = {
    "/api/leverandoer": add_supplier,
    "/api/leverandoer/ret": edit_supplier,
    "/api/leverandoer/slet": delete_supplier,
    "/api/advarsel/godkend": approve_warning,
}


def make_handler(app: App):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

        def log_message(self, fmt, *args):
            pass                                    # hold konsollen ren – fejl skrives stadig

        def end_headers(self):
            if not self.path.startswith("/api/"):
                self.send_header("Cache-Control", "no-cache")   # browseren henter altid den nyeste side
            super().end_headers()

        def _json(self, payload, status=HTTPStatus.OK, headers=None):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _allowed(self) -> bool:
            if from_office_network(self.client_address[0]):
                return True
            self.send_error(HTTPStatus.FORBIDDEN, "Kun åbent på kontorets netværk")
            return False

        def do_HEAD(self):
            if self._allowed():
                super().do_HEAD()

        def do_GET(self):
            if not self._allowed():
                return
            url = urlparse(self.path)
            if not url.path.startswith("/api/"):
                return super().do_GET()
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/api/fwy-status":
                    # Forsiden ligger på en anden port og må gerne læse statuslinjen
                    self._json(app.fwy_status(), headers={"Access-Control-Allow-Origin": "*"})
                elif url.path == "/api/resultater":
                    self._json(app.results())
                elif url.path == "/api/leverandoerer":
                    self._json(app.suppliers())
                elif url.path == "/api/tjek":
                    self._json(app.run_status())
                elif url.path == "/api/log":
                    self._json({"koersler": app.runs()})
                elif url.path == "/api/log/tekst":
                    self._json({"tekst": app.run_output(q.get("id"))})
                else:
                    self._json({"fejl": "Ukendt adresse"}, HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._json({"fejl": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                traceback.print_exc()
                self._json({"fejl": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_POST(self):
            if not self._allowed():
                return
            # Kun dashboardet selv må ændre noget – ikke en fremmed side, der sender en formular hertil
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                return self._json({"fejl": "Forespørgsel fra en anden side afvist"}, HTTPStatus.FORBIDDEN)
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._json({"fejl": "Forventede JSON"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)

            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
                if path == "/api/tjek":
                    self._json(app.start_check("manuel"), HTTPStatus.ACCEPTED)
                elif path in POST_ROUTES:
                    self._json(app.change_suppliers(POST_ROUTES[path], data))
                else:
                    self._json({"fejl": "Ukendt adresse"}, HTTPStatus.NOT_FOUND)
            except Busy as exc:
                self._json({"fejl": str(exc)}, HTTPStatus.CONFLICT)
            except ValueError as exc:
                self._json({"fejl": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                traceback.print_exc()
                self._json({"fejl": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    return Handler


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    cfg["_tjek_tid"] = None
    if cfg["dagligt_tjek"]:
        try:
            t = dt.time.fromisoformat(cfg["dagligt_tjek"])
        except ValueError:
            sys.exit(f"config.json: dagligt_tjek skal være et klokkeslæt som \"08:00\" eller null, "
                     f"ikke {cfg['dagligt_tjek']!r}")
        cfg["_tjek_tid"] = (t.hour, t.minute)
    return cfg


def secrets_overview() -> str:
    """Hvilke nøgler tjekket kan se (miljøvariabler eller hemmeligheder.json) – uden at vise dem."""
    found = {k for k in SECRET_KEYS if os.getenv(k)}
    if SECRETS_FILE.exists():
        found |= {k for k, v in read_json(SECRETS_FILE).items() if k in SECRET_KEYS and v}
    return ", ".join(f"{k} {'✓' if k in found else 'mangler'}" for k in SECRET_KEYS)


def main():
    parser = argparse.ArgumentParser(description="Certifikat-dashboard")
    parser.add_argument("--port", type=int, help="port (standard fra config.json)")
    parser.add_argument("--ingen-browser", action="store_true", help="åbn ikke browseren automatisk")
    args = parser.parse_args()

    cfg = load_config()
    if args.port:
        cfg["port"] = args.port
    app = App(cfg)
    server = ThreadingHTTPServer((cfg["host"], cfg["port"]), make_handler(app))
    if cfg["_tjek_tid"]:
        threading.Thread(target=app.scheduler_loop, daemon=True).start()

    local_url = f"http://localhost:{cfg['port']}/"
    print(f"\nCertifikat-dashboard:  {local_url}")
    if cfg["host"] == "0.0.0.0":
        print(f"Fra andre pc'er på netværket:  http://{socket.gethostname()}:{cfg['port']}/")
    print(f"Dagligt tjek: {'kl. ' + cfg['dagligt_tjek'] if cfg['_tjek_tid'] else 'slået fra i config.json'}")
    print(f"Nøgler: {secrets_overview()}")
    print("Stop: Ctrl+C\n")
    if not args.ingen_browser:
        webbrowser.open(local_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
