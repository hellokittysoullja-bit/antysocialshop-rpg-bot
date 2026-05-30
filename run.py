import sys
import traceback
import time

# Сразу включаем небуферизированный вывод
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, "reconfigure") else None
sys.stderr.reconfigure(line_buffering=True) if hasattr(sys.stderr, "reconfigure") else None

print("=== Запуск run.py ===", flush=True)

try:
    # Пробуем импортировать bot.py как модуль
    import bot
except Exception as e:
    print("❌ Ошибка импорта bot:", file=sys.stderr, flush=True)
    traceback.print_exc()
    sys.exit(1)

print("✅ Импорт bot успешен, запускаем main()...", flush=True)

try:
    bot.main()
except Exception as e:
    print("❌ Ошибка в main():", file=sys.stderr, flush=True)
    traceback.print_exc()
    sys.exit(1)
