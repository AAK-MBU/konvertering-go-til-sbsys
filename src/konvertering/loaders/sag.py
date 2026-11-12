"""Sagsloader — den første og vigtigste. Alt andet hænger på en sag."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from ..models import Objekttype, Sag
from ..sbsys.errors import PermanentFejl, ValideringsFejl
from .base import Loader, boelge_filter

log = logging.getLogger(__name__)


class SagLoader(Loader[Sag]):
    arbejdsliste = "stg.v_sager_til_indlaesning"
    objekt_type = Objekttype.SAG

    def __init__(self, k) -> None:  # noqa: ANN001
        super().__init__(k)
        self._part_cache: dict[str, int] = {}

    def hent_arbejde(self, graense: int | None = None) -> Iterable[dict[str, Any]]:
        # Sideopdelt efter nøgle, ikke streamet: loaderen skriver til
        # konv.id_map undervejs, og et commit dræber en åben cursor.
        filter_sql, params = boelge_filter(self.k.boelge)
        return self.k.db.hent_i_sider(
            self.arbejdsliste,
            filter_sql,
            parametre=params,
            graense=graense,
        )

    def byg(self, raekke: dict[str, Any]) -> Sag:
        m = self.k.kodelister
        return Sag(
            kilde_noegle=str(raekke["kilde_noegle"]),
            titel=(raekke.get("titel") or "").strip() or "Uden titel",
            go_sagsnummer=raekke.get("go_sagsnummer"),
            beskrivelse=raekke.get("beskrivelse"),
            # KLE er blokerende: en sag uden korrekt emnesystematik må ikke
            # oprettes — den kan ikke findes igen og ikke bevaringsvurderes.
            kle=m.slaa_op("kle", raekke.get("kle_go"), paakraevet=True),
            facet=m.slaa_op("facet", raekke.get("facet_go"), paakraevet=False),
            sagstype=m.slaa_op("sagstype", raekke.get("sagstype_go"), paakraevet=False),
            # Skabelonen er blokerende: SBSYS kan ikke oprette en sag uden.
            skabelon=m.slaa_op("sagsskabelon", raekke.get("sagstype_go"), paakraevet=True),
            status=m.slaa_op("sagsstatus", raekke.get("status_go"), paakraevet=False),
            # Ukendt sagsbehandler er ikke blokerende: sager fra medarbejdere
            # der er fratrådt skal stadig med over. De havner på en fælles
            # konto via 'standard'-handlingen i kodelisten.
            ansvarlig=m.slaa_op("bruger", raekke.get("ansvarlig_go"), paakraevet=False),
            afdeling=m.slaa_op("afdeling", raekke.get("afdeling_go"), paakraevet=True),
            oprettet_dato=raekke.get("oprettet_dato"),
            afsluttet_dato=raekke.get("afsluttet_dato"),
            fortrolighed=m.slaa_op("fortrolighed", raekke.get("fortrolighed_go"), paakraevet=False),
            bevaring=m.slaa_op("bevaring", raekke.get("bevaring_go"), paakraevet=False),
            primaer_part_noegle=raekke.get("primaer_part_noegle"),
        )

    def payload(self, objekt: Sag) -> dict[str, Any]:
        krop = self.k.endpoints.byg_payload(
            "sag",
            {
                "titel": objekt.titel,
                "beskrivelse": objekt.beskrivelse,
                "kle": objekt.kle,
                "facet": objekt.facet,
                "sagstype": objekt.sagstype,
                "skabelon": objekt.skabelon,
                "status": objekt.status,
                "ansvarlig": objekt.ansvarlig,
                "afdeling": objekt.afdeling,
                "oprettet_dato": objekt.oprettet_dato.isoformat() if objekt.oprettet_dato else None,
                "afsluttet_dato": objekt.afsluttet_dato.isoformat() if objekt.afsluttet_dato else None,
                # Ingen ekstern_noegle: besluttet 11-11-2026 at GO-nøglen IKKE
                # skrives ind i SBSYS. Koblingen lever kun i konv.id_map.
                # Se ADR-0001 og _find_identisk nedenfor for følgen.
            },
        )
        # Parten bygges HER og ikke i send(), selv om det koster et opslag i
        # dry-run: ellers viser --vis-payload en payload der ikke er den der
        # sendes, og så er gennemsynet før første rigtige kald værdiløst.
        # Opslaget er read-only.
        part = self._part_reference(objekt)
        krop[self.k.endpoints.felt("sag", "primaer_part")] = part
        krop[self.k.endpoints.felt("sag", "parter")] = [part]
        return krop

    def send(self, objekt: Sag, payload: dict[str, Any]) -> tuple[str, str | None]:
        # Før oprettelse: findes sagen allerede? Fanger det tilfælde hvor et
        # tidligere kald nåede frem, men svaret gik tabt.
        eksisterende = self._find_identisk(objekt, payload)
        if eksisterende:
            log.warning("Sag %s fandtes allerede i SBSYS (id=%s) — genbruger",
                        objekt.kilde_noegle[:12], eksisterende)
            return eksisterende, None

        op = self.k.endpoints.operation("opret_sag")
        svar = self.k.klient.kald(
            op.metode, op.sti, operation="opret_sag", json=payload,
            forventede_status=op.forventede_status,
            kontekst={"objekt_type": "sag", "kilde_noegle": objekt.kilde_noegle},
        )
        krop = svar.krop or {}
        sbsys_id = _foerste(krop, "Id", "id", "SagId", "sagId")
        if not sbsys_id:
            raise PermanentFejl(
                "SBSYS returnerede intet id ved oprettelse af sag",
                korrelations_id=svar.korrelations_id,
            )
        return str(sbsys_id), _foerste(krop, "Nummer", "SagsNummer", "sagsnummer")

    def _part_reference(self, objekt: Sag) -> dict[str, Any]:
        """Slå sagens primære part op i SBSYS og byg referencen.

        Raises:
            ValideringsFejl: Hvis sagen ingen part har, eller parten ikke
                findes i SBSYS. Begge dele afviser ÉN sag og stopper ikke
                bølgen — en borger der er udvandret eller afgået skal en
                fagperson tage stilling til.
        """
        if not objekt.primaer_part_noegle:
            raise ValideringsFejl(
                "Sagen har ingen primær part, og SBSYS' skabelon kræver en. "
                "Hvad der skal ske med emnesager uden borger er spørgsmål A16.",
                regel="mangler_part", felt="primaer_part_noegle",
            )

        raekke = self.k.db.hent_en(
            "SELECT part_type, cpr, cvr, navn FROM stg.part WHERE kilde_noegle = ?",
            objekt.primaer_part_noegle,
        )
        if not raekke:
            raise ValideringsFejl(
                f"Parten {objekt.primaer_part_noegle[:12]} findes ikke i stg.part",
                regel="ukendt_part", felt="primaer_part_noegle",
            )

        er_firma = (raekke.get("part_type") or "").lower() in ("virksomhed", "firma")
        nummer = (raekke.get("cvr") if er_firma else raekke.get("cpr")) or ""
        nummer = nummer.strip()
        if not nummer:
            raise ValideringsFejl(
                "Parten har hverken CPR eller CVR, og SBSYS slår op på nummeret",
                regel="part_uden_nummer", felt="cpr",
            )

        part_id = self._slaa_part_op(nummer, er_firma)
        return {
            self.k.endpoints.felt("part", "part_id"): part_id,
            self.k.endpoints.felt("part", "part_type"): "Firma" if er_firma else "Person",
        }

    def _slaa_part_op(self, nummer: str, er_firma: bool) -> int:
        """CPR/CVR -> SBSYS' interne PartId. Resultatet huskes for kørslen.

        Mange sager deler den samme borger — søskende, flere PPR-forløb —
        og uden cachen ville vi slå den samme op tusindvis af gange.
        """
        if nummer in self._part_cache:
            return self._part_cache[nummer]

        op = self.k.endpoints.operation("soeg_firma" if er_firma else "soeg_person")
        felt = self.k.endpoints.felt("part", "cvr" if er_firma else "cpr")
        # SBSYS vil have CPR med bindestreg: DDMMYY-NNNN.
        vaerdi = nummer if er_firma or "-" in nummer else f"{nummer[:6]}-{nummer[6:]}"
        svar = self.k.klient.kald(
            op.metode, op.sti, operation=op.sti.strip("/").replace("/", "_"),
            json={felt: vaerdi}, forventede_status=(200, 204, 404),
        )
        traef = self.k.endpoints.soegningstraef(svar.krop)
        if not traef:
            raise ValideringsFejl(
                f"{'CVR' if er_firma else 'CPR'} findes ikke i SBSYS. Historiske, "
                f"udenlandske og fiktive numre er netop det spørgsmål A8 handler om.",
                regel="part_ikke_i_sbsys", felt="cvr" if er_firma else "cpr",
            )
        part_id = _foerste(traef[0], "Id", "id", "PartId")
        if not part_id:
            raise ValideringsFejl("Partsøgningen returnerede intet id", regel="part_uden_id")
        self._part_cache[nummer] = int(part_id)
        return int(part_id)

    def _find_identisk(self, objekt: Sag, payload: dict[str, Any]) -> str | None:
        """Findes sagen allerede i SBSYS?

        GO-nøglen skrives ikke ind i SBSYS (besluttet 11-11-2026), så der
        er ingen fremmed nøgle at søge på. Tilbage står en **naturlig
        nøgle**: den primære part plus sagens titel. To sager på samme
        barn med samme titel er ikke til at skelne fra hinanden — og det
        er præcis derfor kun ÉT træf accepteres. Flere træf behandles som
        "ved ikke", og sagen fejler hellere end at hægte dokumenter og
        indsatser på den forkerte.

        Det er svagere end et nøglefelt. Konsekvensen står i ADR-0001:
        et tabt svar på en sag med en tvetydig titel skal ryddes op i
        hånden med ``konv genskab``.
        """
        try:
            op = self.k.endpoints.operation("soeg_sag")
        except Exception:
            return None

        part = payload.get(self.k.endpoints.felt("sag", "primaer_part"))
        if not part or not objekt.titel:
            # Uden en part at afgrænse på ville søgningen ramme hele SBSYS.
            # Så hellere ingen kontrol end en der tilfældigvis rammer.
            return None

        kriterier: dict[str, Any] = {
            self.k.endpoints.felt("sag", "titel"): objekt.titel,
            self.k.endpoints.felt("sag", "primaer_part"): part,
        }
        # SBSYS' søgning er en POST med kriterierne i kroppen, ikke en GET
        # med query-parametre (en GET giver 405), og paging-felterne SKAL
        # med. Formen er den som kommunens egen SBSYS-klient bruger og har
        # verificeret mod testmiljøet.
        som_krop = op.metode.upper() in ("POST", "PUT", "PATCH")
        svar = self.k.klient.kald(
            op.metode, op.sti, operation="soeg_sag",
            json=self.k.endpoints.soegningskrop(kriterier) if som_krop else None,
            params=None if som_krop else kriterier,
            forventede_status=(200, 204, 404),
        )
        if svar.status != 200 or not svar.krop:
            return None
        traef = self.k.endpoints.soegningstraef(svar.krop)
        if len(traef) == 1:
            return str(_foerste(traef[0], "Id", "id", "SagId") or "") or None
        return None


def _foerste(krop: Any, *navne: str) -> Any:
    if not isinstance(krop, dict):
        return None
    for navn in navne:
        if krop.get(navn) is not None:
            return krop[navn]
    return None
