"""Kommandolinje.

Samme kode kører fra terminalen og fra Automation Server. Det er et bevidst
valg: alt kan afprøves manuelt af en udvikler før det sættes på en trigger,
og en driftsvagt kan køre præcis det samme igen i hånden kl. 03.

    konv db init                     # opret schemas og tabeller
    konv mapping load                # indlæs kodelister fra mapping/*.csv
    konv mapping mangler             # skriv uafklarede værdier til CSV
    konv filer scan                  # find, mål og checksum dokumentfiler
    konv boelge list                 # de ti bølger og deres status
    konv load sag --boelge ppr       # indlæs én bølge (dry-run som standard)
    konv load dokument --boelge ppr --live
    konv boelge gate --boelge ppr    # klar til undervisning? returkode 0 = ja
    konv boelge afstem ppr --af ab1234
    konv status --boelge ppr
    konv spec noegle                 # findes der et felt til GO-nøglen? (A1)
    konv nulstil --boelge ppr --regel manglende_mapping   # efter rettet mapping
    konv oprettede --boelge ppr      # hvad har kørslen lagt i SBSYS?
    konv db nulstil --jatak          # tøm VORES database før næste dump

Værktøjet sletter aldrig noget i SBSYS. Oprydningen dér håndteres af andre.
Vores egen database kan derimod nulstilles, så det endelige dump kan læses
ind i et rent udgangspunkt — se `konv db nulstil`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from . import boelge
from .app import byg_kontekst, opsaet_logging
from .config import (
    KonfigurationsFejl,
    SbsysIndstillinger,
    Settings,
    tls_verifikation,
)
from .db import Database
from .files import FilOpslag
from .hashing import kort_noegle
from .idmap import IdMap, afslut_run, opret_run, tidligere_maal
from .loaders import LOADERS, boelge_filter
from .mapping import aktive_testvaerdier, indlaes_mapping, skriv_mangler_csv, skriv_mappe
from .models import Objekttype
from .sbsys.errors import (
    AutentifikationsFejl,
    CertifikatFejl,
    KonverteringsFejl,
    VaertsnavnFejl,
)

log = logging.getLogger(__name__)

SQL_MAPPE = Path(__file__).resolve().parents[2] / "sql"
MAPPING_MAPPE = Path(__file__).resolve().parents[2] / "mapping"


def paakraevede_variabler() -> list[str]:
    """De variabler der skal være sat, før noget som helst kan køre."""
    kraevet = ["KONV_DB_SERVER", "SBSYS_BASE_URL", "KONV_FIL_ROD"]
    if os.environ.get("KONV_DB_TRUSTED", "").strip().lower() not in {"1", "true", "ja", "yes", "on"}:
        kraevet += ["KONV_DB_BRUGER", "KONV_DB_KODEORD"]
    return kraevet


def _auth_form_fra_env() -> str:
    """Hvilken SBSYS-autentifikation miljøvariablerne giver.

    Bygges uden om Settings, fordi `konv env` netop skal kunne køre når
    Settings ikke kan dannes — det er dér man har allermest brug for svaret.
    """
    return SbsysIndstillinger(
        base_url=os.environ.get("SBSYS_BASE_URL", ""),
        token_url=os.environ.get("SBSYS_TOKEN_URL") or None,
        client_id=os.environ.get("SBSYS_CLIENT_ID") or None,
        client_secret=os.environ.get("SBSYS_CLIENT_SECRET") or None,
        brugernavn=os.environ.get("SBSYS_USERNAME") or None,
        kodeord=os.environ.get("SBSYS_PASSWORD") or None,
        statisk_token=os.environ.get("SBSYS_TOKEN") or None,
    ).auth_form


def _tls_beskrivelse_fra_env() -> str:
    """Hvordan SBSYS' certifikat kontrolleres — eller hvorfor stien er forkert."""
    try:
        return SbsysIndstillinger(
            base_url="", verificer_tls=tls_verifikation()
        ).tls_beskrivelse
    except KonfigurationsFejl as e:
        return f"FEJL — {e}"


def _env_diagnose(env_status: str) -> int:
    """Fortæl hvad der faktisk blev læst — uden at vise værdierne.

    Findes fordi "variablen mangler" og "variablen står i filen" kan være
    sande samtidig: forkert tegnkodning, forkert mappe, eller en nøgle der
    hedder noget andet end man tror. Her kan man se hvad programmet ser.

    Værdier printes aldrig. Filen indeholder et databasekodeord.
    """
    print(env_status)
    print()

    sti = None
    try:
        from dotenv import dotenv_values, find_dotenv

        fundet = find_dotenv(usecwd=True)
        sti = Path(fundet) if fundet else None
    except ImportError:
        pass

    if sti and sti.exists():
        noegler = dotenv_values(sti, encoding=_sniff_kodning(sti))
        print(f"Nøgler i filen ({len(noegler)}):")
        for navn in noegler:
            vist = navn if navn.isprintable() else repr(navn)
            markering = "" if vist.strip() == vist and vist.isidentifier() else "   <- ugyldigt navn"
            print(f"  {vist}{markering}")
        print()

    # Autentifikationen er den hyppigste kilde til 401 senere, og den kan
    # ikke ses ved at kigge på enkeltnøgler: den afhænger af kombinationen.
    print(f"SBSYS-autentifikation: {_auth_form_fra_env()}")
    print(f"SBSYS-certifikat:      {_tls_beskrivelse_fra_env()}")
    print()

    mangler = [n for n in paakraevede_variabler() if not os.environ.get(n, "").strip()]
    if not mangler:
        print("Alle påkrævede variabler er sat.")
        return 0

    # Står navnet i filens tekst, men blev det ikke læst som en nøgle, er det
    # linjens form der er gal — ikke tegnkodningen. Den skelnen sparer megen tid.
    raa = ""
    if sti and sti.exists():
        try:
            raa = sti.read_text(encoding=_sniff_kodning(sti))
        except (OSError, UnicodeDecodeError):
            raa = ""

    print("Mangler stadig:")
    for navn in mangler:
        if navn in raa:
            print(f"  {navn}   <- står i filen, men blev ikke læst som en nøgle")
        else:
            print(f"  {navn}")

    if any(n in raa for n in mangler):
        print()
        print("Linjen skal være NAVN=værdi — uden `set` foran, uden mellemrum")
        print("omkring = og uden anførselstegn medmindre værdien indeholder #.")
    elif raa:
        print()
        print("Navnene optræder slet ikke i filen. Tjek stavemåden,")
        print("og at du redigerer den .env der nævnes øverst.")
    return 2


_VARIABEL = re.compile(r"\$\(([A-Za-z_][A-Za-z0-9_]*)(?:\|([^)]*))?\)")
# Variabler med dette præfiks hentes fra miljøet (.env), hvis --var ikke giver dem.
ENV_PRAEFIKS = "TEST_"


def erstat_variabler(sql: str, tildelinger: list[str]) -> tuple[str, list[str]]:
    """Erstat ``$(navn)`` som sqlcmd gør, så scripts kan køres begge veje.

    ``$(navn|standard)`` har en indbygget reserveværdi. Det lader et script
    køre uændret ud af kassen og samtidig tage et rigtigt input — fx et CPR
    der findes i testmiljøet — uden at nogen skal redigere i filen og
    risikere at få det med i en commit.

    Returnerer den færdige SQL og navnene på de variabler der *ikke* blev
    erstattet og heller ikke har en standard. De sidste er vigtige: et
    uerstattet ``$(run_id)`` ender som tekst i en INSERT og giver enten en
    syntaksfejl eller — værre — en afstemningsrække med noget vrøvl i.
    """
    vaerdier: dict[str, str] = {}
    for par in tildelinger:
        navn, skilletegn, vaerdi = par.partition("=")
        if not skilletegn:
            raise KonfigurationsFejl(f"--var skal skrives NAVN=VÆRDI, ikke '{par}'")
        vaerdier[navn.strip()] = vaerdi

    mangler: list[str] = []

    def erstat(m: re.Match[str]) -> str:
        navn, standard = m.group(1), m.group(2)
        if navn in vaerdier:
            return vaerdier[navn]
        if navn.startswith(ENV_PRAEFIKS) and (fra_env := os.environ.get(navn, "").strip()):
            # Fra .env, så fx et rigtigt sagsnummer aldrig skal skrives på
            # kommandolinjen eller i filen. Kun TEST_-navne: et script må
            # aldrig kunne trække et kodeord ind i SQL'en. Apostroffer
            # fordobles, da værdien står inde i en N'...'-streng.
            return fra_env.replace("'", "''")
        if standard is not None:
            return standard
        mangler.append(navn)
        return m.group(0)

    return _VARIABEL.sub(erstat, sql), sorted(set(mangler))


GO_KILDE = "GOStaging.dbo"
_SKEMANAVN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?")


def skift_kilde(sql: str, kilde: str) -> str:
    """Peg extract på en anden kilde end ``GOStaging.dbo``.

    Til simulering: tabeller med præcis GOStaging's form, men falske data
    (sql/998_gofake_kilde.sql). Selve extract-filen ændres ikke, så den
    stadig kan køres i SSMS mod det rigtige dump.

    Kun ``GOStaging.dbo.`` foran et tabelnavn erstattes. Navnet valideres:
    det sættes ind i SQL, og et skemanavn kan ikke være en parameter.
    """
    if not _SKEMANAVN.fullmatch(kilde):
        raise KonfigurationsFejl(
            f"--kilde skal være SKEMA eller DATABASE.SKEMA, ikke '{kilde}'")
    if kilde.lower() == GO_KILDE.lower():
        return sql
    antal = sql.count(f"{GO_KILDE}.")
    if not antal:
        raise KonfigurationsFejl(f"Scriptet læser ikke fra {GO_KILDE} — --kilde giver ingen mening her")
    return sql.replace(f"{GO_KILDE}.", f"{kilde}.")


def _sniff_kodning(sti: Path) -> str:
    """Gæt filens tegnkodning ud fra dens BOM.

    Nødvendigt på Windows. PowerShell 5.1's ``Out-File`` og ``>`` skriver
    **UTF-16LE** som standard, og Notepad kan gemme UTF-8 med BOM. Læser man
    sådan en fil som almindelig UTF-8, bliver første nøgle til noget i retning
    af ``\\ufeffKONV_DB_SERVER`` eller ren volapyk — og så ser variablen ud
    til at mangle, selv om den tydeligvis står i filen.
    """
    try:
        start = sti.read_bytes()[:4]
    except OSError:
        return "utf-8"

    if start.startswith(b"\xff\xfe"):
        return "utf-16-le"
    if start.startswith(b"\xfe\xff"):
        return "utf-16-be"
    if start.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    # UTF-16 uden BOM: ASCII-tegn og nulbyte skiftevis. Rækkefølgen afslører
    # byteorden. En gyldig UTF-8-fil begynder aldrig med en nulbyte.
    if len(start) >= 2:
        if start[0] != 0 and start[1] == 0:
            return "utf-16-le"
        if start[0] == 0 and start[1] != 0:
            return "utf-16-be"
    return "utf-8"


