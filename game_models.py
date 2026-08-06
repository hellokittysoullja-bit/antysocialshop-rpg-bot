"""Доменные модели.

Слой моделей разбивки монолита: чистые Pydantic-модели без зависимостей
на рантайм (репозиторий/сервисы/хендлеры).
"""
from datetime import datetime, date
from typing import Optional, List, Any

from pydantic import BaseModel, ConfigDict, Field


class Player(BaseModel):
    user_id: int
    username: str = ""
    balance: int = 0
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
    oath: str = ""
    keys: int = 0
    check_count: int = 0
    m_essence: int = 0
    lab_chests: int = 0
    lab_deaths: int = 0
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
    # Пожизненный заработок OAC — метрика статуса (рангов и balance-ачивок).
    # В отличие от balance, никогда не уменьшается: траты (крафт, магазин,
    # донаты, ставки) больше не наказывают прогресс к рангу.
    total_earned: int = 0

    def credit(self, amount: int) -> int:
        """Единственная точка начисления OAC.

        Положительный заработок увеличивает и balance, и total_earned.
        Отрицательное значение (проигрыш/штраф) меняет только balance —
        потеря не может «заработать» статус. Возвращает amount.
        """
        if amount > 0:
            self.total_earned = (self.total_earned or 0) + amount
        self.balance = (self.balance or 0) + amount
        return amount

    @property
    def rank_metric(self) -> int:
        """Метрика статуса: пожизненный заработок.

        max(total_earned, balance) — страховка миграции и самолечение:
        ранг математически не может оказаться ниже прежнего (по балансу),
        поэтому ни один игрок не понижается ни при каких условиях.
        """
        return max(self.total_earned or 0, self.balance or 0)
