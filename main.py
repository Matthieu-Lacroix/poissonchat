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
    dept: str | None = Query(None, pattern=r"^(\d{2}|2[AB]|\d{3})$", description="Code département (vide = France entière)"),
):
    def load():
        rows, url, params = [], f"{BASE}/referentiel/stations", {"size": 5000}
        if dept:
            params["code_departement"] = dept
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
                "code_departement": s.get("code_departement"),
                "latitude_station": s["latitude_station"],
                "longitude_station": s["longitude_station"],
            }
            for s in rows
            if s.get("en_service")
            and s.get("latitude_station") is not None
            and s.get("longitude_station") is not None
        ]
        return sorted(out, key=lambda s: s["libelle_station"])

    data = cached(f"stations:{dept or 'all'}", ttl=24 * 3600, loader=load, response=response)
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


# --- Fiche station Vigicrues : crues historiques de référence ----------------
@app.get("/api/station/{code_station}")
def get_station_info(response: Response, code_station: str = Path(pattern=r"^[A-Za-z0-9]{8,10}$")):
    """Crues historiques (hauteurs en m) si la station est dans le réseau Vigicrues, sinon liste vide."""

    def load():
        try:
            r = session.get("https://www.vigicrues.gouv.fr/services/station.json",
                            params={"CdStationHydro": code_station}, timeout=20)
        except requests.RequestException as e:
            print(f"[VIGICRUES] station {code_station}: {e!r}")
            raise HTTPException(status_code=502, detail="Vigicrues injoignable")
        if 400 <= r.status_code < 500:
            return {"crues": []}          # station hors réseau Vigicrues
        if not r.ok:
            raise HTTPException(status_code=502, detail=f"Vigicrues a répondu {r.status_code}")
        try:
            j = r.json()
        except ValueError:
            return {"crues": []}
        brut = (j.get("VigilanceCrues") or {}).get("CruesHistoriques") or []
        return {"crues": [{"nom": c.get("LbUsuel"), "h": c["ValHauteur"]}
                          for c in brut if c.get("ValHauteur")]}

    data = cached(f"station:{code_station}", ttl=24 * 3600, loader=load, response=response)
    response.headers["Cache-Control"] = "public, max-age=3600"
    return data


# --- Situation historique : séries élaborées Hub'Eau (obs_elab) ---------------
# Indicatif : compare le débit actuel aux débits journaliers de la même période de l'année
# (±15 jours, années passées) et aux maxima annuels. Unités : l/s (identiques en temps réel).
from bisect import bisect_left
from datetime import date, datetime, timedelta, timezone


def _elab_series(code: str, grandeur: str) -> list[tuple[str, float]]:
    rows, url, params = [], f"{BASE}/obs_elab", {
        "code_entite": code, "grandeur_hydro_elab": grandeur, "size": 5000}
    for _ in range(12):  # 12 x 5000 = 60 000 jours, soit ~160 ans
        j = hubeau_json(url, params)
        rows += [(d["date_obs_elab"], d["resultat_obs_elab"])
                 for d in j.get("data", []) if d.get("resultat_obs_elab") is not None]
        nxt = j.get("next")
        if not nxt:
            break
        url, params = nxt.replace("http://", "https://", 1), None
    return rows


def _ref_historique(code: str, doy: int, year: int) -> dict:
    saison, annees = [], set()
    for d, v in _elab_series(code, "QmnJ"):
        a = int(d[:4])
        if a >= year:      # on exclut l'année en cours (incomplète, pré-validée)
            continue
        ecart = abs(date.fromisoformat(d[:10]).timetuple().tm_yday - doy)
        if min(ecart, 366 - ecart) <= 15:
            saison.append(v)
            annees.add(a)
    par_an: dict[int, list] = {}
    for d, v in _elab_series(code, "QIXnJ"):
        a = int(d[:4])
        if a < year:
            n, m = par_an.get(a, (0, 0.0))
            par_an[a] = (n + 1, max(m, v))
    return {
        "saison": sorted(saison),
        "n_annees": len(annees),
        "debut": min(annees) if annees else None,
        "maxima": {a: m for a, (n, m) in par_an.items() if n >= 300},  # années à peu près complètes
    }