@contextmanager
def _opsamlede_dotenv_advarsler() -> Iterator[list[str]]:
    """Opsaml python-dotenvs "could not parse statement"-advarsler.

    Dotenv springer en linje den ikke forstår over og skriver en advarsel —
    men advarslen havner i logningen, mens følgen ("Miljøvariablen X mangler")
    kommer et andet sted fra. Så ser man to beskeder der ikke ligner hinanden,
    og leder efter X i en fil hvor X står helt tydeligt.
    """
    linjer: list[str] = []

    class Opsamler(logging.Handler):
        def emit(self, post: logging.LogRecord) -> None:
            if "could not parse" in post.getMessage().lower():
                linjer.extend(str(a) for a in post.args or ())

    logger = logging.getLogger("dotenv")
    handler = Opsamler()
    tidligere = logger.propagate
    logger.addHandler(handler)
    logger.propagate = False  # undgå at den samme advarsel også rammer stderr
    try:
        yield linjer
    finally:
        logger.removeHandler(handler)
        logger.propagate = tidligere


def _indlaes_env_fil() -> str:
    """Læs .env hvis den findes. Returnerer en forklaring på hvad der skete.

    ``override=False`` er vigtigt: allerede satte miljøvariabler vinder. Det
    betyder at Automation Servers credential-store og en eksplicit
    ``set KONV_MILJOE=prod`` ikke kan blive overskrevet af en glemt .env-fil
    på en udviklermaskine.

    Returværdien bruges i fejlbeskeden når en variabel mangler. Uden den er
    "Miljøvariablen KONV_DB_SERVER mangler" en blindgyde: man kan ikke se om
    .env slet ikke blev fundet, eller om den blev læst og bare var mangelfuld.
    """
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return "python-dotenv er ikke installeret — kør `uv sync` igen"

    sti = find_dotenv(usecwd=True)
    if sti:
        kodning = _sniff_kodning(Path(sti))
        with _opsamlede_dotenv_advarsler() as advarsler:
            load_dotenv(sti, override=False, encoding=kodning)
        log.debug("Indlæste miljøvariabler fra %s (%s)", sti, kodning)
        if kodning.startswith("utf-16"):
            return (f".env læst fra {sti} — filen er {kodning.upper()}, "
                    f"formentlig gemt med PowerShells Out-File. Den virker, men "
                    f"gem den hellere som UTF-8: Set-Content -Encoding utf8")
        if advarsler:
            linjer = ", ".join(advarsler)
            return (f".env læst fra {sti} ({kodning}) — men linje {linjer} kunne "
                    f"IKKE tolkes og blev sprunget over. Alt på de linjer er "
                    f"ikke-sat. Et NAVN=værdi pr. linje; værdier med mellemrum "
                    f"eller # skal i anførselstegn")
        return f".env læst fra {sti} ({kodning})"

    # Ingen .env fundet. De tre hyppigste årsager, i rækkefølge efter
    # hvor ofte de rammer folk på Windows.
    mappe = Path.cwd()
    if (mappe / ".env.txt").exists():
        return (f"Fandt {mappe / '.env.txt'} — men filen skal hedde .env uden "
                f"endelse. Notepad tilføjer .txt medmindre man vælger "
                f"\"Alle filer\" i Gem som")
    if (mappe / ".env.example").exists():
        return (f"Ingen .env i {mappe} — kun .env.example. "
                f"Kopiér den: copy .env.example .env")
    return f"Ingen .env fundet i {mappe} eller mapper over den"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser("konv", description="GO -> SBSYS konvertering")
    p.add_argument("--live", action="store_true",
                   help="Send rigtigt til SBSYS. Uden dette flag køres dry-run.")
    under = p.add_subparsers(dest="kommando", required=True)

    db = under.add_parser("db").add_subparsers(dest="db_kommando", required=True)
    db.add_parser("init", help="Kør sql/0*.sql — opretter schemas, tabeller og views")
    d_koer = db.add_parser("koer", help="Kør en vilkårlig .sql-fil (erstatter sqlcmd)")
    d_koer.add_argument("fil", type=Path)
    d_koer.add_argument("--var", action="append", default=[], metavar="NAVN=VÆRDI",
                        help="Erstat $(NAVN) i scriptet. Kan gentages")
    d_koer.add_argument("--kilde", default=None, metavar="SKEMA",
                        help="Læs fra en anden kilde end GOStaging.dbo, fx "
                             "SBSYS_KONV.gofake (falske data med GOStaging's form)")
    d_koer.add_argument("--raekker", type=int, default=40,
                        help="Højst så mange rækker pr. resultatsæt. 0 = alle")
    d_nul = db.add_parser(
        "nulstil",
        help="Tøm KONVERTERINGSDATABASEN, så den kan bruges til næste dump")
    d_nul.add_argument("--jatak", action="store_true",
                       help="Uden dette flag vises kun hvad der ville blive slettet")
    d_nul.add_argument("--ogsaa-mapning", action="store_true",
                       help="Ryd også map.kodeliste. Det er fagpersonernes arbejde — "
                            "som regel skal det BLIVE")
    d_nul.add_argument("--ud", type=Path, default=None,
                       help="Hvor listen over det indlæste gemmes først. "
                            "Standard: rapporter/oprettede-foer-nulstilling.csv")

    mapping = under.add_parser("mapping").add_subparsers(dest="mapping_kommando", required=True)
    mapping.add_parser("load", help="Indlæs mapping/*.csv i map.kodeliste")
    m_mangler = mapping.add_parser("mangler", help="Eksportér uafklarede værdier")
    m_mangler.add_argument("--ud", type=Path, default=Path("rapporter/mangler.csv"))
    m_gem = mapping.add_parser(
        "gem", help="Skriv map.kodeliste tilbage til mapping/*.csv (modsat load)")
    m_gem.add_argument("--ud", type=Path, default=MAPPING_MAPPE,
                       help="Mappe at skrive til. Standard: mapping/")
    m_forbered = mapping.add_parser(
        "forbered",
        help="Byg kodelisterne: GO's værdier fra stg.*, SBSYS' fra API'et, "
             "parret hvor navnet er entydigt")
    m_forbered.add_argument("--liste", default=None, help="Kun denne liste")
    m_forbered.add_argument("--ud", type=Path, default=Path("rapporter/kodelister"))

    filer = under.add_parser("filer").add_subparsers(dest="filer_kommando", required=True)
    f_scan = filer.add_parser("scan", help="Find og checksum dokumentfiler")
    f_scan.add_argument("--graense", type=int, default=None)
    f_scan.add_argument("--boelge", default=None, help="Scan kun én bølges dokumenter")
    f_scan.add_argument("--uden-checksum", action="store_true",
                        help="Spring SHA-256 over — meget hurtigere ved første gennemløb")

    load = under.add_parser("load", help="Indlæs en objekttype i SBSYS")
    load.add_argument("objekt_type", choices=sorted(LOADERS))
    load.add_argument("--graense", type=int, default=None)
    load.add_argument("--noegle", default=None, help="Kør kun ét objekt")
    load.add_argument("--boelge", default=None,
                      help="Kør kun én bølge, fx ppr eller dagtilbud-4")
    load.add_argument("--vis-payload", action="store_true",
                      help="Udskriv den payload der ville blive sendt (kun i dry-run)")
    # --live findes BÅDE her og på hovedparseren. argparse kræver ellers at
    # globale flag står før underkommandoen (`konv --live load sag`), og det
    # er ikke til at gætte. SUPPRESS er nødvendigt: uden den ville dette
    # flags default (False) overskrive et --live givet før underkommandoen.
    load.add_argument("--live", action="store_true", default=argparse.SUPPRESS,
                      help="Send rigtigt til SBSYS. Uden dette flag køres dry-run.")

    b = under.add_parser("boelge").add_subparsers(dest="boelge_kommando", required=True)
    b.add_parser("list", help="Vis de ti bølger og deres status")
    b_gate = b.add_parser("gate", help="Er bølgen klar til undervisning?")
    b_gate.add_argument("--boelge", default=None)
    b_afstem = b.add_parser("afstem", help="Markér en bølge som afstemt")
    b_afstem.add_argument("boelge")
    b_afstem.add_argument("--af", required=True, help="Hvem godkender afstemningen")

    nul = under.add_parser(
        "nulstil", help="Send afviste eller fejlede objekter tilbage i køen")
    nul.add_argument("--objekt-type", choices=sorted(LOADERS), default=None)
    nul.add_argument("--boelge", default=None)
    nul.add_argument("--noegle", default=None, help="Én bestemt kilde-nøgle")
    nul.add_argument("--regel", default=None,
                     help="Kun objekter afvist med denne regel, fx manglende_mapping")
    nul.add_argument("--status", default=None,
                     help="Kun denne status, fx failed eller manual")
    nul.add_argument("--ogsaa-indlaeste", action="store_true",
                     help="Nulstil også 'loaded'. Kun når objektet FAKTISK er "
                          "slettet i SBSYS — ellers opstår der dubletter")

    opslag = under.add_parser(
        "opslag", help="Hent SBSYS' egne lister (afdelinger, skabeloner) som CSV")
    opslag.add_argument(
        "hvad",
        choices=["afdelinger", "skabeloner", "statusser", "dokumentarter", "brugere", "mig",
                 "katalog", "tilbud"])
    opslag.add_argument("--ud", type=Path, default=None,
                        help="Standard: rapporter/<hvad>-fra-sbsys.csv")

    opryd = under.add_parser(
        "oprettede",
        help="Liste over alt konverteringen har oprettet i SBSYS")
    opryd.add_argument("--boelge", default=None)
    opryd.add_argument("--run-id", type=int, default=None)
    opryd.add_argument("--objekt-type", choices=sorted(LOADERS), default=None)
    opryd.add_argument("--ud", type=Path, default=Path("rapporter/oprettede.csv"))

    gen = under.add_parser(
        "genskab",
        help="Genskab en linje i id_map mellem en kilde-nøgle og et SBSYS-id")
    gen.add_argument("--objekt-type", choices=sorted(LOADERS), required=True)
    gen.add_argument("--noegle", required=True, help="Kilde-nøglen fra GO")
    gen.add_argument("--sbsys-id", required=True, help="Objektets id i SBSYS")
    gen.add_argument("--nummer", default=None, help="Sagsnummer, til menneskelig afstemning")
    gen.add_argument("--boelge", default=None)

    kald = under.add_parser(
        "kald", help="Kald ét endpoint og vis svaret — til at afdække en snitflade")
    kald.add_argument("metode", help="GET, OPTIONS, POST ...")
    kald.add_argument("sti", help="Fx /bu_api/sag_foranstaltning")
    kald.add_argument("--base", default=None, help="Anden vært end SBSYS_BASE_URL")
    kald.add_argument("--bu", action="store_true",
                      help="Kald Børn og Unge-API'et (værten i SBSYS_BU_BASE_URL)")
    kald.add_argument("--krop", type=Path, default=None, help="JSON-fil som request-body")
    kald.add_argument("--ud", type=Path, default=None, metavar="FIL",
                      help="Skriv HELE svaret til en fil (UTF-8). Skærmen viser kun "
                           "de første 20.000 tegn")
    kald.add_argument("--jatak", action="store_true",
                      help="Påkrævet for POST/PUT/PATCH/DELETE — de kan ændre data")

    under.add_parser("env", help="Vis hvilken .env der læses, og hvad der mangler")

    vaerdi = under.add_parser(
        "vaerdier", help="Værdimapping trin 1: saml GO's stavemåder til de værdier der er brug for")
    v_under = vaerdi.add_subparsers(dest="vaerdier_kommando", required=True)
    v_saml = v_under.add_parser(
        "saml", help="Læs distinkte_vaerdier.xlsx og foreslå hvilke stavemåder der er samme værdi")
    v_saml.add_argument("fil", type=Path, help="Excel-fil med én fane pr. GO-kolonne")
    v_saml.add_argument("--ud", type=Path, default=None,
                        help="Mappe til gennemsynet. Standard: samme mappe som Excel-filen")
    v_saml.add_argument("--tidligere", type=Path, default=None, metavar="GENNEMSYN",
                        help="Et tidligere gennemgået *-gennemsyn.xlsx: dine afgørelser "
                             "kopieres ind, så kun nye GO-værdier skal gennemgås")
    v_saml.add_argument("--overskriv", action="store_true",
                        help="Overskriv eksisterende filer — også en gennemgang du er i gang med")
    v_map = v_under.add_parser(
        "mapning", help="Trin 2: læs det gennemgåede gennemsyn og byg mappingrækker mod SBSYS")
    v_map.add_argument("fil", type=Path, help="Den gennemgåede *-gennemsyn.xlsx")
    v_map.add_argument("--sbsys", action="append", default=[], metavar="LISTE=FIL",
                       help="SBSYS' valgmuligheder som JSON, fx "
                            "bevillingssted=rapporter/bevillingssted-liste-prod.json. Kan gentages")
    v_map.add_argument("--hent-sbsys", action="store_true",
                       help="Hent SBSYS' valglister (bevillingssted, kommune, klassetrin) "
                            "direkte fra BU-API'et med GET i stedet for --sbsys-filer")
    v_map.add_argument("--ud", type=Path, default=None,
                       help="Mappe til resultatet. Standard: samme mappe som gennemsynet")
    v_map.add_argument("--flet", action="append", default=[], metavar="LISTE",
                       help="Flet listens 'klar'-rækker ind i mapping/tilbudsfeltvaerdi.csv "
                            "(eksisterende nøgler erstattes). Kan gentages")

    v_sb = v_under.add_parser(
        "sagsbehandlere", help="Rens GO's sagsbehandlere til AZ-identer mod LIS' brugerliste")
    v_sb.add_argument("fil", type=Path,
                      help="distinkte_vaerdier.xlsx (kun fanen Sagsbehandler læses)")
    v_sb.add_argument("--brugerliste", type=Path, required=True,
                      help="LIS' brugerhistorik (IDENT, NAVN, STARTDATO, SLUTDATO, AKTIV)")
    v_sb.add_argument("--tidligere", type=Path, default=None, metavar="GENNEMSYN",
                      help="Et tidligere sagsbehandlere-gennemsyn.xlsx: afgørelserne kopieres ind")
    v_sb.add_argument("--ud", type=Path, default=None,
                      help="Mappe til gennemsynet. Standard: samme mappe som fil. "
                           "Aldrig i repoet (persondata)")
    v_sb.add_argument("--overskriv", action="store_true")

    spec = under.add_parser(
        "spec", help="Læs SBSYS' egen OpenAPI-beskrivelse — stier, felter, nøglekandidater")
    spec.add_argument("hvad", choices=["stier", "noegle", "felt", "model", "soegning", "gem"],
                      help="stier: list endpoints | noegle: felter der kan bære "
                           "GO-nøglen | felt: hvor et felt kan SKRIVES og SØGES | "
                           "model: felter og krav på én model | "
                           "soegning: hvad der kan søges på | gem: skriv dokumentet til disk")
    spec.add_argument("filter", nargs="?", default="",
                      help="Tekst der skal indgå i stien eller modelnavnet")
    spec.add_argument("--version", default="default",
                      help="Dokumentnavn, fx default eller v10")
    spec.add_argument("--praefiks", default="",
                      help="Sti foran dokumentet, når det ligger under en anden "
                           "sti på SAMME vært")
    spec.add_argument("--base", default=None,
                      help="Helt anden vært, fx Børn og Unge-API'ets: "
                           "https://<vaert>/bu_api. Tokenet genbruges")
    spec.add_argument("--ud", type=Path, default=Path("rapporter/sbsys-spec.json"))

    status = under.add_parser("status", help="Status pr. bølge og objekttype")
    status.add_argument("--boelge", default=None)
    status.add_argument("--noegle", default=None,
                        help="Vis ét objekt: hvad blev det til i SBSYS, og hvornår")
    under.add_parser("selvtest", help="Kontrollér konfiguration og forbindelser")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    env_status = _indlaes_env_fil()

    # Skal kunne køres NÅR konfigurationen fejler — derfor før valideringen.
    if args.kommando == "env":
        return _env_diagnose(env_status)
    # Læser kun en Excel-fil og skriver CSV — ingen database, ingen SBSYS.
    if args.kommando == "vaerdier":
        if args.vaerdier_kommando == "mapning":
            return _vaerdier_mapning(args)
        if args.vaerdier_kommando == "sagsbehandlere":
            return _vaerdier_sagsbehandlere(args)
        return _vaerdier_saml(args)

    try:
        settings = Settings.fra_environment()
    except KonfigurationsFejl as e:
        print(f"Konfigurationsfejl: {e}", file=sys.stderr)
        print(f"  {env_status}", file=sys.stderr)
        return 2

    if args.live:
        settings = replace(settings, dry_run=False)
        # Valider igen: nogle krav gælder kun når der FAKTISK sendes, og
        # de skal fejle her — ikke efter 3.000 vellykkede sager.
        try:
            settings.valider()
        except KonfigurationsFejl as e:
            print(f"Konfigurationsfejl: {e}", file=sys.stderr)
            print(f"  {env_status}", file=sys.stderr)
            return 2
    opsaet_logging(settings)

    if settings.dry_run:
        log.info("DRY-RUN — der sendes intet til SBSYS. Brug --live for rigtig kørsel.")
    elif settings.er_produktion:
        log.warning("LIVE mod PRODUKTION (%s)", settings.sbsys.base_url)

    try:
        return _udfoer(args, settings)
    except AutentifikationsFejl as e:
        log.error("Kørsel stoppet: %s", e)
        return 3
    except (boelge.UkendtBoelge, boelge.TomBoelge) as e:
        # Fanges separat, fordi det er en betjeningsfejl og ikke en systemfejl:
        # brugeren skal se beskeden, ikke en stacktrace.
        log.error("%s", e)
        return 4
    except KonverteringsFejl as e:
        log.error("Kørsel stoppet: %s", e)
        return 5
    except KeyboardInterrupt:
        log.warning("Afbrudt. Intet er i en halv tilstand — kør igen for at fortsætte.")
        return 130


