import os
import re
import html
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, request as freq
import requests

try:  # impersona l'impronta TLS di Chrome (aggira i blocchi di Cloudflare basati su python-requests)
    from curl_cffi import requests as cffi_requests
except Exception:
    cffi_requests = None

app = Flask(__name__)

# ─── CONFIGURA QUI ─────────────────────────────────────────────────────────────
# Meglio impostarle come Environment Variables su Vercel; i valori sotto sono il fallback.
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "b6a0ccf54e2f808390e4626b0e98ebd8")  # https://www.themoviedb.org/settings/api

# Aggiorna quando StreamingCommunity cambia dominio (oppure variabile SC_DOMAIN su Vercel)
SC_DOMAIN = os.environ.get("SC_DOMAIN", "streamingcommunityz.shiksha")
# ──────────────────────────────────────────────────────────────────────────────

# Opzionale: proxy HTTP(S) se Cloudflare blocca l'IP del server, es. http://user:pass@host:porta
PROXY_URL = os.environ.get("PROXY_URL", "")

SC_BASE = f"https://{SC_DOMAIN}"

SC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9",
    "Referer": f"https://{SC_DOMAIN}/",
    "X-Requested-With": "XMLHttpRequest",
}

_img_cache:   dict = {}
_imdb_cache:  dict = {}
_imdb_to_src: dict = {}
_session:     dict = {"http": None, "xsrf": "", "version": "", "ts": 0}
_cache_lock = threading.Lock()
SESSION_TTL = 15 * 60

GENRE_MAP = {
    "Action":           "Azione",
    "Adventure":        "Avventura",
    "Animation":        "Animazione",
    "Comedy":           "Commedia",
    "Crime":            "Crime",
    "Documentary":      "Documentario",
    "Drama":            "Dramma",
    "Family":           "Famiglia",
    "Fantasy":          "Fantasy",
    "Horror":           "Horror",
    "Reality":          "Reality",
    "Romance":          "Romance",
    "Science Fiction":  "Fantascienza",
    "Thriller":         "Thriller",
}

SECTION_MAP = {
    "top10":    "Top 10",
    "trending": "Trending",
    "latest":   "Ultimi",
    "upcoming": "Prossimamente",
}

RETRY_STATUS = (401, 403, 409, 419)


def _json_resp(payload: dict):
    resp = jsonify(payload)
    resp.headers.add("Access-Control-Allow-Origin", "*")
    return resp


# ═════════════════════════════════════════════════════════════════════════════
# SESSION
# ═════════════════════════════════════════════════════════════════════════════

def _new_http():
    if cffi_requests is not None:
        s = cffi_requests.Session(impersonate="chrome124")
        s.headers.update({k: v for k, v in SC_HEADERS.items() if k != "User-Agent"})
    else:
        s = requests.Session()
        s.headers.update(SC_HEADERS)
    if PROXY_URL:
        s.proxies = {"http": PROXY_URL, "https": PROXY_URL}
    return s


def _parse_page(text: str) -> dict | None:
    m = re.search(r'data-page="([^"]*)"', text)
    if not m:
        return None
    return json.loads(html.unescape(m.group(1)))


def _ensure_session(force: bool = False):
    now = time.time()
    if not force and _session["http"] and _session["version"] and now - _session["ts"] < SESSION_TTL:
        return
    s = _new_http()
    r1 = s.get(f"{SC_BASE}/it", timeout=15)
    page = _parse_page(r1.text)
    if page is None:
        raise RuntimeError(
            f"SC: data-page non trovato (HTTP {r1.status_code}, server={r1.headers.get('server')}, url finale={r1.url})"
        )
    version = page.get("version", "")
    s.get(f"{SC_BASE}/sanctum/csrf-cookie", headers={"Referer": f"{SC_BASE}/it/"}, timeout=10)
    try:
        xsrf = requests.utils.unquote(s.cookies.get("XSRF-TOKEN") or "")
    except Exception:
        xsrf = ""
    with _cache_lock:
        _session["http"]    = s
        _session["xsrf"]    = xsrf
        _session["version"] = version
        _session["ts"]      = now


