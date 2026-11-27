# Кластер Drone Watch: два вузли даних і свідок

Автоматичне перемикання без split-brain на **двох VPS (A, B) плюс третя крихітна машина-свідок (W)**, усе своє: PostgreSQL під
Patroni, etcd, Redis із Sentinel, MQTT-брокер на кожному вузлі й копія сервісу на кожному вузлі. Дизайн і рішення:
`docs/superpowers/specs/2026-11-24-ha-v2-postgres-design.md`. Кластер з двох машин безпечно перемикатися автоматично не може:
Patroni (etcd) і Sentinel вирішують більшістю голосів, тож їх три. Свідок не тримає даних, лише два службових процеси.

```
 VPS-A (node-a)             VPS-B (node-b)             W (witness)
 service + mosquitto        service + mosquitto
 Patroni + PostgreSQL <---> Patroni + PostgreSQL        (даних нема)
 Redis  <----------------> Redis
 etcd   <----------------> etcd   <----------------->  etcd
 Sentinel <--------------> Sentinel <--------------->  Sentinel
```

## Що де слухає
- **Адреса WireGuard (`NODE_IP`)**: etcd 2379/2380, PostgreSQL 5432, Patroni 8008, Redis 6379, Sentinel 26379. Назовні не видно.
- **Публічна адреса (`PUBLIC_BIND`)**: MQTT-брокер 8883 (датчики) і HTTPS сервісу 8000 (дашборд). Більше нічого.
- Порти публікуються лише за обов'язковою змінною (`${NODE_IP:?...}`): без неї compose відмовляє, а не відкриває порт усім.
- TLS скрізь: etcd із взаємною автентифікацією сертифікатами, PostgreSQL лише `hostssl` із `scram-sha-256`, Redis/Sentinel лише TLS
  і пароль, Patroni REST вимагає клієнтський сертифікат. Нічого не працює без TLS.

## Розгортання (одноразово)
1. **WireGuard** між трьома машинами (mesh): кожна має адресу з приватної підмережі, наприклад A `10.10.0.1`, B `10.10.0.2`,
   W `10.10.0.3`; у `AllowedIPs` кожного піра лише адреси інших двох. Фаєрвол: порт WireGuard (UDP) і публічні 8883, 8000 назовні,
   усе інше лише з інтерфейсу `wg0`. Приклад `[Interface]` вузла A: `Address = 10.10.0.1/24`, `ListenPort = 51820`, `PrivateKey = ...`;
   `[Peer]` для B і W: `PublicKey`, `Endpoint = <публічна адреса>:51820`, `AllowedIPs = 10.10.0.2/32` (відповідно `.3/32`),
   `PersistentKeepalive = 25`.
2. **Сертифікати** (на довіреній машині): `A_IP=10.10.0.1 B_IP=10.10.0.2 W_IP=10.10.0.3 A_PUBLIC_SAN=DNS:a.example.org
   B_PUBLIC_SAN=DNS:b.example.org sh gen-certs.sh`. Це `certs/ca/ca.key` (тримати **офлайн**), `certs/node-a`, `certs/node-b`,
   `certs/witness`. На кожну машину скопіювати лише її каталог як `./certs` (поруч із compose-файлом); ключі `0600`.
3. **Змінні**: `cp env.example .env`, `chmod 600 .env`, заповнити. Паролі згенерувати (`openssl rand -hex 24`), однакові на всіх машинах;
   `NODE_NAME`/`NODE_IP`/`MQTT_USER` різні (див. коментарі в `env.example`); на node-b `REDIS_REPLICAOF=<A_IP>`.
4. **Брокер** (на кожному вузлі): `cp mosquitto/acl.example mosquitto/acl`; у `mosquitto/passwd` логіни датчиків і сервісу
   (`dronewatch-keygen --mosquitto ІМ'Я ПАРОЛЬ`). Власний логін копії сервісу: на node-a `dronewatch-service`, на node-b
   `dronewatch-service-2` (брокер ставить client_id = логін, тож спільний логін вибивав би одну копію іншою).
5. **Конфіг сервісу**: `config.cluster.yaml` це `deploy/config.yaml` плюс блоки `storage`, `ha`, `redis`. Замініть адреси `10.10.0.x`,
   демо-користувача дашборда й ключі датчиків на свої (`dronewatch-keygen`).

## Порядок запуску
```sh
# 1. свідок (W):
docker compose -f docker-compose.witness.yml --env-file .env up -d
# 2. вузол A, далі вузол B (etcd кластер збереться, коли підніметься більшість):
docker compose -f docker-compose.node.yml --env-file .env up -d --build etcd postgres redis sentinel
# 3. перевірка кластера: один лідер PostgreSQL, Redis master і три Sentinel
docker compose -f docker-compose.node.yml exec postgres /opt/patroni/bin/patronictl -c /tmp/patroni.yml list
docker compose -f docker-compose.node.yml exec sentinel redis-cli --tls --cacert /certs/ca.crt -p 26379 -a "$REDIS_PASSWORD" sentinel master dw
```
Patroni на першому вузлі створює ролі `dw_owner` (власник схеми, DDL) і `dw_app` (робоча роль сервісу) та БД `dronewatch`.