def _udfoer(args, settings: Settings) -> int:
    if args.kommando == "db":
        db = Database(settings.db)

        if args.db_kommando == "koer":
            if not args.fil.exists():
                log.error("Filen findes ikke: %s", args.fil)
                return 2
            sql, mangler = erstat_variabler(args.fil.read_text(encoding="utf-8"), args.var)
            if args.kilde:
                sql = skift_kilde(sql, args.kilde)
                log.info("Kilden er %s i stedet for %s", args.kilde, GO_KILDE)
            if mangler:
                log.warning("Uerstattede variabler i scriptet: %s", ", ".join(mangler))
            log.info("Kører %s", args.fil.name)
            resultater = db.koer_script(sql, args.fil.name, vis_resultater=True)
            for saet in resultater:
                _udskriv_tabel(saet, args.raekker)
            return 0

        if args.db_kommando == "nulstil":
            return _db_nulstil(db, args)

        for fil in sorted(SQL_MAPPE.glob("0*.sql")):
            log.info("Kører %s", fil.name)
            db.koer_script(fil.read_text(encoding="utf-8"), fil.name)
        return 0

    if args.kommando == "mapping":
        # Hver underkommando nævnes ved navn. Et `else` her betød at
        # `mapping forbered` stilfærdigt kørte `mangler` i stedet — den
        # svarede endda venligt "0 uafklarede værdier".
        if args.mapping_kommando == "forbered":
            return _forbered_kodelister(settings, args)

        db = Database(settings.db)
        if args.mapping_kommando == "load":
            antal = indlaes_mapping(db, MAPPING_MAPPE, settings.miljoe)
            log.info("Indlæste %d mapping-rækker (miljø: %s)", antal, settings.miljoe)
            return 0
        if args.mapping_kommando == "gem":
            talt = skriv_mappe(db, args.ud)
            for liste, antal in sorted(talt.items()):
                log.info("%-24s %4d rækker -> %s", liste, antal, args.ud / f"{liste}.csv")
            log.info("%d lister skrevet til %s", len(talt), args.ud)
            return 0
        if args.mapping_kommando == "mangler":
            antal = skriv_mangler_csv(db, args.ud)
            log.info("%d uafklarede værdier skrevet til %s", antal, args.ud)
            return 1 if antal else 0   # så en pipeline kan fejle på uafklaret mapping

        log.error("Ukendt mapping-kommando: %s", args.mapping_kommando)
        return 2

    if args.kommando == "filer":
        return _scan_filer(settings, args)

    if args.kommando == "boelge":
        return _boelge(settings, args)

    if args.kommando == "nulstil":
        db = Database(settings.db)
        boelge.valider(db, args.boelge)
        antal = IdMap(db).nulstil(
            objekt_type=args.objekt_type,
            boelge=args.boelge,
            kilde_noegle=args.noegle,
            regel=args.regel,
            status=args.status,
            ogsaa_indlaeste=args.ogsaa_indlaeste,
        )
        log.info("%d objekter sat tilbage til 'pending'", antal)
        if args.ogsaa_indlaeste:
            log.warning("Indlæste objekter blev også nulstillet. Er de ikke "
                        "slettet i SBSYS, opretter næste kørsel dubletter.")
        return 0

    if args.kommando == "status":
        db = Database(settings.db)
        if args.noegle:
            raekker = db.hent_alle(
                """
                SELECT objekt_type, boelge, status, sbsys_id, sbsys_nummer,
                       forsoeg, sidste_fejl, run_id, opdateret
                FROM   konv.id_map
                WHERE  kilde_noegle = ?
                ORDER  BY objekt_type
                """,
                args.noegle,
            )
            if not raekker:
                print(f"{args.noegle} findes ikke i konv.id_map — "
                      f"objektet er aldrig forsøgt indlæst.")
                return 1
            for raekke in raekker:
                for navn, vaerdi in raekke.items():
                    if vaerdi not in (None, ""):
                        print(f"  {navn:<14} {vaerdi}")
                print()
            return 0
        sql = "SELECT * FROM konv.v_status"
        raekker = (
            db.hent_alle(sql + " WHERE boelge = ? ORDER BY objekt_type", args.boelge)
            if args.boelge
            else db.hent_alle(sql + " ORDER BY boelge, objekt_type")
        )
        for r in raekker:
            log.info(
                "%-20s %-10s indlæst=%-8d fejlet=%-6d manuel=%-6d venter=%-8d i alt=%d",
                r["boelge"], r["objekt_type"], r["indlaest"], r["fejlet"],
                r["manuel"], r["venter"], r["i_alt"],
            )
        return 0

    if args.kommando == "kald":
        return _kald(settings, args)

    if args.kommando == "oprettede":
        return _oprettede(settings, args)

    if args.kommando == "genskab":
        # Når forbindelsen mellem GO-nøgle og SBSYS-objekt er gået tabt —
        # en forkert sletning, en afbrudt kørsel hvor svaret forsvandt —
        # er alternativet at objektet oprettes igen som en dublet. Her kan
        # båndet knyttes igen, når man ved hvad id'et er.
        db = Database(settings.db)
        idmap = IdMap(db)
        objekt_type = Objekttype(args.objekt_type)
        eksisterende = idmap.sbsys_id(objekt_type, args.noegle)
        if eksisterende and str(eksisterende) != str(args.sbsys_id):
            print(f"{args.noegle} peger allerede på {eksisterende} i SBSYS. "
                  f"Ret den ikke til {args.sbsys_id} uden at vide hvad der "
                  f"står hvor — det ene af de to objekter bliver forældreløst.",
                  file=sys.stderr)
            return 1
        idmap.reserver(objekt_type, args.noegle, "", None, args.boelge)
        idmap.markér_indlaest(objekt_type, args.noegle, str(args.sbsys_id), args.nummer)
        log.info("%s %s peger nu på SBSYS-id %s", args.objekt_type,
                 kort_noegle(args.noegle), args.sbsys_id)
        return 0

    if args.kommando == "opslag":
        return _opslag(settings, args)

    if args.kommando == "spec":
        return _spec(settings, args)

    if args.kommando == "selvtest":
        return _selvtest(settings)

    if args.kommando == "load":
        return _load(settings, args)

    return 0