def _post_sliders(chunk: list):
    return _session["http"].post(
        f"{SC_BASE}/api/sliders/fetch?lang=it",
        headers={
            "X-XSRF-TOKEN":      _session["xsrf"],
            "X-Inertia-Version": _session["version"],
            "Origin":            SC_BASE,
            "Accept":            "application/json, text/plain, */*",
            "Content-Type":      "application/json",
        },
        json={"sliders": chunk},
        timeout=15
    )


def _fetch_sliders(slider_requests: list) -> list:
    _ensure_session()
    all_results = []
    for i in range(0, len(slider_requests), 6):
        chunk = slider_requests[i:i+6]
        try:
            r = _post_sliders(chunk)
            if r.status_code in RETRY_STATUS:
                _ensure_session(force=True)
                r = _post_sliders(chunk)
            if r.status_code == 200:
                body = r.text.strip()
                if body.startswith("["):
                    all_results.extend(json.loads(body))
                else:
                    print(f"[SC sliders] risposta non-JSON: {body[:150]}")
            else:
                print(f"[SC sliders] HTTP {r.status_code}: {r.text[:150]}")
        except Exception as e:
            print(f"[SC sliders] {e}")
    return all_results


def _inertia_get(url: str):
    _ensure_session()
    r = None
    for attempt in range(2):
        r = _session["http"].get(
            url,
            headers={
                "X-Inertia":         "true",
                "X-Inertia-Version": _session["version"],
                "Accept":            "application/json",
            },
            timeout=15
        )
        if r.status_code in RETRY_STATUS and attempt == 0:
            _ensure_session(force=True)
            continue
        break
    return r


def _fetch_title_detail(title_id: str, slug: str) -> dict | None:
    url = f"{SC_BASE}/it/titles/{title_id}-{slug}" if slug else f"{SC_BASE}/it/titles/{title_id}"
    try:
        r = _inertia_get(url)
        if r.status_code == 200:
            return r.json()
        print(f"[SC detail] HTTP {r.status_code}")
    except Exception as e:
        print(f"[SC detail] {e}")
    return None


# ═════════════════════════════════════════════════════════════════════════════
# TMDB IMMAGINI
# ═════════════════════════════════════════════════════════════════════════════

NO_IMGS = {"poster": None, "bg": None}


def _imgs_from(data: dict) -> dict:
    return {
        "poster": f"https://image.tmdb.org/t/p/w500{data['poster_path']}"    if data.get("poster_path")   else None,
        "bg":     f"https://image.tmdb.org/t/p/w1280{data['backdrop_path']}"  if data.get("backdrop_path") else None,
    }


def _tmdb_images_by_id(tmdb_id: int, tmdb_type: str) -> dict:
    cache_key = f"id:{tmdb_type}:{tmdb_id}"
    with _cache_lock:
        if cache_key in _img_cache:
            return _img_cache[cache_key]
    try:
        r = requests.get(
            f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}",
            params={"api_key": TMDB_API_KEY, "language": "it-IT"},
            timeout=8
        )
        if r.status_code != 200:
            print(f"[TMDB img id] {tmdb_id}: HTTP {r.status_code}")
            return dict(NO_IMGS)  # non in cache: riprova alla prossima richiesta
        result = _imgs_from(r.json())
    except Exception as e:
        print(f"[TMDB img id] {tmdb_id}: {e}")
        return dict(NO_IMGS)
    with _cache_lock:
        _img_cache[cache_key] = result
    return result


def _tmdb_images_by_title(title: str, year: str | None, tmdb_type: str) -> dict:
    clean     = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()
    cache_key = f"search:{tmdb_type}:{clean.lower()}:{year or ''}"
    with _cache_lock:
        if cache_key in _img_cache:
            return _img_cache[cache_key]
    try:
        params = {"api_key": TMDB_API_KEY, "query": clean, "language": "it-IT"}
        if year:
            params["year" if tmdb_type == "movie" else "first_air_date_year"] = year
        r = requests.get(f"https://api.themoviedb.org/3/search/{tmdb_type}", params=params, timeout=8)
        if r.status_code != 200:
            print(f"[TMDB img search] '{title}': HTTP {r.status_code}")
            return dict(NO_IMGS)
        results = r.json().get("results", [])
        result  = _imgs_from(results[0] if results else {})
    except Exception as e:
        print(f"[TMDB img search] '{title}': {e}")
        return dict(NO_IMGS)
    with _cache_lock:
        _img_cache[cache_key] = result
    return result


