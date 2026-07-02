#!/usr/bin/env python3
import csv
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime

import trakt.core
from trakt import init

from processor import TVShowProcessor, MovieProcessor
from searcher import TVTimeTVShow, TVTimeMovie

# Setup logger
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] :: %(message)s",
    encoding='utf-8',
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
#    datefmt="%x %X", #Uncomment for locale date and time, if preffered
)

# Adjust this value to increase/decrease your requests between episodes.
# Make to remain within the rate limit: https://trakt.docs.apiary.io/#introduction/rate-limiting
DELAY_BETWEEN_ITEMS_IN_SECONDS = 1


@dataclass
class Config:
    trakt_username: str
    client_id: str
    client_secret: str
    movie_path: str
    show_path: str
    date_format: str


def is_authenticated() -> bool:
    with open("pytrakt.json") as f:
        data = json.load(f)
        days_before_expiration = (
                datetime.fromtimestamp(data["OAUTH_EXPIRES_AT"]) - datetime.now()
        ).days
        return days_before_expiration >= 1


def get_configuration() -> Config:
    try:
        with open("config.json") as f:
            data = json.load(f)

        return Config(
            data["TRAKT_USERNAME"],
            data["CLIENT_ID"],
            data["CLIENT_SECRET"],
            data["MOVIE_DATA_PATH"],
            data["SHOW_DATA_PATH"],
            data["DATE_FORMAT"]
        )
    except FileNotFoundError:
        logging.info("config.json not found prompting user for input")
        return Config(
            input("Enter your Trakt.tv username: "),
            input("Enter you Client id: "),
            input("Enter your Client secret: "),
            input("Enter your Movie Data Path: "),
            input("Enter your Show Data Path: "),
            input("Please enter the date format: ")
        )


config = get_configuration()

WATCHED_SHOWS_PATH = config.show_path
WATCHED_MOVIES_PATH = config.movie_path

# TV Time's GDPR export has changed format over time. There are two shapes we support:
#
# 1. UNIFIED format (current, as of 2026): a single "tracking-prod-records.csv" file
#    holds BOTH movie and episode watch/follow events in the same rows, distinguished
#    by an "entity_type" column ("movie" or "episode"). Other files in the export
#    (tracking-prod-records-v2.csv, *-votes.csv, *-ratings.csv, where-to-watch-*.csv)
#    are votes/ratings/stats tables that this script does not use.
#
# 2. LEGACY format (older exports): separate files for movies and shows, without an
#    "entity_type" column, matching the column sets below.
UNIFIED_REQUIRED_COLUMNS = {
    "entity_type", "movie_name", "series_name", "type", "created_at", "updated_at",
    "episode_id", "season_number", "episode_number", "release_date",
}
LEGACY_SHOW_REQUIRED_COLUMNS = {"series_name", "created_at", "episode_id", "season_number", "episode_number"}
LEGACY_MOVIE_REQUIRED_COLUMNS = {"movie_name", "updated_at", "type", "release_date"}

# TV Time spreads episode watch-history across SEVERAL tables in the GDPR export, which
# barely overlap with each other (each seems to log watches from a different part of the
# app). To get a complete import we read all of them and merge on episode_id. Each entry
# maps: exact filename in the export -> the column in that file that corresponds to each
# field TVTimeTVShow needs.
SHOW_SUPPLEMENTARY_SOURCES = [
    {
        "filename": "watched_on_episode.csv",
        "column_map": {
            "series_name": "tv_show_name",
            "created_at": "created_at",
            "episode_id": "episode_id",
            "season_number": "episode_season_number",
            "episode_number": "episode_number",
        },
    },
    {
        "filename": "seen_episode_source.csv",
        "column_map": {
            "series_name": "tv_show_name",
            "created_at": "created_at",
            "episode_id": "episode_id",
            "season_number": "episode_season_number",
            "episode_number": "episode_number",
        },
    },
    {
        "filename": "seen_episode_latest.csv",
        "column_map": {
            "series_name": "tv_show_name",
            "created_at": "created_at",
            "episode_id": "episode_id",
            "season_number": "episode_season_number",
            "episode_number": "episode_number",
        },
    },
]


def find_csv_files(directory: str, required_columns: set) -> list[str]:
    """
    Scans `directory` for CSV files whose header contains all of `required_columns`,
    and returns the matching file paths, sorted alphabetically for deterministic order.
    If `directory` is actually a single file (legacy config pointing directly at a CSV),
    that file is returned on its own, without checking the header.
    """
    if os.path.isfile(directory):
        return [directory]

    matched_files = []
    for filename in sorted(os.listdir(directory)):
        if not filename.lower().endswith(".csv"):
            continue

        file_path = os.path.join(directory, filename)
        try:
            with open(file_path, newline="", encoding="UTF-8") as csvfile:
                reader = csv.DictReader(csvfile, delimiter=",")
                headers = set(reader.fieldnames or [])
                if required_columns.issubset(headers):
                    matched_files.append(file_path)
        except (OSError, csv.Error) as e:
            logging.warning(f"Could not read '{file_path}' as CSV, skipping. ({e})")

    return matched_files


