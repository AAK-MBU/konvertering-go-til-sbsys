"""Indsatser (foranstaltninger).

Et andet API end resten af konverteringen: Børn og Unge-tjenesten på sin
egen vært, `POST /bu_api/sag_foranstaltning`. Tokenet gælder på tværs
(bekræftet 09-11-2026), så kun adressen er anderledes —
``SBSYS_BU_BASE_URL``.

Tre beslutninger fra 09-11-2026 former loaderen:

**Økonomi er ikke en del af konverteringen.** Vederlag, tillægspris,
engangstillæg og antal døgn sendes derfor ikke, selv om kaldet tager dem.
Vi sender perioden, omfanget og hjemlen. Bemærk at SBSYS alligevel
returnerer en beregning — den ignoreres, men hvis tilbuddet har takster,
kan SBSYS selv regne videre på indsatsen. Det er SBSYS' egen logik og
uden for vores kontrol.

**GO-nøglen må ikke skrives ind i SBSYS.** Dermed findes ADR-0001's
sikkerhedsnet ikke her, og vi kan ikke genfinde en indsats på vores egen
nøgle. I stedet bruges en **naturlig nøgle**: en indsats er den samme hvis
sag, foranstaltning, tilbud og startdato er ens. Søgningen kan netop
filtrere på det, så et tabt svar kan opdages uden et felt i SBSYS.

**E2a og E2b mappes senere.** Paragraf, foranstaltning og tilbud er
blokerende: uden dem afvises indsatsen til `konv.afvist`, en fagperson
udfylder kodelisten, og næste kørsel tager den med. Det er samme
reparationssløjfe som for KLE på sagerne.

Konverteringsarket (17-11-2026) gav tre regler mere:

**Tilbuddet ER foranstaltningsmappens type** (GO's ``Indholdstype``:
Psykolog, ABA ...). Alle tre id'er i tripellet slås op på typen. GO's
``Ydelse`` og ``Bevillingstype`` er almindelige tilbudsfelter, ikke tripellet.

**En tom Fra-dato bliver 1900-01-01** — ikke en afvisning.

**Nogle typer konverteres ikke** ("data skal IKKE konverteres"). De står i
kodelisten med ``handling = ignorer`` og springes over.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from ..models import Indsats, Objekttype
from ..sbsys.errors import PermanentFejl, UdeladtMedVilje, ValideringsFejl
from .base import Loader, boelge_filter

log = logging.getLogger(__name__)

# SBSYS' egne enhedstyper. Andre værdier afvises frem for at blive gættet
# på: "timer" og "Time" er ikke det samme for en .NET-enum.
ENHEDSTYPER = ("Time", "Doegn", "Uge", "Maaned", "Aar", "Stk")

# Konverteringsarket: "Hvis feltet er tomt i GO, skal fiktiv dato sættes
# 1900-01-01 00:00:00" — for alle typer.
FIKTIV_STARTDATO = datetime(1900, 1, 1)


class IndsatsLoader(Loader[Indsats]):
    arbejdsliste = "stg.v_indsatser_til_indlaesning"
    objekt_type = Objekttype.INDSATS

    def hent_arbejde(self, graense: int | None = None) -> Iterable[dict[str, Any]]:
        filter_sql, params = boelge_filter(self.k.boelge)
        return self.k.db.hent_i_sider(
            self.arbejdsliste,
            filter_sql,
            parametre=params,
            graense=graense,
        )

    def byg(self, raekke: dict[str, Any]) -> Indsats:
        m = self.k.kodelister
        enhed = (raekke.get("enhedstype_go") or "").strip()
        typen = raekke.get("indholdstype_go")
        if m.er_udeladt("tilbud", typen):
            raise UdeladtMedVilje(f"foranstaltningstypen '{typen}' konverteres ikke")
        return Indsats(
            kilde_noegle=str(raekke["kilde_noegle"]),
            sag_noegle=str(raekke["sag_noegle"]),
            # Alle tre er blokerende: SBSYS kan ikke oprette en indsats uden.
            # Og alle tre slås op på mappetypen — tilbuddet ER typen.
            paragraf=m.slaa_op("paragraf", typen, paakraevet=True),
            foranstaltning=m.slaa_op("foranstaltning", typen, paakraevet=True),
            tilbud=m.slaa_op("tilbud", typen, paakraevet=True),
            periode_start=raekke.get("periode_start") or FIKTIV_STARTDATO,
            periode_slut=raekke.get("periode_slut"),
            antal_enheder=raekke.get("antal_enheder"),
            enhedstype=m.slaa_op("enhedstype", enhed, paakraevet=False) or (
                enhed if enhed in ENHEDSTYPER else None
            ),
            udslagsgivende_forhold=m.slaa_op(
                "udslagsgivende_forhold", raekke.get("udslagsgivende_go"), paakraevet=False
            ),
        )

    def payload(self, objekt: Indsats) -> dict[str, Any]:
        if not objekt.periode_start:
            raise ValideringsFejl(
                "Indsatsen har ingen startdato. En indsats uden periode er ikke "
                "en indsats, og SBSYS kræver periodeStart.",
                regel="mangler_periode", felt="periode_start",
            )
        if objekt.antal_enheder is not None and not objekt.enhedstype:
            raise ValideringsFejl(
                f"Antal enheder er sat ({objekt.antal_enheder}), men enhedstypen "
                f"kendes ikke. SBSYS tager kun {', '.join(ENHEDSTYPER)}.",
                regel="ukendt_enhedstype", felt="enhedstype_go",
            )

        krop: dict[str, Any] = {
            "paragrafId": int(objekt.paragraf),       # type: ignore[arg-type]
            "foranstaltningId": int(objekt.foranstaltning),  # type: ignore[arg-type]
            "tilbudId": int(objekt.tilbud),           # type: ignore[arg-type]
            "periodeStart": objekt.periode_start.isoformat(),
        }
        if objekt.periode_slut:
            krop["periodeSlut"] = objekt.periode_slut.isoformat()
        if objekt.antal_enheder is not None:
            krop["antalEnheder"] = int(objekt.antal_enheder)
            krop["antalEnhederEnhedsType"] = objekt.enhedstype
        if objekt.udslagsgivende_forhold:
            krop["udslagsgivendeForholdIds"] = [int(objekt.udslagsgivende_forhold)]
        # Ingen antalVederlag, tillaegsPrisPerEnhed, engangsTillaeg eller
        # antalDoegn: økonomi er ikke en del af konverteringen.
        return krop

    def send(self, objekt: Indsats, payload: dict[str, Any]) -> tuple[str, str | None]:
        if self.k.bu_klient is None:
            raise ValideringsFejl(
                "SBSYS_BU_BASE_URL er ikke sat. Indsatser ligger i Børn og "
                "Unge-API'et, som er en selvstændig tjeneste på sin egen vært.",
                regel="bu_api_mangler",
            )

        sbsys_sag_id = self.k.idmap.sbsys_id(Objekttype.SAG, objekt.sag_noegle)
        if not sbsys_sag_id:
            raise ValideringsFejl(
                f"Sagen {objekt.sag_noegle[:12]} er ikke indlæst endnu — "
                f"indsatsen kan ikke placeres",
                regel="sag_ikke_indlaest", felt="sag_noegle",
            )

        krop = {"sagId": int(sbsys_sag_id), **payload}

        # Naturlig nøgle frem for ekstern: GO-nøglen må ikke skrives ind i
        # SBSYS, så det her er den eneste måde at opdage at et tidligere kald
        # nåede frem, men at svaret gik tabt.
        eksisterende = self._find_identisk(krop)
        if eksisterende:
            log.warning("Indsats %s fandtes allerede i SBSYS (id=%s) — genbruger",
                        objekt.kilde_noegle[:12], eksisterende)
            return eksisterende, None

        op = self.k.endpoints.operation("opret_indsats")
        svar = self.k.bu_klient.kald(
            op.metode, op.sti, operation="opret_indsats", json=krop,
            forventede_status=op.forventede_status,
            kontekst={"objekt_type": "indsats", "kilde_noegle": objekt.kilde_noegle},
        )
        legeme = svar.krop or {}
        indsats_id = legeme.get("foranstaltningSagId") or legeme.get("id")
        if not indsats_id:
            raise PermanentFejl(
                "Børn og Unge-API'et returnerede intet foranstaltningSagId",
                korrelations_id=svar.korrelations_id,
            )
        return str(indsats_id), None

    def _find_identisk(self, krop: dict[str, Any]) -> str | None:
        """Findes indsatsen allerede på sagen — oprettet af en tidligere
        kørsel, hvis svar gik tabt før id'et blev gemt?

        Spørger sagen om dens egne indsatser
        (``GET /bu_api/sag/{sagId}/sag_foranstaltninger``) frem for at søge.
        Søgningen kan det samme, men kræver at vi gætter rigtigt på
        kriteriernes navne; sagens egen liste kræver kun et sags-id vi i
        forvejen har, og den er formålsbygget til netop dette spørgsmål.

        Et træf er samme tilbud, paragraf, startdato og — hvis SBSYS oplyser
        den — slutdato. Det er IKKE nok i sig selv: to forskellige GO-
        foranstaltninger af samme type på samme sag kan have samme periode
        (testdumpet 17-11-2026: 63 rækker delte den gamle nøgle). Derfor
        fjernes de træf, konverteringen allerede KENDER fra id_map. De er
        søskende, der allerede er oprettet, ikke den tabte.

        Returns:
            SBSYS-id'et hvis præcis ét ukendt træf er tilbage, ellers None.

        Raises:
            PermanentFejl: flere ukendte træf. At genbruge det ene ville være
                et gæt, og at oprette endnu én ville måske give en dublet.
                Indsatsen stopper og skal ses af et menneske.
        """
        if self.k.bu_klient is None:
            return None
        try:
            op = self.k.endpoints.operation("hent_sag_indsatser", sag_id=krop["sagId"])
        except Exception:
            return None

        svar = self.k.bu_klient.kald(
            op.metode, op.sti, operation="hent_sag_indsatser",
            forventede_status=op.forventede_status,
        )
        if svar.status != 200:
            return None

        traef = [
            r for r in self.k.endpoints.soegningstraef(svar.krop)
            if isinstance(r, dict) and r.get("id") and _samme_indsats(r, krop)
        ]
        if not traef:
            return None

        kendte = self.k.idmap.kendte_sbsys_id(
            Objekttype.INDSATS, [str(r["id"]) for r in traef])
        ukendte = [r for r in traef if str(r["id"]) not in kendte]
        if not ukendte:
            return None
        if len(ukendte) == 1:
            return str(ukendte[0]["id"])
        raise PermanentFejl(
            f"Sagen har {len(ukendte)} ens indsatser i SBSYS, som konverteringen "
            f"ikke selv har registreret. Kan ikke afgøre om én af dem er denne."
        )


def _dato(r: dict[str, Any], *navne: str) -> str | None:
    """Første datofelt der findes i svaret, som ÅÅÅÅ-MM-DD. None hvis ingen
    af navnene findes; tom tekst hvis feltet findes men er tomt."""
    for navn in navne:
        if navn in r:
            return str(r[navn] or "")[:10]
    return None


def _samme_indsats(r: dict[str, Any], krop: dict[str, Any]) -> bool:
    if r.get("tilbudId") != krop["tilbudId"] or r.get("paragrafId") != krop["paragrafId"]:
        return False
    if (_dato(r, "startDato", "periodeStart") or "") != str(krop["periodeStart"])[:10]:
        return False
    # Slutdatoen sammenlignes kun hvis svaret har feltet. Ellers ville et
    # ukendt feltnavn få hver eneste indsats til at se ny ud — og give en
    # dublet ved hver genkørsel.
    slut = _dato(r, "slutDato", "periodeSlut")
    return slut is None or slut == str(krop.get("periodeSlut") or "")[:10]