def _resolve_images(t: dict, media_type: str) -> dict:
    tmdb_type = "tv" if media_type == "series" else "movie"
    year      = (t.get("release_date") or "")[:4] or None
    if t.get("tmdb_id"):
        imgs = _tmdb_images_by_id(t["tmdb_id"], tmdb_type)
        if imgs["poster"]:
            return imgs
    imgs = _tmdb_images_by_title(t.get("name", ""), year, tmdb_type)
    if imgs["poster"]:
        return imgs
    return dict(NO_IMGS)


# ═════════════════════════════════════════════════════════════════════════════
# TMDB IMDB ID
# ═════════════════════════════════════════════════════════════════════════════

def _tmdb_to_imdb(tmdb_id: int, tmdb_type: str) -> str | None:
    if not tmdb_id:
        return None
    cache_key = f"imdb:{tmdb_type}:{tmdb_id}"
    with _cache_lock:
        if cache_key in _imdb_cache:
            return _imdb_cache[cache_key]
    try:
        r = requests.get(
            f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}/external_ids",
            params={"api_key": TMDB_API_KEY}, timeout=8
        )
        if r.status_code != 200:
            print(f"[TMDB ext_ids] {tmdb_id}: HTTP {r.status_code}")
            return None
        imdb_id = r.json().get("imdb_id") or None
    except Exception as e:
        print(f"[TMDB ext_ids] {tmdb_id}: {e}")
        return None
    with _cache_lock:
        _imdb_cache[cache_key] = imdb_id
    return imdb_id


def _tmdb_search_imdb(title: str, year: str | None, tmdb_type: str) -> str | None:
    clean     = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()
    cache_key = f"search_imdb:{tmdb_type}:{clean.lower()}:{year or ''}"
    with _cache_lock:
        if cache_key in _imdb_cache:
            return _imdb_cache[cache_key]
    try:
        params = {"api_key": TMDB_API_KEY, "query": clean, "language": "it-IT"}
        if year:
            params["year" if tmdb_type == "movie" else "first_air_date_year"] = year
        r = requests.get(f"https://api.themoviedb.org/3/search/{tmdb_type}", params=params, timeout=8)
        if r.status_code != 200:
            print(f"[TMDB search imdb] '{title}': HTTP {r.status_code}")
            return None
        results = r.json().get("results", [])
        imdb_id = _tmdb_to_imdb(results[0]["id"], tmdb_type) if results else None
    except Exception as e:
        print(f"[TMDB search imdb] '{title}': {e}")
        return None
    with _cache_lock:
        _imdb_cache[cache_key] = imdb_id
    return imdb_id


def _resolve_imdb(t: dict, media_type: str) -> str | None:
    tmdb_type = "tv" if media_type == "series" else "movie"
    year      = (t.get("release_date") or "")[:4] or None
    if t.get("tmdb_id"):
        imdb_id = _tmdb_to_imdb(t["tmdb_id"], tmdb_type)
        if imdb_id:
            _imdb_to_src[imdb_id] = {"id": str(t.get("id", "")), "slug": t.get("slug", "")}
            return imdb_id
    imdb_id = _tmdb_search_imdb(t.get("name", ""), year, tmdb_type)
    if imdb_id:
        _imdb_to_src[imdb_id] = {"id": str(t.get("id", "")), "slug": t.get("slug", "")}
    return imdb_id


# ═════════════════════════════════════════════════════════════════════════════
# BUILD META
# ═════════════════════════════════════════════════════════════════════════════

def _build_meta(t: dict) -> dict | None:
    title = (t.get("name") or "").strip()
    sc_id = t.get("id")
    if not title or not sc_id:
        return None
    media_type = "series" if t.get("type") == "tv" else "movie"
    year       = (t.get("release_date") or "")[:4]
    imgs       = _resolve_images(t, media_type)
    imdb_id    = _resolve_imdb(t, media_type)
    if not imgs.get("poster"):
        return None
    return {
        "id":          imdb_id or f"cg{sc_id}",
        "type":        media_type,
        "name":        title,
        "poster":      imgs["poster"],
        "background":  imgs.get("bg") or "",
        "posterShape": "poster",
        "description": t.get("plot") or "",
        "releaseInfo": year,
        "imdbRating":  str(t.get("score") or ""),
        "runtime":     f"{t['runtime']} min" if t.get("runtime") else "",
        "genres":      [g["name"] for g in t.get("genres", [])],
    }


