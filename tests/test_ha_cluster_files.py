"""deploy/ha-cluster: статичні перевірки файлів розгортання (без docker): безпека за замовчуванням, узгодженість із кодом."""
import re
from pathlib import Path

import pytest
import yaml

from dronewatch.service import config

ROOT = Path(__file__).resolve().parents[1]
CL = ROOT / "deploy" / "ha-cluster"
NODE = CL / "docker-compose.node.yml"
WITNESS = CL / "docker-compose.witness.yml"


def text(rel: str) -> str:
    return (CL / rel).read_text(encoding="utf-8")


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("compose", [NODE, WITNESS])
def test_every_published_port_is_bound_to_an_interface(compose):
    """Жодного порту на всіх інтерфейсах: лише адреса WireGuard (${NODE_IP}), публічна адреса датчиків (${PUBLIC_BIND}) чи loopback."""
    # змінна ОБОВ'ЯЗКОВА (`:?`): незадана змінна дала б порт на всіх інтерфейсах, а так compose відмовляє
    ok = re.compile(r"^(\$\{(NODE_IP|PUBLIC_BIND):\?[^}]+\}|127\.0\.0\.1):\d+:\d+$")
    for name, svc in load(compose)["services"].items():
        for port in svc.get("ports", []):
            assert ok.match(str(port)), f"{compose.name}: {name}: порт {port} не прив'язаний до інтерфейсу"
        assert svc.get("network_mode") != "host", f"{name}: мережа host обходить прив'язку портів"


def test_node_compose_has_all_roles():
    names = set(load(NODE)["services"])
    assert {"etcd", "postgres", "redis", "sentinel", "mosquitto", "service"} <= names
    assert set(load(WITNESS)["services"]) == {"etcd", "sentinel"}   # свідок без даних і без PostgreSQL


def test_etcd_uses_mutual_tls_everywhere():
    for compose in (NODE, WITNESS):
        env = load(compose)["services"]["etcd"]["environment"]
        assert env["ETCD_CLIENT_CERT_AUTH"] == "true" and env["ETCD_PEER_CLIENT_CERT_AUTH"] == "true"
        assert env["ETCD_LISTEN_CLIENT_URLS"].startswith("https://") and env["ETCD_LISTEN_PEER_URLS"].startswith("https://")
        assert env["ETCD_ADVERTISE_CLIENT_URLS"].startswith("https://") and env["ETCD_INITIAL_ADVERTISE_PEER_URLS"].startswith("https://")
        assert env["ETCD_INITIAL_CLUSTER"].count("https://") == 3 and "http://" not in env["ETCD_INITIAL_CLUSTER"]


def test_patroni_template_is_safe_and_synchronous_without_strict():
    t = text("patroni/patroni.yml.tmpl")
    assert re.search(r"^\s*synchronous_mode:\s*true\s*$", t, re.M)
    assert re.search(r"^\s*synchronous_mode_strict:\s*false\s*$", t, re.M)   # рішення користувача 2026-11-24
    assert re.search(r"ssl:\s*'?on'?", t) and "scram-sha-256" in t and "password_encryption" in t
    assert not re.search(r"^\s*-\s*host\s", t, re.M), "pg_hba: відкритий (не TLS) host-рядок"
    assert not re.search(r"\btrust\b", t), "pg_hba: trust заборонено"
    assert "protocol: https" in t and "verify_client: required" in t
    # ttl >= loop_wait + 2 * retry_timeout (вимога Patroni)
    ttl, loop, retry = (int(re.search(rf"^\s*{k}:\s*(\d+)", t, re.M).group(1)) for k in ("ttl", "loop_wait", "retry_timeout"))
    assert ttl >= loop + 2 * retry


def test_redis_and_sentinel_are_tls_only_with_password():
    for rel in ("redis/entrypoint.sh", "sentinel/entrypoint.sh"):
        t = text(rel)
        assert re.search(r"^port 0$", t, re.M) and "tls-port" in t and "requirepass" in t and "tls-replication yes" in t, rel
    s = text("sentinel/entrypoint.sh")
    assert re.search(r"sentinel monitor \S+ \S+ \d+ 2\b", s)   # кворум 2 з 3
    assert "sentinel auth-pass" in s and "sentinel sentinel-pass" in s


def test_service_gets_secrets_from_env_not_from_files():
    env = load(NODE)["services"]["service"]["environment"]
    assert env["DRONEWATCH_PG_PASSWORD"] == "${DW_APP_PASSWORD}" and env["DRONEWATCH_REDIS_PASSWORD"] == "${REDIS_PASSWORD}"
    assert env["HA_INSTANCE_ID"] == "${NODE_NAME}"


