"""Тест целостности контента квестов.

Ловит «непроходимые» главы: задания с ключом, который игра нигде не отмечает,
и переходы next_quest на несуществующие главы. БД/Redis не нужны.

    python tests/content_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TOKEN", "123:DUMMY")
os.environ.setdefault("DATABASE_URL_AIVEN", "postgresql://botuser:botpass@127.0.0.1:5432/botdb")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")
os.environ.setdefault("ADMIN_ID", "0")
os.environ.setdefault("RENDER_URL", "")

from bot import QUEST_TEMPLATES

# Ключи заданий, которые хендлеры реально выставляют в daily_progress.
TRACKED_KEYS = {
    "farm", "craft", "smoke", "guild_action", "ritual", "repent",
    "donate", "lab", "pet", "train",
}

# Известные пробелы: механики пока нет. Пусто — у всех ключей заданий теперь
# есть рабочая механика (pet — кормление, train — тренировка).
KNOWN_UNTRACKED = set()


def main() -> int:
    passed = []

    # 1. Каждый ключ задания либо трекается, либо в списке известных пробелов.
    unknown = []
    for qid, tpl in QUEST_TEMPLATES.items():
        for task in tpl.get("tasks", []):
            key = task["key"]
            if key not in TRACKED_KEYS and key not in KNOWN_UNTRACKED:
                unknown.append(f"{qid}:{key}")
    assert not unknown, f"Нетрекаемые ключи заданий (глава непроходима): {unknown}"
    passed.append("все ключи заданий трекаемы (или в известных пробелах)")

    # 2. Переходы next_quest ведут на существующие главы.
    bad_next = []
    for qid, tpl in QUEST_TEMPLATES.items():
        nq = tpl.get("next_quest")
        if nq and nq not in QUEST_TEMPLATES:
            bad_next.append(f"{qid}.next_quest → {nq}")
        for ch in tpl.get("choices", []):
            cnq = ch.get("next_quest")
            if cnq and cnq not in QUEST_TEMPLATES:
                bad_next.append(f"{qid}.choice → {cnq}")
    assert not bad_next, f"Переходы на несуществующие главы: {bad_next}"
    passed.append("все next_quest ведут на существующие главы")

    # 3. Известные пробелы всё ещё присутствуют (чтобы список не устарел молча).
    still_used = set()
    for tpl in QUEST_TEMPLATES.values():
        for task in tpl.get("tasks", []):
            if task["key"] in KNOWN_UNTRACKED:
                still_used.add(task["key"])
    # если пробел больше не используется — его надо убрать из списка
    stale = KNOWN_UNTRACKED - still_used
    assert not stale, f"KNOWN_UNTRACKED устарел, убери: {stale}"
    passed.append(f"известные пробелы актуальны: {sorted(still_used)}")

    # 4. Достижения: у каждого (кроме lunar_lord) есть условие на реальном поле Player.
    from bot import ACHIEVEMENTS, ACHIEVEMENT_CONDITIONS, Player
    player_fields = set(Player.model_fields.keys())
    bad_ach = []
    for a in ACHIEVEMENTS:
        aid = a["id"]
        if aid == "lunar_lord":
            continue
        cond = ACHIEVEMENT_CONDITIONS.get(aid)
        if not cond:
            bad_ach.append(f"{aid}: нет условия (недостижимо)")
        elif cond[0] not in player_fields:
            bad_ach.append(f"{aid}: поле '{cond[0]}' не существует в Player")
    assert not bad_ach, f"Достижения без рабочих условий: {bad_ach}"
    passed.append(f"достижения: {len(ACHIEVEMENTS)} шт, все условия на реальных полях Player")

    for name in passed:
        print(f"  OK  {name}")
    print(f"\nТест контента пройден: {len(passed)}/{len(passed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
