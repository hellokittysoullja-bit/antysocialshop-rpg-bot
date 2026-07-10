"""Конфигурация приложения и игровой баланс.

Слой конфигурации разбивки монолита: настройки окружения (env) и производные
игровые константы баланса. Зависит только от pydantic-settings.
"""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="")

    bot_token: str = Field(..., alias="TOKEN")
    database_url: str = Field(..., alias="DATABASE_URL_AIVEN")
    render_url: str = Field("", alias="RENDER_URL")
    redis_url: str = Field("", alias="REDIS_URL")
    port: int = Field(default=10000, alias="PORT")
    webhook_path: str = "/webhook"
    webhook_secret: str = "SuperSecret"
    sentry_dsn: str = ""
    environment: str = "production"
    admin_id: int = 0

    # Игровые конфиги
    farm_cooldown_hours: float = 0.5
    farm_min: int = 45
    farm_max: int = 100
    happy_hour_multiplier: int = 2
    happy_hour_duration_min: int = 30
    veteran_threshold: int = 5000
    phantom_threshold: int = 20000
    necromant_threshold: int = 50000
    lab_cooldown_hours: int = 12
    ritual_cooldown_hours: int = 12
    repent_cooldown_hours: int = 12

    @property
    def webhook_url(self) -> str:
        return f"{self.render_url}{self.webhook_path}"


settings = Settings()
FARM_MIN = settings.farm_min
FARM_MAX = settings.farm_max
FARM_COOLDOWN_HOURS = settings.farm_cooldown_hours
HAPPY_HOUR_MULTIPLIER = settings.happy_hour_multiplier

# ── Глобальные конфиги игры ──
GAME_CONFIG = {
    "craft_cost": 15,
    "named_blunt_cost": 50,
    "farm_cooldown_hours": settings.farm_cooldown_hours,
    "ritual_cooldown_hours": settings.ritual_cooldown_hours,
    "repent_cooldown_hours": settings.repent_cooldown_hours,
    "lab_cooldown_hours": settings.lab_cooldown_hours,
    "veteran_threshold": settings.veteran_threshold,
    "phantom_threshold": settings.phantom_threshold,
    "necromant_threshold": settings.necromant_threshold,
}
PET_CONFIG = {
    "dog": {"name": "🐕 Песик", "price": 3000, "max_name_len": 15},
}
