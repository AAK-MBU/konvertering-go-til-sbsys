"""Konfiguration.

Alt der skifter mellem test/preprod/prod ligger her og kommer fra
environment. Ingen hemmeligheder i git, ingen hemmeligheder i databasen.

Under Automation Server hentes brugernavn/kodeord fra Credential-store og
lægges i environment af ``automation/main.py`` inden Settings læses.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

Miljoe = Literal["test", "preprod", "prod"]


class KonfigurationsFejl(RuntimeError):
    """Rejses ved opstart, ikke midt i en indlæsning."""


def _kraev(navn: str) -> str:
    vaerdi = os.environ.get(navn, "").strip()
    if not vaerdi:
        raise KonfigurationsFejl(f"Miljøvariablen {navn} mangler")
    return vaerdi


def _flag(navn: str, standard: bool) -> bool:
    raa = os.environ.get(navn)
    if raa is None:
        return standard
    return raa.strip().lower() in {"1", "true", "ja", "yes", "on"}


def _bu_base_url() -> str | None:
    """Børn og Unge-API'ets vært — uden ``/bu_api`` til sidst.

    Stierne i ``endpoints.py`` indeholder selv ``/bu_api/``, fordi de er
    skrevet af tjenestens egen OpenAPI-beskrivelse. Skriver man derfor
    værten MED ``/bu_api``, bliver kaldet til ``/bu_api/bu_api/...`` og
    svaret en 404 der peger på en sti, man aldrig har bedt om. Det er en
    halv times fejlsøgning, så den rettes her frem for at blive fundet.
    """
    raa = (os.environ.get("SBSYS_BU_BASE_URL") or "").strip().rstrip("/")
    if not raa:
        return None
    if raa.lower().endswith("/bu_api"):
        rettet = raa[: -len("/bu_api")]
        log.warning("SBSYS_BU_BASE_URL slutter på /bu_api. Stierne indeholder "
                    "den selv, så værten bruges som %s", rettet)
        return rettet
    return raa


_SANDT = {"1", "true", "ja", "yes", "on"}
_FALSK = {"0", "false", "nej", "no", "off"}


def tls_verifikation() -> bool | str:
    """Læs SBSYS_VERIFY: til, fra, eller en sti til et CA-bundt.

    De tre former findes, fordi de tre situationer findes. Internt
    testmiljø med kommunens egen CA er det normale, og det løses med en sti
    — ikke ved at slå verifikationen fra. ``SBSYS_VERIFICER_TLS`` virker
    stadig som det gamle navn, men SBSYS_VERIFY vinder.
    """
    raa = os.environ.get("SBSYS_VERIFY")
    if raa is None:
        raa = os.environ.get("SBSYS_VERIFICER_TLS")
    if raa is None or not raa.strip():
        return True

    raa = raa.strip().strip('"').strip("'")
    if raa.lower() in _SANDT:
        return True
    if raa.lower() in _FALSK:
        return False

    # Alt andet er en sti. At kontrollere den HER er pointen: en stavefejl
    # giver ellers nøjagtig samme SSL-fejl som et manglende certifikat, og
    # så leder man det forkerte sted.
    sti = Path(raa)
    if not sti.exists():
        raise KonfigurationsFejl(
            f"SBSYS_VERIFY peger på et CA-bundt der ikke findes: {sti}. "
            f"Brug skråstreg i stien (C:/certs/ca.pem), eller sæt "
            f"SBSYS_VERIFY=1 for systemets CA-lager."
        )
    if sti.is_file() and sti.stat().st_size == 0:
        raise KonfigurationsFejl(f"CA-bundtet {sti} er tomt")
    return str(sti)


@dataclass(frozen=True)
class DatabaseIndstillinger:
    """Forbindelse til konverteringsdatabasen.

    ``trusted_connection`` er False som standard, fordi konverteringen kører
    på SQL Server på Linux. Integreret sikkerhed forudsætter AD-integration
    og virker sjældent der; en servicekonto fra credential-storen er normalen.
    Sæt ``KONV_DB_TRUSTED=1`` hvis I kører mod en Windows-instans med Kerberos.
    """

    server: str
    database: str
    driver: str = "ODBC Driver 18 for SQL Server"
    trusted_connection: bool = False
    brugernavn: str | None = None
    kodeord: str | None = None
    timeout_sekunder: int = 300
    # ODBC Driver 18 kræver kryptering som standard. På en testserver med et
    # selvsigneret certifikat skal certifikatet derfor accepteres blindt —
    # ellers kan man ikke forbinde. I produktion er det omvendt: dér skal
    # certifikatet være gyldigt, og Settings.valider() afviser at køre uden.
    trust_server_certificate: bool = True

    def __post_init__(self) -> None:
        if not self.trusted_connection and not self.brugernavn:
            raise KonfigurationsFejl(
                "Uden Trusted_Connection kræves KONV_DB_BRUGER og KONV_DB_KODEORD. "
                "Kører I mod en Windows-instans med Kerberos, sæt KONV_DB_TRUSTED=1."
            )

    @property
    def odbc_connection_string(self) -> str:
        dele = [
            f"DRIVER={{{self.driver}}}",
            f"SERVER={self.server}",
            f"DATABASE={self.database}",
            "Encrypt=yes",
            f"TrustServerCertificate={'yes' if self.trust_server_certificate else 'no'}",
        ]
        if self.trusted_connection:
            dele.append("Trusted_Connection=yes")
        else:
            dele.append(f"UID={self.brugernavn}")
            dele.append(f"PWD={self.kodeord}")
        return ";".join(dele)

    def __repr__(self) -> str:
        """Kodeordet må aldrig ende i en logfil eller en exception-trace."""
        auth = "trusted" if self.trusted_connection else f"uid={self.brugernavn}"
        return f"DatabaseIndstillinger(server={self.server!r}, database={self.database!r}, {auth})"


@dataclass(frozen=True)
class SbsysIndstillinger:
    base_url: str
    token_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    # Børn og Unge-API'et (indsatser) er en selvstændig tjeneste på sin egen
    # vært. Tokenet gælder på tværs — bekræftet 09-11-2026 — så kun adressen
    # er anderledes. Tom = indsatser kan ikke indlæses.
    bu_base_url: str | None = None
    # SBSYS' snitflade kræver en navngiven bruger oven i klienten (password
    # grant). Alt konverteringen opretter, står i SBSYS som udført af den.
    brugernavn: str | None = None
    kodeord: str | None = None
    scope: str | None = None
    statisk_token: str | None = None
    timeout_sekunder: float = 60.0
    maks_forsoeg: int = 5
    maks_samtidige: int = 4
    kald_pr_sekund: float = 8.0
    # True = verificér mod systemets CA-lager. En sti = verificér mod netop
    # det CA-bundt (kommunens egen rod-CA). False = verificér ikke; afvises
    # i produktion. Værdien gives direkte videre til httpx' verify.
    verificer_tls: bool | str = True

    @property
    def tls_beskrivelse(self) -> str:
        if self.verificer_tls is True:
            return "systemets CA-lager"
        if self.verificer_tls is False:
            return "SLÅET FRA — forbindelsen er ikke beskyttet mod aflytning"
        return f"CA-bundt: {self.verificer_tls}"

    @property
    def auth_form(self) -> str:
        """Hvilken autentifikation opsætningen faktisk giver.

        Vises af `konv env` og `konv selvtest`. Uden den opdager man først
        at der mangler legitimationsoplysninger når SBSYS svarer 401 — og
        et 401 ligner til forveksling en forkert adgangskode.
        """
        if self.statisk_token:
            return "statisk token"
        if not self.token_url:
            return "ingen (SBSYS_TOKEN_URL mangler)"
        if not self.client_id:
            return "ingen (SBSYS_CLIENT_ID mangler)"
        if self.brugernavn and self.kodeord:
            return f"password grant som {self.brugernavn}"
        if self.brugernavn or self.kodeord:
            mangler = "SBSYS_PASSWORD" if self.brugernavn else "SBSYS_USERNAME"
            return f"ufuldstændig ({mangler} mangler)"
        if self.client_secret:
            return "client_credentials"
        return "ingen (hverken SBSYS_USERNAME/PASSWORD eller SBSYS_CLIENT_SECRET)"

    def __repr__(self) -> str:
        """Hverken adgangskode eller client secret må ende i en trace."""
        return f"SbsysIndstillinger(base_url={self.base_url!r}, auth={self.auth_form!r})"


@dataclass(frozen=True)
class FilIndstillinger:
    """Dokumentfilerne kommer som separat fileshare-eksport ved siden af bacpac'en."""

    rod: Path
    maks_stoerrelse_mb: int = 250
    tillad_tomme_filer: bool = False


