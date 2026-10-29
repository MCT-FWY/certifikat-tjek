# Certifikat-tjek

Dagligt tjek af MSC-, ASC- og Øko-certifikater for leverandører.
Scriptet henter status via API og web scraping, gemmer resultater som JSON og viser dem i et browser-dashboard.
Certifikater, der udløber inden for 14 dage eller er udløbet, giver en advarselsmail.

Det hele kører på kontorets mini-pc (se [Mini-pc på kontoret](#mini-pc-på-kontoret)). Indtil den er sat op, kører
tjekket på GitHub Actions, og dashboardet ligger på GitHub Pages fra `main`.

## Kom i gang på din egen pc

Kræver Python 3.10 eller nyere.

```bash
pip install -r requirements.txt
python server.py
```

Dashboardet åbner på http://localhost:8080/. Eller dobbeltklik på `start.bat` – den installerer `requests` første
gang og starter dashboardet.

`server.py` viser dashboardet, gemmer ændringer i `suppliers.json` og kører tjekket hver dag kl. 08:00. Selve
tjekket kan også køres alene:

```bash
python check_certificates.py
python check_certificates.py --test-email                     # test-mail (udløber snart)
python check_certificates.py --test-email --scenario expired  # test-mail (udløbet)
```

## Dashboardet

- **Tjek alle nu** starter et tjek i baggrunden (ca. 20 sekunder til et par minutter). Knappen viser, når et tjek
  kører – også det automatiske morgentjek.
- **Tjek log** viser de seneste 60 kørsler. Klik på en kørsel for at se, hvad tjekket skrev.
- **Tilføj, ret og slet** leverandører gemmes med det samme i `suppliers.json` og ses i dashboardet med det
  samme. Nye certifikatnumre tjekkes ved næste kørsel.
- **Godkend** en advarsel (fx et bevidst delt certifikatnummer), så den ikke vises igen. Den gemmes i
  `suppliers.json` under `approved_warnings` og gælder kun præcis de samme leverandører.

Dashboardet har ikke noget login. Det må kun være åbent på kontorets netværk (se *Sikkerhed* nedenfor).

## config.json

Ligger kun på den pc, der kører serveren (ikke i git). Kopier `config.eksempel.json` til `config.json`.

| Felt | Betydning |
|---|---|
| `host` | `127.0.0.1` = kun denne pc (standard). `0.0.0.0` = andre pc'er på kontorets netværk kan åbne dashboardet |
| `port` | Standard 8080 |
| `dagligt_tjek` | Klokkeslæt for det daglige tjek, fx `"08:00"`. `null` = intet automatisk tjek |
| `dashboard_url` | Link i advarselsmails, fx `http://FWY-MINIPC:8080/`. Standard er `http://<pc-navn>:<port>/` |

Er pc'en slukket kl. 08:00, kører tjekket, så snart serveren starter. Har nogen trykket *Tjek alle nu* tidligere
samme dag, springes morgentjekket over. Fejler tjekket tre gange i træk, prøves igen en time senere.

## Nøgler (hemmeligheder)

Tjekket bruger tre nøgler. På mini-pc'en ligger de i `hemmeligheder.json` ved siden af scriptet. Filen kommer
aldrig i git. Kopier `hemmeligheder.eksempel.json` til `hemmeligheder.json`, og udfyld den:

| Nøgle | Bruges til | Mangler den |
|---|---|---|
| `MSC_API_KEY` | MSC's Azure-API | MSC tjekkes via cert.msc.org i stedet |
| `ASC_API_KEY` | ASC's API (data.asc-aqua.org) | Alle ASC-certifikater står som *Ukendt* |
| `EMAIL_PASSWORD` | Gmail-app-adgangskode til fwypakkeri@gmail.com | Ingen advarselsmails |

Miljøvariabler med samme navn går forud for filen (sådan får GitHub Actions dem). Når serveren starter, viser den,
hvilke nøgler den kan se – uden at vise selve nøglerne.

## Mini-pc på kontoret

Samme opsætning som ordre-dashboardet (se dets README for selve pc'en, strøm og automatisk logon).

1. Installer Python (sæt flueben i *Add python.exe to PATH*).
2. Hent koden, mens GitHub stadig findes: `git clone https://github.com/MCT-FWY/certifikat-tjek.git` ved siden af
   `ordre-dashboard`, og skift til branchen med mini-pc-versionen. Eller kopier mappen fra en anden pc.
3. Kopier `config.eksempel.json` til `config.json`. Ret `dashboard_url` til pc'ens navn.
4. Kopier `hemmeligheder.eksempel.json` til `hemmeligheder.json`, og indsæt nøglerne (se ovenfor). Mens GitHub
   stadig sender mails, så lad `EMAIL_PASSWORD` stå tom – ellers kommer hver mail to gange.
5. Dobbeltklik på `start.bat` én gang. Den installerer `requests` og starter serveren.
6. Opgavestyring (*Task Scheduler*): ny opgave **Ved logon** → start `start.bat` med argumentet `--ingen-browser`.
   Eller i en kommandoprompt:
   ```
   schtasks /create /tn "Certifikat-tjek" /sc onlogon /tr "\"C:\FWY\certifikat-tjek\start.bat\" --ingen-browser"
   ```
   (ret stien til mappen).
7. Første gang spørger Windows Firewall – tillad Python på **privat netværk**.
8. Kollegerne åbner `http://<pc-navn>:8080/`. Forsiden (`fwy-vaerktoejer`) viser certifikat-tjeks statuslinje via
   `GET /api/fwy-status`.

`results/latest.json` og `suppliers.json` på mini-pc'en er nu de rigtige data. Kørselsloggen ligger i `logs/`.
Tag gerne en kopi af mappen med i den almindelige backup.

**Sikkerhed:** Leverandørlisten kan rettes af alle, der kan åbne dashboardet. Det har ikke noget login og må kun være
åbent på kontorets netværk. Åbn ikke porten mod internettet. Serveren svarer kun pc'er med en lokal netværksadresse
og afviser ændringer, der kommer fra andre hjemmesider. Skal det kunne ses hjemmefra, så lad IT-partneren sætte VPN op.

## Når GitHub lukkes – tjekliste

Gør det i denne rækkefølge, helst samme dag:

1. **Mini-pc'en kører** og er testet: dashboardet åbner fra en anden pc, og *Tjek alle nu* lykkes.
2. **Hent de nyeste data fra GitHub** til mini-pc'en. Leverandører, der er rettet på GitHub-dashboardet, og
   sidste resultat ligger på `main`:
   ```
   git fetch origin
   git checkout origin/main -- suppliers.json results/latest.json
   ```
3. **Flyt hemmelighederne**: skriv `MSC_API_KEY`, `ASC_API_KEY` og `EMAIL_PASSWORD` i `hemmeligheder.json` på
   mini-pc'en. GitHub kan ikke vise gemte hemmeligheder – find dem i password-manageren, eller lav nye (MSC/ASC-
   portalen og en ny app-adgangskode på fwypakkeri@gmail.com). Tjek bagefter serverens linje *Nøgler: … ✓*.
4. **Test mailen**: `python check_certificates.py --test-email`.
5. **Slå workflows fra** på GitHub: *Actions* → *Dagligt certifikat-tjek* → `···` → *Disable workflow*, og det
   samme for *Test e-mail notifikation*. Ellers sendes advarselsmails dobbelt.
6. **Luk GitHub Pages**: *Settings* → *Pages* → *Unpublish site*. Merg først mini-pc-branchen til `main` efter
   dette trin. Dashboardet på `main` taler med serveren og virker ikke på Pages.
7. **Forsiden**: slet `url` for certifikat-tjek i `fwy-vaerktoejer/vaerktoejer.json`, så kortet linker til
   `http://<pc-navn>:8080/`.
8. **Fortæl kollegerne** den nye adresse, og slet bogmærker til `mct-fwy.github.io`. Gamle advarselsmails linker
   stadig til Pages.
9. **Tilbagekald GitHub-tokens** (fine-grained PAT'er til dashboardet) under *Settings* → *Developer settings*.
10. **Arkivér repoet**: *Settings* → *Archive this repository*. Det kan ikke længere ændres, men kan stadig læses.
    Repoet er offentligt – vælg evt. først *Change visibility* → *Private*.

## MSC-certifikater

Scriptet bruger to metoder til MSC, i prioriteret rækkefølge:

### 1. cert.msc.org (kræver ingen nøgle)

Scriptet henter automatisk certifikatdata fra MSCs offentlige supplier directory på `cert.msc.org`. Dette kræver
ingen konfiguration og virker med det samme. Udløbsdato hentes fra certifikatdetailsiden.

### 2. Azure API (primær – kræver API-nøgle)

Bruges, når `MSC_API_KEY` er sat (se [Nøgler](#nøgler-hemmeligheder)).

| | |
|---|---|
| URL | `https://api-msc-api-prod.azure-api.net/api/chaincustody` |
| Metode | POST |
| Auth | Header: `Ocp-Apim-Subscription-Key: <nøgle>` |
| Body | `[{"certificateCode": "MSC-C-XXXXX", "speciesCode": ""}]` |

---

## Leverandørlisten (suppliers.json)

Ret helst via dashboardet. Øko-leverandører bruger ét certifikat-ID (kontrolorganets kode, som flere leverandører
kan dele), MSC/ASC bruger en liste:

```json
{ "name": "Ny leverandør", "certificate_ids": ["MSC-C-XXXXX"] }
```

## Fil-struktur

```
certifikat-tjek/
├── server.py                   # Start dette – dashboard, API og dagligt tjek
├── check_certificates.py       # Selve tjekket (køres af serveren)
├── suppliers.json              # Leverandør- og certifikatliste
├── start.bat                   # Dobbeltklik for at starte
├── config.eksempel.json        # Kopier til config.json (ikke i git)
├── hemmeligheder.eksempel.json # Kopier til hemmeligheder.json (ikke i git)
├── requirements.txt
├── dashboard/
│   └── index.html              # Dashboardet
├── results/
│   └── latest.json             # Sidste resultat
├── logs/                       # Kørselslog (ikke i git)
└── .github/workflows/          # GitHub Actions – bruges kun, indtil GitHub lukkes
```

## API

Serveren svarer kun pc'er på det lokale netværk. Ændringer kræver `Content-Type: application/json`.

| Adresse | Betydning |
|---|---|
| `GET /api/resultater` | Sidste tjek, tilpasset den nuværende leverandørliste |
| `GET /api/leverandoerer` | `suppliers.json` |
| `POST /api/leverandoer` | Tilføj: `{type, name, certificate_id, country?, search_query?}` |
| `POST /api/leverandoer/ret` | Ret: `{fra: {type, name, certificate_id}, til: {name, certificate_id, ...}}` |
| `POST /api/leverandoer/slet` | Slet: `{type, name, certificate_id}` |
| `POST /api/advarsel/godkend` | Godkend: `{warning_type, certificate_id, names}` |
| `POST /api/tjek` / `GET /api/tjek` | Start et tjek / se om et kører |
| `GET /api/log`, `GET /api/log/tekst?id=…` | Kørselslog / output fra én kørsel |
| `GET /api/fwy-status` | Statuslinje til FWY-forsiden, fx `{"tekst": "3 udløber snart · tjekket i dag 08:02", "advarsler": 3}` |

`advarsler` i `/api/fwy-status` tæller udløbne, snart udløbende og ukendte certifikater, advarsler, der ikke er
godkendt, og et fejlet sidste tjek.

## Certifikat-statusser

| Status | Betydning |
|---|---|
| Gyldig | Certifikat er aktivt og udløber om mere end 30 dage |
| Udløber snart | Udløber inden for 30 dage |
| Udløbet | Certifikat er udløbet |
| Ukendt | Kunne ikke hentes (nøgle mangler, eller fejl i kald) – eller er ikke tjekket endnu |
