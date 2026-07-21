"""Interface en ligne de commande du benchmarker.

Deux sous-commandes :
  - search    : critères → benchmark (CSV/Excel) + synthèse statistique.
  - recommend : à partir d'un benchmark existant, préconise des tarifs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from . import analysis, recommend
from .models import Concert, SearchCriteria


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --- search -----------------------------------------------------------------
def cmd_search(args: argparse.Namespace) -> int:
    # Import paresseux : la découverte/extraction dépend du SDK anthropic,
    # dont la sous-commande `recommend` n'a pas besoin.
    from .pipeline import build_benchmark

    criteria = _load_criteria(args)
    concerts = build_benchmark(
        criteria,
        with_prices=not args.no_prices,
        profile=args.profile,
        strategy=args.fetch_strategy,
        progress=_log,
    )

    df = analysis.concerts_to_frame(concerts)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    if out.suffix.lower() in (".xlsx", ".xls"):
        df.to_excel(out, index=False)
    else:
        df.to_csv(out, index=False)
    _log(f"Benchmark écrit : {out}")

    # Sauvegarde brute JSON (fidélité totale au schéma).
    raw = out.with_suffix(".json")
    raw.write_text(
        json.dumps([c.model_dump() for c in concerts], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _log(f"Données brutes : {raw}")

    summary = analysis.summarize(df)
    md = analysis.summary_to_markdown(summary)
    report = out.with_name(out.stem + "_synthese.md")
    report.write_text(md, encoding="utf-8")
    _log(f"Synthèse : {report}")

    print(md)
    return 0


# --- selftest (sans LLM, sans crédits) --------------------------------------
def cmd_selftest(args: argparse.Namespace) -> int:
    """Test d'intégration de toute la chaîne non-LLM sur des données d'exemple."""
    from .sample import sample_concerts

    concerts = sample_concerts()
    _log(f"{len(concerts)} dates d'exemple.")

    df = analysis.concerts_to_frame(concerts)
    summary = analysis.summarize(df)
    reco = recommend.recommend(df, genre="rap", capacity=7000, artist="Artiste Test")

    # Invariants de base (échoue avec code ≠ 0 si la chaîne est cassée).
    assert summary["dates_trouvees"] == len(concerts), "compte de dates incohérent"
    assert summary["global"]["count"] > 0, "aucun tarif agrégé"
    assert summary["par_categorie"], "aucune stat par catégorie"
    assert reco["reco_par_categorie"], "reco vide"

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    out.with_suffix(".json").write_text(
        json.dumps([c.model_dump() for c in concerts], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    out.with_name(out.stem + "_synthese.md").write_text(
        analysis.summary_to_markdown(summary), encoding="utf-8"
    )
    _log(f"Fichiers écrits : {out}, {out.with_suffix('.json')}, {out.with_name(out.stem + '_synthese.md')}")

    print(analysis.summary_to_markdown(summary))
    print("\n" + recommend.recommendation_to_markdown(reco))
    print("\n✅ selftest OK — toute la chaîne non-LLM fonctionne (aucun crédit consommé).")
    return 0


# --- recommend --------------------------------------------------------------
def cmd_recommend(args: argparse.Namespace) -> int:
    df = _load_benchmark_frame(args.benchmark)
    reco = recommend.recommend(
        df, genre=args.genre, capacity=args.capacity, artist=args.artist
    )
    md = recommend.recommendation_to_markdown(reco)
    print(md)
    if args.output:
        Path(args.output).write_text(md, encoding="utf-8")
        _log(f"Préconisation écrite : {args.output}")
    return 0


# --- helpers ----------------------------------------------------------------
def _load_criteria(args: argparse.Namespace) -> SearchCriteria:
    if args.criteria:
        data = json.loads(Path(args.criteria).read_text(encoding="utf-8"))
        return SearchCriteria(**data)
    return SearchCriteria(
        genres=args.genre or [],
        location=args.location,
        countries=args.country or ["France"],
        capacity_min=args.capacity_min,
        capacity_max=args.capacity_max,
        date_from=args.date_from,
        date_to=args.date_to,
        max_results=args.max_results,
        extra=args.extra,
    )


def _load_benchmark_frame(path: str) -> pd.DataFrame:
    p = Path(path)
    if p.suffix.lower() == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        concerts = [Concert(**c) for c in data]
        return analysis.concerts_to_frame(concerts)
    if p.suffix.lower() in (".xlsx", ".xls"):
        return pd.read_excel(p)
    return pd.read_csv(p)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchmarker",
        description="Benchmark billetterie propulsé par les LLM.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    s = sub.add_parser("search", help="Construire un benchmark depuis des critères.")
    s.add_argument("--criteria", help="Fichier JSON de critères (prioritaire).")
    s.add_argument("--genre", action="append", help="Style musical (répétable).")
    s.add_argument("--location", help="Zone géographique (ville/région/pays).")
    s.add_argument("--country", action="append", help="Pays (répétable).")
    s.add_argument("--capacity-min", type=int, dest="capacity_min")
    s.add_argument("--capacity-max", type=int, dest="capacity_max")
    s.add_argument("--date-from", dest="date_from", help="Début période (YYYY-MM-DD).")
    s.add_argument("--date-to", dest="date_to", help="Fin période (YYYY-MM-DD).")
    s.add_argument("--max-results", type=int, default=15, dest="max_results")
    s.add_argument("--extra", help="Contraintes libres additionnelles.")
    s.add_argument("--no-prices", action="store_true", help="Découverte seule, sans extraction des prix.")
    s.add_argument(
        "--profile",
        choices=["eco", "equilibre", "max"],
        default=None,
        help="Compromis coût/qualité : eco (défaut, le moins cher) / equilibre / max.",
    )
    s.add_argument(
        "--fetch-strategy",
        choices=["web_fetch", "playwright", "auto"],
        default=None,
        dest="fetch_strategy",
        help="Récupération des prix : web_fetch (LLM), playwright (navigateur), auto (défaut config).",
    )
    s.add_argument("-o", "--output", default="out/benchmark.csv", help="Fichier de sortie (.csv/.xlsx).")
    s.set_defaults(func=cmd_search)

    # selftest
    st = sub.add_parser("selftest", help="Test d'intégration sans LLM ni crédits (données d'exemple).")
    st.add_argument("-o", "--output", default="out/selftest.csv", help="Fichier de sortie (.csv).")
    st.set_defaults(func=cmd_selftest)

    # recommend
    r = sub.add_parser("recommend", help="Préconiser des tarifs depuis un benchmark.")
    r.add_argument("--benchmark", required=True, help="Benchmark existant (.csv/.xlsx/.json).")
    r.add_argument("--artist", help="Artiste ciblé (pour le libellé).")
    r.add_argument("--genre", help="Style musical de la cible.")
    r.add_argument("--capacity", type=int, help="Jauge de la salle ciblée.")
    r.add_argument("-o", "--output", help="Fichier Markdown de sortie (optionnel).")
    r.set_defaults(func=cmd_recommend)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