def _vaerdier_mapning(args: argparse.Namespace) -> int:
    from collections import Counter

    from . import vaerdimapning as vm

    if not args.fil.exists():
        print(f"Findes ikke: {args.fil}", file=sys.stderr)
        return 2
    sbsys: dict[str, list[dict]] = {}
    for tildeling in args.sbsys:
        liste, _, fil = tildeling.partition("=")
        if liste not in vm.SBSYS_LISTE_ID or not fil:
            print(f"--sbsys skal være LISTE=FIL med LISTE en af {sorted(vm.SBSYS_LISTE_ID)}",
                  file=sys.stderr)
            return 2
        sbsys[liste] = vm.laes_sbsys_liste(Path(fil))
    if args.hent_sbsys:
        try:
            hentet = _hent_sbsys_lister([liste for liste in vm.HENTES_AUTOMATISK
                                         if liste not in sbsys])
        except (KonfigurationsFejl, KonverteringsFejl, RuntimeError) as e:
            print(f"Kunne ikke hente SBSYS' lister: {e}", file=sys.stderr)
            return 2
        sbsys |= {liste: vm.laes_sbsys_liste_data(data) for liste, data in hentet.items()}
    try:
        valg = vm.laes_gennemsyn(args.fil)
    except ValueError as e:
        print(f"Intet skrevet. {e}", file=sys.stderr)
        return 1
    res = vm.byg(valg, sbsys)
    raekker = vm.mapping_raekker(res, args.fil.name)
    konflikter = vm.konflikter_med_mapping(
        raekker, vm.eksisterende(SQL_MAPPE.parent / "mapping" / "tilbudsfeltvaerdi.csv"))

    mappe = args.ud or args.fil.parent
    stamme = args.fil.stem.removesuffix("-gennemsyn")
    xlsx = vm.skriv_xlsx(res, konflikter, mappe / f"{stamme}-mapning.xlsx")
    ud_csv = vm.skriv_csv(raekker, mappe / f"{stamme}-tilbudsfeltvaerdi.csv")
    muligheder = vm.vaerdilistemuligheder(res)
    ud_muligheder = vm.skriv_muligheder_xlsx(
        muligheder, mappe / f"{stamme}-vaerdilistemuligheder.xlsx")

    print(f"{'liste':16}{'stavemåder':>11}{'klar':>7}{'mangler_i_sbsys':>17}"
          f"{'ikke_tjekket':>14}{'uafklaret':>11}{'ingen_tekstmatch':>18}")
    for liste in sorted({v.liste for v in valg}):
        s = Counter(v.status for v in valg if v.liste == liste)
        print(f"{liste:16}{sum(s.values()):>11}{s[vm.KLAR]:>7}{s[vm.MANGLER_I_SBSYS]:>17}"
              f"{s[vm.IKKE_TJEKKET]:>14}{s[vm.UAFKLARET]:>11}{s[vm.INGEN_TEKSTMATCH]:>18}")
    print(f"\n  {len(res.tjek)} ting at tjekke, {len(konflikter)} konflikter med den nuværende mapping")
    print(f"  oversigt:      {xlsx}")
    print(f"  mappingrækker: {ud_csv} ({len(raekker)} rækker, kun 'klar')")
    typer = Counter(m["noegletype"] for m in muligheder)
    print(f"  nøgletabel:    {ud_muligheder} ({len(muligheder)} rækker: "
          + ", ".join(f"{k} {n}" for k, n in sorted(typer.items())) + ")")
    if not sbsys:
        print("\nIngen SBSYS-lister givet, så intet er 'klar'. Brug --sbsys LISTE=FIL.")
    for liste in args.flet:
        if liste not in sbsys:
            print(f"--flet {liste}: listen er ikke tjekket mod SBSYS (mangler --sbsys "
                  f"{liste}=FIL). Intet flettet.", file=sys.stderr)
            return 1
        kolonner = vm.kolonner_for(liste)
        valgte = [r for r in raekker if r["go_vaerdi"].split("|", 1)[0] in kolonner]
        maal = SQL_MAPPE.parent / "mapping" / "tilbudsfeltvaerdi.csv"
        # GO-værdier i listen der IKKE længere er klar: deres gamle map-række skal væk,
        # ellers mapper konverteringen stadig til det gamle mål.
        ikke_klar = {f"{kolonne}|{v.go_vaerdi}" for v in valg
                     if v.liste == liste and v.status != vm.KLAR for kolonne in v.go_kolonner}
        erstattet, tilfoejet, fjernet = vm.flet(maal, valgte, ikke_klar)
        print(f"\nFlettet {liste} ind i {maal}: {len(erstattet)} erstattet, "
              f"{len(tilfoejet)} tilføjet, {len(fjernet)} fjernet")
        for k in tilfoejet:
            print(f"  tilføjet:  {k}")
        for k in fjernet:
            print(f"  fjernet:   {k}")
    return 0


def _hent_sbsys_lister(lister: list[str]) -> dict[str, object]:
    """GET af SBSYS' valgmuligheder for hver liste via BU-API'et. Kun læsning."""
    from . import vaerdimapning as vm

    settings = Settings.fra_environment()
    sbsys = settings.sbsys
    if not sbsys.bu_base_url:
        raise RuntimeError("SBSYS_BU_BASE_URL er ikke sat i .env")
    sbsys = replace(sbsys, base_url=sbsys.bu_base_url)
    print(f"Henter valglister fra {sbsys.base_url}")
    kontekst = byg_kontekst(replace(settings, sbsys=sbsys))
    ud: dict[str, object] = {}
    try:
        for liste in lister:
            nr = vm.SBSYS_LISTE_ID[liste]
            svar = kontekst.klient.kald(
                "GET", f"/bu_api/tilbudfeltvaerdiliste/{nr}/tilbudfeltvaerdilistemulighed",
                operation="vaerdier_mapning", forventede_status=(200,))
            ud[liste] = svar.krop
            antal = len(svar.krop) if isinstance(svar.krop, list) else "?"
            print(f"  {liste:15} liste {nr:>3}: {antal} valgmuligheder")
    finally:
        kontekst.klient.luk()
    return ud


