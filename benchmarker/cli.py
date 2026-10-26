"""Interface en ligne de commande du benchmarker.

Sous-commandes :
  - search             : mode Artiste — critères → benchmark (CSV/Excel) + synthèse.
  - recommend          : mode Artiste — préconise des tarifs depuis un benchmark.
  - festival           : mode Festival — critères → benchmark des grilles de pass.
  - festival-recommend : mode Festival — préconise des prix de pass.
  - selftest           : test d'intégration sans LLM (les deux modes).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from . import analysis, recommend
from .models import Concert, Festival, FestivalCriteria, SearchCriteria


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
    md = analysis.summary_to_markdown(analysis.summarize(df))
    _write_outputs(Path(args.output), df, concerts, md)
    print(md)
    return 0


def _write_outputs(out: Path, df: pd.DataFrame, items: list, md: str) -> None:
    """Écrit le benchmark (CSV/Excel), les données brutes JSON et la synthèse."""
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() in (".xlsx", ".xls"):
        df.to_excel(out, index=False)
    else:
        df.to_csv(out, index=False)
    _log(f"Benchmark écrit : {out}")

    raw = out.with_suffix(".json")
    raw.write_text(
        json.dumps([i.model_dump() for i in items], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _log(f"Données brutes : {raw}")

    report = out.with_name(out.stem + "_synthese.md")
    report.write_text(md, encoding="utf-8")
    _log(f"Synthèse : {report}")


# --- festival ---------------------------------------------------------------
def cmd_festival(args: argparse.Namespace) -> int:
    from .pipeline import build_festival_benchmark

    if args.criteria:
        criteria = FestivalCriteria(**json.loads(Path(args.criteria).read_text(encoding="utf-8")))
    else:
        criteria = FestivalCriteria(
            reference_festival=args.reference_festival,
            genres=args.genre or [],
            location=args.location,
            countries=args.country or ["France"],
            settings=args.setting or [],
            capacity_min=args.capacity_min,
            capacity_max=args.capacity_max,
            nights_min=args.nights_min,
            nights_max=args.nights_max,
            artists_per_night_min=args.artists_per_night_min,
            artists_per_night_max=args.artists_per_night_max,
            date_from=args.date_from,
            date_to=args.date_to,
            max_results=args.max_results,
            extra=args.extra,
        )
    festivals = build_festival_benchmark(
        criteria,
        with_prices=not args.no_prices,
        profile=args.profile,
        strategy=args.fetch_strategy,
        progress=_log,
    )
    df = analysis.festivals_to_frame(festivals)
    md = analysis.summary_festivals_to_markdown(analysis.summarize_festivals(df))
    _write_outputs(Path(args.output), df, festivals, md)
    print(md)
    return 0


def cmd_festival_recommend(args: argparse.Namespace) -> int:
    data = json.loads(Path(args.benchmark).read_text(encoding="utf-8"))
    df = analysis.festivals_to_frame([Festival(**f) for f in data])
    reco = recommend.recommend_festival(
        df, name=args.name, genre=args.genre, capacity=args.capacity,
        nb_nights=args.nights, setting=args.setting, artists_per_night=args.artists_per_night,
    )
    md = recommend.recommendation_festival_to_markdown(reco)
    print(md)
    if args.output:
        Path(args.output).write_text(md, encoding="utf-8")
        _log(f"Préconisation écrite : {args.output}")
    return 0


# --- selftest (sans LLM, sans crédits) --------------------------------------
def cmd_selftest(args: argparse.Namespace) -> int:
    """Test d'intégration de toute la chaîne non-LLM sur des données d'exemple."""
    from .sample import sample_concerts, sample_festivals

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

    # --- Mode Festival ---
    festivals = sample_festivals()
    fdf = analysis.festivals_to_frame(festivals)
    fsummary = analysis.summarize_festivals(fdf)
    freco = recommend.recommend_festival(fdf, genre="rap", capacity=30000, nb_nights=3, setting="rural")
    assert fsummary["festivals_trouves"] == len(festivals), "compte de festivals incohérent"
    assert fsummary["prix_par_jour"]["count"] > 0, "aucun prix/jour calculé"
    assert "Pass 1 jour" in fsummary["par_categorie"], "normalisation des pass cassée"
    assert freco["reco_par_categorie"], "reco festival vide"
    fout = out.with_name(out.stem + "_festivals.csv")
    fdf.to_csv(fout, index=False)
    _log(f"Fichier festival écrit : {fout}")
    print("\n" + analysis.summary_festivals_to_markdown(fsummary))
    print("\n" + recommend.recommendation_festival_to_markdown(freco))
    print("\n✅ selftest OK — chaîne non-LLM des modes Artiste et Festival fonctionnelle (aucun crédit consommé).")
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
        reference_artist=args.reference_artist,
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
    s.add_argument(
        "--reference-artist",
        dest="reference_artist",
        help="Artiste de référence : recherche des artistes comparables (style, notoriété, taille de salle) pour orienter la découverte.",
    )
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

    # festival
    f = sub.add_parser("festival", help="Mode Festival : benchmark des grilles de pass.")
    f.add_argument("--criteria", help="Fichier JSON de critères festival (prioritaire).")
    f.add_argument("--reference-festival", dest="reference_festival",
                   help="Festival de référence : recherche des festivals comparables (jauge, cadre, programmation, format).")
    f.add_argument("--genre", action="append", help="Programmation (répétable).")
    f.add_argument("--location", help="Zone géographique.")
    f.add_argument("--country", action="append", help="Pays (répétable).")
    f.add_argument("--setting", action="append", choices=["urbain", "periurbain", "rural"],
                   help="Cadre d'implantation (répétable ; aucun = tous).")
    f.add_argument("--capacity-min", type=int, dest="capacity_min", help="Jauge journalière min.")
    f.add_argument("--capacity-max", type=int, dest="capacity_max", help="Jauge journalière max.")
    f.add_argument("--nights-min", type=int, dest="nights_min", help="Nombre de soirs min.")
    f.add_argument("--nights-max", type=int, dest="nights_max", help="Nombre de soirs max.")
    f.add_argument("--artists-per-night-min", type=int, dest="artists_per_night_min")
    f.add_argument("--artists-per-night-max", type=int, dest="artists_per_night_max")
    f.add_argument("--date-from", dest="date_from", help="Début période (YYYY-MM-DD).")
    f.add_argument("--date-to", dest="date_to", help="Fin période (YYYY-MM-DD).")
    f.add_argument("--max-results", type=int, default=10, dest="max_results")
    f.add_argument("--extra", help="Contraintes libres additionnelles.")
    f.add_argument("--no-prices", action="store_true", help="Découverte seule, sans extraction des prix.")
    f.add_argument("--profile", choices=["eco", "equilibre", "max"], default=None)
    f.add_argument("--fetch-strategy", choices=["web_fetch", "playwright", "auto"], default=None, dest="fetch_strategy")
    f.add_argument("-o", "--output", default="out/festivals.csv", help="Fichier de sortie (.csv/.xlsx).")
    f.set_defaults(func=cmd_festival)

    fr = sub.add_parser("festival-recommend", help="Mode Festival : préconiser des prix de pass.")
    fr.add_argument("--benchmark", required=True, help="Benchmark festival existant (.json).")
    fr.add_argument("--name", help="Festival ciblé (libellé).")
    fr.add_argument("--genre", help="Programmation de la cible.")
    fr.add_argument("--capacity", type=int, help="Jauge journalière de la cible.")
    fr.add_argument("--nights", type=int, help="Nombre de soirs de la cible.")
    fr.add_argument("--setting", choices=["urbain", "periurbain", "rural"], help="Cadre de la cible.")
    fr.add_argument("--artists-per-night", type=int, dest="artists_per_night", help="Artistes par soir de la cible.")
    fr.add_argument("-o", "--output", help="Fichier Markdown de sortie (optionnel).")
    fr.set_defaults(func=cmd_festival_recommend)

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
