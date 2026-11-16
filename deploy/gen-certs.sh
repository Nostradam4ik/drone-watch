#!/bin/sh
# Генерує ДЕМО-сертифікати (запускає сервіс certs у docker-compose.yml): для брокера Mosquitto і для HTTPS сервісу.
# Приватні ключі в репозиторій не потрапляють: вони створюються тут, при першому запуску стенда, і живуть лише в Docker-томах.
#
#   /out/ca      ca.crt                         публічний сертифікат CA: його читають брокер, сервіс і емулятор
#   /out/server  server.crt, server.key         лише для брокера (ключ 0600, власник mosquitto)
#   /out/http    http.crt, http.key             лише для сервісу (HTTPS API, дашборд, WebSocket)
#
# Ключі брокера й сервісу лежать у РІЗНИХ томах: кожен компонент бачить лише свій. Ключ CA ніде не зберігається: CA
# одноразовий, підписує два сертифікати й зникає, тож ніхто (зокрема й цей репозиторій) не може випустити ще один,
# якому довіряють клієнти. Ціна: перевипуск = новий CA, а клієнти мусять перечитати ca.crt, тому сервіс certs і
# запускається першим на кожен `docker compose up`, а вже запущені клієнти після перевипуску треба перезапустити.
#
# Перевипуск автоматичний, якщо чогось немає, до кінця строку лишилось менше RENEW_BEFORE_DAYS днів, сертифікат не
# підписано цим CA або змінилися SAN. Повторний запуск за чинних сертифікатів нічого не змінює.
#
# ЦЕ ДЛЯ ДЕМОНСТРАЦІЇ. Для бойового розгортання візьміть сертифікати від свого CA (ім'я хоста має бути в SAN)
# і підставте їх замість цих томів або змініть шляхи в mosquitto.conf та TLS_CERT_FILE/TLS_KEY_FILE: див. README.
set -eu

CA_DIR="${CA_DIR:-/out/ca}"
BROKER_DIR="${SRV_DIR:-/out/server}"
HTTP_DIR="${HTTP_DIR:-/out/http}"
DAYS="${CERT_DAYS:-365}"
RENEW_BEFORE_DAYS="${RENEW_BEFORE_DAYS:-30}"
BROKER_UID="${BROKER_UID:-1883}"                                      # користувач mosquitto в офіційному образі
HTTP_UID="${HTTP_UID:-0}"                                             # користувач, від якого працює сервіс
BROKER_SANS="${CERT_SANS:-DNS:mosquitto,DNS:localhost,IP:127.0.0.1}"  # імена, за якими клієнти звертаються до брокера
HTTP_SANS="${HTTP_CERT_SANS:-DNS:service,DNS:localhost,IP:127.0.0.1}" # ... і до сервісу

# leaf_valid КАТАЛОГ ІМ'Я SANS: файли на місці, SAN ті самі, строк не добігає кінця, підписано поточним CA
leaf_valid() {
    dir="$1"; name="$2"; sans="$3"
    [ -s "$dir/$name.crt" ] && [ -s "$dir/$name.key" ] && [ -s "$dir/sans.txt" ] || return 1
    [ "$(cat "$dir/sans.txt")" = "$sans" ] || return 1
    openssl x509 -noout -checkend "$((RENEW_BEFORE_DAYS * 86400))" -in "$dir/$name.crt" >/dev/null 2>&1 || return 1
    openssl verify -CAfile "$CA_DIR/ca.crt" "$dir/$name.crt" >/dev/null 2>&1 || return 1
}

still_valid() {
    [ -s "$CA_DIR/ca.crt" ] || return 1
    openssl x509 -noout -checkend "$((RENEW_BEFORE_DAYS * 86400))" -in "$CA_DIR/ca.crt" >/dev/null 2>&1 || return 1
    leaf_valid "$BROKER_DIR" server "$BROKER_SANS" && leaf_valid "$HTTP_DIR" http "$HTTP_SANS"
}

if still_valid; then
    echo "certs: сертифікати чинні (до кінця строку більше $RENEW_BEFORE_DAYS днів), нічого не змінюю"
    exit 0
fi

echo "certs: генерую новий CA ($DAYS днів) і сертифікати: брокер ($BROKER_SANS), сервіс ($HTTP_SANS)"
umask 077
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

openssl ecparam -name prime256v1 -genkey -noout -out "$work/ca.key"
openssl req -x509 -new -key "$work/ca.key" -sha256 -days "$DAYS" -subj "/CN=Drone Watch DEMO CA" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash" -out "$work/ca.crt"

# issue ІМ'Я CN SANS: ключ і сертифікат у $work. SKI/AKI прописано явно, а не з розрахунку на типову поведінку OpenSSL:
# суворий режим X.509 (Python 3.13+ вмикає його за замовчуванням) відхиляє сертифікат без authorityKeyIdentifier.
issue() {
    name="$1"; cn="$2"; sans="$3"
    openssl ecparam -name prime256v1 -genkey -noout -out "$work/$name.key"
    openssl req -new -key "$work/$name.key" -subj "/CN=$cn" -out "$work/$name.csr"
    printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=%s\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n' "$sans" > "$work/$name.cnf"
    openssl x509 -req -in "$work/$name.csr" -CA "$work/ca.crt" -CAkey "$work/ca.key" -CAcreateserial -days "$DAYS" -sha256 \
        -extfile "$work/$name.cnf" -out "$work/$name.crt" >/dev/null 2>&1
    openssl verify -CAfile "$work/ca.crt" "$work/$name.crt" >/dev/null   # не віддаємо те, що не перевіряється
}

# install_leaf ІМ'Я КАТАЛОГ UID SANS
install_leaf() {
    name="$1"; dir="$2"; uid="$3"; sans="$4"
    install -d -m 0755 -o "$uid" -g "$uid" "$dir"
    install -m 0644 -o "$uid" -g "$uid" "$work/$name.crt" "$dir/$name.crt"
    install -m 0600 -o "$uid" -g "$uid" "$work/$name.key" "$dir/$name.key"
    printf '%s' "$sans" > "$dir/sans.txt"
}

issue server mosquitto "$BROKER_SANS"
issue http service "$HTTP_SANS"

install -d -m 0755 "$CA_DIR"
install -m 0644 "$work/ca.crt" "$CA_DIR/ca.crt"
install_leaf server "$BROKER_DIR" "$BROKER_UID" "$BROKER_SANS"
install_leaf http "$HTTP_DIR" "$HTTP_UID" "$HTTP_SANS"
# ключ CA лежав лише в $work і зникає разом із ним (trap)

echo "certs: готово. Дійсні до: $(openssl x509 -noout -enddate -in "$CA_DIR/ca.crt" | cut -d= -f2)"
