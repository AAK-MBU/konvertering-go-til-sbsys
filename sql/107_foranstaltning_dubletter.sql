/*
  107_foranstaltning_dubletter.sql — foranstaltninger der er HELT ens i alle
  felter (undtagen GO's loebenummer ID). Til PPR's afgoerelse: én indsats
  eller flere?

  Kun laesning, kun mod GOStaging.

  To resultater:
    1. Optaelling pr. type — kun tal, maa gerne deles.
    2. Listen med SAGSNUMRE — til dig og PPR, saa I kan slaa sagerne op i
       GO. Indeholder sagsnumre: del den ikke uden for kredsen der maa se
       sagerne.

  "kopier" er hvor mange gange den samme registrering staar. 2 betyder at
  den er registreret to gange, altsaa én ekstra.
*/

SET NOCOUNT ON;

;WITH grupper AS (
    SELECT f.[SagsId],
             f.[Indholdstype],
             f.[Visiteret],
             f.[Fra],
             f.[Til],
             f.[Sagsbehandler],
             f.[ABAGruppe],
             f.[Ydelse],
             f.[SkoleEllerInstitution],
             f.[Betalingssted],
             f.[Timer],
             f.[VisiteretTilTilbud],
             f.[Visitationsteam],
             f.[Specialundervisningstilbud],
             f.[TypeAfEnkelteIntegration],
             f.[Betalingskommune],
             f.[Skoleaar],
             f.[Faggruppe],
             f.[Indsats],
             f.[Institutionsnavn],
             f.[Aarsag],
             f.[HenvisningsAarsag],
             f.[Klassetrin],
             f.[Klassebetegnelse],
             f.[Skole],
             f.[NavnPaaSkole],
             f.[Bopaelsoplysning],
             f.[AndenBetalingskommune],
             f.[EkstraBevilling],
             f.[IndstilletAf],
             f.[Indstillingsaarsag],
             f.[Aktivitet],
             f.[BetegnelseSpecialklasse],
             f.[FritSkolevalg],
             f.[NavnPaaSpecialskole],
             f.[BetegnelseSpecialskole],
             f.[Bevillingstype],
             f.[Institutionstype],
             f.[Bevillingssted],
             f.[Bevillingsaarsag],
             f.[Emneliste],
           kopier = COUNT(*),
           foerste_id = MIN(f.[ID]),
           sidste_id  = MAX(f.[ID])
    FROM   GOStaging.dbo.Foranstaltningsdata AS f
    GROUP BY f.[SagsId],
             f.[Indholdstype],
             f.[Visiteret],
             f.[Fra],
             f.[Til],
             f.[Sagsbehandler],
             f.[ABAGruppe],
             f.[Ydelse],
             f.[SkoleEllerInstitution],
             f.[Betalingssted],
             f.[Timer],
             f.[VisiteretTilTilbud],
             f.[Visitationsteam],
             f.[Specialundervisningstilbud],
             f.[TypeAfEnkelteIntegration],
             f.[Betalingskommune],
             f.[Skoleaar],
             f.[Faggruppe],
             f.[Indsats],
             f.[Institutionsnavn],
             f.[Aarsag],
             f.[HenvisningsAarsag],
             f.[Klassetrin],
             f.[Klassebetegnelse],
             f.[Skole],
             f.[NavnPaaSkole],
             f.[Bopaelsoplysning],
             f.[AndenBetalingskommune],
             f.[EkstraBevilling],
             f.[IndstilletAf],
             f.[Indstillingsaarsag],
             f.[Aktivitet],
             f.[BetegnelseSpecialklasse],
             f.[FritSkolevalg],
             f.[NavnPaaSpecialskole],
             f.[BetegnelseSpecialskole],
             f.[Bevillingstype],
             f.[Institutionstype],
             f.[Bevillingssted],
             f.[Bevillingsaarsag],
             f.[Emneliste]
    HAVING COUNT(*) > 1
)
SELECT * INTO #dubletter FROM grupper;

/* --- 1. Optaelling pr. type (kun tal) --------------------------------- */
SELECT  [Indholdstype]           AS indholdstype,
        COUNT(*)                 AS registreringer_med_dubletter,
        SUM(kopier)              AS raekker_i_alt,
        SUM(kopier - 1)          AS ekstra_raekker
FROM    #dubletter
GROUP BY [Indholdstype]
ORDER BY ekstra_raekker DESC;

/* --- 2. Listen (indeholder sagsnumre) --------------------------------- */
SELECT  [SagsId]        AS sagsnummer,
        [Indholdstype]  AS indholdstype,
        [Visiteret]     AS visiteret,
        [Fra]           AS fra,
        [Til]           AS til,
        kopier,
        foerste_id      AS go_raekke_id_foerste,
        sidste_id       AS go_raekke_id_sidste
FROM    #dubletter
ORDER BY [Indholdstype], [SagsId];

DROP TABLE #dubletter;