def _debit_actuel(code: str):
    d = hubeau_json(f"{BASE}/observations_tr",
                    {"code_entite": code, "grandeur_hydro": "Q", "size": 1}).get("data") or []
    return {"q": d[0]["resultat_obs"], "date": d[0]["date_obs"]} if d and d[0].get("resultat_obs") is not None else None


@app.get("/api/historique/{code_station}")
def get_historique(response: Response, code_station: str = Path(pattern=r"^[A-Za-z0-9]{8,10}$")):
    now = datetime.now(timezone.utc)
    doy = now.timetuple().tm_yday
    ref = cached(f"hist:{code_station}:{doy}", ttl=24 * 3600, response=response,
                 loader=lambda: _ref_historique(code_station, doy, now.year))   # lourd : 1 fois/jour/station
    act = cached(f"qnow:{code_station}", ttl=300, response=response,
                 loader=lambda: _debit_actuel(code_station))
    s, mx = ref["saison"], ref["maxima"]
    response.headers["Cache-Control"] = "public, max-age=300"
    if not act or len(s) < 300:
        return {"disponible": False}
    q = act["q"]
    out = {
        "disponible": True, "unite": "l/s", "q": q, "date": act["date"],
        "saison": {
            "n_annees": ref["n_annees"], "debut": ref["debut"],
            "percentile": round(100 * bisect_left(s, q) / len(s)),
            "sous_minimum": q < s[0],
        },
    }
    if len(mx) >= 20:
        top = sorted(mx.items(), key=lambda kv: -kv[1])[:3]
        mediane = sorted(mx.values())[len(mx) // 2]
        out["crues"] = {
            "n_annees": len(mx), "debut": min(mx), "fin": max(mx),
            "top": [{"annee": a, "q": v} for a, v in top],
            "rang": 1 + sum(1 for v in mx.values() if v > q) if q >= mediane else None,
        }
    return out


# --- Hausses récentes du niveau d'eau (toutes les stations, hauteurs temps réel) ----
@app.get("/api/hausses")
def get_hausses(response: Response, heures: int = Query(2, ge=1, le=6)):
    """Variation de hauteur (cm) sur les N dernières heures, pour les stations avec mesures récentes.
    Le seuil d'affichage est appliqué par le front. Brut et non validé : indicatif."""

    def parse(t: str) -> datetime:
        return datetime.fromisoformat(t.replace("Z", "+00:00"))

    def load():
        now = datetime.now(timezone.utc)
        debut = (now - timedelta(minutes=heures * 60 + 20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        series: dict[str, list] = {}
        url, params = f"{BASE}/observations_tr", {
            "grandeur_hydro": "H", "date_debut_obs": debut, "size": 20000,
            "fields": "code_station,date_obs,resultat_obs"}
        for _ in range(15):
            j = hubeau_json(url, params)
            for o in j.get("data", []):
                if o.get("resultat_obs") is not None:
                    series.setdefault(o["code_station"], []).append((parse(o["date_obs"]), o["resultat_obs"]))
            nxt = j.get("next")
            if not nxt:
                break
            url, params = nxt.replace("http://", "https://", 1), None
        out = []
        for code, obs in series.items():
            t1, h1 = max(obs)
            if now - t1 > timedelta(hours=3):
                continue                      # station silencieuse
            cible = t1 - timedelta(hours=heures)
            tr, hr = min(obs, key=lambda p: abs(p[0] - cible))
            if abs(tr - cible) > timedelta(minutes=20):
                continue                      # pas de mesure comparable
            out.append({"code": code, "cm": round((h1 - hr) / 10, 1), "h": round(h1 / 1000, 3),
                        "t": t1.isoformat()})
        return out

    data = cached(f"hausses:{heures}", ttl=300, loader=load, response=response)
    response.headers["Cache-Control"] = "public, max-age=120"
    return data