def _build_meta_safe(t: dict) -> dict | None:
    try:
        return _build_meta(t)
    except Exception as e:
        print(f"[build_meta] {e}")
        return None


def _shows_to_metas(shows: list) -> list:
    # Chiamate TMDB in parallelo: su Vercel il tempo massimo della funzione è limitato
    with ThreadPoolExecutor(max_workers=8) as ex:
        built = list(ex.map(_build_meta_safe, shows))
    metas, seen = [], set()
    for meta in built:
        if meta and meta["id"] not in seen:
            seen.add(meta["id"])
            metas.append(meta)
    return metas


# ═════════════════════════════════════════════════════════════════════════════
# MANIFEST
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/manifest.json")
def manifest():
    catalogs = []
    for ctype in ["movie", "series"]:
        for sid, sname in SECTION_MAP.items():
            catalogs.append({
                "type": ctype, "id": sid, "name": sname,
                "extra": [{"name": "search", "isRequired": False}]
            })
    for ctype in ["movie", "series"]:
        for genre_en, genre_it in GENRE_MAP.items():
            catalogs.append({
                "type": ctype, "id": f"genre_{genre_en}", "name": genre_it,
                "extra": [{"name": "search", "isRequired": False}]
            })
    data = {
        "id":          "com.catgen.stremio.v2",
        "version":     "2.0.0",
        "name":        "CatGen – StreamingCommunity",
        "description": "Cataloghi da StreamingCommunity divisi per sezione e genere.",
        "resources":   ["catalog", "meta"],
        "types":       ["movie", "series"],
        "catalogs":    catalogs,
        "idPrefixes":  ["tt", "cg"],
    }
    return _json_resp(data)


# ═════════════════════════════════════════════════════════════════════════════
# CATALOG
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/catalog/<content_type>/<catalog_id>.json")
def catalog(content_type, catalog_id):
    search_query = freq.args.get("search", "")
    if search_query:
        try:
            _ensure_session()
            r = _session["http"].get(
                f"{SC_BASE}/it/search?q={requests.utils.quote(search_query)}",
                timeout=15
            )
            d     = _parse_page(r.text)
            shows = []
            if d:
                shows = [t for t in d.get("props", {}).get("titles", []) if t.get("type") in ("movie", "tv")]
                if content_type == "series":
                    shows = [t for t in shows if t.get("type") == "tv"]
                elif content_type == "movie":
                    shows = [t for t in shows if t.get("type") == "movie"]
            metas = _shows_to_metas(shows)
        except Exception as e:
            print(f"[search] {e}")
            metas = []
        return _json_resp({"metas": metas})

    if catalog_id.startswith("genre_"):
        genre      = catalog_id.replace("genre_", "")
        slider_req = [{"name": "genre", "genre": genre}]
    else:
        slider_req = [{"name": catalog_id, "genre": None}]

    try:
        sections = _fetch_sliders(slider_req)
        shows = []
        for s in sections:
            for t in s.get("titles", []):
                if content_type == "series" and t.get("type") != "tv":
                    continue
                if content_type == "movie" and t.get("type") != "movie":
                    continue
                shows.append(t)
        metas = _shows_to_metas(shows)
    except Exception as e:
        print(f"[catalog] {e}")
        metas = []

    payload = {"metas": metas}
    if metas:
        payload["cacheMaxAge"] = 1800  # non far cachare ai client un risultato vuoto
    return _json_resp(payload)


