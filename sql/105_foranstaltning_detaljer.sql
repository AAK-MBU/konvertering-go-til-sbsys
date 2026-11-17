/*
  105_foranstaltning_detaljer.sql — opfoelgning paa 104: noegle, Timer,
  Sagsbehandler og datoformater i Foranstaltningsdata.

  Kun laesning. Koeres i SSMS mod serveren med GOStaging.

  Viser INGEN navne eller sagsnumre:
    * Sagsbehandler vises kun som FORMAT (fx "domaene\login"), aldrig vaerdien.
    * Datoer vises kun som moenster (fx "dd-mm-aaaa").
    * Timer vises som den staar. Det er et omfang ("15", "4 t/uge"), ikke
      persondata — men kig listen igennem, foer den sendes videre. Skulle
      der staa fritekst med navne i, saa send kun de korte vaerdier.

  Svarer paa:
    1. Kolliderer indsatsnoeglen (sag + type + ydelse + fra)? Og hvilken
       udvidelse af noeglen goer den unik?
    2. Hvordan ser Timer ud — kan antal og enhed udledes?
    3. Hvilket format har Sagsbehandler — initialer, login, navn?
    4. Hvilke datoformater staar i Visiteret, Fra og Til, og hvor mange
       kan IKKE tolkes som dd-mm-aaaa (stil 105)?
*/

SET NOCOUNT ON;

/* --- 1a. Kollisioner paa den nuvaerende noegle, pr. type --------------
   Samme noegle som extract (100_extract_skabelon.sql). "raekker_i_kollision"
   er antal raekker der deler noegle med mindst én anden. */
SELECT  f.[Indholdstype]                AS indholdstype,
        COUNT(*)                        AS noegler_i_kollision,
        SUM(x.antal)                    AS raekker_i_kollision
FROM (
    SELECT [SagsId], [Indholdstype], [Ydelse],
           TRY_CONVERT(DATE, [Fra], 105) AS fra, COUNT(*) AS antal
    FROM   GOStaging.dbo.Foranstaltningsdata
    GROUP BY [SagsId], [Indholdstype], [Ydelse], TRY_CONVERT(DATE, [Fra], 105)
    HAVING COUNT(*) > 1
) AS x
JOIN (SELECT DISTINCT [Indholdstype] FROM GOStaging.dbo.Foranstaltningsdata) AS f
  ON f.[Indholdstype] = x.[Indholdstype]
GROUP BY f.[Indholdstype]
ORDER BY raekker_i_kollision DESC;

/* --- 1b. Hjaelper en bredere noegle? ----------------------------------
   Taeller kolliderende raekker for tre kandidater. Er "helt_ens" > 0,
   findes der raekker der er ens i ALLE kolonner (undtagen ID) — de er
   aegte dubletter i GO, og ingen noegle kan skille dem ad. */
SELECT
    (SELECT COALESCE(SUM(antal), 0) FROM (
        SELECT COUNT(*) AS antal FROM GOStaging.dbo.Foranstaltningsdata
        GROUP BY [SagsId], [Indholdstype], [Ydelse], TRY_CONVERT(DATE, [Fra], 105)
        HAVING COUNT(*) > 1) a)                                    AS nuvaerende_noegle,
    (SELECT COALESCE(SUM(antal), 0) FROM (
        SELECT COUNT(*) AS antal FROM GOStaging.dbo.Foranstaltningsdata
        GROUP BY [SagsId], [Indholdstype], [Ydelse], [Fra], [Til], [Visiteret]
        HAVING COUNT(*) > 1) b)                                    AS plus_til_og_visiteret,
    (SELECT COALESCE(SUM(antal), 0) FROM (
        SELECT COUNT(*) AS antal FROM GOStaging.dbo.Foranstaltningsdata
        GROUP BY [SagsId], [Indholdstype], [Ydelse], [Fra], [Til], [Visiteret],
                 [ABAGruppe], [Timer], [Betalingssted], [SkoleEllerInstitution]
        HAVING COUNT(*) > 1) c)                                    AS plus_aba_felter,
    (SELECT COALESCE(SUM(antal), 0) FROM (
        SELECT COUNT(*) AS antal FROM GOStaging.dbo.Foranstaltningsdata
        GROUP BY [SagsId], [Indholdstype], [Visiteret], [Fra], [Til], [Sagsbehandler],
                 [ABAGruppe], [Ydelse], [SkoleEllerInstitution], [Betalingssted], [Timer],
                 [VisiteretTilTilbud], [Visitationsteam], [Specialundervisningstilbud],
                 [TypeAfEnkelteIntegration], [Betalingskommune], [Skoleaar], [Faggruppe],
                 [Indsats], [Institutionsnavn], [Aarsag], [HenvisningsAarsag], [Klassetrin],
                 [Klassebetegnelse], [Skole], [NavnPaaSkole], [Bopaelsoplysning],
                 [AndenBetalingskommune], [EkstraBevilling], [IndstilletAf],
                 [Indstillingsaarsag], [Aktivitet], [BetegnelseSpecialklasse],
                 [FritSkolevalg], [NavnPaaSpecialskole], [BetegnelseSpecialskole],
                 [Bevillingstype], [Institutionstype], [Bevillingssted],
                 [Bevillingsaarsag], [Emneliste]
        HAVING COUNT(*) > 1) d)                                    AS helt_ens;

