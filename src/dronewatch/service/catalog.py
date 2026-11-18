"""Каталог відомих моделей дронів: що саме це, чиє воно, і чия це сторона для оператора.

Датчик (RF-детектор протоколів, Remote ID) повідомляє лише ключ моделі (`drone_model`). Приналежність («свій/ворог»)
сервіс бере ЗВІДСИ, а не з події: датчик не вирішує, кого вважати ворогом, і підкинута подія не може перемалювати
чужий дрон на «свого». Ключ поза каталогом дає «невідомий», а не відмову: невідомий дрон теж треба показати.
"""
from typing import Literal, TypedDict

Side = Literal["enemy", "friend", "unknown"]
# Клас форми для 3D-моделі на дашборді: узагальнений силует класу (дельта-крило, пряме крило, X-крило, мультикоптер),
# а не точна копія конкретної моделі.
Shape = Literal["quad", "delta", "straight", "loiter"]


class DroneInfo(TypedDict):
    name: str        # як показати оператору
    side: Side
    country: str
    kind: str        # клас: ударний, розвідувальний, мультикоптер тощо
    shape: Shape     # силует 3D-моделі


DRONES: dict[str, DroneInfo] = {
    # ворожі (російські та іранські за конструкцією)
    "geran-2": {"name": "Герань-2 (Shahed-136)", "side": "enemy", "country": "РФ / Іран", "kind": "ударний БпЛА (шахед)", "shape": "delta"},
    "orlan-10": {"name": "Орлан-10", "side": "enemy", "country": "РФ", "kind": "розвідувальний БпЛА", "shape": "straight"},
    "lancet-3": {"name": "Ланцет-3", "side": "enemy", "country": "РФ", "kind": "дрон-камікадзе", "shape": "loiter"},
    "zala-421": {"name": "ZALA 421-16", "side": "enemy", "country": "РФ", "kind": "розвідувальний БпЛА", "shape": "straight"},
    "kub-bla": {"name": "КУБ-БЛА", "side": "enemy", "country": "РФ", "kind": "дрон-камікадзе", "shape": "loiter"},
    # свої (українські, французькі, союзні)
    "leleka-100": {"name": "Лелека-100", "side": "friend", "country": "Україна", "kind": "розвідувальний БпЛА", "shape": "straight"},
    "furia": {"name": "Furia", "side": "friend", "country": "Україна", "kind": "розвідувальний БпЛА", "shape": "straight"},
    "liutyi": {"name": "Лютий", "side": "friend", "country": "Україна", "kind": "ударний БпЛА", "shape": "straight"},
    "bayraktar-tb2": {"name": "Bayraktar TB2", "side": "friend", "country": "Туреччина (на озброєнні України)",
                      "kind": "ударний БпЛА", "shape": "straight"},
    "anafi-usa": {"name": "Parrot Anafi USA", "side": "friend", "country": "Франція", "kind": "мультикоптер", "shape": "quad"},
    "delair-dt26": {"name": "Delair DT26", "side": "friend", "country": "Франція", "kind": "розвідувальний БпЛА", "shape": "straight"},
    # невідомі за приналежністю: цивільні комерційні
    "dji-mavic-3": {"name": "DJI Mavic 3", "side": "unknown", "country": "Китай (цивільний)", "kind": "мультикоптер", "shape": "quad"},
    "dji-mini-4": {"name": "DJI Mini 4 Pro", "side": "unknown", "country": "Китай (цивільний)", "kind": "мультикоптер", "shape": "quad"},
}


def describe(model: str | None) -> DroneInfo | None:
    """Опис за ключем моделі; None, якщо ідентифікації немає. Ключ поза каталогом дає «невідомий»."""
    if model is None:
        return None
    return DRONES.get(model) or {"name": "Невідома модель", "side": "unknown", "country": "—", "kind": "невідомо", "shape": "quad"}
