# Kodelister

Én CSV pr. liste. **Filnavnet er listenavnet** — `kle.csv` bliver til
`liste = 'kle'` i `map.kodeliste`, og det er det navn koden slår op på.
Opretter du en ny fil, skal koden kende navnet, ellers bliver den aldrig
brugt. Testen `test_mapping_filer.py` holder de to sider sammen.

Indlæses med:

```powershell
uv run konv mapping load
```

Eksisterende rækker opdateres, nye tilføjes, **ingen slettes automatisk**.
En mapning der fjernes fra CSV'en skal fjernes bevidst i databasen.

Den anden vej:

```powershell
uv run konv mapping gem
```

Skriver `map.kodeliste` tilbage til én CSV pr. liste. Filerne er kilden,
databasen er kopien — men kopien er den der overlever, når en fil bliver
overskrevet af en udpakning eller slettet ved en fejl. Er en udfyldt fil
væk, mens værdierne stadig står i databasen, er det denne kommando der
henter dem hjem.

## Format

Semikolonsepareret, UTF-8. Dansk Excel bruger semikolon som listeseparator,
så filerne åbner direkte i kolonner.

| Kolonne | Betydning |
|---|---|
| `liste` | Skal matche filnavnet |
| `go_vaerdi` | Værdien præcis som den står i GO. Matches **eksakt** — mellemrum og store bogstaver tæller |
| `go_tekst` | Læsbar tekst, så en fagperson kan genkende værdien |
| `sbsys_vaerdi` | Det der sendes til SBSYS. Næsten altid et **id** fra SBSYS, ikke et navn |
| `sbsys_tekst` | Læsbar tekst for SBSYS-siden |
| `handling` | `map` (oversæt), `standard` (fælles reserveværdi), `bloker` (afvis objektet), `ignorer` (felt: tomt; for indsatsens mappetype: konverteres bevidst ikke, springes over) |
| `kommentar` | Hvorfor. Skrives også af `mapping forbered` og `mapping mangler` |
| `godkendt_af` | Valgfri — udfyldes i praksis ikke |
| `godkendt_dato` | Valgfri — udfyldes i praksis ikke |

**Kun tre felter er beslutningen:** `go_vaerdi`, `sbsys_vaerdi` og `handling`.
Resten er læsehjælp. De to sidste kolonner står tomme; vil man kunne spore
hvem der sagde god for en mapning, er det dér det skal skrives.

En række med `handling = map` og tom `sbsys_vaerdi` tæller som uafklaret og
kommer med i `konv mapping mangler`.

## Listerne

| Fil | Oversætter | Blokerende | Hvor kommer SBSYS-værdien fra |
|---|---|---|---|
| `kle.csv` | GO's emnekode → KLE-nummer | ja | KLE-emneplanen |
| `sagsskabelon.csv` | GO's sagstype → `SkabelonId` | ja | `konv opslag skabeloner` |
| `afdeling.csv` | GO's afdeling → `AnsaettelsesstedId` | ja | `konv opslag afdelinger` |
| `bruger.csv` | GO-login → SBSYS-bruger-id | nej | `konv opslag brugere` |
| `sagsstatus.csv` | GO-status → `SagsStatusId` | nej | `konv selvtest` rammer statuslisten |
| `boelge.csv` | `Cases.Organisering` (PPR/ELM/BOR/EMN) → bølgekode | nej | `konv boelge list` |
| `boelge_enhed.csv` | enhed → bølgekode, hvor enheden afgør den (ELM's seks, Børneterapien) | nej | `konv boelge list` |
| `sagstype.csv`, `facet.csv`, `fortrolighed.csv`, `bevaring.csv` | diverse sagsfelter | nej | SBSYS' opsætning |
| `dokumentkategori.csv` | GO-dokumenttype → `DokumentTypeId` | nej | SBSYS' opsætning |
| `notattype.csv` | GO-notattype | nej | SBSYS' opsætning |
| `paragraf.csv` | Foranstaltningsmappens type (`Indholdstype`) → `paragrafId` | **ja** | `konv opslag katalog` |
| `foranstaltning.csv` | Mappetypen → `foranstaltningId` | **ja** | `konv opslag katalog` |
| `tilbud.csv` | Mappetypen → `tilbudId` | **ja** | `konv opslag katalog` |
| `enhedstype.csv` | GO's enhed → `Time`/`Doegn`/`Uge`/`Maaned`/`Aar`/`Stk` | nej | SBSYS' enum, se docs/09-indsatser.md |
| `udslagsgivende_forhold.csv` | GO's årsag → id | nej | `konv opslag` mod `/bu_api/udslagsgivende_forhold` |

De tre blokerende til indsatser hænger sammen: `paragraf`,
`foranstaltning` og `tilbud` udgør det tripel
`POST /bu_api/sag_foranstaltning` kræver, og de skal vælges **samlet** —
et tilbud hører til en bestemt foranstaltning under en bestemt paragraf.
Hent hele hierarkiet fladet ud med:

```powershell
uv run konv opslag katalog
```

Alle tre slår op på foranstaltningsmappens type (GO's `Indholdstype`):
samme GO-værdi, tre forskellige SBSYS-id'er. Tilbuddet ER mappetypen
(Psykolog, ABA ...), jf. konverteringsarket 17-11-2026. GO's `Ydelse` og
`Bevillingstype` er almindelige tilbudsfelter, ikke tripellet.

Typer hvis data ikke skal konverteres (Konsulent, Fysioterapeut PPR ...),
står med `handling = ignorer` i alle tre. Deres indsatser springes over —
ikke afvist — mens sagerne konverteres som normalt.

## Start med at lade maskinen gøre forarbejdet

```powershell
uv run konv mapping forbered
```

Den henter **begge sider**: GO's distinkte værdier fra `stg.*` og SBSYS'
id'er og navne fra API'et, og parrer dem hvor navnet er entydigt
identisk. Resultatet lander i `rapporter/kodelister/` i præcis dette
format, så filerne kan kopieres til `mapping/` når de er gennemgået.

Tre ting den gør, og én den ikke gør:

* **Kun de værdier GO faktisk bruger.** En liste over alt der er muligt er
  ubrugelig; en over de 23 værdier der står i data, kan udfyldes på en
  formiddag.
* **Skrivemåden er ligegyldig** ved sammenligningen: `PPR Nord`,
  `ppr-nord` og `PPRNORD` regnes som samme ord.
* **Allerede mappede værdier markeres** i stedet for at blive foreslået
  igen.
* **Den godkender aldrig.** Et udfyldt felt er mærket `FORSLAG`, og
  feltet er udfyldt af en maskine. Parringen mellem GO og SBSYS er en
  faglig vurdering — maskinen kan spare afskriften, ikke beslutningen.

Lister uden en modsvarende liste i SBSYS (KLE, facet, bevaring) får stadig
et ark med GO-værdierne; der står bare, at SBSYS-siden skal udfyldes i
hånden.

## Arbejdsgangen

0. `uv run konv mapping forbered` — se ovenfor
1. Kør en tør kørsel: `uv run konv load sag --boelge ppr`
2. `uv run konv mapping mangler` skriver de uafklarede værdier til CSV
3. Fagpersonen udfylder `sbsys_vaerdi` og `handling`
4. `uv run konv mapping load` igen
5. `uv run konv nulstil --boelge ppr --regel manglende_mapping`
6. Kør igen

Trin 3 er det eneste der ikke kan automatiseres, og det er også det der
tager tid. Tag tid på hele sløjfen i testkonverteringen — i produktion skal den køres i
ugerne 51-53, hvor de samme fagpersoner holder fri.
