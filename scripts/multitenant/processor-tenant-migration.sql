-- Migración multitenant A2 para las tablas del processor (rama exp/multitenant-a2).
--
-- Estrategia: columna tenant_slug (NO schemas: el processor resuelve el agente
-- por UUID desde la URL y filtra por tenant; los UUID son inguessables pero
-- los endpoints de LISTADO (`get_agents_by_account` y CRUD) exponen todo sin
-- filtro => la columna + filtro es obligatoria, no solo defensa).
--
--   * text NOT NULL DEFAULT 'default': rows legacy e INSERTs viejos siguen
--     funcionando; el runtime nuevo siempre escribe el slug real.
--   * Idempotente y tolerante: DO-block por tabla (algunas, como
--     evo_core_agent_integrations, no tienen modelo SQLAlchemy y pueden no
--     existir según el deployment).
--   * Índice simple por tabla para los filtros `WHERE tenant_slug = ?`.
--
-- Resolución en runtime (Fase 2, código): el webhook trae `/{agent_id}` ->
-- `Agent.tenant_slug` => contexto; listados y sesiones filtran por él.
-- Las credenciales Google/MCP se heredan del agente (JOIN por agent_id),
-- no llevan columna propia salvo `evo_core_integration_credentials` si existe.

DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'evo_core_agents',
    'evo_core_agent_folders',
    'evo_core_folder_shares',
    'evo_core_agent_integrations',
    'evo_core_integration_credentials',
    'evo_core_mcp_servers',
    'evo_core_custom_mcp_servers',
    'evo_core_custom_tools',
    'evo_core_api_keys',
    'evo_ai_agent_processor_sessions',
    'evo_ai_agent_processor_session_metadata',
    'evo_ai_agent_processor_execution_metrics'
  ]
  LOOP
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = t) THEN
      EXECUTE format(
        'ALTER TABLE %I ADD COLUMN IF NOT EXISTS tenant_slug text NOT NULL DEFAULT ''default''', t);
      EXECUTE format(
        'CREATE INDEX IF NOT EXISTS %I ON %I (tenant_slug)',
        'idx_' || t || '_tenant', t);
    END IF;
  END LOOP;
END $$;