/* --- 2. Timer -------------------------------------------------------- */
SELECT  [Indholdstype]          AS indholdstype,
        LEFT(LTRIM(RTRIM([Timer])), 40) AS timer,
        COUNT(*)                AS antal,
        CASE WHEN TRY_CONVERT(DECIMAL(9, 2), REPLACE(LTRIM(RTRIM([Timer])), ',', '.')) IS NOT NULL
             THEN 'tal' ELSE 'tekst' END AS kan_laeses_som_tal
FROM    GOStaging.dbo.Foranstaltningsdata
WHERE   NULLIF(LTRIM(RTRIM([Timer])), N'') IS NOT NULL
GROUP BY [Indholdstype], LEFT(LTRIM(RTRIM([Timer])), 40),
         CASE WHEN TRY_CONVERT(DECIMAL(9, 2), REPLACE(LTRIM(RTRIM([Timer])), ',', '.')) IS NOT NULL
              THEN 'tal' ELSE 'tekst' END
ORDER BY indholdstype, antal DESC;

/* --- 3. Sagsbehandler: kun formatet ----------------------------------
   GO bygger paa SharePoint, saa en person kan staa som en "claim"
   (i:0#.w|domaene\login), som domaene\login, som e-mail eller som navn.
   Formatet afgoer hvordan den mappes til en SBSYS-bruger. */
SELECT  format,
        COUNT(*)                      AS antal,
        COUNT(DISTINCT v)             AS forskellige,
        MIN(LEN(v))                   AS min_laengde,
        MAX(LEN(v))                   AS maks_laengde
FROM (
    SELECT v = LTRIM(RTRIM([Sagsbehandler])),
           format = CASE
               WHEN [Sagsbehandler] LIKE N'%|%'                THEN N'claim (indeholder |)'
               WHEN [Sagsbehandler] LIKE N'%\%'                THEN N'domaene\login'
               WHEN [Sagsbehandler] LIKE N'%@%'                THEN N'e-mail'
               WHEN [Sagsbehandler] LIKE N'%;#%'               THEN N'SharePoint-opslag (id;#navn)'
               WHEN LTRIM(RTRIM([Sagsbehandler])) LIKE N'% %'  THEN N'indeholder mellemrum (navn?)'
               WHEN LTRIM(RTRIM([Sagsbehandler])) LIKE N'%[0-9]%' THEN N'ét ord med cifre (AZ-ident?)'
               WHEN LEN(LTRIM(RTRIM([Sagsbehandler]))) <= 6    THEN N'kort, kun bogstaver (initialer?)'
               ELSE N'andet'
           END
    FROM   GOStaging.dbo.Foranstaltningsdata
    WHERE  NULLIF(LTRIM(RTRIM([Sagsbehandler])), N'') IS NOT NULL
) AS s
GROUP BY format
ORDER BY antal DESC;

/* --- 4. Datoformater i Visiteret, Fra og Til -------------------------- */
SELECT  kolonne, moenster,
        COUNT(*) AS antal,
        SUM(CASE WHEN TRY_CONVERT(DATE, v, 105) IS NULL THEN 1 ELSE 0 END) AS kan_ikke_tolkes_105
FROM (
    SELECT kolonne, v = LTRIM(RTRIM(v)),
           moenster = CASE
               WHEN LTRIM(RTRIM(v)) LIKE N'[0-3][0-9]-[01][0-9]-[12][0-9][0-9][0-9]'
                    THEN N'dd-mm-aaaa'
               WHEN LTRIM(RTRIM(v)) LIKE N'[0-3][0-9]-[01][0-9]-[12][0-9][0-9][0-9] %'
                    THEN N'dd-mm-aaaa + klokkeslaet'
               WHEN LTRIM(RTRIM(v)) LIKE N'[12][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]%'
                    THEN N'aaaa-mm-dd'
               WHEN LTRIM(RTRIM(v)) LIKE N'%/%'
                    THEN N'med skraastreg'
               WHEN LTRIM(RTRIM(v)) LIKE N'%.%'
                    THEN N'med punktum'
               ELSE N'andet'
           END
    FROM GOStaging.dbo.Foranstaltningsdata
    CROSS APPLY (VALUES (N'Visiteret', [Visiteret]),
                        (N'Fra',       [Fra]),
                        (N'Til',       [Til])) AS k (kolonne, v)
    WHERE NULLIF(LTRIM(RTRIM(v)), N'') IS NOT NULL
) AS d
GROUP BY kolonne, moenster
ORDER BY kolonne, antal DESC;
