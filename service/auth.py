"""Автентифікація датчиків ключем: у кожного датчика свій ключ, сервер зберігає лише його SHA-256.

Навіщо хеш, а не сам ключ: витік config.yaml (репозиторій, бекап, скріншот) не дає змоги видавати себе за датчик.
Ключ довгий і випадковий (keygen.py: 256 біт), тож швидкого SHA-256 досить: перебирати нема що, а повільний KDF
тут лише додав би затримку на кожну подію.

Ключ належить одному датчику: ключем rf1 не можна слати події від radar1. Тому викрадений ключ одного датчика
не дає підробляти решту, а події з чужим або відсутнім ключем рушій відкидає, не зараховуючи навіть heartbeat
(інакше підроблений «я живий» маскував би відмову справжнього датчика).
"""
import hashlib
import hmac

MAX_KEY_LEN = 256   # довший ключ відкидаємо без хешування: захист від великих «ключів» у кожній події


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class SensorAuth:
    def __init__(self, key_hashes):
        """key_hashes: {sensor_id: sha256 hex}."""
        self._hashes = {sid: bytes.fromhex(h) for sid, h in key_hashes.items()}

    def verify(self, sensor_id, key) -> bool:
        if not isinstance(key, str) or not key or len(key) > MAX_KEY_LEN:
            return False
        digest = hashlib.sha256(key.encode("utf-8", "surrogatepass")).digest()   # хешуємо завжди, щоб час не видавав, чи є такий датчик
        expected = self._hashes.get(sensor_id)
        return expected is not None and hmac.compare_digest(digest, expected)
