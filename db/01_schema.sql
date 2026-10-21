-- Schema "pagos" (payments): payment orders (OP, "orden de pago") of a small holding of companies.
-- Conventions: Spanish domain vocabulary (CUIT, CBU, OP are Argentine concepts), snake_case,
-- singular table names, FK = <table>_id, surrogate identity keys and natural keys as UNIQUE.
-- No derived data is stored: the total of an OP is the sum of its lines (view pagos.v_orden_pago).
-- Run order: 01_schema.sql, 02_views.sql, 03_bi_views.sql, 04_treasury.sql.
CREATE SCHEMA IF NOT EXISTS pagos;

-- ---------- Lookup tables ----------
CREATE TABLE pagos.forma_pago (            -- payment method
    id     smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    nombre text NOT NULL UNIQUE
);
CREATE TABLE pagos.estado_op (             -- OP status: pendiente / pagada / anulada
    id     smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    nombre text NOT NULL UNIQUE
);
CREATE TABLE pagos.banco (                 -- bank catalogue
    id     smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    codigo text NOT NULL UNIQUE CHECK (codigo ~ '^[0-9]{3}$'),   -- first 3 digits of the CBU
    nombre text NOT NULL UNIQUE
);

-- ---------- Master data ----------
CREATE TABLE pagos.empresa (               -- the paying company (legal entity)
    id     smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    nombre text NOT NULL UNIQUE
);
-- Customer and supplier are the same entity with roles (as in an ERP): one CUIT, one record.
CREATE TABLE pagos.tercero (               -- third party
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    razon_social text    NOT NULL,
    cuit         text    UNIQUE CHECK (cuit ~ '^[0-9]{11}$'),   -- Argentine tax id
    alias        text[]  NOT NULL DEFAULT '{}',   -- alternative names / tax ids that resolve to this third party
    es_proveedor boolean NOT NULL DEFAULT false,
    es_cliente   boolean NOT NULL DEFAULT false,
    CHECK (es_proveedor OR es_cliente)
);
CREATE INDEX idx_tercero_alias ON pagos.tercero USING gin (alias);

CREATE TABLE pagos.cuenta_bancaria (       -- bank account of a third party
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tercero_id bigint   NOT NULL REFERENCES pagos.tercero(id),
    banco_id   smallint REFERENCES pagos.banco(id),
    cbu        text     CHECK (cbu ~ '^[0-9]{22}$'),   -- Argentine 22-digit bank account code
    nro_cuenta text,                         -- account number or alias, as received
    titular    text,                         -- only if the account belongs to someone else
    CHECK (cbu IS NOT NULL OR nro_cuenta IS NOT NULL OR banco_id IS NOT NULL)
);
CREATE UNIQUE INDEX uq_cuenta_tercero_cbu ON pagos.cuenta_bancaria (tercero_id, cbu) WHERE cbu IS NOT NULL;
CREATE INDEX idx_cuenta_tercero ON pagos.cuenta_bancaria (tercero_id);

-- Common mould for purchase and sales invoices. It only identifies the document today;
-- type, date, net, VAT, total and tax authorisation code are added when invoicing is loaded.
CREATE TABLE pagos.comprobante (           -- invoice / voucher
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tercero_id      bigint NOT NULL REFERENCES pagos.tercero(id),
    sentido         text   NOT NULL DEFAULT 'compra' CHECK (sentido IN ('compra', 'venta')),
    nro_comprobante text   NOT NULL,
    UNIQUE (tercero_id, sentido, nro_comprobante)
);

-- Several businesses (cost centres) can operate inside one legal entity.
CREATE TABLE pagos.centro_costo (
    id          smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    empresa_id  smallint NOT NULL REFERENCES pagos.empresa(id),
    nombre      text     NOT NULL,
    activo      boolean  NOT NULL DEFAULT true,
    UNIQUE (empresa_id, nombre),
    UNIQUE (id, empresa_id)
);

-- ---------- Core ----------
CREATE TABLE pagos.orden_pago (            -- payment order header: one row per OP
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    empresa_id         smallint NOT NULL REFERENCES pagos.empresa(id),
    nro_op             integer,              -- NULL = OP without number (assigned on payment day)
    fecha_op           date     NOT NULL,
    tercero_id         bigint   NOT NULL REFERENCES pagos.tercero(id),   -- who gets paid
    forma_pago_id      smallint NOT NULL REFERENCES pagos.forma_pago(id),
    cuenta_bancaria_id bigint   REFERENCES pagos.cuenta_bancaria(id),
    estado_id          smallint NOT NULL REFERENCES pagos.estado_op(id),
    fecha_pago         date,
    fecha_vencimiento  date,
    condicion_detalle  text,                 -- payment condition as received ("CASH", "QR"...)
    plazo              text,                 -- term as received ("20 DAYS", "ALREADY PAID"...)
    referencia_pago    text,                 -- payment code / link for online payments
    creado_en          timestamptz NOT NULL DEFAULT now(),
    actualizado_en     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (empresa_id, nro_op)
);
CREATE INDEX idx_op_tercero ON pagos.orden_pago (tercero_id);
CREATE INDEX idx_op_estado_empresa ON pagos.orden_pago (estado_id, empresa_id);
CREATE INDEX idx_op_fecha ON pagos.orden_pago (fecha_op);

