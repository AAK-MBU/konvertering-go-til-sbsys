"""Adapterlaget mod SBSYS' API.

Her — og kun her — findes viden om hvilke stier og felter SBSYS bruger.
Resten af løsningen taler kanonisk model.

Stierne nedenfor er PLACEHOLDERS indtil vi har jeres snitfladebeskrivelse.
De kan overskrives uden kodeændring via en TOML-fil (KONV_ENDPOINTS), fordi
forskellige SBSYS-versioner har forskellige stier, og fordi det gør det
muligt at rette en sti i test kl. 23 uden en ny release:

    [operationer.opret_sag]
    metode = "POST"
    sti    = "/api/sag"

    [felter.sag]
    titel        = "SagsTitel"
    kle          = "SagsNummerId"
    ekstern_noegle = "AndenPartId"

Når den rigtige beskrivelse foreligger, gøres to ting:
 1. `felter`-tabellerne rettes til de faktiske feltnavne
 2. kontrakttesten i tests/test_endpoints.py køres mod SBSYS-testmiljøet
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Operation:
    metode: str
    sti: str
    forventede_status: tuple[int, ...] = (200, 201, 202, 204)


# Stierne herunder stammer fra AAK-MBU/api-sbsys-client, kommunens egen
# delte SBSYS-klient, og ikke længere fra gætteri. Mærkningen er pakkens egen:
#
#   [v]  verificeret mod testmiljøet
#   [k]  brugt af klienten, men ikke bekræftet mod specifikationen
#
# Klienten kan selv revidere stierne mod SBSYS' OpenAPI-dokument med
# `uv run python scripts/endpoints.py --check`. Gør det, før en bølge køres.
STANDARD_OPERATIONER: dict[str, Operation] = {
    # /api/version findes ikke — statuslisten er det billigste rigtige kald
    # at bruge som livstegn.
    "hent_version":     Operation("GET",  "/api/sag/sagStatusList", (200,)),      # [k]
    "hent_sag":         Operation("GET",  "/api/sag/{sag_id}", (200,)),           # [v]
    "soeg_sag":         Operation("POST", "/api/sag/search", (200, 204, 404)),    # [v]
    # Sager oprettes FRA EN SKABELON. Der findes ikke et felt-for-felt
    # POST /api/sag — se docs/08-sbsys-klient.md og spørgsmål A15.
    "opret_sag":        Operation("POST", "/api/sag/template", (200, 201)),       # [k]
    # Og de opdateres kun ved at sende hele sagen retur. Hent først.
    "opdater_sag":      Operation("PUT",  "/api/sag/{sag_id}", (200, 204)),       # [k]
    "soeg_person":      Operation("POST", "/api/person/search", (200,)),          # [k]
    "soeg_firma":       Operation("POST", "/api/firma/search", (200,)),           # [k]
    "soeg_bruger":      Operation("POST", "/api/bruger/search", (200,)),          # [k]
    "tilknyt_part":     Operation("POST", "/api/sag/{sag_id}/part", (200, 201, 204)),  # [k]
    "hent_skabeloner":  Operation("GET",  "/api/sagsskabelon", (200,)),           # [k]
    "hent_afdelinger":  Operation("GET",  "/api/ansaettelsessted/alle", (200,)),  # [v]
    "hent_mig":         Operation("GET",  "/api/bruger/me", (200,)),             # [k]
    # Dokumenter oprettes og uploades i ÉT multipart-kald, ikke to.
    "journaliser":      Operation("POST", "/api/dokument/journaliser", (200, 201)),    # [k]
    # Kladder. Et dokument paa en sag i SBSYS er enten journaliseret eller
    # en kladde (GO's Journaliseret = 1/0), og de to former oprettes ad
    # hver sin vej. Stierne er bekraeftet 11-11-2026; kroppens feltnavne
    # er derimod gaettet efter moensteret fra journaliseringen, saa
    # [k] staar indtil et rigtigt kald har bevist dem.
    "opret_kladde":     Operation("POST", "/api/kladde", (200, 201)),                  # [v] sti, [k] krop
    "soeg_kladde":      Operation("POST", "/api/kladde/search", (200,)),               # [v]
    "hent_dokumenter":  Operation("GET",  "/api/sag/{sag_id}/dokumenter", (200,)),     # [k]
    "opret_notat":      Operation("POST", "/api/journalarknote/create", (200, 201)),   # [k]
    # Boern og Unge-API'et — ANDEN vaert, se SBSYS_BU_BASE_URL. Stierne er
    # laest direkte af tjenestens egen OpenAPI-beskrivelse 09-11-2026.
    "opret_indsats":    Operation("POST", "/bu_api/sag_foranstaltning", (200, 201)),   # [v]
    "opdater_indsats":  Operation("PUT",  "/bu_api/sag_foranstaltning/{indsats_id}", (200,)),  # [v]
    "soeg_indsats":     Operation("POST", "/bu_api/sag_foranstaltning/search", (200,)),        # [v]
    # Sagens egne indsatser. Formaalsbygget og uden et soegeskema at gaette
    # paa — derfor den vi bruger til at opdage en indsats der allerede er
    # oprettet. ADVARSEL: GET /bu_api/sag_foranstaltning/{id} ser ud til at
    # vaere den samme ting, men er det IKKE. Tjenestens egen beskrivelse
    # siger "alle indsatser, paa alle sager, som har en given primaer part":
    # {id} er en person, ikke en indsats. Et opslag paa et indsats-id dér
    # svarer altsaa med noget helt andet end det man tror.
    "hent_sag_indsatser": Operation(                                                   # [v]
        "GET", "/bu_api/sag/{sag_id}/sag_foranstaltninger", (200, 204, 404)),
    # Tilbudsfelter: et tilbud kan have egne felter, og vaerdierne saettes
    # i et SELVSTAENDIGT kald EFTER indsatsen findes. Se A19.
    "hent_tilbudfelter": Operation("GET", "/bu_api/tilbud/{tilbud_id}/tilbudfelt", (200,)),    # [v]
    "opret_tilbudfeltvaerdi": Operation(
        "POST", "/bu_api/sag_foranstaltning/{indsats_id}/tilbudfeltvaerdi", (200, 201)),       # [v]
    # Livstegn paa BU-vaerten. Bemaerk /api/, ikke /bu_api/.
    "bu_livstegn":      Operation("GET",  "/api/health", (200,)),                              # [v]
    "hent_katalog":     Operation("GET",  "/bu_api/foranstaltning/katalog", (200,)),           # [v]
    "hent_paragraffer": Operation("GET",  "/bu_api/paragraf", (200,)),                         # [v]
    "hent_tilbud":      Operation("GET",  "/bu_api/tilbud", (200,)),                           # [v]
    "hent_udslagsgivende": Operation("GET", "/bu_api/udslagsgivende_forhold", (200,)),         # [v]
    # Samme sti som hent_version, men et andet formaal: livstegn er ikke
    # en kodeliste, og det skal kunne laeses i koden hvad kaldet skal.
    "hent_statusser":   Operation("GET",  "/api/sag/sagStatusList", (200,)),      # [k]
}

# Kanonisk feltnavn -> SBSYS feltnavn.
# `ekstern_noegle` er afgørende: det er her GO-nøglen skrives med, så et
# objekt kan genfindes hvis id_map og SBSYS kommer ud af trit.
STANDARD_FELTER: dict[str, dict[str, str]] = {
    "sag": {
        # SBSYS kalder titlen SagsTitel ved oprettelse og soegning, men
        # Titel ved laesning. Her skrives, saa SagsTitel.
        "titel": "SagsTitel",
        "beskrivelse": "Beskrivelse",         # TODO
        "kle": "SagsNummer",                  # TODO
        "facet": "Facet",                     # TODO
        "sagstype": "SagsTypeId",             # TODO
        "status": "SagsStatusId",             # TODO
        # Bekraeftet 02-11-2026: ansaettelsesstedet peges paa ved reference,
        # ikke ved vaerdi — ExternalAnsaettelsesstedreferenceDto har
        # {AnsaettelsesstedId, ExternalKey}. Punktummet giver et indlejret
        # objekt; se byg_payload.
        "ansvarlig": "SagsBehandlerID",
        # Bekraeftet 02-11-2026: alle 15 ansaettelsessteder i Aarhus' testmiljoe
        # har TOM ExternalKey, saa der kan ikke peges paa dem med ekstern
        # noegle. mapping/afdeling.csv skal give AnsaettelsesstedId.
        "afdeling": "Ansaettelsessted.AnsaettelsesstedId",
        "skabelon": "SkabelonId",
        "oprettet_dato": "SagsDato",          # TODO
        "afsluttet_dato": "Afsluttetdato",    # TODO
        # Skabelonen kraever parter. Begge skal med: PrimaryPart er den
        # sagen handler om, Parts er listen af alle tilknyttede.
        "primaer_part": "PrimaryPart",
        "parter": "Parts",
        "ekstern_noegle": "EksternSagId",     # soegbar; se A17/A18
    },
    "dokument": {
        "titel": "DokumentNavn",
        "kategori": "DokumentTypeId",         # TODO
        "dokument_dato": "DokumentDato",      # TODO
        "retning": "Kategori",                # TODO
        "forfatter": "OprettetAfId",          # TODO
        # Journaliseringen skriver "Eksternid"; soegningen laeser "EksternId".
        "ekstern_noegle": "Eksternid",
    },
    "part": {
        # SBSYS peger paa parten ved sit EGET id, ikke ved CPR.
        "part_id": "PartId",
        "part_type": "PartType",
        "navn": "Navn",                       # TODO
        "cpr": "CprNummer",                   # TODO
        "cvr": "CvrNummer",                   # TODO
        "ekstern_noegle": "EksternNoegle",    # TODO
    },
    "kladde": {
        # KladdeRedigerDto / KladdeMetadataDto. Navn er kladdens titel,
        # Filnavn og Filendelse staar hver for sig — i modsaetning til
        # journaliseringen, hvor filnavnet foelger med filen.
        "sag_id": "SagID",
        "titel": "Navn",
        "beskrivelse": "Beskrivelse",
        "emne": "Emne",
        "filnavn": "Filnavn",
        "filendelse": "Filendelse",
    },
    "notat": {
        # Bekraeftet mod snitfladen: journalarknoten har SagID, Overskrift,
        # Note og KontaktTidspunkt. Note renderes som HTML.
        "sag_id": "SagID",
        "titel": "Overskrift",
        "tekst": "Note",
        "dato": "KontaktTidspunkt",
        "ekstern_noegle": "EksternNoegle",    # TODO — findes den paa noter?
    },
}


# SBSYS' søgning vil have kriterierne pakket ind i et objekt — svarer den
# "Query er null", er det dette den savner. Navnet står her og ikke i koden,
# fordi det er en egenskab ved installationen, ikke ved konverteringen.
# Kommunens egen klient sender kriterierne i roden sammen med PageNumber og
# PageSize, og den form ER verificeret mod testmiljøet. Derfor ingen
# indpakning som standard. Felterne står her, fordi navnene varierer mellem
# API-versioner.
STANDARD_SOEGNING = {
    "indpak": "",
    "side_felt": "PageNumber",
    "side_stoerrelse_felt": "PageSize",
    "side_stoerrelse": 100,
    "resultat_felt": "Results",
}


class ManglendeOperation(KeyError):
    pass


def _tal_hvis_id(feltnavn: str, vaerdi: Any) -> Any:
    """Send et id som tal, ikke som streng.

    Kodelisterne er CSV, saa alt derfra er tekst. SBSYS' id-felter er
    heltal, og "4711" afvises hvor 4711 accepteres. Reglen er bevidst
    snaever: kun felter der slutter paa Id, og kun vaerdier der udelukkende
    er cifre — et sagsnummer som "17.01.05" roeres ikke.
    """
    if isinstance(vaerdi, str) and feltnavn.lower().endswith("id") and vaerdi.isdigit():
        return int(vaerdi)
    return vaerdi


class Endpoints:
    def __init__(
        self,
        operationer: dict[str, Operation] | None = None,
        felter: dict[str, dict[str, str]] | None = None,
        soegning: dict[str, Any] | None = None,
    ) -> None:
        self._soegning = {**STANDARD_SOEGNING, **(soegning or {})}
        self._operationer = dict(STANDARD_OPERATIONER)
        self._operationer.update(operationer or {})
        self._felter = {k: dict(v) for k, v in STANDARD_FELTER.items()}
        for objekt, kort in (felter or {}).items():
            self._felter.setdefault(objekt, {}).update(kort)

    @classmethod
    def fra_fil(cls, sti: Path | None = None) -> Endpoints:
        sti = sti or (Path(os.environ["KONV_ENDPOINTS"]) if os.environ.get("KONV_ENDPOINTS") else None)
        if sti is None or not sti.exists():
            log.warning(
                "Ingen endpoints-fil fundet — bruger placeholder-stier. "
                "Disse SKAL verificeres mod SBSYS' snitfladebeskrivelse."
            )
            return cls()
        data = tomllib.loads(sti.read_text(encoding="utf-8"))
        operationer = {
            navn: Operation(
                metode=v["metode"],
                sti=v["sti"],
                forventede_status=tuple(v.get("forventede_status", (200, 201, 202, 204))),
            )
            for navn, v in data.get("operationer", {}).items()
        }
        return cls(
            operationer=operationer,
            felter=data.get("felter", {}),
            soegning=data.get("soegning", {}),
        )

    @property
    def notat_er_html(self) -> bool:
        """Indeholder GO's notattekst allerede HTML?

        SBSYS renderer notefeltet som HTML. Er teksten ren tekst, skal den
        escapes, ellers bliver et "<" i en adresse til et tag. Standard er
        ren tekst, fordi det er det sikre valg: escapet HTML ser forkert ud,
        men uescapet tekst kan OEDELAEGGE notatet.
        """
        return bool(self._soegning.get("notat_er_html"))

    @property
    def soegnings_indpakning(self) -> str | None:
        """Nøglen kriterierne pakkes ind i, fx "Query". Tom = ingen indpakning."""
        return self._soegning.get("indpak") or None

    def soegningskrop(self, kriterier: dict[str, Any]) -> dict[str, Any]:
        """Byg søgekroppen med paging, som SBSYS kræver den.

        Vi søger altid efter én bestemt ekstern nøgle og vil højst have ét
        svar, men paging-felterne skal med: uden dem afviser SBSYS kaldet.
        """
        krop = dict(kriterier)
        indpak = self.soegnings_indpakning
        if indpak:
            krop = {indpak: dict(kriterier)}
        krop[str(self._soegning["side_felt"])] = 1
        krop[str(self._soegning["side_stoerrelse_felt"])] = self._soegning["side_stoerrelse"]
        return krop

    def soegningstraef(self, krop: Any) -> list[dict[str, Any]]:
        """Træk resultatrækkerne ud, uanset om svaret er en liste eller et objekt."""
        if isinstance(krop, list):
            return krop
        if isinstance(krop, dict):
            return list(krop.get(str(self._soegning["resultat_felt"])) or [])
        return []

    def operation(self, navn: str, **sti_parametre: Any) -> Operation:
        try:
            op = self._operationer[navn]
        except KeyError as e:
            raise ManglendeOperation(f"Operationen '{navn}' er ikke defineret") from e
        return Operation(op.metode, op.sti.format(**sti_parametre), op.forventede_status)

    def felt(self, objekt: str, kanonisk_navn: str) -> str:
        try:
            return self._felter[objekt][kanonisk_navn]
        except KeyError as e:
            raise ManglendeOperation(
                f"Feltet '{kanonisk_navn}' er ikke mappet for objekttypen '{objekt}'"
            ) from e

    def byg_payload(self, objekt: str, vaerdier: dict[str, Any]) -> dict[str, Any]:
        """Oversæt kanoniske feltnavne til SBSYS-feltnavne.

        None-værdier udelades: vi sender aldrig tomme felter med, fordi nogle
        API'er tolker eksplicit null som "nulstil" i stedet for "ikke angivet".

        Et feltnavn med punktummer bliver til et indlejret objekt:

            afdeling = "Ansaettelsessted.ExternalKey"
            ->  {"Ansaettelsessted": {"ExternalKey": "..."}}

        SBSYS peger på flere ting ved reference frem for ved værdi
        (ansættelsessted, skabelon, part), og hvilken form det er, skal
        kunne rettes i endpoints.toml — ikke i koden.
        """
        ud: dict[str, Any] = {}
        for kanonisk, vaerdi in vaerdier.items():
            if vaerdi is None:
                continue
            dele = self.felt(objekt, kanonisk).split(".")
            maal = ud
            for led in dele[:-1]:
                naeste = maal.setdefault(led, {})
                if not isinstance(naeste, dict):
                    raise ManglendeOperation(
                        f"Feltet '{led}' er både en værdi og et objekt i "
                        f"felter.{objekt}. Ret endpoints.toml."
                    )
                maal = naeste
            maal[dele[-1]] = _tal_hvis_id(dele[-1], vaerdi)
        return ud
