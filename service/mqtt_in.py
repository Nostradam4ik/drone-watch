"""Прийом подій датчиків з MQTT: підписка на dronewatch/events/#, тіло = одна подія або масив подій (JSON).

Подія з топіка <префікс>/<id> має належати датчику <id>: брокер за ACL пускає датчик лише в його власний топік
(deploy/mosquitto.acl), а сервіс перевіряє, що sensor_id у події збігається з топіком. Разом це означає, що навіть
автентифікований датчик не може видавати події за інший. Невідповідність рахується як events_unauthorized."""
import json
import logging
import time

import paho.mqtt.client as mqtt

DEFAULT_TOPIC = "dronewatch/events/#"
log = logging.getLogger("dronewatch.mqtt")


class MqttIngest:
    def __init__(self, engine, host, port=1883, topic=DEFAULT_TOPIC, username=None, password=None):
        self.engine, self.host, self.port, self.topic = engine, host, port, topic
        self.connected = False
        self.subscribed = False
        self.since = None           # коли з'єднання стало робочим (monotonic)
        self.last_message = None    # коли востаннє прийшло повідомлення (monotonic)
        self._last_error = None   # щоб цикл перепідключення (кожні 1-15 с) не засипав лог однаковою помилкою
        # З use_username_as_clientid у брокері це ім'я однаково буде замінено на логін, але без нього воно мусить бути стабільним
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=username or "dronewatch-service")
        if username:
            self.client.username_pw_set(username, password)
        self.client.reconnect_delay_set(min_delay=1, max_delay=15)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_subscribe = self._on_subscribe
        self.client.on_message = self._on_message

    def start(self):
        # connect_async + loop_start: сервіс стартує, навіть якщо брокер ще не піднявся, і сам перепідключається
        self.client.connect_async(self.host, self.port, keepalive=30)
        self.client.loop_start()

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()

    def _report(self, key, message, *args):
        if key != self._last_error:
            self._last_error = key
            log.error(message, *args)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        self.connected = not reason_code.is_failure
        self.subscribed = False
        if self.connected:
            self._last_error = None
            self.since = time.monotonic()
            client.subscribe(self.topic, qos=0)  # QoS 0: застарілі вимірювання все одно нікому не потрібні
        else:   # пароль у повідомлення не потрапляє
            self._report(("connect", str(reason_code)), "MQTT: брокер %s:%s відхилив підключення (%s). Перевірте MQTT_USER і MQTT_PASSWORD",
                         self.host, self.port, reason_code)

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        ok = all(not rc.is_failure for rc in reason_codes)
        self.subscribed = ok
        if not ok:   # не всі брокери так роблять: Mosquitto приймає й заборонену ACL підписку мовчки, див. status()["idle_s"]
            self._report(("subscribe",), "MQTT: брокер відмовив у підписці на %s (%s). Перевірте право read цього користувача в ACL брокера",
                         self.topic, [str(rc) for rc in reason_codes])

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = self.subscribed = False

    def _on_message(self, client, userdata, msg):
        self.last_message = time.monotonic()
        try:
            data = json.loads(msg.payload)
        except (ValueError, UnicodeDecodeError):
            self.engine.count("events_rejected")
            return
        events = data if isinstance(data, list) else [data]
        good = [e for e in events if isinstance(e, dict)]
        self.engine.count("events_rejected", len(events) - len(good))  # не-обʼєкти рахуємо як відхилені
        topic_sensor = msg.topic.rsplit("/", 1)[-1]
        own = [e for e in good if e.get("sensor_id") == topic_sensor]
        if len(own) != len(good):   # подія за іншого датчика в чужому топіку: підміна або хибно налаштований шлюз
            self.engine.count("events_unauthorized", len(good) - len(own))
        if own:
            self.engine.ingest(own)

    def status(self):
        """connected: є з'єднання І прийнята підписка. idle_s: секунд без жодного повідомлення (None, якщо не підключені).
        Датчики шлють heartbeat щоскану, тож велике idle_s за робочого з'єднання майже напевно означає хибну ACL або
        топіки (Mosquitto приймає заборонену підписку мовчки й просто нічого не доставляє) чи те, що всі датчики мовчать."""
        ok = self.connected and self.subscribed
        marks = [t for t in (self.last_message, self.since) if t is not None]   # пізніше з двох: повідомлення чи (пере)підключення
        idle = round(time.monotonic() - max(marks), 1) if ok and marks else None
        return {"host": f"{self.host}:{self.port}", "connected": ok, "idle_s": idle}