def _vaerdier_sagsbehandlere(args: argparse.Namespace) -> int:
    """Persondata: kun ANTAL i terminalen. Navnene står kun i Excel-filen."""
    from . import sagsbehandlere as sb

    for sti in (args.fil, args.brugerliste, args.tidligere):
        if sti is not None and not sti.exists():
            print(f"Findes ikke: {sti}", file=sys.stderr)
            return 2
    mappe = (args.ud or args.fil.parent).resolve()
    repo = SQL_MAPPE.parent.resolve()
    if mappe == repo or repo in mappe.parents:
        print(f"Gennemsynet indeholder navne og må ikke ligge i repoet ({repo}). "
              f"Brug --ud til en mappe udenfor.", file=sys.stderr)
        return 2
    ud = mappe / "sagsbehandlere-gennemsyn.xlsx"
    if args.tidligere and args.tidligere.resolve() == ud:
        print(f"Det nye gennemsyn ville overskrive det tidligere ({ud}).\n"
              f"Omdøb det tidligere først (fx til ...-runde1.xlsx), eller brug --ud.",
              file=sys.stderr)
        return 1
    if ud.exists() and not args.overskriv:
        print(f"{ud} findes allerede — måske med din gennemgang i. Intet er overskrevet.\n"
              f"Brug --overskriv, eller --ud til en anden mappe.", file=sys.stderr)
        return 1
    try:
        personer = sb.laes_brugerliste(args.brugerliste)
        go = sb.laes_go_sagsbehandlere(args.fil)
    except ValueError as e:
        print(f"Intet skrevet. {e}", file=sys.stderr)
        return 1
    print(f"Brugerliste: {len(personer)} personer (AZ-identer), "
          f"{sum(p.aktiv for p in personer.values())} aktive")
    print(f"GO: {len(go)} forskellige sagsbehandler-værdier")
    raekker = sb.saml(go, personer)
    forsvundne: list[str] = []
    if args.tidligere:
        try:
            forsvundne = sb.overfoer(raekker, args.tidligere)
        except ValueError as e:
            print(f"Intet skrevet. Det tidligere gennemsyn kunne ikke læses: {e}", file=sys.stderr)
            return 1
    sb.skriv(raekker, personer, ud)

    status, grund = sb.opsummering(raekker)
    print(f"\n  auto {status[sb.AUTO]}   forslag {status[sb.FORSLAG]}   afklar {status[sb.AFKLAR]}")
    for g, n in grund.most_common():
        print(f"  {n:>6}  {g}")
    if args.tidligere:
        from collections import Counter
        o = Counter(r.overfoert for r in raekker)
        print(f"\n  Overført: {o['overført'] + o['overført, forslag ændret']}, "
              f"tidligere uafklaret: {o['tidligere uafklaret']}, NY: {o['ny']}"
              + (f", forsvundet fra GO: {len(forsvundne)}" if forsvundne else ""))
    print(f"\n  skrevet: {ud}")
    print("\n'forslag' godkendes med x i 'godkendt'. 'afklar': skriv AZ-identen i "
          "'rettet_vaerdi'. Uafklarede får en konvertering_-nøgle.")
    return 0


def _vaerdier_saml(args: argparse.Namespace) -> int:
    from collections import Counter

    from . import vaerdier

    if not args.fil.exists():
        print(f"Findes ikke: {args.fil}", file=sys.stderr)
        return 2
    faner, sprunget_over = vaerdier.laes_xlsx(args.fil)
    for fane in sprunget_over:
        print(f"Springer fanen {fane} over: {vaerdier.UDELADTE_FANER[fane]}")
    try:
        resultat = vaerdier.saml(faner)
    except ValueError as e:
        print(f"Intet skrevet. {e}", file=sys.stderr)
        return 1
    sti = vaerdier.gennemsyn_sti(args.fil, args.ud)
    if args.tidligere and not args.tidligere.exists():
        print(f"Findes ikke: {args.tidligere}", file=sys.stderr)
        return 2
    if args.tidligere and args.tidligere.resolve() == sti.resolve():
        print(f"Det nye gennemsyn ville overskrive det tidligere ({sti}) — dine afgørelser.\n"
              f"Omdøb det tidligere først (fx til ...-gennemsyn-runde1.xlsx), eller brug --ud.",
              file=sys.stderr)
        return 1
    if sti.exists() and not args.overskriv:
        print(f"{sti} findes allerede — måske med din gennemgang i. Intet er overskrevet.\n"
              f"Brug --overskriv, eller --ud til en anden mappe.", file=sys.stderr)
        return 1

    forsvundne: list[tuple[str, str]] = []
    if args.tidligere:
        try:
            forsvundne = vaerdier.overfoer(resultat, args.tidligere)
        except ValueError as e:
            print(f"Intet skrevet. Det tidligere gennemsyn kunne ikke læses: {e}", file=sys.stderr)
            return 1

    print(f"{'liste':16}{'stavemåder':>11}{'værdier':>9}{'auto':>7}{'forslag':>9}{'afklar':>8}")
    for liste, raekker in resultat.items():
        status = Counter(r.status for r in raekker)
        print(f"{liste:16}{len(raekker):>11}{len(vaerdier.vaerdiliste(raekker)):>9}"
              f"{status['auto']:>7}{status['forslag']:>9}{status['afklar']:>8}")
    print(f"  skrevet: {vaerdier.skriv(resultat, sti)}")
    if args.tidligere:
        alle = [r for rs in resultat.values() for r in rs]
        o = Counter(r.overfoert for r in alle)
        print(f"\nOverført fra {args.tidligere.name}:")
        print(f"  {o[vaerdier.OVERFOERT]:>5}  overført - din afgørelse er kopieret ind")
        print(f"  {o[vaerdier.OVERFOERT_AENDRET]:>5}  overført, forslag ændret - "
              f"afgørelsen står i rettet_vaerdi")
        print(f"  {o[vaerdier.TIDLIGERE_UAFKLARET]:>5}  tidligere uafklaret - stadig ikke afgjort")
        print(f"  {o[vaerdier.NY]:>5}  NY - skal gennemgås (filtrér kolonnen 'overfoert' på 'ny')")
        if forsvundne:
            print(f"  {len(forsvundne):>5}  stod i det tidligere gennemsyn, men ikke i det nye "
                  f"udtræk (ingen handling)")
    print("\nÉn fane pr. liste. 'forslag' godkendes (ja/nej i 'godkendt'), "
          "'afklar' kræver en beslutning. 'rettet_vaerdi' overskriver forslaget.")
    return 0


def _db_nulstil(db: Database, args) -> int:
    """Tøm konverteringsdatabasen, så næste dump kan læses ind i et rent bord.

    Det her er VORES database, ikke SBSYS. Værktøjet rører aldrig SBSYS'
    indhold — men her skal der kunne ryddes, for ellers kan det endelige
    dump ikke køres: `konv.id_map` ville stadig sige 'loaded' om objekter
    fra testkørslen, og konverteringen ville springe dem over.

    To værn, fordi handlingen ikke kan fortrydes:

    * Uden ``--jatak`` vises kun hvad der ville ryge. Man ser tallene før
      man beslutter.
    * Listen over det der stod som indlæst, skrives til en CSV FØR
      sletningen. GO-nøglen står ikke i SBSYS, så id_map er det eneste
      sted koblingen findes — og efter nulstillingen er den væk. Filen er
      det sidste spor af hvad testkørslen lagde i SBSYS.
    """
    import csv

    from .idmap import antal_raekker, nulstil_database, oprettede_objekter

    talt = antal_raekker(db)
    i_alt = sum(talt.values())
    for tabel, antal in talt.items():
        if antal:
            log.info("%-20s %8d rækker", tabel, antal)

    if not i_alt:
        log.info("Databasen er allerede tom")
        return 0

    if not args.jatak:
        print()
        print(f"{i_alt} rækker ville blive slettet i KONVERTERINGSDATABASEN.")
        print("SBSYS røres ikke — det er en anden opgave, og ikke vores.")
        print("Kodelisterne i map.kodeliste bevares.")
        print()
        print("Kør igen med --jatak hvis det er meningen.")
        return 2

    # Sidste spor, skrevet FØR sletningen. Efter den findes koblingen
    # mellem GO-nøgle og SBSYS-id ingen steder.
    indlaeste = oprettede_objekter(db)
    if indlaeste:
        ud = args.ud or Path("rapporter/oprettede-foer-nulstilling.csv")
        ud.parent.mkdir(parents=True, exist_ok=True)
        kolonner = ["objekt_type", "kilde_noegle", "sbsys_id", "sbsys_nummer", "boelge", "run_id",
                    "opdateret"]
        with ud.open("w", encoding="utf-8-sig", newline="") as f:
            skriver = csv.DictWriter(f, fieldnames=kolonner, extrasaction="ignore",
                                     delimiter=";")
            skriver.writeheader()
            for r in indlaeste:
                skriver.writerow({k: "" if r.get(k) is None else r[k] for k in kolonner})
        log.warning("%d indlæste objekter gemt i %s — det er det sidste spor af "
                    "hvad der blev lagt i SBSYS", len(indlaeste), ud)

    slettet = nulstil_database(db, ogsaa_mapning=args.ogsaa_mapning)
    for tabel, antal in slettet.items():
        if antal:
            log.info("%-20s %8d slettet", tabel, antal)
    if args.ogsaa_mapning:
        log.info("Konverteringsdatabasen er nulstillet. Bølgerne er bevaret.")
        log.warning("Kodelisterne er også ryddet. Kør `konv mapping load` igen.")
    else:
        log.info("Konverteringsdatabasen er nulstillet. Bølger og kodelister er bevaret.")
    return 0


def _oprettede(settings: Settings, args) -> int:
    """Skriv listen over alt konverteringen har oprettet i SBSYS.

    `konv.id_map` er det eneste sted koblingen mellem en GO-nøgle og et
    SBSYS-id findes — GO-nøglen skrives ikke ind i SBSYS. Uden en udskrift
    kan ingen andre end os svare på hvad en kørsel har lagt derind.

    Tre anvendelser, og den tredje er ikke vores opgave:

    * **Afstemning.** Antal pr. objekttype mod kildens egne tal.
    * **Opslag.** Et SBSYS-id at slå op på, når nogen spørger til én sag.
    * **Oprydning.** Testkonverteringen lander i produktion og skal væk
      igen inden den rigtige kørsel. Sletningen foretages af andre —
      værktøjet her sletter ingenting — men listen kan kun komme herfra.

    Rækkefølgen er omvendt af oprettelsen: indsatser, notater og dokumenter
    før sagerne, så listen også kan læses oppefra og ned af den der rydder.
    """
    import csv

    from .idmap import oprettede_objekter

    db = Database(settings.db)
    if args.boelge:
        boelge.valider(db, args.boelge)
    raekker = oprettede_objekter(
        db, boelge=args.boelge, run_id=args.run_id, objekt_type=args.objekt_type
    )
    if not raekker:
        log.info("Intet oprettet i SBSYS med de filtre")
        return 0

    args.ud.parent.mkdir(parents=True, exist_ok=True)
    kolonner = ["objekt_type", "kilde_noegle", "sbsys_id", "sbsys_nummer", "boelge", "run_id", "opdateret"]
    with args.ud.open("w", encoding="utf-8-sig", newline="") as f:
        skriver = csv.DictWriter(f, fieldnames=kolonner, extrasaction="ignore",
                                 delimiter=";")
        skriver.writeheader()
        for r in raekker:
            skriver.writerow({k: "" if r.get(k) is None else r[k] for k in kolonner})

    pr_type: dict[str, int] = {}
    for r in raekker:
        pr_type[str(r["objekt_type"])] = pr_type.get(str(r["objekt_type"]), 0) + 1
    for navn, antal in pr_type.items():
        log.info("%-10s %6d oprettet i SBSYS", navn, antal)
    log.info("%d objekter skrevet til %s", len(raekker), args.ud)
    print()
    print("Sorteret med børnene først: indsatser, notater og dokumenter før")
    print("sagerne. Konverteringen sletter ikke selv noget — skal objekterne")
    print("fjernes igen, gøres det af dem der rydder miljøet.")
    return 0


