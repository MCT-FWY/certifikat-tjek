# Certifikat-tjek

Dagligt tjek af MSC-, ASC- og Øko-certifikater for leverandører.
Scriptet henter status via API og web scraping, gemmer resultater som JSON, og viser dem i et browser-dashboard.

## Opsætning

```bash
pip install -r requirements.txt
```

## Kør tjek

```bash
python check_certificates.py
```

## Åbn dashboard

```bash
python serve.py
```

Åbner automatisk `http://localhost:8080/dashboard/` i browseren.

---

## MSC-certifikater

Scriptet bruger to metoder til MSC, i prioriteret rækkefølge:

### 1. cert.msc.org (midlertidig løsning – kræver ingen nøgle)

Scriptet henter automatisk certifikatdata fra MSCs offentlige supplier directory på `cert.msc.org`. Dette kræver ingen konfiguration og virker med det samme. Udløbsdato hentes fra certifikatdetailsiden.

### 2. Azure API (primær – kræver API-nøgle)

Når nøglen er klar, sættes den ét sted og Azure API bruges fremover:

```python
MSC_API_KEY = os.getenv("MSC_API_KEY", "")  # ← Indsæt nøgle her
```

Alternativt som Windows-miljøvariabel (anbefales):

```
setx MSC_API_KEY "din-nøgle-her"
```

Genstart terminalen bagefter.

| | |
|---|---|
| URL | `https://api-msc-api-prod.azure-api.net/api/chaincustody` |
| Metode | POST |
| Auth | Header: `Ocp-Apim-Subscription-Key: <nøgle>` |
| Body | `[{"certificateCode": "MSC-C-XXXXX", "speciesCode": ""}]` |

**Når API-svaret er modtaget første gang:** Bekræft feltnavne og tilpas de markerede `# TODO`-linjer i `check_msc_all()`.

---

## Tilføj leverandører

Rediger `suppliers.json`. Øko-leverandører bruger ét certifikat-ID, MSC/ASC bruger en liste:

```json
{ "name": "Ny leverandør", "certificate_ids": ["MSC-C-XXXXX"] }
```

---

## Daglig automatisk kørsel (Windows Task Scheduler)

1. Åbn **Task Scheduler** → *Create Basic Task*
2. Trigger: Dagligt, f.eks. kl. 07:00
3. Action: Start a program
   - Program: `python`
   - Arguments: `C:\Users\MortenChewinsTiedema\certifikat-tjek\check_certificates.py`
   - Start in: `C:\Users\MortenChewinsTiedema\certifikat-tjek`

---

## Fil-struktur

```
certifikat-tjek/
├── check_certificates.py   # Hovedscript – kør dette dagligt
├── suppliers.json           # Leverandør- og certifikatliste
├── serve.py                 # Starter lokal webserver og åbner dashboard
├── requirements.txt
├── dashboard/
│   └── index.html           # Dashboard (kræver serve.py eller en webserver)
└── results/
    └── latest.json          # Genereres af scriptet
```

## Certifikat-statusser

| Status | Betydning |
|---|---|
| Gyldig | Certifikat er aktivt og udløber om mere end 30 dage |
| Udløber snart | Udløber inden for 30 dage |
| Udløbet | Certifikat er udløbet |
| Ukendt | Kunne ikke hentes (API/URL ikke konfigureret, eller fejl i kald) |