-- Each line allocates (part of) an invoice; the payable balance is derived from here.
CREATE TABLE pagos.orden_pago_detalle (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    orden_pago_id   bigint   NOT NULL REFERENCES pagos.orden_pago(id) ON DELETE CASCADE,
    renglon         smallint NOT NULL CHECK (renglon >= 1),
    comprobante_id  bigint   REFERENCES pagos.comprobante(id),
    concepto        text,
    fecha           date,
    importe         numeric(14,2) NOT NULL,    -- negative for discounts
    centro_costo_id smallint REFERENCES pagos.centro_costo(id),   -- NULL = not assigned yet
    UNIQUE (orden_pago_id, renglon)
);
CREATE INDEX idx_detalle_comprobante ON pagos.orden_pago_detalle (comprobante_id);
CREATE INDEX idx_detalle_centro ON pagos.orden_pago_detalle (centro_costo_id);

-- ---------- Audit and files ----------
CREATE TABLE pagos.orden_pago_historial (  -- one row per status change
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    orden_pago_id bigint   NOT NULL REFERENCES pagos.orden_pago(id) ON DELETE CASCADE,
    estado_id     smallint NOT NULL REFERENCES pagos.estado_op(id),
    cambiado_en   timestamptz NOT NULL DEFAULT now(),
    nota          text
);
CREATE INDEX idx_historial_op ON pagos.orden_pago_historial (orden_pago_id);

CREATE TABLE pagos.adjunto (               -- files attached to an OP
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    orden_pago_id bigint NOT NULL REFERENCES pagos.orden_pago(id) ON DELETE CASCADE,
    tipo          text   NOT NULL CHECK (tipo IN ('op', 'oc', 'factura', 'comprobante')),   -- 'comprobante' = payment proof
    ruta          text   NOT NULL,
    UNIQUE (orden_pago_id, tipo, ruta)
);

-- ---------- Triggers ----------
CREATE FUNCTION pagos.set_actualizado_en() RETURNS trigger LANGUAGE plpgsql AS
$$ BEGIN NEW.actualizado_en := now(); RETURN NEW; END $$;
CREATE TRIGGER trg_op_actualizado BEFORE UPDATE ON pagos.orden_pago
    FOR EACH ROW EXECUTE FUNCTION pagos.set_actualizado_en();

-- A line can only carry a cost centre of the company that issues its OP.
CREATE FUNCTION pagos.chk_centro_empresa() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.centro_costo_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM pagos.orden_pago o JOIN pagos.centro_costo c ON c.empresa_id = o.empresa_id
        WHERE o.id = NEW.orden_pago_id AND c.id = NEW.centro_costo_id) THEN
        RAISE EXCEPTION 'Cost centre % does not belong to the company of OP %', NEW.centro_costo_id, NEW.orden_pago_id;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_detalle_centro BEFORE INSERT OR UPDATE OF centro_costo_id, orden_pago_id
    ON pagos.orden_pago_detalle FOR EACH ROW EXECUTE FUNCTION pagos.chk_centro_empresa();

-- ---------- Documentation inside the database ----------
COMMENT ON SCHEMA pagos IS 'Payment orders: who pays, to whom, how, how much and in which status.';
COMMENT ON TABLE pagos.tercero IS 'Customer or supplier (same tax id, one record). Roles: es_proveedor, es_cliente.';
COMMENT ON TABLE pagos.orden_pago IS 'OP header, one row per order. The total is the sum of orden_pago_detalle (view v_orden_pago).';
COMMENT ON TABLE pagos.orden_pago_detalle IS 'OP lines: each invoice or concept with its amount and cost centre.';
COMMENT ON TABLE pagos.comprobante IS 'Purchase or sales invoice of a third party. One invoice can be paid by several OPs.';
COMMENT ON TABLE pagos.cuenta_bancaria IS 'Bank account of a third party (CBU, number or alias). A third party can have several.';
COMMENT ON TABLE pagos.banco IS 'Bank catalogue; the code is the first 3 digits of the CBU.';
COMMENT ON TABLE pagos.centro_costo IS 'Business unit inside a legal entity, used to split spend.';
COMMENT ON TABLE pagos.orden_pago_historial IS 'One record per status change of an OP.';
COMMENT ON COLUMN pagos.tercero.alias IS 'Alternative names or tax ids that resolve to this third party (used by the loader and when merging duplicates).';
COMMENT ON COLUMN pagos.orden_pago.nro_op IS 'OP number per company; NULL for an OP without number.';
COMMENT ON COLUMN pagos.orden_pago.fecha_pago IS 'Date the money moves (for a deferred cheque, the cash date).';
COMMENT ON COLUMN pagos.orden_pago.referencia_pago IS 'Payment code, link or reference of online/QR payments (not bank accounts).';
COMMENT ON COLUMN pagos.orden_pago_detalle.importe IS 'Negative for discounts and rebates.';