def _scan_filer(settings: Settings, args) -> int:
    """Kortlæg fileshare'et før indlæsning. Resultatet er lige så meget en
    leverance til projektledelsen som et teknisk skridt: 'vi mangler 412
    filer' er et tal der skal på bordet tidligt."""
    db = Database(settings.db)
    valgt = boelge.valider(db, args.boelge)
    opslag = FilOpslag(settings.filer)
    filter_sql, params = boelge_filter(valgt)
    opdateringer: list[tuple] = []
    talt = {"ok": 0, "mangler": 0, "tvetydig": 0, "tom": 0, "for_stor": 0}

    # Sideopdelt, ikke streamet: vi skriver fil_status undervejs.
    for raekke in db.hent_i_sider(
        "stg.dokument",
        " AND fil_status IS NULL",
        filter_sql,
        parametre=params,
        graense=args.graense,
    ):
        r = opslag.undersoeg(
            raekke["fil_relativ_sti"],
            beregn_checksum=not args.uden_checksum,
        )
        talt[r.status] = talt.get(r.status, 0) + 1
        opdateringer.append((r.status, r.stoerrelse, r.sha256, r.mime, raekke["kilde_noegle"]))
        if len(opdateringer) >= 1000:
            _gem_filstatus(db, opdateringer)
            opdateringer.clear()

    _gem_filstatus(db, opdateringer)
    log.info("Filscan færdig: %s", talt)
    return 0 if talt.get("mangler", 0) == 0 and talt.get("tvetydig", 0) == 0 else 1


def _gem_filstatus(db: Database, raekker: list[tuple]) -> None:
    """Skriv scanningens resultat på dokumenterne — og kontrollér at det skete.

    Kørt med fast_executemany matchede UPDATE'en ingenting: fil_status stod
    NULL på alle dokumenter, mens loggen meldte "ok: 3, mangler: 1". Et
    dokument uden fil blev derfor stående på arbejdslisten. Nu køres den én
    række ad gangen, og antallet af ændrede rækker skal gå op.

    COALESCE på checksummen: extract har allerede lagt KILDENS checksum
    (Documents.SHACheckSum / DocumentSHA) i fil_sha256. Uden den blev den
    overskrevet af vores egen — og med --uden-checksum af NULL. Se B13.
    """
    if not raekker:
        return
    beroert = db.udfoer_mange(
        "UPDATE stg.dokument SET fil_status = ?, fil_stoerrelse = ?, "
        "fil_sha256 = COALESCE(fil_sha256, ?), mime = ? WHERE kilde_noegle = ?",
        raekker,
    )
    if beroert != len(raekker):
        raise KonverteringsFejl(
            f"Filscanningen skrev kun status på {beroert} af {len(raekker)} dokumenter. "
            f"Resten står uændret i stg.dokument, og et dokument uden fil kan "
            f"derfor komme med på arbejdslisten. Kør `konv filer scan` igen, og "
            f"undersøg sagen hvis tallene stadig ikke går op."
        )


def _boelge(settings: Settings, args) -> int:
    db = Database(settings.db)

    if args.boelge_kommando == "list":
        for b in boelge.hent_alle(db):
            log.info("%-20s %-45s uge %-8s %s",
                     b.boelge, b.navn, b.planlagt_uge or "-", b.status)
        return 0

    if args.boelge_kommando == "gate":
        raekker = boelge.gate_status(db, args.boelge)
        ikke_klar = 0
        for r in raekker:
            log.info(
                "%-20s sager %d/%d  dokumenter %d/%d  afviste %d  ->  %s",
                r["boelge"], r["indlaeste_sager"], r["antal_sager"],
                r["indlaeste_dokumenter"], r["antal_dokumenter"],
                r["afviste"], r["gate"].upper(),
            )
            if r["gate"] != "klar":
                ikke_klar += 1
        # Returkoden gør gaten brugbar i en pipeline: undervisningen må
        # ikke planlægges på en bølge der ikke er grøn.
        return 0 if ikke_klar == 0 else 1

    boelge.valider(db, args.boelge)
    boelge.markér_afstemt(db, args.boelge, args.af)
    log.info("Bølge %s markeret som afstemt af %s", args.boelge, args.af)
    return 0


def _load(settings: Settings, args) -> int:
    db = Database(settings.db)

    # Valideres FØR kørslen starter. En stavefejl i bølgekoden ville ellers
    # give nul rækker, nul fejl og en fuldstændig grøn rapport.
    valgt = boelge.valider(db, args.boelge)
    if valgt and not args.noegle:
        antal = boelge.kraev_arbejde(db, args.objekt_type, valgt)
        log.info("Bølge %s: %d %s-objekter til indlæsning", valgt, antal, args.objekt_type)

    _kontroller_maal(db, settings, args.objekt_type, valgt)

    # Testmiljøets id'er og feltnavne må aldrig følge med ind i produktion.
    if settings.er_produktion and (antal_test := aktive_testvaerdier(db)):
        log.error("%d aktive kodelisterækker kommer fra testmiljøet. Kør "
                  "`konv mapping load` med KONV_MILJOE=prod først.", antal_test)
        return 2

    run_id = opret_run(
        db, fase="load", miljoe=settings.miljoe, dry_run=settings.dry_run,
        objekt_type=args.objekt_type, kodeversion=settings.kodeversion,
        boelge=valgt, maal=settings.sbsys.base_url,
    )
    kontekst = byg_kontekst(settings, run_id, valgt, args.vis_payload)
    loader = LOADERS[args.objekt_type](kontekst)
    try:
        resultat = (
            loader.behandl_noegle(args.noegle) if args.noegle
            else loader.koer(args.graense)
        )
        afslut_run(db, run_id, "ok", str(resultat))
        log.info("Kørsel %d færdig: %s", run_id, resultat)
        return 0 if resultat.fejlet == 0 else 1
    except Exception as e:
        afslut_run(db, run_id, "fejlet", str(e)[:1000])
        raise
    finally:
        kontekst.klient.luk()


# Hvilke felter der er værd at se, pr. liste. Resten er støj i en CSV
# en fagperson skal udfylde ved siden af.
_OPSLAG = {
    "afdelinger": ("hent_afdelinger",
                   ("Id", "Navn", "Sti", "ExternalKey", "Uuid", "CvrNummer")),
    "skabeloner": ("hent_skabeloner",
                   ("Id", "Navn", "Beskrivelse", "SagsTypeId", "SagsNummer")),
    # Brugersøgningen er en POST med et filter; et tomt filter giver alle.
    "brugere": ("soeg_bruger", ("Id", "LogonId", "Navn", "Initialer", "Email")),
    # Servicekontoen selv — det hurtigste gyldige behandler-id at komme i gang med.
    "mig": ("hent_mig", ("Id", "LogonId", "Navn")),
    # Børn og Unge-API'et. Kataloget er mapningskilden til indsatser:
    # paragraf -> foranstaltning -> tilbud, som er netop det tripel
    # POST /bu_api/sag_foranstaltning kræver.
    "katalog": ("hent_katalog", ("paragrafId", "paragraf", "foranstaltningId",
                                 "foranstaltning", "tilbudId", "tilbud")),
    "tilbud": ("hent_tilbud", ("id", "navn", "tilbudType", "inaktiv", "inaktivDato")),
    # SagsStatusDto. RequireComments viser hvilke statusser der kræver en
    # kommentar ved skift — loaderen sender derfor altid en.
    "statusser": ("hent_statusser", ("Id", "Navn", "SagsTilstand", "RequireComments", "Orden")),
    # DokumentArtDto — kilden til mapping/dokumentkategori.csv.
    "dokumentarter": ("hent_dokumentarter", ("Id", "Navn", "Uuid")),
}

# Lister der ligger i Børn og Unge-API'et og ikke i SBSYS selv.
_BU_OPSLAG = {"katalog", "tilbud"}


def _fold_katalog(raekker: list) -> list[dict]:
    """Fladgør paragraf -> foranstaltning -> tilbud til én række pr. tripel.

    Kataloget kommer som et træ, men det er tripler en fagperson skal vælge
    imellem, og det er tripler kaldet kræver. En indlejret struktur i en
    CSV hjælper ingen.
    """
    flade: list[dict] = []
    for paragraf in raekker:
        if not isinstance(paragraf, dict):
            continue
        for f in paragraf.get("foranstaltninger") or []:
            tilbud = f.get("tilbuds") or [{}]
            for t in tilbud:
                flade.append({
                    "paragrafId": paragraf.get("id"),
                    "paragraf": paragraf.get("navn"),
                    "foranstaltningId": f.get("id"),
                    "foranstaltning": f.get("navn"),
                    "tilbudId": t.get("id"),
                    "tilbud": t.get("navn"),
                })
    return flade


def _udskriv_tabel(raekker: list[dict], graense: int = 40) -> None:
    """Vis et resultatsæt som en tabel i terminalen.

    Kolonner der er tomme hele vejen ned udelades: et overbliksscript med
    tyve kolonner, hvoraf de fjorten er NULL, er ikke et overblik.
    """
    if not raekker:
        return
    brugte = [
        k for k in raekker[0]
        if any(r.get(k) not in (None, "") for r in raekker)
    ]
    if not brugte:
        return

    def tekst(v: object) -> str:
        return "" if v is None else str(v).replace("\n", " ")[:60]

    bredder = {
        k: max(len(k), *(len(tekst(r.get(k))) for r in raekker)) for k in brugte
    }
    print()
    print("  ".join(k.ljust(bredder[k]) for k in brugte))
    print("  ".join("-" * bredder[k] for k in brugte))
    vist = raekker if graense <= 0 else raekker[:graense]
    for r in vist:
        print("  ".join(tekst(r.get(k)).ljust(bredder[k]) for k in brugte))
    if len(vist) < len(raekker):
        print(f"... {len(raekker) - len(vist)} rækker mere "
              f"(--raekker 0 for dem alle)")
    print(f"({len(raekker)} rækker)")


