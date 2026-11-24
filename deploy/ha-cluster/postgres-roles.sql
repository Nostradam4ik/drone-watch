-- Ролі PostgreSQL для Drone Watch. Виконувати ПІСЛЯ створення таблиць (dronewatch-db-init / dronewatch-db-migrate) під dw_owner.
-- dw_owner: власник схеми, DDL і міграція (лише для ручних операцій, у застосунку його пароля нема).
-- dw_app: єдина роль сервісу. Пароль задає адміністратор окремо: ALTER ROLE dw_app PASSWORD '...' (тут його немає навмисно).
-- Журнал дій незмінний і на рівні прав: dw_app може лише INSERT і SELECT (плюс тригер від UPDATE/DELETE/TRUNCATE).
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dw_app') THEN
        CREATE ROLE dw_app LOGIN;
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO dw_app;
REVOKE CREATE ON SCHEMA public FROM dw_app;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO dw_app;
REVOKE UPDATE ON audit_log FROM dw_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO dw_app;
DO $$ BEGIN
    IF to_regclass('public.incident_replays') IS NOT NULL THEN
        GRANT DELETE ON incident_replays TO dw_app;   -- прибирання записів старших за retention_days
    END IF;
END $$;
