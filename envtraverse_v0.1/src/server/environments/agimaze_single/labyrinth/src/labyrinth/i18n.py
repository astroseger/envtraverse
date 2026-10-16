"""Lightweight i18n for the text interface.

Goal: keep the game engine deterministic and stable, but allow bilingual (RU/EN)
player-facing narration.

We intentionally keep this tiny (no external deps). Messages are keyed and
formatted with Python's `str.format`.

Rules:
- RU and EN variants MUST use the same placeholder names.
- Keep messages short and child-friendly.
"""

from __future__ import annotations

from typing import Literal


Lang = Literal["ru", "en"]


MESSAGES: dict[str, dict[Lang, str]] = {
    # static description (shown in help/about)
    "game.description": {
        "ru": (
            "Вы оказались в мире из клеток, в котором можно только двигаться вверх, вниз, вправо и влево. "
            "Ваша цель — выйти из лабиринта с сокровищами за как можно меньшее число шагов. "
            "Лабиринт окружён неразрушимой стеной — монолитом. В монолите есть только одно отверстие — выход, но где он находится, заранее неизвестно. "
            "Внутри лабиринта также есть стены. Сквозь стены и монолит пройти нельзя (хотя в некоторых расширениях игры могут встречаться предметы или механизмы, "
            "позволяющие разрушать, раскрывать или передвигать стены). "
            "Если на пути оказывается стена или монолит, вы остаётесь на месте, но счётчик ходов увеличивается.\n\n"
            "В лабиринте могут попадаться предметы. Обязательно есть сундук с сокровищами и ключ от него: не найдя ключ, нельзя взять клад. "
            "Не взяв клад, нельзя выйти из лабиринта. В лабиринтах побольше также часто бывает ключ от выхода (есть он или нет — сказано в описании конкретного лабиринта). "
            "Если ключ от выхода есть, без него тоже нельзя выйти. Ключи берутся и используются автоматически — для этого не нужно совершать отдельное действие.\n\n"
            "В некоторых лабиринтах есть реки и ямы. Река состоит из нескольких клеток и течёт от истока к устью. Когда вы шагаете на клетку с рекой, "
            "вас сносит по течению на две клетки (или на одну, если это была последняя клетка перед устьем). "
            "Если же вы шагнули на клетку с ямой, вы окажетесь в следующей по номеру яме (из последней ямы вы попадаете в первую). "
            "В лабиринте может быть несколько рек и несколько циклов ям — об этом сообщается в описании лабиринта.\n\n"
            "Есть разные расширения игры. В расширенных лабиринтах вам могут встретиться новые предметы (например, лодка, гранаты или фонарик) "
            "или новые типы клеток. Иногда предметы дают новые действия (например, взорвать или посветить). "
            "Как именно работают эти предметы, действия и клетки, иногда приходится выяснять опытным путём."
        ),
        "en": (
            "You find yourself in a maze made of grid cells. On each step you can only move up, down, left, or right. "
            "Your goal is to leave the maze with the treasure in as few steps as possible. "
            "The maze is surrounded by an unbreakable border wall—the monolith. There is exactly one opening in the monolith: the exit, but its location is unknown. "
            "Inside the maze there are also internal walls. You cannot walk through walls or the monolith (though some game extensions may introduce items or mechanisms that can "
            "destroy, reveal, or move walls). "
            "If you attempt to move into a wall or the monolith, you stay in place, but the step counter still increases.\n\n"
            "The maze may contain items. There is always a treasure chest and a key for it: without the key, you cannot take the treasure. "
            "Without the treasure, you cannot exit the maze. Larger mazes often also include an exit key (whether it exists is stated in the maze description). "
            "If an exit key exists, you cannot leave without it either. Keys are picked up and used automatically—no extra action is required.\n\n"
            "Some mazes include rivers and pits. A river occupies several cells and flows from a source to a mouth. When you step onto a river cell, the current carries you "
            "two cells downstream (or one cell if you stepped onto the last cell before the mouth). "
            "If you step onto a pit cell, you appear at the next pit in its cycle (from the last pit you go back to the first). "
            "A maze may contain multiple rivers and multiple pit cycles—this is stated in the maze description.\n\n"
            "There are also game extensions. In extended mazes you may encounter new items (for example, a boat, grenades, or a flashlight) or new cell types. "
            "Sometimes items grant new actions (for example, to blast or to shine the flashlight). "
            "In some cases, you have to figure out how new items, actions, and cell types work by experimentation."
        ),
    },
    # movement / blocked
    "move.attempt_blocked": {
        "ru": "Вы попытались пойти {dir}, но {why}.",
        "en": "You tried to go {dir}, but {why}.",
    },
    "blocked.wall": {"ru": "путь преграждает стена", "en": "a wall blocks the way"},
    "blocked.monolith": {"ru": "путь преграждает монолит", "en": "a monolith blocks the way"},
    "blocked.generic": {"ru": "путь закрыт", "en": "the way is blocked"},

    # river on blocked move
    "river.carries.tail": {
        "ru": " Течение уносит вас на {cells}.",
        "en": " The current carries you {cells}.",
    },
    "river.carries.tail.to_mouth": {
        "ru": " Течение уносит вас на {cells} в устье.",
        "en": " The current carries you {cells} to the mouth.",
    },
    "river.carries.far": {
        "ru": " Течение уносит вас дальше.",
        "en": " The current carries you further.",
    },
    "river.carries.far.to_mouth": {
        "ru": " Течение уносит вас в устье.",
        "en": " The current carries you to the mouth.",
    },

    # exit
    "exit.found_but_stay": {
        "ru": "Вы пошли {dir} и нашли выход из лабиринта, но остаетесь, чтобы найти сокровища.",
        "en": "You went {dir} and found the exit, but you stay to find the treasure.",
    },
    "exit.found_but_stay.need_exit_key": {
        "ru": "Вы пошли {dir} и нашли выход из лабиринта, но вам нужен ключ от выхода.",
        "en": "You went {dir} and found the exit, but you need the exit key.",
    },
    "exit.leave_with_treasure": {
        "ru": "Вы выходите из лабиринта с сокровищами.",
        "en": "You leave the labyrinth with the treasure.",
    },

    # move success intro
    "move.landed": {
        "ru": "Вы пошли {dir} и попали {where}.",
        "en": "You went {dir} and stepped {where}.",
    },
    "move.landed.found": {
        "ru": "Вы пошли {dir} и нашли {what}.",
        "en": "You went {dir} and found {what}.",
    },

    # landed where
    "where.empty": {"ru": "в пустую клетку", "en": "on an empty tile"},
    "where.key": {"ru": "в клетку с ключом", "en": "on a tile with a key"},
    "where.exit_key": {"ru": "в клетку с ключом от выхода", "en": "on a tile with an exit key"},
    "where.treasure": {"ru": "в клетку с сундуком", "en": "on a tile with a chest"},
    "where.boat": {"ru": "лодку", "en": "a boat"},
    "where.grenade_pack": {"ru": "в клетку с гранатами", "en": "on a tile with grenades"},
    "where.river.source": {"ru": "в исток реки", "en": "at the river source"},
    "where.river.body": {"ru": "в реку", "en": "into the river"},
    "where.river.mouth": {"ru": "в устье реки", "en": "at the river mouth"},

    # pickups / revisits
    "revisit.key": {"ru": "Вы на клетке, на которой был ключ.", "en": "You are on the tile where a key used to be."},
    "pickup.key": {"ru": "Вы подбираете ключ.", "en": "You pick up the key."},

    "revisit.exit_key": {
        "ru": "Вы на клетке, на которой был ключ от выхода.",
        "en": "You are on the tile where an exit key used to be.",
    },
    "pickup.exit_key": {"ru": "Вы подбираете ключ от выхода.", "en": "You pick up the exit key."},

    "pickup.grenades": {
        "ru": "Вы нашли гранаты ({n}).",
        "en": "You found grenades ({n}).",
    },
    "pickup.boat": {
        "ru": "Вы нашли лодку. Теперь течение вас не унесет.",
        "en": "You found a boat. The current will no longer carry you.",
    },

    "revisit.treasure": {"ru": "Вы на клетке, где был сундук.", "en": "You are on the tile where the chest used to be."},
    "treasure.open": {
        "ru": "Вы открываете сундук ключом и забираете сокровища.",
        "en": "You open the chest with your key and take the treasure.",
    },
    "treasure.locked": {"ru": "Но у вас нет ключа.", "en": "But you don't have a key."},

    "bomb.no_grenades": {"ru": "У вас нет гранат.", "en": "You have no grenades."},
    "bomb.no_wall": {"ru": "Тут нет стены.", "en": "There is no wall here."},
    "bomb.monolith": {"ru": "Монолит слишком прочный — его не взорвать.", "en": "The monolith is too strong to blast."},
    "bomb.success": {"ru": "Вы взорвали стену.", "en": "You blasted the wall."},

    # pits
    "move.pit_fell": {
        "ru": "Вы пошли {dir}, провалились в яму и выбрались из другой.",
        "en": "You went {dir}, fell into a pit, and climbed out of another one.",
    },

    # flashlight / look
    "look.wall": {"ru": "стену", "en": "a wall"},
    "look.monolith": {"ru": "монолит", "en": "the monolith"},
    "look.exit": {"ru": "выход из лабиринта", "en": "the exit"},
    "look.empty": {"ru": "пустую клетку", "en": "an empty tile"},
    "look.key": {"ru": "ключ", "en": "a key"},
    "look.exit_key": {"ru": "ключ от выхода", "en": "an exit key"},
    "look.treasure": {"ru": "сундук", "en": "a chest"},
    "look.boat": {"ru": "лодку", "en": "a boat"},
    "look.grenade_pack": {"ru": "гранаты", "en": "grenades"},
    "look.river.source": {"ru": "исток реки", "en": "a river source"},
    "look.river.body": {"ru": "реку", "en": "a river"},
    "look.river.mouth": {"ru": "устье реки", "en": "a river mouth"},
    "look.pit": {"ru": "яму", "en": "a pit"},

    "look.no_charges": {"ru": "Фонарик разрядился.", "en": "Your flashlight is out of charges."},
    "look.see": {
        "ru": "Вы посветили {dir} и увидели {what}.",
        "en": "You shine the flashlight {dir} and see {what}.",
    },

    # river forced after move
    "river.carried": {"ru": "Вас снесло течением.", "en": "The current swept you away."},
    "river.carried.cells": {
        "ru": "Вас снесло течением на {cells}.",
        "en": "The current swept you {cells}.",
    },
    "river.carried.to_mouth": {"ru": "Вас снесло течением в устье.", "en": "The current swept you to the mouth."},
    "river.carried.cells.to_mouth": {
        "ru": "Вас снесло течением на {cells} в устье.",
        "en": "The current swept you {cells} to the mouth.",
    },
}


DIR_WORD: dict[Lang, dict[str, str]] = {
    "ru": {
        "up": "вверх",
        "down": "вниз",
        "left": "влево",
        "right": "вправо",
        "look_up": "вверх",
        "look_down": "вниз",
        "look_left": "влево",
        "look_right": "вправо",
    },
    "en": {
        "up": "up",
        "down": "down",
        "left": "left",
        "right": "right",
        "look_up": "up",
        "look_down": "down",
        "look_left": "left",
        "look_right": "right",
    },
}


def cells_phrase(n: int, lang: Lang) -> str:
    """Return a short phrase for 'N cells/tiles' with basic plural handling."""
    if lang == "ru":
        # For our current usage we only need 1 or 2 explicitly.
        if n == 1:
            return "1 клетку"
        if n == 2:
            return "2 клетки"
        return f"{n} клеток"
    # en
    return f"{n} tile" if n == 1 else f"{n} tiles"


def t(key: str, lang: Lang, **kwargs: object) -> str:
    try:
        template = MESSAGES[key][lang]
    except KeyError as e:
        raise KeyError(f"Missing i18n key/lang: {key}/{lang}") from e
    return template.format(**kwargs)