def _forbered_kodelister(settings: Settings, args: argparse.Namespace) -> int:
    """Hent begge sider af hver kodeliste og par dem hvor det er entydigt.

    SBSYS-siden kan hentes, GO-siden kan læses — men parringen er en faglig
    vurdering. Her gøres alt det der ikke er vurdering, så fagpersonen kun
    skal tage stilling til det der faktisk kræver det.
    """
    import csv

    from . import forbered as f

    db = Database(settings.db)
    probe = replace(settings, sbsys=replace(settings.sbsys, maks_forsoeg=1))
    kontekst = byg_kontekst(probe)
    kendte = {
        (r["liste"], r["go_vaerdi"])
        for r in db.hent_alle(
            "SELECT liste, go_vaerdi FROM map.kodeliste "
            "WHERE sbsys_vaerdi IS NOT NULL AND sbsys_vaerdi <> ''"
        )
    }

    args.ud.mkdir(parents=True, exist_ok=True)
    i_alt = 0
    try:
        for liste, kilde in sorted(f.KILDER.items()):
            if args.liste and liste != args.liste:
                continue
            go = f.go_vaerdier(db, kilde)
            if not go:
                log.info("%-22s ingen værdier i stg.* endnu", liste)
                continue

            muligheder = [(v, v) for v in kilde.faste]
            if kilde.operation:
                muligheder = _hent_muligheder(kontekst, kilde, f)

            raekker = f.byg_raekker(
                liste, go, muligheder,
                kendte={v for (lst, v) in kendte if lst == liste},
            )
            sti = args.ud / f"{liste}.csv"
            with sti.open("w", encoding="utf-8-sig", newline="") as fil:
                skriver = csv.DictWriter(fil, fieldnames=f.KOLONNER, delimiter=";")
                skriver.writeheader()
                skriver.writerows(raekker)

            udfyldt = sum(1 for r in raekker if r["sbsys_vaerdi"])
            log.info("%-22s %3d GO-værdier, %3d foreslået, %3d SBSYS-muligheder -> %s",
                     liste, len(go), udfyldt, len(muligheder), sti)
            i_alt += len(raekker)
    finally:
        kontekst.klient.luk()
        if kontekst.bu_klient:
            kontekst.bu_klient.luk()

    if not i_alt:
        log.warning("Ingenting at forberede. Er stg.* fyldt? Kør extract-laget først.")
        return 1
    print(f"\n{i_alt} rækker skrevet til {args.ud}.")
    print("Gennemgå dem, udfyld sbsys_vaerdi og godkendt_af, og kopiér")
    print("filerne til mapping/. Et FORSLAG er et forslag — bekræft det.")
    return 0


def _hent_muligheder(kontekst, kilde, f):  # noqa: ANN001, ANN201
    """SBSYS-siden af én liste. En tjeneste der ikke svarer er ikke fatalt:
    arket kan stadig udfyldes i hånden."""
    klient = kontekst.bu_klient if kilde.bu else kontekst.klient
    if klient is None:
        log.warning("Børn og Unge-API'et er ikke sat op (SBSYS_BU_BASE_URL) — "
                    "listen skrives uden forslag")
        return []
    try:
        op = kontekst.endpoints.operation(kilde.operation)
        svar = klient.kald(op.metode, op.sti, operation=kilde.operation,
                           json={} if op.metode.upper() == "POST" else None,
                           forventede_status=op.forventede_status)
    except Exception as e:  # noqa: BLE001
        log.warning("%s kunne ikke hentes: %s", kilde.operation, e)
        return []

    krop = svar.krop
    raekker = krop if isinstance(krop, list) else (krop or {}).get("Results") or []
    if kilde.operation == "hent_katalog":
        raekker = _fold_katalog(raekker)
    return f.sbsys_muligheder(raekker, kilde)


def _opslag(settings: Settings, args: argparse.Namespace) -> int:
    """Hent en af SBSYS' egne lister og skriv den som CSV.

    Mapningerne kan ikke udfyldes uden dem: `mapping/afdeling.csv` skal
    indeholde ansættelsesstedets ExternalKey, og `mapping/sagsskabelon.csv`
    skal indeholde skabelonens id. Begge dele står kun i SBSYS, og at
    skrive dem af fra en skærm er både langsomt og en kilde til tastefejl.
    """
    import csv

    operation, kolonner = _OPSLAG[args.hvad]
    # Bevidst IKKE i mapping/: `konv mapping load` indlæser hver .csv der
    # ligger der, og filnavnet bliver listenavnet. En opslagsfil ville
    # blive slugt som en kodeliste med forkerte kolonner.
    ud = args.ud or Path("rapporter") / f"{args.hvad}-fra-sbsys.csv"

    probe = replace(settings, sbsys=replace(settings.sbsys, maks_forsoeg=1))
    kontekst = byg_kontekst(probe)
    klient = kontekst.bu_klient if args.hvad in _BU_OPSLAG else kontekst.klient
    if klient is None:
        log.error("%s ligger i Børn og Unge-API'et. Sæt SBSYS_BU_BASE_URL i .env.",
                  args.hvad)
        return 2
    try:
        op = kontekst.endpoints.operation(operation)
        svar = klient.kald(
            op.metode, op.sti, operation=operation,
            json={} if op.metode.upper() == "POST" else None,
            forventede_status=op.forventede_status,
        )
    finally:
        kontekst.klient.luk()

    krop = svar.krop
    # /api/bruger/me svarer med ét objekt, ikke en liste.
    raekker = (
        krop if isinstance(krop, list)
        else [krop] if isinstance(krop, dict) and "Id" in krop
        else (krop or {}).get("Results") or []
    )
    if args.hvad == "katalog":
        raekker = _fold_katalog(raekker)

    if not raekker:
        log.error("SBSYS svarede med en tom liste på %s", op.sti)
        return 1

    # Tag de kolonner vi kender, og læg resten bagefter — en SBSYS-version
    # kan have felter vi ikke kendte, og de er netop interessante her.
    ekstra = sorted({n for r in raekker if isinstance(r, dict) for n in r} - set(kolonner))
    alle = [*kolonner, *ekstra]

    ud.parent.mkdir(parents=True, exist_ok=True)
    with ud.open("w", encoding="utf-8-sig", newline="") as f:
        # Semikolon som i mapping/*.csv: dansk Excel bruger semikolon som
        # listeseparator, og en kommafil lander i én kolonne.
        skriver = csv.DictWriter(f, fieldnames=alle, extrasaction="ignore",
                                 delimiter=";")
        skriver.writeheader()
        for raekke in raekker:
            if isinstance(raekke, dict):
                skriver.writerow({k: _fladt(raekke.get(k)) for k in alle})

    log.info("%d %s skrevet til %s", len(raekker), args.hvad, ud)
    tomme = sum(1 for r in raekker if isinstance(r, dict) and not r.get("ExternalKey"))
    if args.hvad == "afdelinger" and tomme:
        log.warning("%d af %d ansættelsessteder har tom ExternalKey. Dem kan der "
                    "ikke peges på med ekstern nøgle — brug AnsaettelsesstedId "
                    "for dem, eller få nøglerne udfyldt i SBSYS", tomme, len(raekker))
    return 0


def _fladt(vaerdi: object) -> str:
    """CSV kan ikke rumme et objekt. Vis det kort frem for at ødelægge filen."""
    if vaerdi is None:
        return ""
    if isinstance(vaerdi, (dict, list)):
        return json.dumps(vaerdi, ensure_ascii=False)[:200]
    return str(vaerdi)


_SIKRE_METODER = {"GET", "HEAD", "OPTIONS"}

# Konverteringen sletter ikke. Oprydningen af SBSYS og af konverterings-
# databasen håndteres af andre, inden den rigtige konvertering begynder
# (besluttet 11-11-2026). Derfor findes der ingen vej til en DELETE herfra
# — heller ikke med --jatak. Skal noget slettes, gøres det et sted hvor
# nogen har taget stilling til det, ikke med vores værktøj i hånden kl. 03.
_FORBUDTE_METODER = {"DELETE"}


def _kald(settings: Settings, args: argparse.Namespace) -> int:
    """Kald ét endpoint og vis hele svaret.

    Findes fordi en snitflade ikke altid har en beskrivelse man kan hente.
    Børn og Unge-API'et har et `sag_foranstaltning`-endpoint, men ingen
    OpenAPI-dokumentation vi kunne finde — og så er tjenesten selv den
    eneste kilde.

    **OPTIONS er det første at prøve.** Mange API'er svarer med hvilke
    metoder stien tager, og nogle (Django REST Framework, som snake_case-
    stierne tyder på) svarer med hele feltlisten: navne, typer og hvad der
    er obligatorisk. Det er præcis det en beskrivelse ville have fortalt.
    """
    metode = args.metode.upper()
    if metode in _FORBUDTE_METODER:
        print(f"{metode} er ikke tilladt. Konverteringen sletter ikke — "
              f"oprydningen i SBSYS håndteres af andre.", file=sys.stderr)
        return 2
    if metode not in _SIKRE_METODER and not args.jatak:
        print(f"{metode} kan ændre data. Tilføj --jatak hvis det er meningen.",
              file=sys.stderr)
        return 2

    krop = None
    if args.krop:
        krop = json.loads(args.krop.read_text(encoding="utf-8-sig"))

    sbsys = replace(settings.sbsys, maks_forsoeg=1)
    if args.bu:
        if not sbsys.bu_base_url:
            print("SBSYS_BU_BASE_URL er ikke sat i .env.", file=sys.stderr)
            return 2
        sbsys = replace(sbsys, base_url=sbsys.bu_base_url)
    elif args.base:
        sbsys = replace(sbsys, base_url=args.base.rstrip("/"))
    log.info("Kalder %s", sbsys.base_url)
    kontekst = byg_kontekst(replace(settings, sbsys=sbsys))
    try:
        svar = kontekst.klient.kald(
            metode, args.sti, operation="kald", json=krop,
            # Alt accepteres: vi er her netop for at SE hvad der kommer.
            forventede_status=tuple(range(100, 600)),
        )
    finally:
        kontekst.klient.luk()

    print(f"HTTP {svar.status}  ({svar.varighed_ms} ms)")
    for navn in ("allow", "content-type", "www-authenticate", "location"):
        if navn in svar.headers:
            print(f"{navn}: {svar.headers[navn]}")
    print()
    legeme = svar.krop
    if isinstance(legeme, bytes):
        legeme = legeme.decode("utf-8", "replace")
    tekst = (json.dumps(legeme, indent=2, ensure_ascii=False)
             if isinstance(legeme, (dict, list)) else str(legeme or ""))

    # Til en fil: hele svaret, i UTF-8. `> fil` i PowerShell klipper ved
    # 20.000 tegn og ødelægger æøå (konsollens tegntabel) — 19-11-2026.
    if args.ud:
        args.ud.write_text(tekst, encoding="utf-8")
        antal = f", {len(legeme)} elementer" if isinstance(legeme, list) else ""
        print(f"Hele svaret ({len(tekst)} tegn{antal}) er skrevet til {args.ud}")
        return 0

    if len(tekst) > 20000:
        print(tekst[:20000])
        print(f"\n... klippet ved 20.000 af {len(tekst)} tegn. Brug --ud FIL for hele svaret.")
    else:
        print(tekst or "(tom krop)")
    return 0


