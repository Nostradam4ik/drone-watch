"""Прийом подій датчиків з MQTT: підписка на dronewatch/events/#, тіло = одна подія або масив подій (JSON)."""
import json
import threading

import paho.mqtt.client as mqtt

DEFAULT_TOPIC = "dronewatch/events/#"


class MqttIngest:
    def __init__(self, engine, host, port=1883, topic=DEFAULT_TOPIC, username=None, password=None):
        self.engine, self.host, self.port, self.topic = engine, host, port, topic
        self.connected = False
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="dronewatch-service")
        if username:
            self.client.username_pw_set(username, password)
        self.client.reconnect_delay_set(min_delay=1, max_delay=15)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def start(self):
        # connect_async + loop_start: сервіс стартує, навіть якщо брокер ще не піднявся, і сам перепідключається
        self.client.connect_async(self.host, self.port, keepalive=30)
        self.client.loop_start()

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        self.connected = not reason_code.is_failure
        if self.connected:
            client.subscribe(self.topic, qos=0)  # QoS 0: застарілі вимірювання все одно нікому не потрібні

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload)
        except (ValueError, UnicodeDecodeError):
            self.engine.count("events_rejected")
            return
        events = data if isinstance(data, list) else [data]
        good = [e for e in events if isinstance(e, dict)]
        self.engine.count("events_rejected", len(events) - len(good))  # не-обʼєкти рахуємо як відхилені
        events = good
        self.engine.ingest(events)

    def status(self):
        return {"host": f"{self.host}:{self.port}", "connected": self.connected}
