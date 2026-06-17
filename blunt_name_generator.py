import random
import re

# ============================================================
#   ГЕНЕРАТОР МЕМ-ИМЁН vFINAL + Old School префиксы
# ============================================================

# ---------- БАНКИ ДЛЯ УМНОЙ ВЕТКИ (обычные слова) ----------
ABSURD_OBJECTS = [
    "Трансформатор", "Паяльник", "Кирпич",
    "Синхрофазотрон", "Экскаватор", "Кальян",
    "Перфоратор", "Гидрант", "Самовар", "Телескоп",
    "Баян", "Рояль", "Саксофон", "Кувалда", "Лом", "Зубило",
    "Блант-Катер", "Искажатор", "Дым-Машина"
]

MEME_TEMPLATES_MALE = [
    "{} вошёл в чат и все замолчали",
    "{} забрал свой блант и ушёл",
    "{} ворвался и всё испортил",
    "{} пришёл с миром но с блантом",
    "{} забыт но не прощён",
    "{} ищет смысл жизни в блантах",
    "{} ждёт свой блант уже вечность",
    "{} теперь главный по блантам",
    "{} выиграл жизнь нажав поделиться",
]

MEME_TEMPLATES_FEMALE = [
    "{} вошла в чат и все замолчали",
    "{} забрала свой блант и ушла",
    "{} сказала не бойся и исчезла",
    "{} проснулась и выбрала хаос",
    "{} ворвалась и всё испортила",
    "{} пришла с миром но с блантом",
    "{} забыта но не прощена",
    "{} ищет смысл жизни в блантах",
    "{} ждёт свой блант уже вечность",
    "{} теперь главная по блантам",
    "{} выиграла жизнь нажав поделиться",
]

MEME_TEMPLATES_NEUTRAL = [
    "{} вошло в чат и все замолчали",
    "{} гласит что оно придёт",
]

# ---------- БАНКИ ДЛЯ ТУПОЙ ВЕТКИ (мат и абракадабра) ----------
GARBAGE_SYLLABLES = ['ыыы', 'лвд', 'вщщ', 'ааа', 'хм', 'брр', 'жжж', 'ууу', 'эээ', 'ыв', 'двж', 'ллл']
TRASH_CHARS = 'ывлджфзщшг'

# ---------- ТВОИ СТАРЫЕ ТОКЕНЫ ----------
OLD_SCHOOL_TOKENS = [
    "ЫХЦВ", "АХАХАХ", "ЛОЛ", "ААА", "XxX_",
    "_ОВЕРДОЗ", "69", "ЖЫЫЫРНЫЙ", "1337_"
]

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------
def _is_mat(word):
    mat_tokens = ['хуй', 'пизд', 'еб', 'бля', 'сук', 'аху', 'заеб', 'пид', 'член', 'жоп', 'хуи']
    low = word.lower()
    return any(t in low for t in mat_tokens)


def _is_numeric(word):
    return word.isdigit()


def _is_gibberish(word):
    if len(word) <= 3:
        return False
    vowels = 'аеёиоуыэюяaeiouy'
    v_count = sum(1 for c in word.lower() if c in vowels)
    if len(word) >= 5 and v_count == 0:
        return True
    if re.search(r'([бвгджзклмнпрстфхцчшщ]{4,})', word, re.IGNORECASE):
        return True
    return False


def _gender_suffix(word):
    last = word[-1].lower()
    if last in 'ая':
        return 'female'
    elif last in 'йьъ':
        return 'male'
    elif last in 'оеэыу':
        return 'neutral'
    if last in 'бвгджзйклмнпрстфхцчшщ':
        return 'male'
    return 'male'