# ═════════════════════════════════════════════════════════════════════════════
# META
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/meta/<content_type>/<meta_id>.json")
def meta(content_type, meta_id):
    try:
        if meta_id.startswith("cg"):
            parts    = meta_id[2:].split("__")
            title_id = parts[0]
            slug     = parts[1] if len(parts) > 1 else ""
        else:
            src = _imdb_to_src.get(meta_id)
            if not src:
                return _json_resp({"meta": None})
            title_id = src["id"]
            slug     = src["slug"]

        data = _fetch_title_detail(title_id, slug)
        if not data or not data.get("props", {}).get("title"):
            return _json_resp({"meta": None})

        t          = data["props"]["title"]
        media_type = "series" if t.get("type") == "tv" else "movie"
        year       = (t.get("release_date") or "")[:4]
        imgs       = _resolve_images(t, media_type)

        meta_data = {
            "id":          meta_id,
            "type":        media_type,
            "name":        t.get("name", ""),
            "poster":      imgs.get("poster") or "",
            "background":  imgs.get("bg") or "",
            "posterShape": "poster",
            "description": t.get("plot") or "",
            "releaseInfo": year,
            "imdbRating":  str(t.get("score") or ""),
            "runtime":     f"{t['runtime']} min" if t.get("runtime") else "",
            "genres":      [g["name"] for g in t.get("genres", [])],
            "cast":        [a["name"] for a in t.get("main_actors", [])],
            "trailers":    [{"source": "YouTube", "url": f"https://www.youtube.com/watch?v={x['youtube_id']}"} for x in t.get("trailers", []) if x.get("youtube_id")],
            "links":       [{"name": "Apri sul sito", "category": "links", "url": f"{SC_BASE}/it/titles/{t.get('id')}-{t.get('slug')}"}],
        }

        if media_type == "series" and t.get("seasons"):
            videos = []
            loaded = data["props"].get("loadedSeason")
            for s in t["seasons"]:
                episodes = None
                if loaded and s["id"] == loaded["id"]:
                    episodes = loaded.get("episodes", [])
                else:
                    try:
                        r2 = _inertia_get(f"{SC_BASE}/it/titles/{t['id']}-{t['slug']}/season-{s['number']}")
                        if r2.status_code == 200:
                            episodes = r2.json().get("props", {}).get("loadedSeason", {}).get("episodes", [])
                    except Exception:
                        pass
                for e in (episodes or []):
                    videos.append({
                        "id":        f"{meta_id}:S{s['number']}E{e['number']}",
                        "season":    s["number"],
                        "episode":   e["number"],
                        "title":     e.get("name") or f"Ep {e['number']}",
                        "released":  s.get("release_date") or t.get("release_date") or "",
                        "thumbnail": "",
                    })
            meta_data["videos"] = videos

    except Exception as e:
        print(f"[meta] {e}")
        meta_data = None

    return _json_resp({"meta": meta_data})


# ═════════════════════════════════════════════════════════════════════════════
# DEBUG  →  apri  https://TUO-PROGETTO.vercel.app/debug/sc
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/debug/sc")
def debug_sc():
    out = {"domain": SC_DOMAIN}

    # 1) La home di SC è raggiungibile da questo server?
    out["impersonate_chrome"] = cffi_requests is not None
    out["proxy"] = bool(PROXY_URL)
    try:
        r   = _new_http().get(f"{SC_BASE}/it", timeout=15)
        txt = r.text
        title = re.search(r"<title>(.*?)</title>", txt, re.S | re.I)
        code  = re.search(r"(?:Error code|Error)\s*:?\s*(\d{4})", txt)
        out["home"] = {
            "status":               r.status_code,
            "final_url":            str(r.url),
            "server":               r.headers.get("server"),
            "data_page_found":      'data-page="' in txt,
            "cloudflare_challenge": any(k in txt for k in ("Just a moment", "cf-chl", "challenge-platform", "cf-turnstile")),
            "page_title":           re.sub(r"\s+", " ", title.group(1)).strip() if title else "",
            "cloudflare_error":     code.group(1) if code else "",
        }
    except Exception as e:
        out["home"] = {"error": str(e)}

    # 2) Sessione + sliders
    try:
        _ensure_session(force=True)
        out["session"] = {"status": "ok", "version": _session["version"], "xsrf": bool(_session["xsrf"])}
        r = _post_sliders([{"name": "top10", "genre": None}])
        body = r.text.strip()
        n = 0
        if r.status_code == 200 and body.startswith("["):
            n = sum(len(s.get("titles", [])) for s in json.loads(body))
        out["sliders"] = {"status": r.status_code, "titles": n, "snippet": body[:120] if n == 0 else ""}
    except Exception as e:
        out["session"] = {"error": str(e)}

    # 3) TMDB risponde con la chiave attuale?
    try:
        r = requests.get("https://api.themoviedb.org/3/configuration", params={"api_key": TMDB_API_KEY}, timeout=8)
        out["tmdb"] = {"status": r.status_code}
    except Exception as e:
        out["tmdb"] = {"error": str(e)}

    return jsonify(out)


# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
