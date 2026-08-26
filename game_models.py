"""Доменные модели.

Слой моделей разбивки монолита: чистые Pydantic-модели без зависимостей
на рантайм (репозиторий/сервисы/хендлеры).
"""
from datetime import datetime, date
from typing import Optional, List, Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr


class Player(BaseModel):
    user_id: int
    username: str = ""
    balance: int = 0
    # Кошелёк и статус — РАЗНЫЕ величины. balance тратится; total_earned только
    # растёт (сумма всего заработанного за жизнь) и служит осью ранга, топа и
    # гейтов. Раньше обе роли играл balance: любая трата отбирала ранг, скидку,
    # доступ к алхимии и место в топе — игра наказывала за то, что в неё играют.
    total_earned: int = 0
    blunts: int = 0
    guild: Optional[str] = None
    last_farm: Optional[datetime] = None
    last_ritual: Optional[datetime] = None
    last_repent: Optional[datetime] = None
    last_daily: Optional[datetime] = None
    titles: str = ""
    last_farm_date: Optional[date] = None
    passive_level: int = 0
    passive_collected: Optional[datetime] = None
    karma: int = 0
    inhaled: int = 0
    smoke_count: int = 0
    farm_count: int = 0
    craft_count: int = 0
    ritual_count: int = 0
    repent_count: int = 0
    referral_count: int = 0
    last_mines: Optional[datetime] = None
    inventory: List[Any] = Field(default_factory=list)
    invited_by: Optional[int] = None
    profile_skins: dict = Field(default_factory=dict)
    login_streak: int = 0
    last_login_date: Optional[date] = None
    streak_freezes: int = 1  # «заморозки» серии: спасают стрик при пропуске ровно 1 дня
    oath: str = ""
    keys: int = 0
    check_count: int = 0
    m_essence: int = 0
    lab_chests: int = 0
    lab_deaths: int = 0
    # Личный рекорд забега (peak-end триумф) — раньше жил в Redis-ключе
    # lab_best:{uid} без миграций; при недоступном Redis (был недоступен в
    # проде несколько дней подряд) строка рекорда просто не показывалась.
    # Поле на игроке пишется в ТОЙ ЖЕ транзакции, что баланс и награда за
    # забег, — не отдельным round-trip после, как было с Redis.
    lab_best_oac: int = 0
    # Личный рекорд глубины в «Минах» (сколько клеток подряд открыто в самой
    # успешной партии) — тот же принцип, что у lab_best_oac: пишется в ТОЙ ЖЕ
    # транзакции, что баланс за партию, не отдельным round-trip.
    mines_best_step: int = 0
    alchemy_count: int = 0
    last_lab_attempt: Optional[datetime] = None
    donated: int = 0
    pending_transfer: Optional[dict] = None
    lab_depth: int = 1
    pet: str = ""
    pet_name: str = ""
    onboarding_step: int = 0
    exists: bool = False
    model_config = ConfigDict(populate_by_name=True)
    pet_hunger: int = 100
    daily_progress: dict = Field(default_factory=dict)
    # Алтарь Вечности: эндгейм-сток. Растёт ТОЛЬКО добровольной жертвой из
    # balance, никогда не убывает и не конвертируется назад — честный
    # односторонний счётчик статуса, не фарм-петля (нечем эксплойтить).
    prestige: int = 0

    # «Инвентарь не загружался» ≠ «инвентарь пуст». get_by_id(with_inventory=
    # False) отдаёт inventory=[] ради экономии, а save() писал этот пустой
    # список в БД — и коллекция именных блантов (включая стартовый, обещанный
    # в приветствии) исчезала. Флаг говорит save() не трогать колонку.
    _inventory_loaded: bool = PrivateAttr(default=True)
