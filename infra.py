"""Инфраструктурный слой: circuit breakers и низкоуровневые утилиты.

Самый нижний слой разбивки монолита — не зависит ни от моделей, ни от
конфигурации, ни от бизнес-логики. Импортируется и bot.py, и repository.py,
что исключает циклические зависимости.
"""
import sys
import json

try:
    import pybreaker
    redis_breaker = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=30)
    db_breaker = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=30)
    tg_breaker = pybreaker.CircuitBreaker(fail_max=3, reset_timeout=30)
except Exception as e:  # pragma: no cover - фолбэк, если pybreaker недоступен
    print(f"WARNING: pybreaker not available: {e}", file=sys.stderr)

    class DummyBreaker:
        def call(self, func, *args, **kwargs):
            return func(*args, **kwargs)

    redis_breaker = DummyBreaker()
    db_breaker = DummyBreaker()
    tg_breaker = DummyBreaker()


def _json_safe_load(value, default):
    """Безопасно превращает значение из БД (str/None/JSON) в list/dict."""
    if isinstance(value, (list, dict)):
        return value
    if value in (None, ""):
        return default.copy() if isinstance(default, (list, dict)) else default
    try:
        parsed = json.loads(value)
        if parsed is None:
            return default.copy() if isinstance(default, (list, dict)) else default
        return parsed
    except Exception:
        return default.copy() if isinstance(default, (list, dict)) else default
