"""Download nflverse sources, calculate persistence, validate, and render site data.

Run from the repository root:
    python analysis/build.py

Use --refresh to re-download public source files. Raw third-party downloads are
cached in data/raw for convenience and ignored by Git.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from core import (
    BOOTSTRAP_REPLICATES,
    BOOTSTRAP_SEED,
    DOMAINS,
    SEASONS,
    WINDOWS,
    ValidationError,
    aggregate_player_qb_seasons,
    calculate_win_pct,
    canonical_franchise,
    construct_strict_pairs,
    json_safe,
    qualification_threshold,
    summarize_pairs,
    validate_team_coverage,
)


ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
DERIVED_DIR = ROOT / "data" / "derived"
SITE_DATA_DIR = ROOT / "docs" / "data"
PBP_URL = "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.csv.gz"
GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
PBP_COLUMNS = [
    "season_type",
    "posteam",
    "defteam",
    "epa",
    "qb_epa",
    "qb_dropback",
    "passer_player_id",
    "passer_player_name",
    "pass",
    "rush",
    "two_point_attempt",
]


def now_utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def retrieve(url: str, destination: Path, *, refresh: bool) -> dict[str, Any]:
    """Retrieve a public file atomically or reuse its local ignored cache."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    downloaded = False
    if refresh or not destination.exists():
        temporary = destination.with_suffix(destination.suffix + ".part")
        if temporary.exists():
            temporary.unlink()
        request = urllib.request.Request(url, headers={"User-Agent": "nfl-persistence-research/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=180) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output)
            temporary.replace(destination)
            downloaded = True
        except Exception as exc:  # pragma: no cover - exercised against network failures
            if temporary.exists():
                temporary.unlink()
            raise RuntimeError(f"Could not retrieve {url}: {exc}") from exc
    return {
        "url": url,
        "local_cache": str(destination.relative_to(ROOT)).replace("\\", "/"),
        "retrieved_at_utc": now_utc() if downloaded else datetime.fromtimestamp(destination.stat().st_mtime, UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "sha256": sha256(destination),
        "bytes": destination.stat().st_size,
        "downloaded_this_run": downloaded,
    }


def numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def build_pbp_metrics(
    source_paths: dict[int, Path],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, int]]:
    """Stream annual PBP CSVs and aggregate the four EPA-based season tables."""

    player_parts: list[pd.DataFrame] = []
    team_qb_parts: list[pd.DataFrame] = []
    offense_parts: list[pd.DataFrame] = []
    defense_parts: list[pd.DataFrame] = []
    diagnostics = {"regular_epa_rows": 0, "qb_dropback_rows": 0, "missing_qb_epa_rows": 0}

    for season, path in source_paths.items():
        required_seen: set[str] = set()
        for chunk in pd.read_csv(path, compression="gzip", usecols=PBP_COLUMNS, chunksize=200_000, low_memory=False):
            required_seen.update(chunk.columns)
            for column in ["epa", "qb_epa", "qb_dropback", "pass", "rush", "two_point_attempt"]:
                chunk[column] = numeric(chunk[column])
            regular = chunk.loc[
                chunk["season_type"].eq("REG") & chunk["posteam"].notna() & chunk["epa"].notna()
            ].copy()
            diagnostics["regular_epa_rows"] += len(regular)
            regular["franchise"] = regular["posteam"].map(canonical_franchise)
            qb = regular.loc[regular["qb_dropback"].eq(1)].copy()
            diagnostics["qb_dropback_rows"] += len(qb)

            team_qb_parts.append(
                qb.groupby("franchise", as_index=False).agg(epa=("epa", "sum"), plays=("qb_dropback", "sum"))
                .assign(season=season)
            )
            player_qb = qb.loc[qb["passer_player_id"].notna()].copy()
            diagnostics["missing_qb_epa_rows"] += int(player_qb["qb_epa"].isna().sum())
            player_parts.append(
                player_qb.groupby(["passer_player_id", "passer_player_name"], dropna=False, as_index=False).agg(
                    qb_epa=("qb_epa", "sum"), dropbacks=("qb_dropback", "sum")
                )
                .rename(columns={"passer_player_id": "player_id", "passer_player_name": "player_name"})
                .assign(season=season)
            )

            scrimmage = regular.loc[
                (regular["pass"].eq(1) | regular["rush"].eq(1)) & ~regular["two_point_attempt"].eq(1)
            ].copy()
            offense_parts.append(
                scrimmage.groupby("franchise", as_index=False).agg(epa=("epa", "sum"), plays=("epa", "size"))
                .assign(season=season)
            )
            defense = scrimmage.loc[scrimmage["defteam"].notna()].copy()
            defense["franchise"] = defense["defteam"].map(canonical_franchise)
            defense_parts.append(
                defense.groupby("franchise", as_index=False).agg(epa=("epa", "sum"), plays=("epa", "size"))
                .assign(season=season)
            )
        if set(PBP_COLUMNS).difference(required_seen):
            raise ValidationError(f"PBP {season} missing required columns: {sorted(set(PBP_COLUMNS).difference(required_seen))}")

    if diagnostics["missing_qb_epa_rows"]:
        raise ValidationError(
            f"Found {diagnostics['missing_qb_epa_rows']} regular-season player dropbacks without qb_epa."
        )

    player_seasons = aggregate_player_qb_seasons(pd.concat(player_parts, ignore_index=True))
    headline_qb = player_seasons.loc[
        player_seasons.apply(lambda row: row["dropbacks"] >= qualification_threshold(int(row["season"])), axis=1)
    ].copy()
    headline_qb["qualifying_threshold"] = headline_qb["season"].map(qualification_threshold)

    def finalize(parts: list[pd.DataFrame], *, reverse: bool = False) -> pd.DataFrame:
        frame = pd.concat(parts, ignore_index=True)
        frame = frame.groupby(["season", "franchise"], as_index=False).agg(epa=("epa", "sum"), plays=("plays", "sum"))
        frame["metric"] = frame["epa"] / frame["plays"]
        if reverse:
            frame["metric"] = -frame["metric"]
        return frame[["season", "franchise", "epa", "plays", "metric"]]

    # The team output is one wide seasonal table to simplify auditing and site use.
    team_qb = finalize(team_qb_parts).rename(
        columns={"epa": "team_qb_epa", "plays": "team_qb_dropbacks", "metric": "team_qb_metric"}
    )
    offense = finalize(offense_parts).rename(
        columns={"epa": "offense_epa", "plays": "offense_plays", "metric": "offense_metric"}
    )
    defense = finalize(defense_parts, reverse=True).rename(
        columns={"epa": "defense_epa_allowed", "plays": "defense_plays", "metric": "defense_metric"}
    )
    team_seasons = team_qb.merge(offense, on=["season", "franchise"], validate="one_to_one").merge(
        defense, on=["season", "franchise"], validate="one_to_one"
    )
    return player_seasons, headline_qb, team_seasons, diagnostics


def build_record_metrics(games_path: Path) -> pd.DataFrame:
    """Calculate regular-season W-L-T records directly from the schedule results."""

    games = pd.read_csv(games_path, low_memory=False)
    needed = {"season", "game_type", "away_team", "away_score", "home_team", "home_score"}
    if missing := needed.difference(games.columns):
        raise ValidationError(f"Schedule source missing columns: {sorted(missing)}")
    games = games.loc[
        games["season"].isin(SEASONS)
        & games["game_type"].eq("REG")
        & games["away_score"].notna()
        & games["home_score"].notna()
    ].copy()
    games["away_score"] = numeric(games["away_score"])
    games["home_score"] = numeric(games["home_score"])
    if games[["away_score", "home_score"]].isna().any().any():
        raise ValidationError("Completed regular-season games contain unparseable scores.")

    def side_frame(team_col: str, own_col: str, opp_col: str) -> pd.DataFrame:
        out = games[["season", team_col, own_col, opp_col]].copy()
        out["franchise"] = out[team_col].map(canonical_franchise)
        out["wins"] = (out[own_col] > out[opp_col]).astype(int)
        out["losses"] = (out[own_col] < out[opp_col]).astype(int)
        out["ties"] = (out[own_col] == out[opp_col]).astype(int)
        return out[["season", "franchise", "wins", "losses", "ties"]]

    records = pd.concat(
        [side_frame("away_team", "away_score", "home_score"), side_frame("home_team", "home_score", "away_score")],
        ignore_index=True,
    )
    records = records.groupby(["season", "franchise"], as_index=False).sum()
    records["games"] = records[["wins", "losses", "ties"]].sum(axis=1)
    records["metric"] = records.apply(lambda row: calculate_win_pct(row.wins, row.losses, row.ties), axis=1)
    for season in SEASONS:
        counts = records.loc[records["season"] == season, "games"]
        # The 2022 Bills–Bengals game was cancelled after Damar Hamlin's injury,
        # leaving those two clubs with 16 completed regular-season games.  Record
        # persistence intentionally uses actual completed games as its denominator.
        valid_games = counts.eq(16) if season <= 2020 else counts.isin([16, 17])
        if len(counts) != 32 or not valid_games.all():
            raise ValidationError(
                f"Record validation failed for {season}; expected 32 teams with valid completed-game totals."
            )
    return records


def make_team_domain_frame(team_seasons: pd.DataFrame, column: str) -> pd.DataFrame:
    return team_seasons[["season", "franchise", column]].rename(columns={column: "metric"}).copy()


def analyze_domain(
    domain_key: str,
    frame: pd.DataFrame,
    *,
    entity_col: str,
    label_col: str | None,
    metric_label: str,
    metric_description: str,
    unit_description: str,
    notes: list[str] | None = None,
) -> tuple[dict[str, Any], dict[int, pd.DataFrame]]:
    """Analyze all strict rolling windows and create a public domain payload."""

    windows: dict[str, Any] = {}
    raw_pairs: dict[int, pd.DataFrame] = {}
    for window_index, window in enumerate(WINDOWS):
        pairs = construct_strict_pairs(frame, entity_col=entity_col, label_col=label_col, window=window)
        raw_pairs[window] = pairs
        summary = summarize_pairs(pairs, bootstrap_seed=BOOTSTRAP_SEED + 100 * window_index + len(domain_key))
        excluding_2020 = pairs.loc[~pairs["touches_2020"]].copy()
        summary["excluding_2020"] = {
            "pearson_r": summarize_pairs(
                excluding_2020, bootstrap_seed=BOOTSTRAP_SEED + 5_000 + 100 * window_index
            )["pearson_r"],
            "n_pairs": int(len(excluding_2020)),
        }
        windows[str(window)] = summary
    return (
        {
            "id": domain_key,
            "metric_label": metric_label,
            "metric_description": metric_description,
            "unit_description": unit_description,
            "notes": notes or [],
            "windows": windows,
        },
        raw_pairs,
    )


def source_provenance(sources: dict[str, Any]) -> dict[str, Any]:
    return {
        "analysis_period": {"start": min(SEASONS), "end": max(SEASONS), "seasons": len(SEASONS)},
        "retrieved_at_utc": now_utc(),
        "sources": sources,
        "software": {
            "python": sys.version.split()[0],
            "pandas": pd.__version__,
            "numpy": np.__version__,
            "platform": platform.platform(),
        },
        "method": {
            "bootstrap_replicates": BOOTSTRAP_REPLICATES,
            "bootstrap_seed": BOOTSTRAP_SEED,
            "ci": "95% percentile cluster bootstrap, resampling QB or franchise identities",
            "rolling_windows": [1, 2, 5],
            "qb_headline_threshold": "224 dropbacks in 2006–2020; 238 in 2021–2025",
        },
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(payload), indent=2, sort_keys=False) + "\n", encoding="utf-8")


def ensure_raw_is_ignored() -> None:
    gitignore = ROOT / ".gitignore"
    if "data/raw/" not in gitignore.read_text(encoding="utf-8"):
        raise ValidationError("data/raw must be ignored before the build can continue.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Re-download all public source files.")
    args = parser.parse_args()
    ensure_raw_is_ignored()

    sources: dict[str, Any] = {"play_by_play": {}, "schedule": {}}
    source_paths: dict[int, Path] = {}
    for season in SEASONS:
        path = RAW_DIR / f"play_by_play_{season}.csv.gz"
        sources["play_by_play"][str(season)] = retrieve(PBP_URL.format(season=season), path, refresh=args.refresh)
        source_paths[season] = path
    games_path = RAW_DIR / "games.csv"
    sources["schedule"] = retrieve(GAMES_URL, games_path, refresh=args.refresh)

    all_player_qb, headline_qb, team_seasons, pbp_diagnostics = build_pbp_metrics(source_paths)
    records = build_record_metrics(games_path)
    team_seasons = team_seasons.merge(
        records[["season", "franchise", "wins", "losses", "ties", "games", "metric"]].rename(
            columns={"metric": "record_metric"}
        ),
        on=["season", "franchise"],
        validate="one_to_one",
    )

    for column, domain in [
        ("team_qb_metric", "team QB"),
        ("offense_metric", "offense"),
        ("defense_metric", "defense"),
        ("record_metric", "record"),
    ]:
        validate_team_coverage(make_team_domain_frame(team_seasons, column), domain=domain)
    if headline_qb.empty:
        raise ValidationError("No individual QB seasons passed the headline playing-time threshold.")

    frames = {
        "individual_qb": headline_qb[["season", "player_id", "player_name", "metric"]].copy(),
        "team_qb": make_team_domain_frame(team_seasons, "team_qb_metric"),
        "offense": make_team_domain_frame(team_seasons, "offense_metric"),
        "defense": make_team_domain_frame(team_seasons, "defense_metric"),
        "record": make_team_domain_frame(team_seasons, "record_metric"),
    }
    payload_domains: dict[str, Any] = {}
    all_pairs: dict[str, dict[int, pd.DataFrame]] = {}
    for spec in DOMAINS:
        notes = []
        label_col = "player_name" if spec.key == "individual_qb" else None
        if spec.key == "individual_qb":
            notes = [
                "Headline eligibility is 224 dropbacks in 2006–2020 and 238 in 2021–2025.",
                "Strict five-year estimates condition on a QB qualifying in all five history seasons and the target season.",
            ]
        if spec.key == "defense":
            notes = ["The metric is sign-inverted EPA allowed, so a higher value always means better performance."]
        if spec.key == "record":
            notes = ["Win percentage, rather than wins, makes 16-game and 17-game seasons comparable."]
        payload, pairs = analyze_domain(
            spec.key,
            frames[spec.key],
            entity_col=spec.entity_col,
            label_col=label_col,
            metric_label=spec.metric_label,
            metric_description=spec.metric_description,
            unit_description=spec.unit_description,
            notes=notes,
        )
        payload_domains[spec.key] = payload
        all_pairs[spec.key] = pairs

    # Lower-threshold QB sensitivity deliberately changes only eligibility, not metric definition/window logic.
    low_threshold_frame = all_player_qb.loc[all_player_qb["dropbacks"] >= 100, ["season", "player_id", "player_name", "metric"]]
    threshold_sensitivity: dict[str, Any] = {"threshold": "At least 100 dropbacks in every required season"}
    for window in WINDOWS:
        low_pairs = construct_strict_pairs(low_threshold_frame, entity_col="player_id", label_col="player_name", window=window)
        threshold_sensitivity[str(window)] = {
            "pearson_r": summarize_pairs(low_pairs, bootstrap_seed=BOOTSTRAP_SEED + 9_000 + window)["pearson_r"],
            "n_pairs": int(len(low_pairs)),
        }
    payload_domains["individual_qb"]["threshold_sensitivity"] = threshold_sensitivity
    payload_domains["individual_qb"]["qualification"] = {
        "headline_rule": "At least 14 QB dropbacks per scheduled team game: 224 (2006–2020), 238 (2021–2025).",
        "qualifying_qb_seasons": int(len(headline_qb)),
        "qualifying_qbs": int(headline_qb["player_id"].nunique()),
    }

    summary_rows = []
    for spec in DOMAINS:
        for window in WINDOWS:
            stats = payload_domains[spec.key]["windows"][str(window)]
            summary_rows.append(
                {
                    "domain": spec.key,
                    "label": spec.nav_label,
                    "window": window,
                    "pearson_r": stats["pearson_r"],
                    "spearman_rho": stats["spearman_rho"],
                    "ci_low": stats["ci_95"][0],
                    "ci_high": stats["ci_95"][1],
                    "n_pairs": stats["n_pairs"],
                }
            )
    summary = pd.DataFrame(summary_rows)

    provenance = source_provenance(sources)
    provenance["validations"] = {
        "team_rows_per_domain_per_season": 32,
        "regular_season_only": True,
        "pbp_diagnostics": pbp_diagnostics,
        "qb_headline_qualifying_seasons": int(len(headline_qb)),
    }
    results = {
        "title": "NFL year-over-year performance persistence",
        "analysis_period": "2006–2025 regular seasons",
        "generated_at_utc": now_utc(),
        "primary_measure": "Pearson correlation of prior-window performance with next-season performance",
        "domains": payload_domains,
        "summary": summary_rows,
        "provenance": {
            "source_manifest": "data/provenance.json",
            "methodology": "methodology.html",
        },
    }

    DERIVED_DIR.mkdir(parents=True, exist_ok=True)
    headline_qb.sort_values(["season", "player_name"]).to_csv(DERIVED_DIR / "qb_seasons_qualifying.csv", index=False)
    team_seasons.sort_values(["season", "franchise"]).to_csv(DERIVED_DIR / "team_seasons.csv", index=False)
    summary.to_csv(DERIVED_DIR / "headline_results.csv", index=False)
    write_json(DERIVED_DIR / "provenance.json", provenance)
    write_json(SITE_DATA_DIR / "results.json", results)
    write_json(SITE_DATA_DIR / "provenance.json", provenance)
    print(f"Built {len(summary_rows)} headline estimates from {len(headline_qb)} qualifying QB-seasons.")
    print(f"Wrote {SITE_DATA_DIR.relative_to(ROOT)} and {DERIVED_DIR.relative_to(ROOT)} artifacts.")


if __name__ == "__main__":
    try:
        main()
    except (ValidationError, RuntimeError) as error:
        raise SystemExit(f"BUILD FAILED: {error}")