def find_file_in_directory(directory: str, filename: str) -> str | None:
    """Looks for `filename` inside `directory`, case-insensitively. Returns None if not found
    or if `directory` isn't actually a directory (e.g. legacy config pointing at a single file)."""
    if not os.path.isdir(directory):
        return None

    exact_path = os.path.join(directory, filename)
    if os.path.isfile(exact_path):
        return exact_path

    for entry in os.listdir(directory):
        if entry.lower() == filename.lower():
            return os.path.join(directory, entry)

    return None


def collect_show_rows(directory: str) -> list[dict]:
    """
    Gathers episode watch events from every known source in the GDPR export and merges
    them into a single de-duplicated list of normalised rows (keyed by episode_id), each
    shaped exactly like what TVTimeTVShow expects: series_name, created_at, episode_id,
    season_number, episode_number.
    """
    episodes_by_id: dict[str, dict] = {}

    # 1. The unified tracking file ("tracking-prod-records.csv"), entity_type == "episode"
    for file_path in find_csv_files(directory, UNIFIED_REQUIRED_COLUMNS):
        with open(file_path, newline="", encoding="UTF-8") as csvfile:
            for row in csv.DictReader(csvfile, delimiter=","):
                if row.get("entity_type") != "episode":
                    continue
                if not row.get("episode_number") or not row.get("series_name") or not row.get("episode_id"):
                    continue
                episodes_by_id.setdefault(row["episode_id"], {
                    "series_name": row["series_name"],
                    "created_at": row["created_at"],
                    "episode_id": row["episode_id"],
                    "season_number": row["season_number"],
                    "episode_number": row["episode_number"],
                })

    # 2. Supplementary per-episode watch-history tables (only available when pointed at a
    #    directory containing the full GDPR export, not a single legacy CSV file)
    for source in SHOW_SUPPLEMENTARY_SOURCES:
        file_path = find_file_in_directory(directory, source["filename"])
        if not file_path:
            continue

        with open(file_path, newline="", encoding="UTF-8") as csvfile:
            for row in csv.DictReader(csvfile, delimiter=","):
                normalized = {target: row.get(source_col, "") for target, source_col in source["column_map"].items()}
                if not normalized["episode_number"] or not normalized["series_name"] or not normalized["episode_id"]:
                    continue
                episodes_by_id.setdefault(normalized["episode_id"], normalized)

    return list(episodes_by_id.values())


def init_trakt_auth() -> bool:

    if is_authenticated():
        return True
    trakt.core.AUTH_METHOD = trakt.core.OAUTH_AUTH
    return init(
        config.trakt_username,
        store=True,
        client_id=config.client_id,
        client_secret=config.client_secret,
    )


def process_watched_shows() -> None:
    show_rows = collect_show_rows(WATCHED_SHOWS_PATH)

    if show_rows:
        total_rows = len(show_rows)
        logging.info(f"Found {total_rows} unique episode watch events across all known sources in '{WATCHED_SHOWS_PATH}'.")

        for rows_count, row in enumerate(show_rows):
            tv_time_show = TVTimeTVShow(row)
            progress = "{:.2f}%".format(rows_count / total_rows * 100 if total_rows else 0)
            TVShowProcessor().process_item(tv_time_show, progress)
        return

    # Fall back to the legacy separate-file format
    show_files = find_csv_files(WATCHED_SHOWS_PATH, LEGACY_SHOW_REQUIRED_COLUMNS)
    if not show_files:
        logging.warning(f"No show CSV files found in '{WATCHED_SHOWS_PATH}'.")
        return

    logging.info(f"Found {len(show_files)} legacy show CSV file(s) to process: {[os.path.basename(f) for f in show_files]}")

    for file_index, file_path in enumerate(show_files, start=1):
        logging.info(f"Processing show file {file_index}/{len(show_files)}: '{os.path.basename(file_path)}'")
        with open(file_path, newline="", encoding="UTF-8") as csvfile:
            reader = csv.DictReader(csvfile, delimiter=",")
            total_rows = len(list(reader))
            csvfile.seek(0, 0)

            # Ignore the header row
            next(reader, None)
            for rows_count, row in enumerate(reader):
                if row["episode_number"] == "":  # if not an episode entry
                    continue
                if row["series_name"] == "":  # if the series name is blank
                    continue
                tv_time_show = TVTimeTVShow(row)
                progress = "File {}/{} - {:.2f}%".format(
                    file_index, len(show_files), rows_count / total_rows * 100 if total_rows else 0
                )
                TVShowProcessor().process_item(tv_time_show, progress)


