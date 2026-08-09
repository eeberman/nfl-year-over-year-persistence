"""Core transformations and statistics for the NFL persistence analysis.

The functions here are deliberately dependency-light: pandas and NumPy are the
only third-party requirements.  They are separately unit tested so the build
script can focus on source retrieval and publishing artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np
import pandas as pd


SEASONS = tuple(range(2006, 2026))
WINDOWS = (1, 2, 5)
BOOTSTRAP_REPLICATES = 10_000
BOOTSTRAP_SEED = 20_260_808

# nflverse uses historical abbreviations in older PBP/schedule records.  The
# mappings below preserve franchise identity without rewriting all other teams.
FRANCHISE_MAP = {
    "STL": "LAR",
    "LA": "LAR",
    "LAR": "LAR",
    "SD": "LAC",
    "LAC": "LAC",
    "OAK": "LV",
    "LV": "LV",
    "JAC": "JAX",
}


class ValidationError(ValueError):
    """Raised when source coverage or construction assumptions are violated."""


@dataclass(frozen=True)
class DomainSpec:
    key: str
    title: str
    nav_label: str
    entity_col: str
    metric_col: str
    metric_label: str
    metric_short_label: str
    metric_description: str
    unit_description: str


DOMAINS = (
    DomainSpec(
        key="individual_qb",
        title="Individual quarterback persistence",
        nav_label="Individual QB",
        entity_col="player_id",
        metric_col="metric",
        metric_label="QB-attributed EPA per dropback",
        metric_short_label="QB EPA / dropback",
        metric_description="sum(qb_epa) divided by QB dropbacks for the same player across all teams.",
        unit_description="Each point is one qualifying QB-season pair; players remain the same entity after a team change.",
    ),
    DomainSpec(
        key="team_qb",
        title="Team-level quarterback-play persistence",
        nav_label="Team QB",
        entity_col="franchise",
        metric_col="metric",
        metric_label="Team EPA per QB dropback",
        metric_short_label="Team QB EPA / dropback",
        metric_description="sum(epa) divided by all franchise QB dropbacks, regardless of who played quarterback.",
        unit_description="Each point is a franchise-season pair; quarterback continuity is not required.",
    ),
    DomainSpec(
        key="offense",
        title="Team offensive persistence",
        nav_label="Offense",
        entity_col="franchise",
        metric_col="metric",
        metric_label="Offensive EPA per scrimmage play",
        metric_short_label="Offensive EPA / play",
        metric_description="mean(epa) on regular-season offensive pass or rush plays, excluding conversions and special teams.",
        unit_description="Each point is a franchise-season pair.",
    ),
    DomainSpec(
        key="defense",
        title="Team defensive persistence",
        nav_label="Defense",
        entity_col="franchise",
        metric_col="metric",
        metric_label="Defensive EPA prevented per scrimmage play",
        metric_short_label="Defensive EPA prevented / play",
        metric_description="negative mean EPA allowed on the same scrimmage-play definition; higher is better.",
        unit_description="Each point is a franchise-season pair; the sign is inverted for intuitive comparison.",
    ),
    DomainSpec(
        key="record",
        title="Team record persistence",
        nav_label="Record",
        entity_col="franchise",
        metric_col="metric",
        metric_label="Regular-season win percentage",
        metric_short_label="Win percentage",
        metric_description="(wins + 0.5 × ties) / games from completed regular-season schedules.",
        unit_description="Each point is a franchise-season pair; schedule length is normalized by construction.",
    ),
)


def canonical_franchise(team: Any) -> Any:
    """Return a stable franchise code while preserving missing values."""

    if pd.isna(team):
        return team
    team = str(team).strip().upper()
    return FRANCHISE_MAP.get(team, team)


def qualification_threshold(season: int) -> int:
    """Headline QB threshold: 14 dropbacks per scheduled regular-season game."""

    return 224 if season <= 2020 else 238


def calculate_win_pct(wins: float, losses: float, ties: float) -> float:
    """Calculate conventional NFL win percentage, counting a tie as half a win."""

    games = wins + losses + ties
    if games <= 0:
        raise ValidationError("A record must contain at least one game.")
    return (wins + 0.5 * ties) / games


def aggregate_player_qb_seasons(rows: pd.DataFrame) -> pd.DataFrame:
    """Aggregate player QB rows across teams into one player-season observation.

    Expected input columns are season, player_id, player_name, qb_epa, and
    dropbacks.  A franchise column is optional; when present it is retained as
    a stable, display-only sequence for the interactive chart.  The player ID
    (rather than display name or team) always defines the individual-QB unit.
    """

    required = {"season", "player_id", "player_name", "qb_epa", "dropbacks"}
    missing = required.difference(rows.columns)
    if missing:
        raise ValidationError(f"QB aggregation missing columns: {sorted(missing)}")
    clean = rows.dropna(subset=["player_id"]).copy()
    numeric = clean.groupby(["season", "player_id"], as_index=False).agg(
        qb_epa=("qb_epa", "sum"), dropbacks=("dropbacks", "sum")
    )
    labels = (
        clean.dropna(subset=["player_name"])
        .drop_duplicates(subset=["season", "player_id"], keep="last")
        [["season", "player_id", "player_name"]]
    )
    out = numeric.merge(labels, on=["season", "player_id"], how="left")
    if "franchise" in clean.columns:
        team_sequences = (
            clean.dropna(subset=["franchise"])
            .assign(franchise=lambda frame: frame["franchise"].map(canonical_franchise))
            .groupby(["season", "player_id"], as_index=False)["franchise"]
            .agg(lambda teams: " / ".join(sorted(set(teams))))
            .rename(columns={"franchise": "team_sequence"})
        )
        out = out.merge(team_sequences, on=["season", "player_id"], how="left", validate="one_to_one")
    out["metric"] = out["qb_epa"] / out["dropbacks"]
    columns = ["season", "player_id", "player_name", "qb_epa", "dropbacks", "metric"]
    if "team_sequence" in out:
        columns.append("team_sequence")
    return out[columns]


def construct_strict_pairs(
    metric_df: pd.DataFrame,
    *,
    entity_col: str,
    metric_col: str = "metric",
    label_col: str | None = None,
    team_col: str | None = None,
    window: int,
) -> pd.DataFrame:
    """Build complete rolling-history pairs, never averaging partial histories."""

    required = {"season", entity_col, metric_col}
    missing = required.difference(metric_df.columns)
    if missing:
        raise ValidationError(f"Pair construction missing columns: {sorted(missing)}")
    if window not in WINDOWS:
        raise ValidationError(f"Unsupported window {window}; expected one of {WINDOWS}.")

    columns = ["season", entity_col, metric_col]
    for column in [label_col, team_col]:
        if column and column not in columns:
            columns.append(column)
    frame = metric_df[columns].copy()
    frame = frame.dropna(subset=[entity_col, metric_col])
    if frame.duplicated([entity_col, "season"]).any():
        raise ValidationError("Metric data must contain only one row per entity-season.")

    lookup = frame.set_index([entity_col, "season"])[metric_col].to_dict()
    labels: dict[Any, Any] = {}
    if label_col:
        labels = (
            frame.dropna(subset=[label_col])
            .drop_duplicates(subset=[entity_col], keep="last")
            .set_index(entity_col)[label_col]
            .to_dict()
        )
    teams: dict[tuple[Any, int], list[str]] = {}
    if team_col:
        team_values = frame[[entity_col, "season"]].copy()
        team_values["_team"] = frame[team_col]
        team_values = team_values.set_index([entity_col, "season"])["_team"].dropna()
        for (entity, season), raw_value in team_values.items():
            team_list = [team.strip() for team in str(raw_value).split("/") if team.strip()]
            teams[(entity, int(season))] = team_list

    pairs: list[dict[str, Any]] = []
    entities = sorted(frame[entity_col].unique())
    for entity in entities:
        for target_season in range(min(SEASONS) + window, max(SEASONS) + 1):
            history = list(range(target_season - window, target_season))
            values = [lookup.get((entity, year)) for year in history]
            target = lookup.get((entity, target_season))
            if target is None or any(value is None for value in values):
                continue
            pairs.append(
                {
                    "entity": entity,
                    "entity_label": labels.get(entity, entity),
                    "history_start": history[0],
                    "history_end": history[-1],
                    "target_season": target_season,
                    "history_team_sequence": [
                        {"season": year, "teams": teams.get((entity, year), [])} for year in history
                    ],
                    "target_teams": teams.get((entity, target_season), []),
                    "predictor": float(np.mean(values)),
                    "outcome": float(target),
                    "touches_2020": 2020 in history or target_season == 2020,
                }
            )
    return pd.DataFrame(
        pairs,
        columns=[
            "entity",
            "entity_label",
            "history_start",
            "history_end",
            "target_season",
            "history_team_sequence",
            "target_teams",
            "predictor",
            "outcome",
            "touches_2020",
        ],
    )


def pearson_correlation(x: Iterable[float], y: Iterable[float]) -> float:
    """Return a finite Pearson correlation or NaN for undefined inputs."""

    x_arr = np.asarray(list(x), dtype=float)
    y_arr = np.asarray(list(y), dtype=float)
    if len(x_arr) < 3 or np.std(x_arr) == 0 or np.std(y_arr) == 0:
        return float("nan")
    return float(np.corrcoef(x_arr, y_arr)[0, 1])


def spearman_correlation(x: Iterable[float], y: Iterable[float]) -> float:
    """Return Spearman rank correlation with average ranks for ties."""

    x_rank = pd.Series(list(x), dtype=float).rank(method="average")
    y_rank = pd.Series(list(y), dtype=float).rank(method="average")
    return pearson_correlation(x_rank, y_rank)


def cluster_bootstrap_ci(
    pairs: pd.DataFrame,
    *,
    replicates: int = BOOTSTRAP_REPLICATES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float]:
    """Percentile CI for Pearson r, resampling entity clusters with replacement."""

    if pairs.empty or pairs["entity"].nunique() < 3:
        return (float("nan"), float("nan"))
    # A bootstrap sample contains all observations from each selected entity. Its
    # correlation can therefore be calculated exactly from entity-level sufficient
    # statistics, avoiding tens of thousands of expensive DataFrame concatenations.
    grouped = pairs.groupby("entity", sort=True)
    sufficient = grouped.agg(
        n=("predictor", "size"),
        sum_x=("predictor", "sum"),
        sum_y=("outcome", "sum"),
    )
    sufficient["sum_x2"] = grouped["predictor"].apply(lambda values: float(np.square(values).sum()))
    sufficient["sum_y2"] = grouped["outcome"].apply(lambda values: float(np.square(values).sum()))
    sufficient["sum_xy"] = grouped.apply(
        lambda frame: float((frame["predictor"] * frame["outcome"]).sum())
    )
    values = sufficient[["n", "sum_x", "sum_y", "sum_x2", "sum_y2", "sum_xy"]].to_numpy(dtype=float)
    groups = len(values)
    generator = np.random.default_rng(seed)
    selected = generator.integers(0, groups, size=(replicates, groups))
    counts = np.apply_along_axis(lambda row: np.bincount(row, minlength=groups), 1, selected)
    totals = counts @ values
    n_obs, sum_x, sum_y, sum_x2, sum_y2, sum_xy = totals.T
    covariance = sum_xy - (sum_x * sum_y / n_obs)
    var_x = sum_x2 - (sum_x * sum_x / n_obs)
    var_y = sum_y2 - (sum_y * sum_y / n_obs)
    with np.errstate(divide="ignore", invalid="ignore"):
        draws = covariance / np.sqrt(var_x * var_y)
    draws = draws[np.isfinite(draws)]
    if not len(draws):
        return (float("nan"), float("nan"))
    low, high = np.quantile(draws, [0.025, 0.975])
    return (float(low), float(high))


def leave_one_entity_out_delta(pairs: pd.DataFrame, full_correlation: float) -> float:
    """Largest absolute Pearson-r shift from omitting a single entity."""

    deltas: list[float] = []
    for entity in pairs["entity"].drop_duplicates():
        reduced = pairs.loc[pairs["entity"] != entity]
        correlation = pearson_correlation(reduced["predictor"], reduced["outcome"])
        if np.isfinite(correlation) and np.isfinite(full_correlation):
            deltas.append(abs(correlation - full_correlation))
    return float(max(deltas)) if deltas else float("nan")


def summarize_pairs(
    pairs: pd.DataFrame,
    *,
    bootstrap_seed: int,
) -> dict[str, Any]:
    """Return metrics and plotting payload for one domain/window pair table."""

    pearson = pearson_correlation(pairs["predictor"], pairs["outcome"])
    spearman = spearman_correlation(pairs["predictor"], pairs["outcome"])
    ci_low, ci_high = cluster_bootstrap_ci(pairs, seed=bootstrap_seed)
    slope = intercept = float("nan")
    if len(pairs) >= 2 and np.std(pairs["predictor"]) > 0:
        slope, intercept = np.polyfit(pairs["predictor"], pairs["outcome"], 1)
    points = []
    for row in pairs.itertuples(index=False):
        point = {
            "entity": str(row.entity),
            "label": str(row.entity_label),
            "history_start": int(row.history_start),
            "history_end": int(row.history_end),
            "target_season": int(row.target_season),
            "predictor": float(row.predictor),
            "outcome": float(row.outcome),
        }
        # Team history is needed only for player-level points.  Keep the site
        # payload compact by deriving franchise-chart team identity from entity.
        if row.target_teams:
            point["history_teams"] = "|".join(
                " / ".join(step["teams"]) for step in row.history_team_sequence
            )
            point["target_teams"] = "|".join(row.target_teams)
        points.append(point)
    return {
        "pearson_r": pearson,
        "spearman_rho": spearman,
        "n_pairs": int(len(pairs)),
        "n_entities": int(pairs["entity"].nunique()),
        "ci_95": [ci_low, ci_high],
        "regression": {"slope": float(slope), "intercept": float(intercept)},
        "leave_one_entity_out_max_delta": leave_one_entity_out_delta(pairs, pearson),
        "points": points,
    }


def validate_team_coverage(metric_df: pd.DataFrame, *, domain: str) -> None:
    """Ensure all 32 stable franchises have a nonmissing value each season."""

    for season in SEASONS:
        subset = metric_df.loc[metric_df["season"] == season]
        if len(subset) != 32 or subset["franchise"].nunique() != 32:
            raise ValidationError(
                f"{domain} has {len(subset)} franchise rows in {season}; expected 32."
            )
        if subset["metric"].isna().any() or ~np.isfinite(subset["metric"]).all():
            raise ValidationError(f"{domain} has nonfinite metric values in {season}.")


def json_safe(value: Any, digits: int = 4) -> Any:
    """Recursively convert NumPy/Pandas values to compact JSON-safe values."""

    if isinstance(value, dict):
        return {str(key): json_safe(item, digits=digits) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item, digits=digits) for item in value]
    if isinstance(value, (np.floating, float)):
        return None if not np.isfinite(value) else round(float(value), digits)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value
