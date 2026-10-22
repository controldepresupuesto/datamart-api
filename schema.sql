-- Esquema DataMart -> PostgreSQL
-- Diseno flexible: una tabla JSONB que nunca se rompe aunque SINCO cambie campos.
-- Las consultas filtran por (endpoint, empresa) con indice btree y por campo con GIN.

CREATE TABLE IF NOT EXISTS datamart (
    id        BIGSERIAL PRIMARY KEY,
    endpoint  TEXT        NOT NULL,        -- 'pyg', 'cotizaciones', 'maestros.terceros'...
    empresa   TEXT,                        -- slug; NULL en endpoints globales
    cc        TEXT,                        -- centro de costo / subnivel; NULL si no aplica
    data      JSONB       NOT NULL,        -- el registro completo tal cual viene del DataMart
    sync_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dm_ep_emp ON datamart (endpoint, empresa);
CREATE INDEX IF NOT EXISTS idx_dm_gin    ON datamart USING GIN (data);
-- (22-oct-2026) La paginacion WHERE endpoint=X ORDER BY id LIMIT/OFFSET recorria la llave primaria entera buscando
-- el endpoint: contratos.actas?limit=1, 886 ms. Con este indice, 2,8 ms. Lo crea tambien el motor (destino_datamart.py).
CREATE INDEX IF NOT EXISTS idx_dm_ep_id  ON datamart (endpoint, id);

-- Control de sincronizacion: que se cargo, cuando, cuantas filas, si fallo.
CREATE TABLE IF NOT EXISTS sync_log (
    id        BIGSERIAL PRIMARY KEY,
    endpoint  TEXT,
    empresa   TEXT,
    filas     INTEGER,
    status    TEXT,                        -- 'ok' | 'error' | 'vacio'
    msg       TEXT,
    sync_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
