-- ============================================================
-- 002 — Puente EMA/AEMPS ePI <-> CIMA + registro de corridas
-- Proyecto Supabase: ggbnfdaxtsngsjssrwrl (compartido con Alertas-analyzer)
-- Requiere: sql/schema.sql aplicado antes.
--
-- Principio: este repo (EMA-APi) es independiente. Solo ESCRIBE en
-- tablas ema_epi_*. De las tablas de CIMA solo se amplía el check de
-- documentos_segmentados.origen, para que la UI de CIMA pueda crear
-- segmentados a partir de un documento ePI (segmentador.js) con
-- origen = 'ema_epi'. Nada aquí modifica filas existentes de CIMA.
-- Idempotente: se puede volver a correr.
-- ============================================================

-- 1) Columnas nuevas en ema_epi_documentos (insumos de vinculación)
alter table public.ema_epi_documentos
    add column if not exists identificadores jsonb  not null default '[]'::jsonb,  -- [{system, value}] de todo el Bundle
    add column if not exists numeros_eu      text[] not null default '{}';         -- EU/1/16/1102[/001]

-- 2) Registro de cada corrida del workflow (incluida la fuente caída)
create table if not exists public.ema_epi_corridas (
    id                 uuid primary key default gen_random_uuid(),
    inicio             timestamptz,
    fin                timestamptz not null default now(),
    estado             text not null check (estado in ('ok', 'parcial', 'fuente_caida', 'desconocido')),
    agencias           text[] not null default '{}',
    http_status        int,
    detalle_error      text,
    pi_lists           int,
    bundles_total      int,
    bundles_con_error  int,
    documentos         int,
    nuevos             int,
    modificados        int,
    sin_cambios        int,
    github_run_url     text
);

create index if not exists idx_ema_epi_corridas_fin
    on public.ema_epi_corridas (fin desc);

alter table public.ema_epi_corridas enable row level security;
drop policy if exists "lectura publica ema_epi_corridas" on public.ema_epi_corridas;
create policy "lectura publica ema_epi_corridas"
    on public.ema_epi_corridas for select
    using (true);

-- 3) Tabla puente ePI (bundle + idioma) <-> CIMA (nregistro)
--    La llena scripts/ema_epi_vincular_cima.py con estado 'propuesto';
--    un usuario con acceso a CIMA la confirma o rechaza.
create table if not exists public.ema_epi_cima_vinculo (
    id              uuid primary key default gen_random_uuid(),
    bundle_id       text not null,
    idioma          text not null default 'und',
    nregistro       text not null,                 -- nregistro CIMA (p. ej. 1161102001)
    nombre_cima     text,
    laboratorio_cima text,
    metodo_match    text not null check (metodo_match in (
                        'numero_eu',         -- EU/1/yy/nnnn/ppp -> 1yynnnnppp, verificado en CIMA
                        'identificador',     -- identifier del Bundle == nregistro, verificado en CIMA
                        'nombre',            -- solo coincidencia de nombre (baja confianza)
                        'manual')),
    confianza       text not null check (confianza in ('alta', 'media', 'baja')),
    evidencia       text,                          -- qué número/identificador produjo el match
    estado          text not null default 'propuesto'
                        check (estado in ('propuesto', 'verificado', 'rechazado')),
    verificado_por  uuid references auth.users (id) on delete set null,
    verificado_en   timestamptz,
    creado_en       timestamptz not null default now(),
    unique (bundle_id, idioma, nregistro)
);

create index if not exists idx_ema_epi_cima_vinculo_nregistro
    on public.ema_epi_cima_vinculo (nregistro);

alter table public.ema_epi_cima_vinculo enable row level security;

drop policy if exists "lectura publica ema_epi_cima_vinculo" on public.ema_epi_cima_vinculo;
create policy "lectura publica ema_epi_cima_vinculo"
    on public.ema_epi_cima_vinculo for select
    using (true);

-- Verificar / rechazar desde la UI: solo usuarios con acceso a CIMA.
-- (Las inserciones del workflow usan service_role, que bypassa RLS.)
drop policy if exists "verificar ema_epi_cima_vinculo" on public.ema_epi_cima_vinculo;
create policy "verificar ema_epi_cima_vinculo"
    on public.ema_epi_cima_vinculo for update
    to authenticated
    using ((select public.tiene_acceso_cima()))
    with check ((select public.tiene_acceso_cima()));

-- 4) CIMA: permitir origen 'ema_epi' en documentos_segmentados
--    (solo amplía el check; las filas existentes 'cima'/'subida' siguen válidas)
do $$
begin
    if to_regclass('public.documentos_segmentados') is not null then
        alter table public.documentos_segmentados
            drop constraint if exists documentos_segmentados_origen_check;
        alter table public.documentos_segmentados
            add constraint documentos_segmentados_origen_check
            check (origen in ('cima', 'subida', 'ema_epi'));
    end if;
end $$;

-- 5) Vista para el dashboard / CIMA: cambios ePI con su nregistro CIMA
--    y el último segmentado CIMA disponible para ese nregistro.
create or replace view public.v_ema_epi_cambios_cima
with (security_invoker = true) as
select
    c.id               as cambio_id,
    c.fecha_deteccion,
    c.agencia,
    c.nombre_medicamento,
    c.tipo_documento,
    c.idioma,
    c.tipo_cambio,
    c.resumen_cambio,
    v.nregistro,
    v.metodo_match,
    v.confianza,
    v.estado           as estado_vinculo,
    seg.id             as ultimo_segmentado_cima_id,
    seg.fecha_version_cima as ultima_version_cima
from public.ema_epi_cambios c
left join public.ema_epi_cima_vinculo v
       on v.bundle_id = c.bundle_id
      and v.idioma = coalesce(c.idioma, 'und')
      and v.estado <> 'rechazado'
left join lateral (
    select s.id, s.fecha_version_cima
    from public.documentos_segmentados s
    where s.nregistro = v.nregistro
      and s.origen = 'cima'
      and s.eliminado_en is null
    order by s.fecha_version_cima desc nulls last, s.creado_en desc
    limit 1
) seg on true;
-- Nota: la vista es security_invoker, así que la columna del segmentado CIMA
-- solo se llena para usuarios con acceso a CIMA (RLS de documentos_segmentados).