## Схема й дані
Під **`dw_owner`** (не `dw_app`: у нього немає права DDL) з будь-якої машини, що бачить PostgreSQL:
```sh
export DRONEWATCH_PG_PASSWORD="$DW_OWNER_PASSWORD"
DSN='postgresql://dw_owner@10.10.0.1:5432,10.10.0.2:5432/dronewatch?sslmode=verify-full&target_session_attrs=read-write'
dronewatch-db-migrate --dsn "$DSN" --init                       # порожня схема (нове розгортання)
dronewatch-db-migrate --dsn "$DSN" --from incidents.db          # або перенести існуючий журнал із SQLite (замість --init)
psql "$DSN" -f postgres-roles.sql                                # права dw_app: дописування в журнал дій, жодного UPDATE/DELETE
# (задайте пароль dw_app, якщо ролі створено не Patroni: ALTER ROLE dw_app PASSWORD '...';)
```
Потім на обох вузлах: `docker compose -f docker-compose.node.yml --env-file .env up -d --build service`. Одна копія стає лідером
(оренда й epoch у PostgreSQL), друга резервом. Перевірка: `/readyz` (лідер 200, резерв 503), `/api/health` (`ha`, `storage.ok`),
`dronewatch-audit --verify --dsn "$DSN"`. Балансувальник чи DNS має дивитися на `/readyz`.

## Датчики
Датчик публікує кожне вимірювання **в обидва брокери** (A і B). Тоді смерть вузла разом із його брокером не лишає систему без
вхідних подій, а резерв бачить потік напряму. Дублі безпечні (рушій їх переживає без зайвих треків і тривог).

## Що варто знати
- **Реплікація не резервна копія.** Видалений запис зникає й на репліці. Налаштуйте окремі резервні копії PostgreSQL
  (pgBackRest чи `pg_dump` за розкладом) і зберігайте їх поза кластером. Тут цього немає.
- **Синхронна реплікація без `strict`** (`synchronous_mode: true`, `synchronous_mode_strict: false`): при смерті репліки master пише
  далі, але якщо потім помре й він, кілька останніх записів можуть зникнути. `strict: true` відмовляв би у записі без репліки
  (тривоги лишалися б на екрані, у журнал не йшли). Змінюється в `patroni/patroni.yml.tmpl`.
- **Перемикання PostgreSQL ≈ 20 с** (`ttl: 20`); втрата синхронної репліки зупиняє запис ще на ≈ 10-25 с, поки Patroni її не прибере; Redis ≈ 5-15 с. Redis недоступний: вхід у дашборд дає 503 (fail-closed), запис
  тривог не зачеплений. PostgreSQL недоступний: сканування триває, тривоги на екрані й дописуються після повернення БД.
- **Втрата свідка**: кворум 2 з 3 лишається, усе працює; втрата ще одного вузла залишає систему без перемикання (як і мало бути).
- **`ca.key` офлайн**: він потрібен лише для сертифіката новому вузлу. Перевипуск сертифікатів = нові файли в `./certs` і перезапуск.
- Паролі лише з `A-Za-z0-9_.-` (вони йдуть у конфіги без лапок; entrypoint відмовляє на інших символах).

## Репетиція на одній машині (sim)
`sim/` піднімає всі три «машини» на одному хості в мережі `10.77.0.0/24` (кожна це контейнер-тримач мережевого простору, а її процеси
живуть у тому ж просторі, тож бачать одну адресу, як на справжньому VPS). Конфіги ті самі, що у справжньому розгортанні. Секрети
(паролі БД і Redis, ключі датчиків, користувач дашборда, логіни брокера) генеруються випадково при першому запуску в `sim/.env`
і `sim/*`; публічних демо-секретів тут нема: кластерний режим відмовив би на них.

```sh
sh deploy/ha-cluster/sim/sim.sh up       # сертифікати, секрети, кластер, схема, сервіси (перший запуск збирає образи)
sh deploy/ha-cluster/sim/sim.sh status   # лідер PostgreSQL, master Redis, ролі копій (https://127.0.0.1:8101 і :8102)
sh scripts/ha_failover_drill.sh kill-leader | kill-pg-master | kill-redis-master | isolate-node-a | kill-witness
sh deploy/ha-cluster/sim/sim.sh down
```
Сценарій міряє секунди до відновлення ПОВНОГО шляху запису (успішний вхід адміна пише рядок журналу дій у PostgreSQL), стежить, щоб
обидві копії ніколи не були лідерами одночасно, і наприкінці перевіряє ланцюжок журналу дій у PostgreSQL та відсутність дублів тривог.
Ліміт відновлення: `lease_ttl_s * 4 + 30` = 54 с. Код виходу 0 лише при успіху.

Виміряно на стенді (Docker Desktop, 2026-11-24; на справжніх VPS буде довше на час мережевих затримок), секунд до відновлення запису:
`kill-leader` 7, `kill-pg-master` 18, `kill-redis-master` 8, `isolate-node-a` 37 (лідер сервісу ізольований разом із вузлом), `kill-witness`
без перерви (40 с роботи без свідка, лідер той самий). Усі п'ять завершилися кодом 0: split-brain не помічено, ланцюжок журналу
дій у PostgreSQL цілий, дублів тривог нема. Де «37»: до ~25 с з них це не збій сервісу, а синхронна реплікація: поки Patroni не
прибере втрачену синхронну репліку зі `synchronous_standby_names`, master не підтверджує записи.