def _spec(settings: Settings, args: argparse.Namespace) -> int:
    """Slå op i SBSYS' egen beskrivelse frem for at gætte.

    Særligt `konv spec noegle` er værd at køre først: den besvarer A1 —
    findes der et felt vi kan skrive GO-nøglen i, og kan der søges på det?
    Hele genkørselssikringen hviler på svaret, og det har indtil nu krævet
    et møde at få.
    """
    from .sbsys import spec as spec_modul

    # Ét forsøg: et manglende dokument er et nej, ikke et netværksglimt.
    # Børn og Unge-API'et er en helt anden vært, så base_url skal kunne
    # byttes ud. Tokenet genbruges — virker det ikke dér, svarer tjenesten
    # 401, og så ved vi at den har sin egen klient/konto.
    sbsys = replace(settings.sbsys, maks_forsoeg=1)
    if args.base:
        sbsys = replace(sbsys, base_url=args.base.rstrip("/"))
        log.info("Henter mod %s", sbsys.base_url)
    probe = replace(settings, sbsys=sbsys)
    kontekst = byg_kontekst(probe)
    try:
        dokument, sti = spec_modul.hent(kontekst.klient, args.version, args.praefiks)
    except spec_modul.SpecFejl as e:
        log.error("%s", e)
        return 1
    finally:
        kontekst.klient.luk()

    log.info("Hentede %s (%d stier, %d modeller)", sti,
             len(dokument.get("paths") or {}), len(spec_modul.skemaer(dokument)))

    if args.hvad == "gem":
        args.ud.parent.mkdir(parents=True, exist_ok=True)
        args.ud.write_text(json.dumps(dokument, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Skrevet til {args.ud}")
        return 0

    if args.hvad == "stier":
        raekker = spec_modul.operationer(dokument)
        if args.filter:
            n = args.filter.lower()
            raekker = [r for r in raekker if n in r[1].lower() or n in r[2].lower()]
        for metode, sti_, tekst in raekker:
            print(f"{metode:<7} {sti_:<60} {tekst}")
        print(f"\n{len(raekker)} operationer")
        return 0

    if args.hvad == "noegle":
        kandidater = spec_modul.noeglekandidater(dokument, args.filter)
        if not kandidater:
            print("Ingen felter der ligner en ekstern nøgle.")
            print("Det er i sig selv et svar på A1 — og et dårligt et.")
            print("Prøv `konv spec model Sag` og læs listen igennem i hånden.")
            return 1
        for model, felt, type_ in kandidater:
            print(f"{model:<45} {felt:<35} {type_}")
        print(f"\n{len(kandidater)} kandidater. Et felt er kun brugbart hvis der "
              f"kan SØGES på det — tjek med `konv spec soegning`.")
        return 0

    if args.hvad == "felt":
        if not args.filter:
            log.error("Angiv et feltnavn, fx: konv spec felt EksternSagId")
            return 2
        raekker = spec_modul.felt_forekomster(dokument, args.filter)
        if not raekker:
            print(f"Intet felt indeholder '{args.filter}'.")
            return 1
        for rolle_, model, felt, type_ in raekker:
            print(f"{rolle_:<6} {model:<70} {felt:<30} {type_}")
        roller = {r for r, *_ in raekker}
        print()
        if "skriv" in roller and "søg" in roller:
            print("Feltet kan både SKRIVES og SØGES på. Det er svaret på A1 —")
            print("sæt det som ekstern_noegle i sbsys/endpoints.toml.")
        elif "søg" in roller:
            print("Feltet kan kun SØGES på. Find ud af hvor værdien skrives,")
            print("ellers er det værdiløst som ekstern nøgle.")
        elif "skriv" in roller:
            print("Feltet kan kun SKRIVES. Uden en søgning kan nøglen gemmes,")
            print("men aldrig findes igen — og så holder ADR-0001 ikke.")
        return 0

    if args.hvad == "soegning":
        modeller = spec_modul.soegemodeller(dokument)
        for navn in modeller:
            if args.filter and args.filter.lower() not in navn.lower():
                continue
            print(f"\n{navn}")
            for felt in sorted(spec_modul.felter(spec_modul.skemaer(dokument)[navn])):
                print(f"    {felt}")
        return 0

    # model
    if not args.filter:
        log.error("Angiv en model, fx: konv spec model Sag")
        return 2
    navne = spec_modul.find_modeller(dokument, args.filter)
    if not navne:
        log.error("Ingen model indeholder '%s'", args.filter)
        return 1
    for navn in navne[:10]:
        paakraevet = set(spec_modul.paakraevede_felter(dokument, navn))
        print(f"\n{navn}")
        for felt, beskrivelse in sorted(spec_modul.felter(spec_modul.skemaer(dokument)[navn]).items()):
            maerke = "*" if felt in paakraevet else " "
            print(f"  {maerke} {felt:<40} {(beskrivelse or {}).get('type', '?')}")
    print("\n* = obligatorisk ved oprettelse")
    return 0


def _kontroller_maal(db: Database, settings: Settings, objekt_type: str,
                     valgt_boelge: str | None) -> None:
    """Advar hvis id_map er bygget mod et ANDET SBSYS end det vi peger på nu.

    Den farligste tilstand i hele løsningen, og den er helt tavs: efter en
    attraptest står objekterne som 'loaded' med id'er fra localhost. Peger
    man så på det rigtige testmiljø, springes de over for evigt — og
    dokumenterne hægtes på sags-id'er der ikke findes. Alt ser grønt ud.
    """
    nuvaerende = settings.sbsys.base_url
    andre = [
        r for r in tidligere_maal(db, objekt_type, valgt_boelge)
        if (r["maal"] or "").rstrip("/") != nuvaerende.rstrip("/")
    ]
    if not andre:
        return

    linjer = [f"  {r['antal']} {objekt_type}-objekter er indlæst til {r['maal']}"
              for r in andre]
    besked = (
        f"konv.id_map er bygget mod et andet SBSYS end {nuvaerende}:\n"
        + "\n".join(linjer)
        + "\nDe objekter springes over, og dokumenter hægtes på sags-id'er der "
        "ikke findes i det nye mål.\n"
        "Var det forrige mål attrappen, så nulstil først:\n"
        f"  konv nulstil --objekt-type {objekt_type}"
        + (f" --boelge {valgt_boelge}" if valgt_boelge else "")
        + " --ogsaa-indlaeste\n"
        "Var det et rigtigt SBSYS, så FÅR du dubletter af at nulstille — "
        "find ud af hvad der står hvor, før du gør noget."
    )
    if settings.dry_run:
        for linje in besked.splitlines():
            log.warning("%s", linje)
        return
    raise KonverteringsFejl(besked)


def _selvtest(settings: Settings) -> int:
    """Kør denne før alt andet på en ny maskine. Fejler hurtigt og forklarende."""
    ok = True
    db = Database(settings.db)
    try:
        db.hent_en("SELECT 1 AS et")
        log.info("Database: OK (%s/%s)", settings.db.server, settings.db.database)
    except Exception as e:
        log.error("Database: FEJL — %s", e)
        ok = False

    if settings.sbsys.verificer_tls is False:
        log.warning("SBSYS-certifikat: verifikation SLÅET FRA (SBSYS_VERIFY=0) — "
                    "kun forsvarligt i test, afvises i produktion")
    else:
        log.info("SBSYS-certifikat: %s", settings.sbsys.tls_beskrivelse)

    form = settings.sbsys.auth_form
    if form.startswith(("ingen", "ufuldstændig")):
        log.warning("SBSYS-autentifikation: %s — dry-run virker, --live gør ikke", form)
    else:
        log.info("SBSYS-autentifikation: %s", form)

    # Ét forsøg: selvtesten er en måling, ikke en kørsel. Fem forsøg med
    # voksende ventetid gør bare at man venter et halvt minut på et nej.
    probe = replace(settings, sbsys=replace(settings.sbsys, maks_forsoeg=1))
    try:
        kontekst = byg_kontekst(probe)
        op = kontekst.endpoints.operation("hent_version")
        svar = kontekst.klient.kald(op.metode, op.sti, operation="hent_version",
                                    forventede_status=(200, 204, 404))
        if svar.status == 404:
            # Forbindelsen virker — stien gør ikke. Næsten altid at
            # endpoints.toml stadig indeholder gættede stier. Det er en
            # åben opgave, ikke en grøn linje.
            log.warning("SBSYS: HTTP 404 på %s efter %d ms. Forbindelsen og "
                        "autentifikationen virker, men stien findes ikke — "
                        "udfyld sbsys/endpoints.toml efter snitfladebeskrivelsen",
                        op.sti, svar.varighed_ms)
        else:
            log.info("SBSYS: svarede HTTP %d på %s efter %d ms",
                     svar.status, op.sti, svar.varighed_ms)

        # Ét kald er ikke en måling, men et sekund pr. kald er 55 timer på
        # 200.000 objekter. Er første kald langsomt, skal det undersøges nu.
        if svar.varighed_ms > 2000:
            log.warning("SBSYS: kaldet tog %.1f sekunder. Ved den hastighed "
                        "tager en stor bølge dage. Mål med `konv selvtest` et "
                        "par gange — er det kun det første kald, er det "
                        "opkobling (TLS, proxy, CRL-opslag); er det hvert kald, "
                        "skal det løses inden testkonverteringen",
                        svar.varighed_ms / 1000)
        kontekst.klient.luk()
    except VaertsnavnFejl:
        # Næsten altid at SBSYS_BASE_URL endnu ikke er udfyldt. Det er en
        # forventet tilstand tidligt i projektet, ikke et nedbrud.
        log.warning("SBSYS: værtsnavnet i %s kunne ikke slås op — "
                    "udfyld SBSYS_BASE_URL i .env", settings.sbsys.base_url)
        log.warning("SBSYS: springes over. Dry-run og niveau 2 virker uden.")
    except (CertifikatFejl, AutentifikationsFejl) as e:
        # Begge beskeder er flerlinjede og skal kunne læses som en liste,
        # ikke som én lang logsætning.
        for nr, linje in enumerate(str(e).splitlines()):
            log.error("SBSYS: %s%s", "FEJL — " if nr == 0 else "       ", linje)
        ok = False
    except Exception as e:
        log.error("SBSYS: FEJL — %s", e)
        ok = False

    if settings.filer.rod.exists():
        log.info("Filrod: OK (%s)", settings.filer.rod)
    else:
        log.error("Filrod: findes ikke (%s)", settings.filer.rod)
        ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