@dataclass(frozen=True)
class Settings:
    miljoe: Miljoe
    db: DatabaseIndstillinger
    sbsys: SbsysIndstillinger
    filer: FilIndstillinger
    dry_run: bool = True
    kodeversion: str = "ukendt"
    batch_stoerrelse: int = 200
    log_niveau: str = "INFO"
    # Personhenførbare data må ikke i logfiler. Slås kun fra bevidst, aldrig i prod.
    maskér_persondata: bool = True
    korrelations_praefiks: str = "mbu-konv"
    ekstra: dict[str, str] = field(default_factory=dict)

    @property
    def er_produktion(self) -> bool:
        return self.miljoe == "prod"

    def valider(self) -> None:
        if self.er_produktion and not self.maskér_persondata:
            raise KonfigurationsFejl("Maskering af persondata må ikke slås fra i produktion")
        if self.er_produktion and self.sbsys.verificer_tls is False:
            raise KonfigurationsFejl(
                "TLS-verifikation må ikke slås fra i produktion. Peg i stedet "
                "SBSYS_VERIFY på kommunens rod-CA, eller få et certifikat på "
                "serveren der kan verificeres."
            )
        if self.er_produktion and self.db.trust_server_certificate:
            raise KonfigurationsFejl(
                "I produktion skal databaseserverens certifikat kunne verificeres. "
                "Sæt KONV_DB_TRUST_CERT=0 og sørg for et gyldigt certifikat på serveren."
            )
        if not self.filer.rod.exists():
            raise KonfigurationsFejl(f"Filrod findes ikke: {self.filer.rod}")
        # Kun ved rigtige kald: dry-run skal kunne køres uden adgang til SBSYS,
        # fx til at bygge og gennemse payloads før kontoen overhovedet findes.
        if not self.dry_run and not self.sbsys.statisk_token:
            form = self.sbsys.auth_form
            if form.startswith(("ingen", "ufuldstændig")):
                raise KonfigurationsFejl(
                    f"Ingen brugbar SBSYS-autentifikation: {form}. SBSYS' snitflade "
                    f"kræver SBSYS_TOKEN_URL, SBSYS_CLIENT_ID og en navngiven bruger "
                    f"(SBSYS_USERNAME + SBSYS_PASSWORD). Uden dem svarer SBSYS 401 "
                    f"på hvert eneste kald."
                )

    @classmethod
    def fra_environment(cls) -> Settings:
        miljoe = os.environ.get("KONV_MILJOE", "test").strip().lower()
        if miljoe not in ("test", "preprod", "prod"):
            raise KonfigurationsFejl(f"Ukendt miljø: {miljoe}")

        # Standard er SQL-autentificering: serveren er SQL Server på Linux.
        trusted = _flag("KONV_DB_TRUSTED", False)
        db = DatabaseIndstillinger(
            server=_kraev("KONV_DB_SERVER"),
            database=os.environ.get("KONV_DB_DATABASE", "SBSYS_KONV"),
            driver=os.environ.get("KONV_DB_DRIVER", "ODBC Driver 18 for SQL Server"),
            trusted_connection=trusted,
            brugernavn=None if trusted else _kraev("KONV_DB_BRUGER"),
            kodeord=None if trusted else _kraev("KONV_DB_KODEORD"),
            trust_server_certificate=_flag("KONV_DB_TRUST_CERT", True),
        )

        sbsys = SbsysIndstillinger(
            base_url=_kraev("SBSYS_BASE_URL").rstrip("/"),
            bu_base_url=_bu_base_url(),
            token_url=os.environ.get("SBSYS_TOKEN_URL") or None,
            client_id=os.environ.get("SBSYS_CLIENT_ID") or None,
            client_secret=os.environ.get("SBSYS_CLIENT_SECRET") or None,
            brugernavn=os.environ.get("SBSYS_USERNAME") or None,
            kodeord=os.environ.get("SBSYS_PASSWORD") or None,
            scope=os.environ.get("SBSYS_SCOPE") or None,
            statisk_token=os.environ.get("SBSYS_TOKEN") or None,
            maks_forsoeg=int(os.environ.get("SBSYS_MAKS_FORSOEG", "5")),
            maks_samtidige=int(os.environ.get("SBSYS_MAKS_SAMTIDIGE", "4")),
            kald_pr_sekund=float(os.environ.get("SBSYS_KALD_PR_SEKUND", "8")),
            verificer_tls=tls_verifikation(),
        )

        filer = FilIndstillinger(
            rod=Path(_kraev("KONV_FIL_ROD")),
            maks_stoerrelse_mb=int(os.environ.get("KONV_FIL_MAKS_MB", "250")),
        )

        s = cls(
            miljoe=miljoe,  # type: ignore[arg-type]
            db=db,
            sbsys=sbsys,
            filer=filer,
            dry_run=_flag("KONV_DRY_RUN", True),
            kodeversion=os.environ.get("KONV_KODEVERSION", "ukendt"),
            batch_stoerrelse=int(os.environ.get("KONV_BATCH", "200")),
            log_niveau=os.environ.get("KONV_LOG_NIVEAU", "INFO"),
            maskér_persondata=_flag("KONV_MASKER", True),
        )
        s.valider()
        return s
