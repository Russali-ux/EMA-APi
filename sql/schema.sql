-- ============================================================
-- Módulo EMA/AEMPS ePI — esquema Supabase
-- Proyecto: ggbnfdaxtsngsjssrwrl (ca-central-1)
-- Mismo patrón que ema_epi <-> india/aems/cima: lectura pública,
-- escritura solo con service_role desde GitHub Actions.
-- ============================================================

-- Estado actual conocido por documento (Bundle + idioma)
create table if not exists public.ema_epi_documentos (
    id uuid primary key default gen_random_uuid(),
    org_id text not null,                -- 'ORG-100013412' | 'ORG-100003943'
    agencia text not null,               -- 'EMA' | 'AEMPS'
    pi_list_id text not null,            -- FHIR List id
    bundle_id text not null,             -- FHIR Bundle id
    nombre_medicamento text,
    tipo_documento text,                 -- SPC | Anexo II | Rotulado | Prospecto
    idioma text,                         -- es, en, ...
    contenido_hash text not null,        -- sha256 del texto normalizado
    contenido_secciones jsonb,           -- secciones extraídas (para mostrar diffs)
    dominio text,                        -- 'Human Use' hoy; por si el piloto agrega veterinario
    fecha_primera_deteccion timestamptz not null default now(),
    fecha_ultima_actualizacion timestamptz not null default now(),
    dedupe_key text not null unique      -- bundle_id || '_' || idioma
);

create index if not exists idx_ema_epi_documentos_agencia
    on public.ema_epi_documentos (agencia);
create index if not exists idx_ema_epi_documentos_nombre
    on public.ema_epi_documentos (nombre_medicamento);

-- Log histórico de cambios detectados (alimenta el dashboard)
create table if not exists public.ema_epi_cambios (
    id uuid primary key default gen_random_uuid(),
    bundle_id text not null,
    nombre_medicamento text,
    tipo_documento text,
    agencia text not null,
    idioma text,
    fecha_deteccion timestamptz not null default now(),
    tipo_cambio text not null,           -- 'nuevo_documento' | 'contenido_modificado'
    hash_anterior text,
    hash_nuevo text not null,
    resumen_cambio text
);

create index if not exists idx_ema_epi_cambios_fecha
    on public.ema_epi_cambios (fecha_deteccion desc);
create index if not exists idx_ema_epi_cambios_agencia
    on public.ema_epi_cambios (agencia);

-- RLS: lectura pública, escritura solo service_role
alter table public.ema_epi_documentos enable row level security;
alter table public.ema_epi_cambios enable row level security;

create policy "lectura publica ema_epi_documentos"
    on public.ema_epi_documentos for select
    using (true);

create policy "lectura publica ema_epi_cambios"
    on public.ema_epi_cambios for select
    using (true);

-- Nota: no se crean policies de insert/update/delete para el rol anon;
-- las escrituras del workflow usan la service_role key, que bypassa RLS.

-- Flag de acceso en la tabla compartida 'perfiles' (patrón india/cima/pavs)
alter table public.perfiles
    add column if not exists acceso_ema_epi boolean not null default true;
