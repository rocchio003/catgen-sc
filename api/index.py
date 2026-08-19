import re
import time
import threading
import json
from flask import Flask, jsonify, request as freq
import requests

app = Flask(__name__)

# ─── CONFIGURA QUI ─────────────────────────────────────────────────────────────
TMDB_API_KEY = "b6a0ccf54e2f808390e4626b0e98ebd8"     # https://www.themoviedb.org/settings/api

# Aggiorna quando StreamingCommunity cambia dominio
SC_DOMAIN = "streamingcommunityz.partners"
# ──────────────────────────────────────────────────────────────────────────────

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
_session:     dict = {"cookie": "", "xsrf": "", "version": "", "ts": 0}
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


# ═════════════════════════════════════════════════════════════════════════════
# SESSION
# ═════════════════════════════════════════════════════════════════════════════

def _ensure_session(force: bool = False):
    now = time.time()
    if not force and _session["cookie"] and _session["version"] and now - _session["ts"] < SESSION_TTL:
        return
    r1 = requests.get(f"{SC_BASE}/it", headers=SC_HEADERS, timeout=15)
    m  = re.search(r'data-page="([^"]*)"', r1.text)
    if not m:
        raise RuntimeError("SC: data-page non trovato")
    page_data = json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&"))
    version   = page_data.get("version", "")
    c1        = "; ".join(c.split(";")[0] for c in r1.headers.get("Set-Cookie", "").split(", ") if "=" in c.split(";")[0])
    r2 = requests.get(
        f"{SC_BASE}/sanctum/csrf-cookie",
        headers={**SC_HEADERS, "Referer": f"{SC_BASE}/it/", "Cookie": c1},
        timeout=10
    )
    all_cookies = []
    for h in [r1.headers, r2.headers]:
        raw = h.get("Set-Cookie", "")
        if raw:
            for part in raw.split(", "):
                kv = part.split(";")[0]
                if "=" in kv:
                    all_cookies.append(kv)
    cookie_str = "; ".join(all_cookies)
    xsrf = ""
    for c in all_cookies:
        if c.startswith("XSRF-TOKEN="):
            xsrf = requests.utils.unquote(c.split("=", 1)[1])
            break
    with _cache_lock:
        _session["cookie"]  = cookie_str
        _session["xsrf"]    = xsrf
        _session["version"] = version
        _session["ts"]      = now


def _fetch_sliders(slider_requests: list) -> list:
    _ensure_session()
    all_results = []
    for i in range(0, len(slider_requests), 6):
        chunk = slider_requests[i:i+6]
        try:
            r = requests.post(
                f"{SC_BASE}/api/sliders/fetch?lang=it",
                headers={
                    **SC_HEADERS,
                    "Cookie":            _session["cookie"],
                    "X-XSRF-TOKEN":      _session["xsrf"],
                    "X-Inertia-Version": _session["version"],
                    "Origin":            SC_BASE,
                    "Accept":            "application/json, text/plain, */*",
                    "Content-Type":      "application/json",
                },
                json={"sliders": chunk},
                timeout=15
            )
            if r.status_code == 200:
                body = r.text.strip()
                if body.startswith("["):
                    all_results.extend(json.loads(body))
        except Exception as e:
            print(f"[SC sliders] {e}")
    return all_results


