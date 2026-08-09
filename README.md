# NFL year-over-year performance persistence

How much does the previous 1, 2, or 5 NFL seasons tell us about performance in the next one? This project measures that relationship—not an elaborate forecasting model—for individual quarterback play, team quarterback play, offense, defense, and record across the 2006–2025 regular seasons.

The published analysis: **https://eeberman.github.io/nfl-year-over-year-persistence/**

## Reproduce

Use Python 3.11+ and install the pinned requirements:

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests
python analysis/build.py
```

`analysis/build.py --refresh` downloads current source copies again. The build streams annual public nflverse PBP CSVs, caches them in `data/raw/`, validates coverage, writes audit-friendly aggregate results in `data/derived/`, and regenerates the site data in `docs/data/`. Raw source files are deliberately ignored by Git.

## Metric choices

| Domain | Metric |
| --- | --- |
| Individual QB | QB-attributed EPA per dropback: `sum(qb_epa) / sum(qb_dropback)` |
| Team QB | Team EPA per QB dropback: `sum(epa) / sum(qb_dropback)` |
| Offense | Offensive EPA per regular-season scrimmage play |
| Defense | Negative EPA allowed per regular-season scrimmage play (higher is better) |
| Record | Regular-season win percentage, counting a tie as half a win |

The 224/238-dropback individual-QB eligibility rule is a dropback analogue of the NFL’s 14 passing-attempts-per-team-game qualification convention. Complete strict windows are required: a five-year QB pair must have five qualifying history seasons and a qualifying target season.

Record percentage uses completed regular-season games. This correctly gives Buffalo and Cincinnati a 16-game denominator in 2022, when their cancelled Week 17 game was not resumed.

## Sources and use

- [nflverse PBP releases](https://github.com/nflverse/nflverse-data/releases/tag/pbp), accessed as annual `play_by_play_{season}.csv.gz` files. nflverse’s release repository is CC BY 4.0; its documentation notes that underlying NFL data remain subject to their owners’ terms.
- [nflverse/nfldata games.csv](https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv) for regular-season scores and records.
- [nflfastR documentation](https://www.nflfastr.com/reference/add_qb_epa.html) for `qb_epa` attribution and [PFR’s minimum-requirements page](https://www.pro-football-reference.com/about/minimums.htm) for the seasonal passer qualification convention.

Exact retrieval times and SHA-256 checksums are generated in `data/derived/provenance.json` and shown on the methodology page. Historical DVOA was investigated but not used because FTN’s complete archive is subscriber-only and its downloads combine the regular season and playoffs.