# ---------- ГЛАВНАЯ ФУНКЦИЯ МУТАЦИИ ----------
def mutate_name(original):
    original = original.strip()
    if not original:
        original = "Блант"

    numeric = _is_numeric(original)
    mat = _is_mat(original)
    gibberish = _is_gibberish(original)

    # ---------- ЧИСЛА (старые токены не добавляются) ----------
    if numeric:
        if original == "666":
            return "666 — Число Зверя"
        elif original == "420":
            return "420 — Время Бланта"
        elif original == "69":
            return "69 — Идеальный Баланс"
        else:
            return f"Число {original[:15]}"

    # ---------- ТУПАЯ ВЕТКА (мат и гиббериш) ----------
    if mat or gibberish:
        strategies = []

        def echo(s):
            rev = s[::-1].lower()
            return f"{s}_{rev}"
        strategies.append(echo)

        def stutter(s):
            return f"{s[:2]}-{s}" if len(s) >= 2 else f"{s}-{s}"
        strategies.append(stutter)

        def repeat(s):
            n = random.choice([2, 3])
            return '_'.join([s] * n)
        strategies.append(repeat)

        def add_garbage(s):
            g = ''.join(random.choices(GARBAGE_SYLLABLES, k=random.randint(1, 2)))
            return f"{s}_{g}"
        strategies.append(add_garbage)

        def scramble(s):
            if len(s) > 1:
                shuffled = ''.join(random.sample(list(s), len(s)))
                return f"{s}_{shuffled}"
            return f"{s}_{s}"
        strategies.append(scramble)

        def case_mix(s):
            return f"{s.upper()}_{s.lower()}"
        strategies.append(case_mix)

        def random_trash(s):
            trash = ''.join(random.choices(TRASH_CHARS, k=random.randint(3, 5)))
            return f"{s}{trash}"
        strategies.append(random_trash)

        strategy = random.choice(strategies)
        result = strategy(original)

        # Добивка тупостью только для мата (иногда)
        if mat and random.random() < 0.3:
            extra = random.choice(['_ор', '_кек', '_лол', '_жесть', '_бах', '_жыыызнь'])
            result += extra

        # === ДОБАВЛЕНИЕ СТАРОГО ТОКЕНА (для мата и гиббериша) ===
        if random.random() < 0.3:
            token = random.choice(OLD_SCHOOL_TOKENS)
            if random.random() < 0.5:
                result = token + result
            else:
                result += token

        return result[:25]

    # ---------- УМНАЯ ВЕТКА (обычные имена) ----------
    gender = _gender_suffix(original)
    strategies = []

    def merge_absurd(s):
        obj = random.choice(ABSURD_OBJECTS)
        style = random.choice([' и ', ' из ', '-', '_'])
        if style == ' из ':
            if obj.endswith('а'):
                obj_gen = obj[:-1] + 'ы'
            elif obj.endswith('я'):
                obj_gen = obj[:-1] + 'и'
            elif obj.endswith('ь'):
                obj_gen = obj[:-1] + 'я'
            else:
                obj_gen = obj + 'а'
            return f"{s} из {obj_gen}"
        elif style == ' и ':
            return f"{s} и {obj}"
        else:
            return f"{s}{style}{obj}"
    strategies.append(merge_absurd)

    def meme_template(s):
        if gender == 'female':
            tpl = random.choice(MEME_TEMPLATES_FEMALE)
        elif gender == 'male':
            tpl = random.choice(MEME_TEMPLATES_MALE)
        else:
            tpl = random.choice(MEME_TEMPLATES_NEUTRAL)
        return tpl.format(s)
    strategies.append(meme_template)

    def echo(s):
        rev = s[::-1].capitalize()
        return f"{s}_{rev}"
    strategies.append(echo)

    def stutter(s):
        if len(s) >= 2:
            return f"{s[:2]}-{s}"
        return f"{s}-{s}"
    strategies.append(stutter)

    strategy = random.choice(strategies)
    result = strategy(original)

    # === ДОБАВЛЕНИЕ СТАРОГО ТОКЕНА (для обычных слов) ===
    if random.random() < 0.3:
        token = random.choice(OLD_SCHOOL_TOKENS)
        if random.random() < 0.5:
            result = token + result
        else:
            result += token

    return result[:25]


# ---------- ФУНКЦИЯ ГЕНЕРАЦИИ РЕАКЦИИ ----------
def generate_reaction(original):
    if _is_numeric(original):
        return "Числа правят миром. Это знак."
    return random.choice(REACTIONS_FUNNY)
