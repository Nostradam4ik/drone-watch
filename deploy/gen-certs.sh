#!/bin/sh
# Генерує ДЕМО-сертифікати для брокера Mosquitto (запускає сервіс certs у docker-compose.yml). Приватні ключі в репозиторій
# не потрапляють: вони створюються тут, при першому запуску стенда, і живуть лише в Docker-томах.
#
#   /out/ca      ca.crt                         тут лише публічний сертифікат CA: його читають брокер, сервіс і емулятор
#   /out/server  server.crt, server.key         лише для брокера (ключ 0600, власник mosquitto)
#
# Ключ CA ніде не зберігається: CA одноразовий, він підписує рівно один серверний сертифікат і зникає. Тож ніхто (зокрема й
# цей репозиторій) не може випустити ще один сертифікат, якому довіряють сервіс і емулятор. Ціна: перевипуск = новий CA,
# а сервіс і емулятор мусять перечитати ca.crt, тому сервіс certs і запускається першим на кожен `docker compose up`.
#
# Перевипуск автоматичний, якщо файлів немає, до кінця строку лишилось менше RENEW_BEFORE_DAYS днів, сертифікат не
# підписано цим CA або змінилися CERT_SANS. Повторний запуск за чинних сертифікатів нічого не змінює.
#
# ЦЕ ДЛЯ ДЕМОНСТРАЦІЇ. Для бойового розгортання візьміть сертифікат від свого CA (ім'я хоста брокера має бути в SAN)
# і покладіть у ці самі томи або змініть шляхи в mosquitto.conf: див. README, розділ про TLS.
set -eu

CA_DIR="${CA_DIR:-/out/ca}"
SRV_DIR="${SRV_DIR:-/out/server}"
DAYS="${CERT_DAYS:-365}"
RENEW_BEFORE_DAYS="${RENEW_BEFORE_DAYS:-30}"
BROKER_UID="${BROKER_UID:-1883}"                                   # користувач mosquitto в офіційному образі
SANS="${CERT_SANS:-DNS:mosquitto,DNS:localhost,IP:127.0.0.1}"      # імена, за якими клієнти звертаються до брокера

still_valid() {
    [ -s "$CA_DIR/ca.crt" ] && [ -s "$SRV_DIR/server.crt" ] && [ -s "$SRV_DIR/server.key" ] && [ -s "$SRV_DIR/sans.txt" ] || return 1
    [ "$(cat "$SRV_DIR/sans.txt")" = "$SANS" ] || return 1
    margin=$((RENEW_BEFORE_DAYS * 86400))
    openssl x509 -noout -checkend "$margin" -in "$CA_DIR/ca.crt" >/dev/null 2>&1 || return 1
    openssl x509 -noout -checkend "$margin" -in "$SRV_DIR/server.crt" >/dev/null 2>&1 || return 1
    openssl verify -CAfile "$CA_DIR/ca.crt" "$SRV_DIR/server.crt" >/dev/null 2>&1 || return 1
}

if still_valid; then
    echo "certs: сертифікати чинні (до кінця строку більше $RENEW_BEFORE_DAYS днів), нічого не змінюю"
    exit 0
fi

echo "certs: генерую новий CA і сертифікат брокера ($SANS, $DAYS днів)"
umask 077
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

openssl ecparam -name prime256v1 -genkey -noout -out "$work/ca.key"
openssl req -x509 -new -key "$work/ca.key" -sha256 -days "$DAYS" -subj "/CN=Drone Watch DEMO CA" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash" -out "$work/ca.crt"

openssl ecparam -name prime256v1 -genkey -noout -out "$work/server.key"
openssl req -new -key "$work/server.key" -subj "/CN=mosquitto" -out "$work/server.csr"
# SKI/AKI прописано явно, а не з розрахунку на типову поведінку OpenSSL: суворий режим X.509 (Python 3.13+ вмикає його
# за замовчуванням) відхиляє сертифікат без authorityKeyIdentifier.
printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=%s\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n' "$SANS" > "$work/ext.cnf"
openssl x509 -req -in "$work/server.csr" -CA "$work/ca.crt" -CAkey "$work/ca.key" -CAcreateserial -days "$DAYS" -sha256 \
    -extfile "$work/ext.cnf" -out "$work/server.crt" >/dev/null 2>&1

openssl verify -CAfile "$work/ca.crt" "$work/server.crt" >/dev/null   # не віддаємо те, що не перевіряється

install -d -m 0755 "$CA_DIR"
install -d -m 0755 -o "$BROKER_UID" -g "$BROKER_UID" "$SRV_DIR"
install -m 0644 "$work/ca.crt" "$CA_DIR/ca.crt"
install -m 0644 -o "$BROKER_UID" -g "$BROKER_UID" "$work/server.crt" "$SRV_DIR/server.crt"
install -m 0600 -o "$BROKER_UID" -g "$BROKER_UID" "$work/server.key" "$SRV_DIR/server.key"
printf '%s' "$SANS" > "$SRV_DIR/sans.txt"
# ключ CA лежав лише в $work і зникає разом із ним (trap)

echo "certs: готово. Дійсні до: $(openssl x509 -noout -enddate -in "$CA_DIR/ca.crt" | cut -d= -f2)"
