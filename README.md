# Flixdex

A Windows desktop app for browsing everything currently streaming on **Netflix** and **HBO Max** in your country, with **IMDb ratings** for every title.

![Flixdex main window](docs/screenshot.png)

## Features

- The full current Netflix and HBO Max catalogs for your country (pick it in Settings), with a **Netflix / HBO Max / Both** switch
- **IMDb rating and vote count** for every movie and series
- Search, plus filters for type, genre, release year, minimum IMDb and minimum TMDB score
- Sort by popularity, IMDb rating, TMDB rating, newest, oldest or title
- **Watch on Netflix / Watch on HBO Max** buttons that open the title's page directly (or a search when no direct link is known)
- Resizable layout: the filter panel tucks away on narrow windows

![Title details](docs/screenshot-detail.png)

## No daily limits

Flixdex only uses sources without a daily quota, and saves what it fetches so it rarely asks twice:

| What | Source | Key needed |
|---|---|---|
| Catalog, posters, genres | [TMDB](https://www.themoviedb.org) (streaming data by JustWatch) | Free TMDB key, no daily cap |
| IMDb ratings | [IMDb's ratings dataset](https://developer.imdb.com/non-commercial-datasets/), downloaded once a day (~9 MB) | None |
| Direct Netflix / HBO Max links | [Wikidata](https://www.wikidata.org) | None |

## Getting started

1. Download `Flixdex.exe` from the [latest release](../../releases/latest) and run it. Nothing to install.
   Windows SmartScreen may warn about an unrecognized app the first time; choose **More info → Run anyway**.
2. Get a free TMDB API key at [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api)
   (the application form asks for a URL; `http://localhost` is fine for personal use).
3. Paste the key (or the longer *API Read Access Token*) into Flixdex and pick your country.

The first start downloads the catalogs and IMDb's ratings file and matches titles to IMDb in the background (a few minutes). After that, starts are instant.

Your key and all saved data stay on your computer in `%APPDATA%\Flixdex`.

## Run or build from source

Requires Python 3.11+ on Windows.

```bat
build.bat
```

This creates a `.venv` with PySide6 and PyInstaller on first run and writes `dist\Flixdex.exe`. To run from source instead, use `run.bat` (after `build.bat` has set up `.venv`), or:

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python flixdex.py
```

## Credits

- This product uses the TMDB API but is not endorsed or certified by TMDB. Streaming availability data by JustWatch.
- IMDb ratings: information courtesy of IMDb ([imdb.com](https://www.imdb.com)), used with permission for personal and non-commercial use.
- Streaming links from Wikidata.
- Not affiliated with Netflix, HBO or Warner Bros. Discovery.

## License

[MIT](LICENSE) for the code. The data sources above have their own terms.