def test_env_example_has_placeholders_only():
    for line in text("env.example").splitlines():
        if re.match(r"^[A-Z_]*(PASSWORD|SECRET|KEY)[A-Z_]*=", line):
            assert line.split("=", 1)[1].strip() in ("CHANGE_ME", ""), f"у env.example справжній секрет: {line.split('=')[0]}"


def test_real_env_files_are_gitignored():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "deploy/ha-cluster/.env" in ignore and "deploy/ha-cluster/certs/" in ignore


CLUSTER_CONFIG = CL / "config.cluster.yaml"
DEMO_STRINGS = (
    "eXh3SgyIi0PXvYt3gCZTBg==",                                           # хеш демо-користувача дашборда
    "40bc1a2ade19ee4629588e5d5b10674c50e0e178a54c6fea6d7f8b99d3314fff",   # ключі датчиків демо-стенда
    "168f535e4bdfb5f79534a7907ab1a5ac085b4b0001be3b644adef1c91142d679",
    "9594f53087f0a4f60f9b1ceca2f5dac54d6493cca3578caa854ef5ee276d18d8",
    "8DgvEHMEllwyXmx4bD_eWc0NOTz690ll",                                   # демо-пароль логіна брокера dronewatch-service
    "SPvOEozcD0Sshfj1zbAQ3ARsM1hKug2W",                                   # ... і dronewatch-service-2
)


def test_cluster_config_ships_placeholders_not_demo_secrets():
    raw = CLUSTER_CONFIG.read_text(encoding="utf-8")
    assert all(demo not in raw for demo in DEMO_STRINGS)
    assert "REPLACE_ME_dashboard_hash" in raw and all(f"REPLACE_ME_{sid}" in raw for sid in ("radar1", "rf1", "ac1"))
    with pytest.raises(SystemExit):   # без заміни плейсхолдерів сервіс не стартує (config.load виходить із помилкою)
        config.load(CLUSTER_CONFIG)


def test_no_public_demo_secrets_anywhere_in_cluster_files():
    for path in CL.rglob("*"):
        if path.is_file() and path.suffix in (".yml", ".yaml", ".sh", ".tmpl", ".md", ".example", ".sql", ".conf")                 and "certs" not in path.parts:
            text_ = path.read_text(encoding="utf-8")
            for demo in DEMO_STRINGS:
                assert demo not in text_, f"{path.relative_to(ROOT)} містить публічний демо-секрет"


def fill_placeholders(raw: str) -> str:
    from dronewatch.service.auth import hash_key
    from dronewatch.service.dashboard_auth import hash_password
    raw = raw.replace("REPLACE_ME_dashboard_hash", hash_password("a-fresh-long-password"))
    for sid in ("radar1", "rf1", "ac1"):
        raw = raw.replace(f"REPLACE_ME_{sid}", hash_key(f"fresh-key-for-{sid}-0123456789"))
    return raw


def test_cluster_config_with_fresh_secrets_loads_and_matches_demo_base(tmp_path):
    """З підставленими власними секретами конфіг чинний, а решта (датчики, зони, карта) не розходиться з deploy/config.yaml."""
    filled = tmp_path / "config.yaml"
    filled.write_text(fill_placeholders(CLUSTER_CONFIG.read_text(encoding="utf-8")), encoding="utf-8")
    cl = config.load(filled)
    assert cl.storage.backend == "postgres" and cl.ha.enabled and cl.ha.lease_store == "postgres"
    assert cl.redis is not None and len(cl.redis.sentinels) == 3 and cl.redis.ca_cert == "/certs/ca.crt"
    assert cl.dashboard.session_store == "redis"   # сесії спільні для обох копій
    base = config.load(ROOT / "deploy" / "config.yaml")

    def strip(c):   # секрети й кластерні блоки відрізняються свідомо
        sensors = [s.model_copy(update={"key_sha256": None}) for s in c.sensors]
        return c.model_copy(update={"storage": base.storage, "ha": base.ha, "redis": base.redis, "sensors": sensors,
                                    "dashboard": base.dashboard.model_copy(update={"users": [], "session_store": "memory"})})

    assert strip(cl) == strip(base)


def test_service_image_has_postgres_driver():
    assert "'postgres'" in (ROOT / "docker" / "service.Dockerfile").read_text(encoding="utf-8")


def test_gen_certs_covers_every_role_with_ip_sans():
    t = text("gen-certs.sh")
    for role in ("etcd", "pg", "redis", "broker", "http"):
        assert f'"{role}"' in t or f" {role} " in t, role
    assert "clientAuth" in t and "serverAuth" in t and "IP:" in t
    assert "ca.key" in t   # ключ CA лишається (для нових вузлів), але в окремому каталозі, що не комітиться
