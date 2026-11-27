/*
  103_vaerdilister_go.sql — hvilke vaerdier bruger GO i de felter der skal
  paa SBSYS' vaerdilister (Vaerdilister.xlsx fra datateamet)?

  Kun laesning. Viser institutionsnavne, kommuner, klassetrin og
  institutionsnumre med antal — ingen personnavne, ingen CPR. Sagsbehandler
  er bevidst udeladt. Kig alligevel resultatet igennem, foer det deles.

      uv run konv db koer sql/103_vaerdilister_go.sql --raekker 0
      (eller koer filen i SSMS)

  To resultatsaet:
    1. Overblik pr. kolonne: hvor mange raekker har en vaerdi, hvor mange
       forskellige vaerdier
    2. Hver forskellig vaerdi med antal og hvilke indholdstyper den optraeder i

  Resultatsaet 2 kan blive et par hundrede raekker. Gem det hellere som CSV
  (hoejreklik i resultatet -> Save Results As...) end at kopiere det.

  Kolonnerne er valgt efter navn og efter PPR-arket i "Konvertering GO-SBSYS
  – Til NC.xlsx". Hvilken vaerdiliste hver kolonne hoerer til, er IKKE
  bekraeftet — derfor er de usikre kandidater med (Betalingssted, Skole,
  NavnPaaSkole, NavnPaaSpecialskole), og Cases' egne skole/institutions-
  felter for PPR, hvis numre maaske matcher listernes [lisid]/[instnr].
*/

SET NOCOUNT ON;

/* Sagsnumre maskeres, hvis de mod forventning optraeder i en vaerdi:
   PPR-2025-123456-001 -> PPR-2025-XXXXXX-001 */
DECLARE @sagsnr NVARCHAR(100) =
    N'%[A-Z][A-Z][A-Z]-[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]-%';

IF OBJECT_ID('tempdb..#vaerdier') IS NOT NULL DROP TABLE #vaerdier;

SELECT x.kilde, x.kolonne, x.indholdstype,
       LTRIM(RTRIM(x.vaerdi)) AS vaerdi
INTO   #vaerdier
FROM (
    /* Foranstaltningsdata — felterne paa selve indsatsen */
    SELECT N'Foranstaltningsdata' AS kilde, v.kolonne, f.Indholdstype AS indholdstype, v.vaerdi
    FROM   GOStaging.dbo.Foranstaltningsdata AS f
    CROSS  APPLY (VALUES
        (N'Bevillingssted',        f.Bevillingssted),
        (N'SkoleEllerInstitution', f.SkoleEllerInstitution),
        (N'Institutionsnavn',      f.Institutionsnavn),
        (N'Klassetrin',            f.Klassetrin),
        (N'Betalingskommune',      f.Betalingskommune),
        (N'AndenBetalingskommune', f.AndenBetalingskommune),
        (N'Betalingssted',         f.Betalingssted),
        (N'Skole',                 f.Skole),
        (N'NavnPaaSkole',          f.NavnPaaSkole),
        (N'NavnPaaSpecialskole',   f.NavnPaaSpecialskole)
    ) AS v (kolonne, vaerdi)

    UNION ALL

    /* Cases (kun PPR) — sagens egne skole/institutionsfelter. Numrene er
       med, fordi listernes navne baerer et id: "Bakkegaardsskolen [654]",
       "AARHUS TECH [instnr 281867]". Matcher GO's numre dem, kan
       mapningen laves paa nummer i stedet for paa navn. */
    SELECT N'Cases (PPR)', v.kolonne, c.Sagsprofil, v.vaerdi
    FROM   GOStaging.dbo.Cases AS c
    CROSS  APPLY (VALUES
        (N'Skolenavn',          c.Skolenavn),
        (N'Skolekode',          c.Skolekode),
        (N'Institutionsnavn',   c.Institutionsnavn),
        (N'Institutionsnummer', c.Institutionsnummer),
        (N'DagtilbudNavn',      c.DagtilbudNavn),
        (N'DagtilbudNummer',    c.DagtilbudNummer),
        (N'PersonligtKlassetrin', c.PersonligtKlassetrin),
        (N'Bopaelsskoledistrikt', c.Bopaelsskoledistrikt)
    ) AS v (kolonne, vaerdi)
    WHERE  c.Organisering = N'PPR'
) AS x
WHERE  NULLIF(LTRIM(RTRIM(x.vaerdi)), N'') IS NOT NULL;

/* Masker eventuelle sagsnumre i selve tabellen, saa begge resultatsaet er rene */
UPDATE #vaerdier
SET    vaerdi = STUFF(vaerdi, PATINDEX(@sagsnr, vaerdi) + 9, 6, N'XXXXXX')
WHERE  PATINDEX(@sagsnr, vaerdi) > 0;

/* --- 1. Overblik pr. kolonne ------------------------------------------ */
SELECT
    '1. Overblik'               AS afsnit,
    kilde,
    kolonne,
    COUNT(*)                    AS raekker_med_vaerdi,
    COUNT(DISTINCT vaerdi)      AS forskellige_vaerdier
FROM   #vaerdier
GROUP  BY kilde, kolonne
ORDER  BY kilde, kolonne;

/* --- 2. Hver forskellig vaerdi ------------------------------------------ */
SELECT
    '2. Vaerdier'               AS afsnit,
    g.kilde,
    g.kolonne,
    g.vaerdi,
    SUM(g.antal)                AS antal,
    STRING_AGG(CONVERT(NVARCHAR(MAX), g.indholdstype), N' | ')
        WITHIN GROUP (ORDER BY g.indholdstype) AS indholdstyper
FROM (
    SELECT kilde, kolonne, vaerdi, indholdstype, COUNT(*) AS antal
    FROM   #vaerdier
    GROUP  BY kilde, kolonne, vaerdi, indholdstype
) AS g
GROUP  BY g.kilde, g.kolonne, g.vaerdi
ORDER  BY g.kilde, g.kolonne, antal DESC, g.vaerdi;

DROP TABLE #vaerdier;