def _fetch_title_detail(title_id: str, slug: str) -> dict | None:
    _ensure_session()
    url = f"{SC_BASE}/it/titles/{title_id}-{slug}" if slug else f"{SC_BASE}/it/titles/{title_id}"
    try:
        r = requests.get(url, headers={
            **SC_HEADERS,
            "Cookie":            _session["cookie"],
            "X-Inertia":         "true",
            "X-Inertia-Version": _session["version"],
            "Accept":            "application/json",
        }, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[SC detail] {e}")
    return None


# ═════════════════════════════════════════════════════════════════════════════
# TMDB IMMAGINI
# ═════════════════════════════════════════════════════════════════════════════

def _tmdb_images_by_id(tmdb_id: int, tmdb_type: str) -> dict:
    cache_key = f"id:{tmdb_type}:{tmdb_id}"
    with _cache_lock:
        if cache_key in _img_cache:
            return _img_cache[cache_key]
    try:
        r = requests.get(
            f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}",
            params={"api_key": TMDB_API_KEY, "language": "it-IT"},
            timeout=5
        )
        data = r.json()
        result = {
            "poster": f"https://image.tmdb.org/t/p/w500{data['poster_path']}"    if data.get("poster_path")   else None,
            "bg":     f"https://image.tmdb.org/t/p/w1280{data['backdrop_path']}"  if data.get("backdrop_path") else None,
        }
    except Exception as e:
        print(f"[TMDB img id] {tmdb_id}: {e}")
        result = {"poster": None, "bg": None}
    time.sleep(0.05)
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
        r       = requests.get(f"https://api.themoviedb.org/3/search/{tmdb_type}", params=params, timeout=5)
        results = r.json().get("results", [])
        first   = results[0] if results else {}
        result  = {
            "poster": f"https://image.tmdb.org/t/p/w500{first['poster_path']}"    if first.get("poster_path")   else None,
            "bg":     f"https://image.tmdb.org/t/p/w1280{first['backdrop_path']}"  if first.get("backdrop_path") else None,
        }
    except Exception as e:
        print(f"[TMDB img search] '{title}': {e}")
        result = {"poster": None, "bg": None}
    time.sleep(0.05)
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
    title = t.get("name", "")
    imgs  = _tmdb_images_by_title(title, year, tmdb_type)
    if imgs["poster"]:
        return imgs
    return {"poster": None, "bg": None}


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
        r       = requests.get(
            f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}/external_ids",
            params={"api_key": TMDB_API_KEY}, timeout=5
        )
        imdb_id = r.json().get("imdb_id")
    except Exception as e:
        print(f"[TMDB ext_ids] {tmdb_id}: {e}")
        imdb_id = None
    time.sleep(0.05)
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
        r       = requests.get(f"https://api.themoviedb.org/3/search/{tmdb_type}", params=params, timeout=5)
        results = r.json().get("results", [])
        imdb_id = _tmdb_to_imdb(results[0]["id"], tmdb_type) if results else None
    except Exception as e:
        print(f"[TMDB search imdb] '{title}': {e}")
        imdb_id = None
    time.sleep(0.05)
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


def _shows_to_metas(shows: list) -> list:
    metas, seen = [], set()
    for t in shows:
        meta = _build_meta(t)
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
    resp = jsonify(data)
    resp.headers.add("Access-Control-Allow-Origin", "*")
    return resp


# ═════════════════════════════════════════════════════════════════════════════
# CATALOG
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/catalog/<content_type>/<catalog_id>.json")
def catalog(content_type, catalog_id):
    search_query = freq.args.get("search", "")
    if search_query:
        try:
            _ensure_session()
            r    = requests.get(
                f"{SC_BASE}/it/search?q={requests.utils.quote(search_query)}",
                headers={**SC_HEADERS, "Cookie": _session["cookie"]}, timeout=15
            )
            m     = re.search(r'data-page="([^"]*)"', r.text)
            shows = []
            if m:
                d     = json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&"))
                shows = [t for t in d.get("props", {}).get("titles", []) if t.get("type") in ("movie", "tv")]
                if content_type == "series":
                    shows = [t for t in shows if t.get("type") == "tv"]
                elif content_type == "movie":
                    shows = [t for t in shows if t.get("type") == "movie"]
            metas = _shows_to_metas(shows)
        except Exception as e:
            print(f"[search] {e}")
            metas = []
        resp = jsonify({"metas": metas})
        resp.headers.add("Access-Control-Allow-Origin", "*")
        return resp

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

    resp = jsonify({"metas": metas, "cacheMaxAge": 1800})
    resp.headers.add("Access-Control-Allow-Origin", "*")
    return resp


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
                resp = jsonify({"meta": None})
                resp.headers.add("Access-Control-Allow-Origin", "*")
                return resp
            title_id = src["id"]
            slug     = src["slug"]

        data = _fetch_title_detail(title_id, slug)
        if not data or not data.get("props", {}).get("title"):
            resp = jsonify({"meta": None})
            resp.headers.add("Access-Control-Allow-Origin", "*")
            return resp

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
                        r2 = requests.get(
                            f"{SC_BASE}/it/titles/{t['id']}-{t['slug']}/season-{s['number']}",
                            headers={**SC_HEADERS, "Cookie": _session["cookie"],
                                     "X-Inertia": "true", "X-Inertia-Version": _session["version"],
                                     "Accept": "application/json"},
                            timeout=15
                        )
                        if r2.status_code == 200:
                            episodes = r2.json().get("props", {}).get("loadedSeason", {}).get("episodes", [])
                    except Exception:
                        pass
                for e in (episodes or []):
                    ep_fn = next((i["filename"] for i in e.get("images", []) if i.get("type") == "cover"), None)
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

    resp = jsonify({"meta": meta_data})
    resp.headers.add("Access-Control-Allow-Origin", "*")
    return resp


# ═════════════════════════════════════════════════════════════════════════════
# DEBUG
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/debug/sc")
def debug_sc():
    try:
        _ensure_session(force=True)
        return jsonify({
            "domain":  SC_DOMAIN,
            "version": _session["version"],
            "status":  "ok"
        })
    except Exception as e:
        return jsonify({"error": str(e)})


# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
