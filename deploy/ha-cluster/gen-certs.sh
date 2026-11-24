#!/bin/sh
# Сертифікати для кластера з двох вузлів і свідка (запускати ОДИН раз на довіреній машині оператора, не на VPS):
#
#   A_IP=10.10.0.1 B_IP=10.10.0.2 W_IP=10.10.0.3 sh gen-certs.sh [ВИХІДНИЙ_КАТАЛОГ]       (типово ./certs)
#
# Результат:
#   certs/ca/ca.crt, ca.key     CA кластера. ca.key ТРИМАТИ ОФЛАЙН (потрібен лише щоб випустити сертифікат новому вузлу)
#   certs/node-a/               ca.crt, etcd.*, pg.*, redis.* (його ж використовує Sentinel), broker.*, http.*
#   certs/node-b/               те саме для вузла B
#   certs/witness/              ca.crt, etcd.*, redis.* (свідок тримає лише etcd і Sentinel)
# На кожну машину копіюється лише її каталог (як ./certs на ній); ключі 0600. Усі сертифікати мають IP-SAN адрес WireGuard,
# бо клієнти (Patroni, redis-py, libpq verify-full) підключаються за IP. Публічні імена для датчиків і дашборда додаються змінними
# A_PUBLIC_SAN / B_PUBLIC_SAN (напр. "DNS:dw-a.example.org,IP:203.0.113.10") до сертифікатів broker і http.
set -eu

OUT="${1:-./certs}"
DAYS="${CERT_DAYS:-825}"
: "${A_IP:?задайте A_IP}" "${B_IP:?задайте B_IP}" "${W_IP:?задайте W_IP}"
A_PUBLIC_SAN="${A_PUBLIC_SAN:-}"
B_PUBLIC_SAN="${B_PUBLIC_SAN:-}"

umask 077
mkdir -p "$OUT/ca" "$OUT/node-a" "$OUT/node-b" "$OUT/witness"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

if [ ! -s "$OUT/ca/ca.key" ]; then
    openssl ecparam -name prime256v1 -genkey -noout -out "$OUT/ca/ca.key"
    openssl req -x509 -new -key "$OUT/ca/ca.key" -sha256 -days "$((DAYS * 2))" -subj "/CN=Drone Watch cluster CA" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -addext "subjectKeyIdentifier=hash" -out "$OUT/ca/ca.crt"
fi
for d in node-a node-b witness; do install -m 0644 "$OUT/ca/ca.crt" "$OUT/$d/ca.crt"; done

# issue ВУЗОЛ РОЛЬ EKU SANS: ключ і сертифікат у $OUT/ВУЗОЛ/РОЛЬ.{key,crt}. SKI/AKI прописано явно (суворий режим X.509 у Python 3.13+).
issue() {
    node="$1"; role="$2"; eku="$3"; sans="$4"
    openssl ecparam -name prime256v1 -genkey -noout -out "$OUT/$node/$role.key"
    openssl req -new -key "$OUT/$node/$role.key" -subj "/CN=$node-$role" -out "$work/$node-$role.csr"
    printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=%s\nsubjectAltName=%s\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n' \
        "$eku" "$sans" > "$work/$node-$role.cnf"
    openssl x509 -req -in "$work/$node-$role.csr" -CA "$OUT/ca/ca.crt" -CAkey "$OUT/ca/ca.key" -CAcreateserial \
        -CAserial "$work/ca.srl" -days "$DAYS" -sha256 -extfile "$work/$node-$role.cnf" -out "$OUT/$node/$role.crt" >/dev/null 2>&1
    openssl verify -CAfile "$OUT/ca/ca.crt" "$OUT/$node/$role.crt" >/dev/null   # не віддаємо те, що не перевіряється
    chmod 0600 "$OUT/$node/$role.key"
    chmod 0644 "$OUT/$node/$role.crt"
}

BOTH="serverAuth,clientAuth"   # etcd, Patroni, Redis і Sentinel є і сервером, і клієнтом (peer-звʼязок, реплікація)

issue node-a "etcd" "$BOTH" "IP:$A_IP,IP:127.0.0.1,DNS:localhost"
issue node-a "pg" "$BOTH" "IP:$A_IP,IP:127.0.0.1,DNS:localhost"
issue node-a "redis" "$BOTH" "IP:$A_IP,IP:127.0.0.1,DNS:localhost"
issue node-a "broker" "serverAuth" "IP:127.0.0.1,DNS:localhost,DNS:mosquitto${A_PUBLIC_SAN:+,$A_PUBLIC_SAN}"
issue node-a "http" "serverAuth" "IP:127.0.0.1,DNS:localhost,DNS:service${A_PUBLIC_SAN:+,$A_PUBLIC_SAN}"

issue node-b "etcd" "$BOTH" "IP:$B_IP,IP:127.0.0.1,DNS:localhost"
issue node-b "pg" "$BOTH" "IP:$B_IP,IP:127.0.0.1,DNS:localhost"
issue node-b "redis" "$BOTH" "IP:$B_IP,IP:127.0.0.1,DNS:localhost"
issue node-b "broker" "serverAuth" "IP:127.0.0.1,DNS:localhost,DNS:mosquitto${B_PUBLIC_SAN:+,$B_PUBLIC_SAN}"
issue node-b "http" "serverAuth" "IP:127.0.0.1,DNS:localhost,DNS:service${B_PUBLIC_SAN:+,$B_PUBLIC_SAN}"

issue witness "etcd" "$BOTH" "IP:$W_IP,IP:127.0.0.1,DNS:localhost"
issue witness "redis" "$BOTH" "IP:$W_IP,IP:127.0.0.1,DNS:localhost"

echo "certs: готово в $OUT. ca/ca.key перенесіть на офлайн-носій; на кожну машину скопіюйте лише її каталог (node-a, node-b, witness)."
