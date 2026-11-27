#!/bin/sh
# Виконується Patroni один раз, на першому вузлі, одразу після initdb (аргумент $1: рядок підключення до щойно створеного кластера).
# Створює роль-власника схеми (dw_owner: DDL і міграція), робочу роль сервісу (dw_app) і БД dronewatch. Паролі лише зі змінних
# оточення. Таблиці створює `dronewatch-db-migrate --init` під dw_owner, а права dw_app видає deploy/ha-cluster/postgres-roles.sql.
set -eu
psql "$1" -v ON_ERROR_STOP=1 -v owner_pw="$DW_OWNER_PASSWORD" -v app_pw="$DW_APP_PASSWORD" <<'SQL'
CREATE ROLE dw_owner LOGIN PASSWORD :'owner_pw';
CREATE ROLE dw_app LOGIN PASSWORD :'app_pw';
CREATE DATABASE dronewatch OWNER dw_owner;
REVOKE ALL ON DATABASE dronewatch FROM PUBLIC;
GRANT CONNECT ON DATABASE dronewatch TO dw_app;
\connect dronewatch
ALTER SCHEMA public OWNER TO dw_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO dw_app;
SQL
