"""API SUiv'Eau : relais léger vers Hub'Eau v2 (hydrométrie).

Lancement local :  uvicorn main:app --reload
Render          :  uvicorn main:app --host 0.0.0.0 --port $PORT
"""
import os
import re
import time

import requests
from fastapi import FastAPI, HTTPException, Path, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --- Configuration (variables d'environnement) -------------------------------
BASE = "https://hubeau.eaufrance.fr/api/v2/hydrometrie"
CONTACT = os.getenv("CONTACT_EMAIL", "contact@exemple.fr")
# En prod : ALLOWED_ORIGINS="https://<utilisateur>.github.io"
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")]
TIMEOUT = 20

app = FastAPI(title="API SUiv'Eau", description="Relais Hub'Eau v2", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,  # pas de cookies ; True est invalide avec "*"
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.add_middleware(GZipMiddleware, minimum_size=1000)

# --- Client HTTP -------------------------------------------------------------
session = requests.Session()
session.headers.update(
    {"User-Agent": f"SuivEau/2.0 (+{CONTACT})", "Accept": "application/json"}
)
# Réessaie sur erreurs transitoires uniquement (pas sur 403/404/400)
session.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(
            total=2,
            backoff_factor=0.5,
            status_forcelist=(502, 503, 504),
            allowed_methods=("GET",),
        )
    ),
)


def hubeau_json(url: str, params: dict | None = None) -> dict:
    """GET JSON vers Hub'Eau ; toute erreur devient une 502 lisible."""
    try:
        r = session.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        body = e.response.text[:300] if e.response is not None else ""
        print(f"[HUBEAU] HTTP {code} {url} {params} body={body!r}")
        raise HTTPException(status_code=502, detail=f"Hub'Eau a répondu {code}")
    except (requests.RequestException, ValueError) as e:
        print(f"[HUBEAU] {url} -> {e!r}")
        raise HTTPException(status_code=502, detail="Hub'Eau injoignable")


# --- Cache mémoire avec secours "stale" --------------------------------------
_cache: dict[str, tuple[float, object]] = {}
MAX_CACHE = 500


def cached(key: str, ttl: int, loader, response: Response):
    """Renvoie la donnée en cache si fraîche ; sinon recharge.
    Si Hub'Eau est en panne, sert la dernière donnée connue (en-tête X-Cache: stale)."""
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        response.headers["X-Cache"] = "hit"
        return hit[1]
    try:
        data = loader()
    except HTTPException:
        if hit:
            response.headers["X-Cache"] = "stale"
            return hit[1]
        raise
    if len(_cache) >= MAX_CACHE:  # borne la mémoire (offres gratuites : 512 Mo)
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest, None)
    _cache[key] = (now, data)
    response.headers["X-Cache"] = "miss"
    return data


# --- Routes ------------------------------------------------------------------
@app.get("/health")
def health():
    """Pour un ping externe (anti-endormissement) ou le monitoring."""
    return {"ok": True}


@app.get("/api/stations")
def get_stations(
    response: Response,
    dept: str = Query("69", pattern=r"^(\d{2}|2[AB]|\d{3})$", description="Code département"),
):
    def load():
        rows, url, params = [], f"{BASE}/referentiel/stations", {
            "code_departement": dept,
            "size": 1000,
        }
        for _ in range(10):  # garde-fou contre une pagination infinie
            j = hubeau_json(url, params)
            rows += j.get("data", [])
            nxt = j.get("next")
            if not nxt:
                break
            url, params = nxt.replace("http://", "https://", 1), None  # 'next' est complet

        out = [
            {
                "code_station": s["code_station"],
                "libelle_station": s.get("libelle_station") or s["code_station"],
                "libelle_cours_eau": s.get("libelle_cours_eau"),
                "libelle_commune": s.get("libelle_commune"),
                "latitude_station": s["latitude_station"],
                "longitude_station": s["longitude_station"],
            }
            for s in rows
            if s.get("en_service")
            and s.get("latitude_station") is not None
            and s.get("longitude_station") is not None
        ]
        return sorted(out, key=lambda s: s["libelle_station"])

    data = cached(f"stations:{dept}", ttl=24 * 3600, loader=load, response=response)
    response.headers["Cache-Control"] = "public, max-age=3600"
    return data


@app.get("/api/observations/{code_station}")
def get_observations(
    response: Response,
    code_station: str = Path(pattern=r"^[A-Za-z0-9]{8,10}$"),
    size: int = Query(288, ge=1, le=2000, description="Nb de mesures (288 ≈ 24 h au pas de 5 min)"),
):
    def load():
        j = hubeau_json(
            f"{BASE}/observations_tr",
            {
                "code_entite": code_station,
                "grandeur_hydro": "H",
                "size": size,
                "fields": "code_station,date_obs,resultat_obs",
            },
        )
        # resultat_obs en mm, relatif au zéro d'échelle (peut être négatif).
        # Tri explicite par date : ne dépend pas de l'ordre renvoyé par l'API.
        return sorted(j.get("data", []), key=lambda o: o["date_obs"])

    data = cached(f"obs:{code_station}:{size}", ttl=300, loader=load, response=response)
    response.headers["Cache-Control"] = "public, max-age=120"
    return data


# --- Vigilance crues (tronçons colorés, flux Vigicrues / Etalab) -------------
VIGICRUES_URL = "https://www.vigicrues.gouv.fr/services/1/InfoVigiCru.geojson"


def _round(c):
    """Arrondit les coordonnées à 4 décimales (~10 m) pour alléger le flux."""
    if isinstance(c[0], (int, float)):
        return [round(c[0], 4), round(c[1], 4)]
    return [_round(x) for x in c]


@app.get("/api/vigilance")
def get_vigilance(response: Response):
    """Tronçons de vigilance crues (niv : 1 vert, 2 jaune, 3 orange, 4 rouge)."""

    def load():
        try:
            r = session.get(VIGICRUES_URL, timeout=30)
            r.raise_for_status()
            j = r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"[VIGICRUES] {e!r}")
            raise HTTPException(status_code=502, detail="Vigicrues injoignable")
        feats = []
        for f in j.get("features", []):
            g, p = f.get("geometry"), f.get("properties") or {}
            if not g or not g.get("coordinates"):
                continue
            feats.append({
                "type": "Feature",
                "properties": {"code": p.get("CdEntCru"), "nom": p.get("lbentcru"), "niv": p.get("NivInfViCr")},
                "geometry": {"type": g["type"], "coordinates": _round(g["coordinates"])},
            })
        return {"type": "FeatureCollection", "publie": j.get("DtHrInfoVigiCru"), "features": feats}

    data = cached("vigilance", ttl=600, loader=load, response=response)
    response.headers["Cache-Control"] = "public, max-age=300"
    return data