def process_watched_movies() -> None:
    unified_files = find_csv_files(WATCHED_MOVIES_PATH, UNIFIED_REQUIRED_COLUMNS)

    if unified_files:
        logging.info(
            f"Found {len(unified_files)} unified tracking file(s) to process for movies: "
            f"{[os.path.basename(f) for f in unified_files]}"
        )
        for file_index, file_path in enumerate(unified_files, start=1):
            logging.info(f"Processing movie entries in file {file_index}/{len(unified_files)}: '{os.path.basename(file_path)}'")
            with open(file_path, newline="", encoding="UTF-8") as csvfile:
                all_rows = list(csv.DictReader(csvfile, delimiter=","))
                movie_rows = [r for r in all_rows if r.get("entity_type") == "movie" and r["movie_name"] != ""]
                watched_list = [r["movie_name"] for r in movie_rows if r["type"] == "watch"]
                total_rows = len(movie_rows)

                for rows_count, row in enumerate(movie_rows):
                    movie = TVTimeMovie(row)
                    progress = "File {}/{} - {:.2f}%".format(
                        file_index, len(unified_files), rows_count / total_rows * 100 if total_rows else 0
                    )
                    MovieProcessor(watched_list).process_item(movie, progress)
        return

    # Fall back to the legacy separate-file format
    movie_files = find_csv_files(WATCHED_MOVIES_PATH, LEGACY_MOVIE_REQUIRED_COLUMNS)
    if not movie_files:
        logging.warning(f"No movie CSV files found in '{WATCHED_MOVIES_PATH}'.")
        return

    logging.info(f"Found {len(movie_files)} legacy movie CSV file(s) to process: {[os.path.basename(f) for f in movie_files]}")

    for file_index, file_path in enumerate(movie_files, start=1):
        logging.info(f"Processing movie file {file_index}/{len(movie_files)}: '{os.path.basename(file_path)}'")
        with open(file_path, newline="", encoding="UTF-8") as csvfile:
            reader = filter(lambda p: p["movie_name"] != "", csv.DictReader(csvfile, delimiter=","))
            watched_list = [row["movie_name"] for row in reader if row["type"] == "watch"]
            csvfile.seek(0, 0)
            total_rows = len(list(reader))
            csvfile.seek(0, 0)

            # Ignore the header row
            next(reader, None)
            for rows_count, row in enumerate(reader):
                movie = TVTimeMovie(row)
                progress = "File {}/{} - {:.2f}%".format(
                    file_index, len(movie_files), rows_count / total_rows * 100 if total_rows else 0
                )
                MovieProcessor(watched_list).process_item(movie, progress)


def menu_selection() -> int:
    # Display a menu selection
    print(">> What do you want to do?")
    print("    1) Import Watch History for TV Shows from TV Time")
    print("    2) Import Watched Movies from TV Time")
    print("    3) Do both 1 and 2 (default)")
    print("    4) Exit")

    while True:
        try:
            selection = input("Enter your menu selection: ")
            selection = 3 if not selection else int(selection)
            break
        except ValueError:
            logging.warning("Invalid input. Please enter a numerical number.")
    # Check if the input is valid
    if not 1 <= selection <= 4:
        logging.warning("Sorry - that's an unknown menu selection")
        exit()
    # Exit if the 4th option was chosen
    if selection == 4:
        logging.info("Exiting as per user's selection.")
        exit()

    return selection


def start():
    selection = menu_selection()

    # Create the initial authentication with Trakt, before starting the process
    if not init_trakt_auth():
        logging.error(
            "ERROR: Unable to complete authentication to Trakt - please try again."
        )

    if selection == 1:
        logging.info("Processing watched shows.")
        process_watched_shows()
        # TODO: Add support for followed shows
    elif selection == 2:
        logging.info("Processing movies.")
        process_watched_movies()
    elif selection == 3:
        logging.info("Processing both watched shows and movies.")
        process_watched_shows()
        process_watched_movies()


if __name__ == "__main__":
    # Check that the user has provided a valid GDPR path - this can now be either
    # a single CSV file (legacy behaviour) or a directory containing one or more CSVs.
    movie_path_valid = os.path.isfile(config.movie_path) or os.path.isdir(config.movie_path)
    show_path_valid = os.path.isfile(config.show_path) or os.path.isdir(config.show_path)

    if movie_path_valid and show_path_valid:
        start()
    else:
        logging.error(
            "Oops! The path provided does not exist on the local system. Please check it, and try again."
        )