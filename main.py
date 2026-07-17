# -*- coding: utf-8 -*-
"""
main.py - API de consultas del DataMart. Se despliega en Railway.
Lee de PostgreSQL y entrega JSON limpio para Claude y Power BI.

Endpoints:
  GET /                         -> salud + catalogo resumido
  GET /catalog                  -> endpoints disponibles con conteo de filas
  GET /sync/status              -> ultima sincronizacion por endpoint
  GET /api/{endpoint}           -> filas de un endpoint
       parametros opcionales:
         empresa=SLUG           filtra por empresa
         cc=CODIGO              filtra por centro de costo
         q=texto                busca el texto en cualquier campo
         limit=N (def 500)      maximo de filas
         offset=N               paginacion
         <campo>=<valor>        filtro exacto por campo del registro (ej: ?Vigente=SI)
  GET /api/{endpoint}/empresas  -> empresas con datos en ese endpoint

Seguridad opcional: si la variable API_KEY esta definida, exige header X-API-Key.
"""
import os
import psycopg2
from psycopg2.pool import SimpleConnectionPool
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

DB = os.environ.get("DATABASE_URL")
API_KEY = os.environ.get("API_KEY")  # opcional
RESERVADOS = {"empresa", "cc", "q", "limit", "offset"}

app = FastAPI(title="DataMart CIMELEC API", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"],
                   allow_headers=["*"])

pool = None


def get_pool():
    global pool
    if pool is None:
        pool = SimpleConnectionPool(1, 8, dsn=DB)
    return pool


def query(sql, params=()):
    p = get_pool()
    conn = p.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        p.putconn(conn)


def check_key(x_api_key):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="API key invalida")


@app.get("/")
def home():
    cat = query("SELECT endpoint, count(*) n FROM datamart GROUP BY endpoint ORDER BY endpoint")
    return {"servicio": "DataMart CIMELEC API", "ok": True,
            "endpoints": len(cat),
            "filas_totales": sum(r["n"] for r in cat)}


@app.get("/catalog")
def catalog(x_api_key: str = Header(None)):
    check_key(x_api_key)
    return query("SELECT endpoint, count(*) filas, count(DISTINCT empresa) empresas, "
                 "max(sync_at) actualizado FROM datamart GROUP BY endpoint ORDER BY endpoint")


@app.get("/sync/status")
def sync_status(x_api_key: str = Header(None)):
    check_key(x_api_key)
    return query("SELECT DISTINCT ON (endpoint, empresa) endpoint, empresa, filas, status, "
                 "sync_at FROM sync_log ORDER BY endpoint, empresa, sync_at DESC")


@app.get("/api/{endpoint}/empresas")
def empresas(endpoint: str, x_api_key: str = Header(None)):
    check_key(x_api_key)
    rows = query("SELECT empresa, count(*) filas FROM datamart WHERE endpoint=%s "
                 "AND empresa IS NOT NULL GROUP BY empresa ORDER BY empresa", (endpoint,))
    return rows


def build_filters(endpoint, qp, extra_reservados=()):
    """WHERE + params comunes a /api, /count y /agg. Filtros: empresa, cc, q (texto libre)
    y <campo>=<valor> exacto sobre el JSON. Devuelve (lista_where, lista_params)."""
    where = ["endpoint = %s"]
    params = [endpoint]
    reservados = RESERVADOS | set(extra_reservados)
    if qp.get("empresa"):
        where.append("empresa = %s"); params.append(qp["empresa"])
    if qp.get("cc"):
        where.append("cc = %s"); params.append(qp["cc"])
    if qp.get("q"):
        where.append("data::text ILIKE %s"); params.append(f"%{qp['q']}%")
    for k, v in qp.items():
        if k not in reservados:
            where.append("data->>%s = %s"); params.extend([k, v])
    return where, params


# Cast numerico robusto: convierte el texto del JSON a numero SOLO si parece numero
# (evita error 500 si el campo trae texto). El %s del campo va DOS veces.
NUMCAST = ("CASE WHEN (data->>%s) ~ '^-?[0-9]+(\\.[0-9]+)?$' "
           "THEN (data->>%s)::numeric ELSE 0 END")


