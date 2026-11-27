/*
  106_indsats_noegle_tjek.sql — virker den nye indsatsnoegle paa dumpet?

  Kun laesning, kun mod GOStaging. Roerer IKKE SBSYS_KONV: extract ville
  erstatte de falske testdata i stg med rigtige, og saa kunne en --live-
  koersel sende rigtige data til testmiljoeet.

  Samme udtryk som afsnit 6 i 100_extract_skabelon.sql (kopieret; test_extract
  holder de to ens). Viser kun optaellinger.

  Forventet paa testdumpet (17-11-2026):
    raekker = forskellige_noegler = 1777, nr_2_eller_hoejere = ca. 8
*/

SET NOCOUNT ON;

;WITH kandidater AS (
    SELECT
        fingeraftryk = CONVERT(VARCHAR(64), HASHBYTES('SHA2_256', CONCAT(
                COALESCE(N'1' + f.[Indholdstype], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Visiteret], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Fra], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Til], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Sagsbehandler], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[ABAGruppe], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Ydelse], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[SkoleEllerInstitution], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Betalingssted], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Timer], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[VisiteretTilTilbud], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Visitationsteam], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Specialundervisningstilbud], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[TypeAfEnkelteIntegration], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Betalingskommune], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Skoleaar], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Faggruppe], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Indsats], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Institutionsnavn], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Aarsag], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[HenvisningsAarsag], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Klassetrin], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Klassebetegnelse], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Skole], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[NavnPaaSkole], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Bopaelsoplysning], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[AndenBetalingskommune], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[EkstraBevilling], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[IndstilletAf], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Indstillingsaarsag], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Aktivitet], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[BetegnelseSpecialklasse], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[FritSkolevalg], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[NavnPaaSpecialskole], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[BetegnelseSpecialskole], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Bevillingstype], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Institutionstype], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Bevillingssted], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Bevillingsaarsag], N'0'), NCHAR(31),
                COALESCE(N'1' + f.[Emneliste], N'0')
        )), 2),
        f.*
    FROM GOStaging.dbo.[Foranstaltningsdata] AS f
),
nummereret AS (
    SELECT
        noegle = CONVERT(VARCHAR(200),
                     k.[SagsId] + '|' + k.fingeraftryk + '|'
                     + CONVERT(VARCHAR(10), ROW_NUMBER() OVER (
                           PARTITION BY k.[SagsId], k.fingeraftryk ORDER BY k.[ID]))),
        k.*
    FROM kandidater AS k
)
SELECT  COUNT(*)                                   AS raekker,
        COUNT(DISTINCT noegle)                     AS forskellige_noegler,
        SUM(CASE WHEN noegle NOT LIKE '%|1' THEN 1 ELSE 0 END) AS nr_2_eller_hoejere,
        MAX(LEN(noegle))                           AS laengste_noegle
FROM    nummereret;