@app.get("/api/{endpoint}")
def api(endpoint: str, request: Request, x_api_key: str = Header(None)):
    check_key(x_api_key)
    qp = dict(request.query_params)
    where, params = build_filters(endpoint, qp)

    try:
        limit = min(int(qp.get("limit", 500)), 5000)
        offset = int(qp.get("offset", 0))
    except ValueError:
        raise HTTPException(400, "limit/offset deben ser enteros")

    sql = (f"SELECT data, empresa, cc FROM datamart WHERE {' AND '.join(where)} "
           f"ORDER BY id LIMIT %s OFFSET %s")
    rows = query(sql, params + [limit, offset])

    # aplanar: devuelve el data con empresa/cc anexados (utiles para Power BI)
    out = []
    for r in rows:
        d = dict(r["data"])
        d.setdefault("_empresa", r["empresa"])
        d.setdefault("_cc", r["cc"])
        out.append(d)
    return JSONResponse(out)


@app.get("/api/{endpoint}/count")
def api_count(endpoint: str, request: Request, x_api_key: str = Header(None)):
    """Total de filas que cumplen el filtro, SIN traer los datos. Permite paginar con
    limit/offset sabiendo cuantas paginas hay (antes el cliente no sabia cuando parar)."""
    check_key(x_api_key)
    where, params = build_filters(endpoint, dict(request.query_params))
    n = query(f"SELECT count(*) n FROM datamart WHERE {' AND '.join(where)}", params)[0]["n"]
    return {"endpoint": endpoint, "filas": n}


@app.get("/api/{endpoint}/agg")
def api_agg(endpoint: str, request: Request, x_api_key: str = Header(None)):
    """Agregacion EN EL SERVIDOR (evita bajar miles de filas para sumar). Parametros:
         groupby=Campo1,Campo2   agrupa por esos campos del JSON
         sum=CampoA,CampoB       suma numerica -> claves sum_CampoA...
         avg=CampoC              promedio numerico -> claves avg_CampoC
       Siempre agrega 'n' (conteo). Acepta los mismos filtros que /api. Ej:
         /api/pyg/agg?empresa=cimelec&groupby=Cuenta&sum=Debito,Credito"""
    check_key(x_api_key)
    qp = dict(request.query_params)
    groupby = [g.strip() for g in qp.get("groupby", "").split(",") if g.strip()]
    sums = [s.strip() for s in qp.get("sum", "").split(",") if s.strip()]
    avgs = [a.strip() for a in qp.get("avg", "").split(",") if a.strip()]
    if not (groupby or sums or avgs):
        raise HTTPException(400, "indica groupby y/o sum y/o avg")

    # alias generados por NOSOTROS (no entran del usuario) -> seguros de inyeccion.
    sel, sel_params = [], []
    for i, g in enumerate(groupby):
        sel.append(f"data->>%s AS g{i}"); sel_params.append(g)
    for i, s in enumerate(sums):
        sel.append(f"COALESCE(SUM({NUMCAST}),0)::float8 AS s{i}"); sel_params.extend([s, s])
    for i, a in enumerate(avgs):
        sel.append(f"AVG(NULLIF({NUMCAST},0))::float8 AS a{i}"); sel_params.extend([a, a])
    sel.append("count(*) AS n")

    where, where_params = build_filters(endpoint, qp, extra_reservados={"groupby", "sum", "avg"})
    gb = f" GROUP BY {', '.join(str(i + 1) for i in range(len(groupby)))}" if groupby else ""
    order = " ORDER BY 1" if groupby else ""
    sql = f"SELECT {', '.join(sel)} FROM datamart WHERE {' AND '.join(where)}{gb}{order}"
    rows = query(sql, sel_params + where_params)

    out = []
    for r in rows:
        o = {}
        for i, g in enumerate(groupby):
            o[g] = r[f"g{i}"]
        for i, s in enumerate(sums):
            o[f"sum_{s}"] = r[f"s{i}"]
        for i, a in enumerate(avgs):
            o[f"avg_{a}"] = r[f"a{i}"]
        o["n"] = r["n"]
        out.append(o)
    return JSONResponse(out)
